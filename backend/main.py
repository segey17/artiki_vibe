from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from contextlib import asynccontextmanager
import sqlite3, os, jwt, bcrypt, uuid, io, json, asyncio, httpx, re
from datetime import datetime, timedelta
from typing import Optional
from PIL import Image as PILImage
import wd14_tagger

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass

# Защита от гонки: ручной "Проверить сейчас"/повторное сохранение настройки
# и фоновый таймер могут попытаться синхронизировать один и тот же источник
# одновременно. Без блокировки оба параллельных запуска читают один и тот же
# "снимок" уже загруженных файлов, не видят файлы, которые другой запуск
# только начал скачивать, и оба независимо загружают одно и то же — отсюда
# дубли в БД. Lock гарантирует, что для каждого источника в любой момент
# выполняется не больше одной синхронизации.
yadisk_sync_lock = asyncio.Lock()
gdrive_sync_lock = asyncio.Lock()

@asynccontextmanager
async def lifespan(app: FastAPI):
    yadisk_task = asyncio.create_task(yadisk_sync_loop())
    gdrive_task = asyncio.create_task(gdrive_sync_loop())
    yield
    for task in (yadisk_task, gdrive_task):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

app = FastAPI(title="PhotoRank", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])

# ── WEBSOCKET MANAGER ─────────────────────────────────────────────────────────

class WSManager:
    def __init__(self):
        # ws -> user_id (None for unauthenticated)
        self.connections: dict[WebSocket, Optional[int]] = {}

    async def connect(self, ws: WebSocket, user_id: Optional[int] = None):
        await ws.accept()
        self.connections[ws] = user_id

    def disconnect(self, ws: WebSocket):
        self.connections.pop(ws, None)

    def online_user_ids(self) -> set[int]:
        """Возвращает множество user_id пользователей онлайн (без None)."""
        return {uid for uid in self.connections.values() if uid is not None}

    async def broadcast(self, data: dict):
        msg = json.dumps(data, ensure_ascii=False)
        dead = []
        for ws in self.connections:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

ws_manager = WSManager()

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket, token: Optional[str] = None):
    user_id = None
    if token:
        try:
            d = jwt.decode(token, SECRET, algorithms=["HS256"])
            user_id = int(d["sub"])
        except Exception:
            pass
    await ws_manager.connect(ws, user_id)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(ws)

SECRET = os.getenv("JWT_SECRET", "change-me-in-production-please")
PHOTOS_DIR = "/app/photos"
DB_PATH = "/app/data/db.sqlite3"
AUTO_TAGGER_USERNAME = "auto-tagger"  # системный "пользователь", от имени которого пишутся автотеги
GOOGLE_DRIVE_API_KEY = os.getenv("GOOGLE_DRIVE_API_KEY", "")  # для доступа к публичным папкам Google Drive
os.makedirs(PHOTOS_DIR, exist_ok=True)
os.makedirs("/app/data", exist_ok=True)

security = HTTPBearer(auto_error=False)

DEFAULT_TIERS = [
    {"id": "S", "label": "S", "color": "#ff6b6b", "order": 0},
    {"id": "A", "label": "A", "color": "#ffa94d", "order": 1},
    {"id": "B", "label": "B", "color": "#ffd43b", "order": 2},
    {"id": "C", "label": "C", "color": "#74c0fc", "order": 3},
    {"id": "D", "label": "D", "color": "#a9e34b", "order": 4},
]

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    db = get_db()
    db.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        is_admin INTEGER DEFAULT 0,
        is_system INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS photos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        filename TEXT NOT NULL,
        original_name TEXT,
        position INTEGER DEFAULT 0,
        uploaded_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS ratings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        photo_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        tier_id TEXT NOT NULL,
        created_at TEXT DEFAULT (datetime('now')),
        UNIQUE(photo_id, user_id)
    );
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    );
    INSERT OR IGNORE INTO settings VALUES ('current_photo_id', NULL);
    INSERT OR IGNORE INTO settings VALUES ('voting_open', '1');
    INSERT OR IGNORE INTO settings VALUES ('tiers', NULL);
    INSERT OR IGNORE INTO settings VALUES ('auto_advance', '0');
    CREATE TABLE IF NOT EXISTS tags (
        id INTEGER PRIMARY KEY,
        name TEXT UNIQUE NOT NULL,
        category INTEGER DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_tags_name ON tags(name);
    CREATE TABLE IF NOT EXISTS photo_tags (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        photo_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        tag_id INTEGER NOT NULL,
        created_at TEXT DEFAULT (datetime('now')),
        UNIQUE(photo_id, user_id, tag_id)
    );
    CREATE INDEX IF NOT EXISTS idx_photo_tags_photo ON photo_tags(photo_id);
    CREATE TABLE IF NOT EXISTS yadisk_watch (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        public_url TEXT NOT NULL,
        interval_minutes INTEGER DEFAULT 60,
        last_sync_at TEXT,
        last_sync_added INTEGER DEFAULT 0,
        last_sync_errors INTEGER DEFAULT 0,
        enabled INTEGER DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS gdrive_watch (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        folder_url TEXT NOT NULL,
        interval_minutes INTEGER DEFAULT 60,
        last_sync_at TEXT,
        last_sync_added INTEGER DEFAULT 0,
        last_sync_errors INTEGER DEFAULT 0,
        enabled INTEGER DEFAULT 1
    );
    """)
    cols = [r[1] for r in db.execute("PRAGMA table_info(ratings)").fetchall()]
    if 'score' in cols and 'tier_id' not in cols:
        db.execute("ALTER TABLE ratings ADD COLUMN tier_id TEXT")
        score_to_tier = {5:'S',4:'A',3:'B',2:'C',1:'D'}
        for row in db.execute("SELECT id, score FROM ratings").fetchall():
            db.execute("UPDATE ratings SET tier_id=? WHERE id=?",
                       (score_to_tier.get(row['score'],'C'), row['id']))
    if db.execute("SELECT COUNT(*) FROM tags").fetchone()[0] == 0:
        import csv
        tags_path = "/app/tags.csv"
        if os.path.exists(tags_path):
            with open(tags_path, newline='', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                batch = [(int(r['tag_id']), r['name'], int(r['category']))
                         for r in reader if r['name'] and r['category'] in ('0','4','9')]
            db.executemany("INSERT OR IGNORE INTO tags (id,name,category) VALUES (?,?,?)", batch)
            print(f"Loaded {len(batch)} tags")
    user_cols = [r[1] for r in db.execute("PRAGMA table_info(users)").fetchall()]
    if 'is_system' not in user_cols:
        db.execute("ALTER TABLE users ADD COLUMN is_system INTEGER DEFAULT 0")
    photo_tags_cols = [r[1] for r in db.execute("PRAGMA table_info(photo_tags)").fetchall()]
    if 'is_suggestion' not in photo_tags_cols:
        # 0 = обычный тег (виден везде: тир-лист, фильтры, счётчики).
        # 1 = предложение от автотегирования WD14, ожидающее подтверждения
        #     хотя бы одним человеком — до этого момента не учитывается
        #     ни в тир-листе, ни в фильтрах по тегам, ни в счётчиках "сколько
        #     пользователей поставили тег".
        db.execute("ALTER TABLE photo_tags ADD COLUMN is_suggestion INTEGER DEFAULT 0")
    # Системный пользователь, от имени которого пишутся автотеги WD14.
    # is_system=1 — исключается из всех счётчиков "обычных" людей
    # (прогресс-бар голосования, список участников статистики и т.п.).
    # Пароль — случайный недостижимый хэш, под этим юзером никто не логинится.
    if not db.execute("SELECT id FROM users WHERE username=?", (AUTO_TAGGER_USERNAME,)).fetchone():
        random_pw_hash = bcrypt.hashpw(str(uuid.uuid4()).encode(), bcrypt.gensalt()).decode()
        db.execute(
            "INSERT INTO users (username, password_hash, is_admin, is_system) VALUES (?,?,0,1)",
            (AUTO_TAGGER_USERNAME, random_pw_hash)
        )
    db.commit()
    db.close()

init_db()

def hash_pw(pw): return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()
def check_pw(pw, h): return bcrypt.checkpw(pw.encode(), h.encode())
def make_token(uid, admin):
    return jwt.encode({"sub":str(uid),"admin":admin,
                       "exp":datetime.utcnow()+timedelta(days=30)}, SECRET, algorithm="HS256")

def current_user(creds: Optional[HTTPAuthorizationCredentials] = Depends(security)):
    if not creds: raise HTTPException(401, "Not authenticated")
    try:
        d = jwt.decode(creds.credentials, SECRET, algorithms=["HS256"])
        return {"id": int(d["sub"]), "admin": d.get("admin", False)}
    except: raise HTTPException(401, "Invalid token")

def admin_user(user=Depends(current_user)):
    if not user["admin"]: raise HTTPException(403, "Admin only")
    return user

def get_setting(db, key):
    r = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else None

def set_setting(db, key, value):
    db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, value))
    db.commit()

def get_tiers(db):
    raw = get_setting(db, "tiers")
    try:
        t = json.loads(raw) if raw else None
        if t: return sorted(t, key=lambda x: x.get("order", 0))
    except: pass
    return DEFAULT_TIERS[:]

_auto_tagger_user_id_cache = None

def _get_auto_tagger_user_id(db) -> Optional[int]:
    global _auto_tagger_user_id_cache
    if _auto_tagger_user_id_cache is None:
        row = db.execute("SELECT id FROM users WHERE username=?", (AUTO_TAGGER_USERNAME,)).fetchone()
        if not row:
            return None
        _auto_tagger_user_id_cache = row["id"]
    return _auto_tagger_user_id_cache

def auto_tag_photo(photo_id: int, photo_path: str):
    """
    Прогоняет фото через локальную WD14-модель и записывает найденные теги
    как ПРЕДЛОЖЕНИЯ (is_suggestion=1) от имени системного пользователя
    auto-tagger — они не считаются "настоящими" тегами фото (не учитываются
    в тир-листе, фильтрах по тегам, счётчиках), пока их не подтвердит хотя бы
    один реальный человек через confirm_suggested_tag().

    Вызывается синхронно сразу после сохранения нового фото на диск (upload,
    импорт с Яндекс.Диска, авто-синхронизация) — все три точки вызова сами
    оборачивают это в try/except, так что любая ошибка здесь (модель не
    скачана, файл повреждён и т.п.) не мешает самой загрузке фото.
    """
    tag_ids = wd14_tagger.predict_tag_ids(photo_path)
    if not tag_ids:
        return

    db = get_db()
    try:
        tagger_id = _get_auto_tagger_user_id(db)
        if tagger_id is None:
            return
        # Берём только те tag_id, что реально есть в нашей таблице tags —
        # на случай несовпадения версий selected_tags.csv модели и tags.csv проекта.
        existing = set(
            r["id"] for r in db.execute(
                "SELECT id FROM tags WHERE id IN ({})".format(",".join("?" * len(tag_ids))),
                tag_ids
            ).fetchall()
        )
        rows = [(photo_id, tagger_id, tid) for tid in tag_ids if tid in existing]
        if rows:
            db.executemany(
                "INSERT OR IGNORE INTO photo_tags (photo_id, user_id, tag_id, is_suggestion) VALUES (?,?,?,1)",
                rows
            )
            db.commit()
    finally:
        db.close()

async def auto_tag_photo_async(photo_id: int, photo_path: str):
    """
    Асинхронная обёртка над auto_tag_photo(): инференс WD14 — это блокирующая
    CPU-bound операция, поэтому выполняем её в отдельном треде через
    run_in_executor, чтобы не подвешивать event loop FastAPI (другие запросы,
    WebSocket и т.п.) на время распознавания тегов.
    Любая ошибка здесь гасится — тегирование не должно прерывать загрузку фото.
    """
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, auto_tag_photo, photo_id, photo_path)
    except Exception as e:
        print(f"[auto_tag_photo_async] Пропускаем автотегирование фото {photo_id}: {e}")

# ── AUTH ──────────────────────────────────────────────────────────────────────

@app.post("/api/register")
def register(username: str = Form(...), password: str = Form(...)):
    if len(username)<3 or len(password)<4:
        raise HTTPException(400, "Username ≥3 chars, password ≥4 chars")
    db = get_db()
    try:
        db.execute("INSERT INTO users (username,password_hash) VALUES (?,?)",
                   (username.strip(), hash_pw(password)))
        db.commit()
        row = db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        return {"token": make_token(row["id"], False), "username": username, "is_admin": False}
    except sqlite3.IntegrityError: raise HTTPException(409, "Username already taken")
    finally: db.close()

@app.post("/api/login")
def login(username: str = Form(...), password: str = Form(...)):
    db = get_db()
    row = db.execute("SELECT * FROM users WHERE username=?", (username.strip(),)).fetchone()
    db.close()
    if not row or not check_pw(password, row["password_hash"]):
        raise HTTPException(401, "Wrong username or password")
    return {"token": make_token(row["id"], bool(row["is_admin"])),
            "username": row["username"], "is_admin": bool(row["is_admin"])}

@app.get("/api/me")
def me(user=Depends(current_user)):
    db = get_db()
    row = db.execute("SELECT id,username,is_admin FROM users WHERE id=?", (user["id"],)).fetchone()
    db.close()
    return dict(row)

# ── TIERS CONFIG ──────────────────────────────────────────────────────────────

@app.get("/api/tiers")
def api_get_tiers(user=Depends(current_user)):
    db = get_db(); t = get_tiers(db); db.close(); return t

@app.post("/api/admin/tiers")
def api_set_tiers(body: dict, user=Depends(admin_user)):
    tiers = body.get("tiers", [])
    if not tiers or len(tiers) < 2:
        raise HTTPException(400, "Минимум 2 тира")
    if len(tiers) > 10:
        raise HTTPException(400, "Максимум 10 тиров")
    # validate
    ids = set()
    for i, t in enumerate(tiers):
        if not t.get("label", "").strip():
            raise HTTPException(400, f"Тир {i+1}: пустое название")
        t["id"] = t.get("id") or str(uuid.uuid4())[:8]
        t["order"] = i
        t["label"] = t["label"].strip()[:30]
        t["color"] = t.get("color", "#888")
        ids.add(t["id"])
    db = get_db()
    set_setting(db, "tiers", json.dumps(tiers, ensure_ascii=False))
    db.close()
    return tiers


# ── YANDEX DISK IMPORT ───────────────────────────────────────────────────────

YADISK_API = "https://cloud-api.yandex.net/v1/disk/public/resources"

def yadisk_public_key(url: str) -> str:
    return url.strip()

async def yadisk_list_files(public_url: str, path: str = "/") -> list:
    """Рекурсивно получает все файлы из публичной папки."""
    params = {
        "public_key": public_url,
        "path": path,
        "limit": 100,
        "offset": 0,
    }
    files = []
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            r = await client.get(YADISK_API, params=params)
            if r.status_code != 200:
                raise HTTPException(502, f"Яндекс.Диск API вернул {r.status_code}: {r.text[:200]}")
            data = r.json()
            items = data.get("_embedded", {}).get("items", [])
            for item in items:
                if item["type"] == "file" and item.get("mime_type", "").startswith("image/"):
                    files.append({
                        "name": item["name"],
                        "path": item["path"],
                        "size": item.get("size", 0),
                    })
                elif item["type"] == "dir":
                    sub = await yadisk_list_files(public_url, item["path"])
                    files.extend(sub)
            total = data.get("_embedded", {}).get("total", 0)
            params["offset"] += len(items)
            if params["offset"] >= total or not items:
                break
    return files

async def yadisk_download_file(public_url: str, path: str) -> bytes:
    """Получает прямую ссылку и скачивает файл."""
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.get(
            "https://cloud-api.yandex.net/v1/disk/public/resources/download",
            params={"public_key": public_url, "path": path}
        )
        if r.status_code != 200:
            raise Exception(f"Не удалось получить ссылку: {r.status_code}")
        download_url = r.json()["href"]
        resp = await client.get(download_url, follow_redirects=True)
        resp.raise_for_status()
        return resp.content


# ── GOOGLE DRIVE ──────────────────────────────────────────────────────────────
# Доступ к публичной папке ("у кого есть ссылка") через Google Drive API v3
# и обычный API-ключ — без OAuth, без входа пользователя, без истекающих
# токенов. Подходит только для папок, открытых на чтение всем по ссылке.

GDRIVE_API = "https://www.googleapis.com/drive/v3/files"
GDRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"

def gdrive_extract_folder_id(url: str) -> str:
    """
    Принимает ссылку вида https://drive.google.com/drive/folders/<ID>?usp=sharing
    (или просто сам <ID>) и возвращает folder_id.
    """
    url = url.strip()
    m = re.search(r"/folders/([a-zA-Z0-9_-]+)", url)
    if m:
        return m.group(1)
    # на случай, если просто вставили сам ID без ссылки
    if re.fullmatch(r"[a-zA-Z0-9_-]{10,}", url):
        return url
    raise HTTPException(400, "Не удалось распознать ID папки Google Drive в ссылке")

async def gdrive_list_files(folder_url: str) -> list:
    """
    Рекурсивно собирает все изображения из публичной папки Google Drive
    (включая вложенные подпапки, без сохранения структуры — плоский список,
    как договорились). Требует GOOGLE_DRIVE_API_KEY и доступ "у кого есть
    ссылка" на папку и все вложенные подпапки/файлы.
    """
    if not GOOGLE_DRIVE_API_KEY:
        raise HTTPException(500, "GOOGLE_DRIVE_API_KEY не задан в переменных окружения сервера")

    root_id = gdrive_extract_folder_id(folder_url)
    files = []

    async def walk(folder_id: str):
        page_token = None
        async with httpx.AsyncClient(timeout=30) as client:
            while True:
                params = {
                    "q": f"'{folder_id}' in parents and trashed=false",
                    "key": GOOGLE_DRIVE_API_KEY,
                    "fields": "nextPageToken, files(id,name,mimeType,size)",
                    "pageSize": 1000,
                }
                if page_token:
                    params["pageToken"] = page_token
                r = await client.get(GDRIVE_API, params=params)
                if r.status_code != 200:
                    raise HTTPException(
                        502, f"Google Drive API вернул {r.status_code}: {r.text[:200]}"
                    )
                data = r.json()
                for item in data.get("files", []):
                    if item["mimeType"] == GDRIVE_FOLDER_MIME:
                        await walk(item["id"])
                    elif item["mimeType"].startswith("image/"):
                        files.append({
                            "id": item["id"],
                            "name": item["name"],
                            "size": int(item.get("size", 0) or 0),
                        })
                page_token = data.get("nextPageToken")
                if not page_token:
                    break

    await walk(root_id)
    return files

async def gdrive_download_file(file_id: str) -> bytes:
    """Скачивает содержимое файла по его id через alt=media."""
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.get(
            f"{GDRIVE_API}/{file_id}",
            params={"key": GOOGLE_DRIVE_API_KEY, "alt": "media"},
            follow_redirects=True,
        )
        if r.status_code != 200:
            raise Exception(f"Не удалось скачать файл {file_id}: {r.status_code}")
        return r.content


@app.post("/api/admin/import-yadisk")
async def import_yadisk(public_url: str = Form(...), user=Depends(admin_user)):
    """
    Сканирует публичную папку Яндекс.Диска и импортирует все изображения.
    Уже существующие файлы (по имени) пропускаются.
    """
    db = get_db()
    existing_names = {r["original_name"] for r in
                      db.execute("SELECT original_name FROM photos").fetchall()}

    # get list of files
    try:
        all_files = await yadisk_list_files(public_url)
    except HTTPException:
        db.close()
        raise
    except Exception as e:
        db.close()
        raise HTTPException(502, f"Ошибка получения списка файлов: {e}")

    new_files = [f for f in all_files if f["name"] not in existing_names]

    added = 0
    errors = 0
    new_photos = []  # (photo_id, full_path) — для автотегирования после вставки
    for file_info in new_files:
        try:
            data = await yadisk_download_file(public_url, file_info["path"])
            img = PILImage.open(io.BytesIO(data))
            img.load()
            uid = str(uuid.uuid4())
            filename = uid + ".jpg"
            path = os.path.join(PHOTOS_DIR, filename)
            img.convert("RGB").save(path, "JPEG", quality=92)
            count = db.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
            cur = db.execute(
                "INSERT INTO photos (filename, original_name, position) VALUES (?,?,?)",
                (filename, file_info["name"], count)
            )
            db.commit()
            new_photos.append((cur.lastrowid, path))
            added += 1
        except Exception:
            errors += 1
            continue

    db.close()

    # Автотегирование WD14 — при ошибке/недоступности модели просто пропускаем,
    # фото всё равно уже импортированы выше.
    for photo_id, path in new_photos:
        await auto_tag_photo_async(photo_id, path)

    return {
        "total_found": len(all_files),
        "skipped": len(all_files) - len(new_files),
        "added": added,
        "errors": errors,
    }


@app.get("/api/admin/preview-yadisk")
async def preview_yadisk(public_url: str, user=Depends(admin_user)):
    """Возвращает список файлов в папке без скачивания."""
    try:
        files = await yadisk_list_files(public_url)
    except Exception as e:
        raise HTTPException(502, str(e))

    db = get_db()
    existing = {r["original_name"] for r in
                db.execute("SELECT original_name FROM photos").fetchall()}
    db.close()

    return {
        "total": len(files),
        "new": len([f for f in files if f["name"] not in existing]),
        "files": [{"name": f["name"], "size": f["size"],
                   "exists": f["name"] in existing} for f in files[:50]],
    }

# ── YANDEX DISK AUTO-SYNC ─────────────────────────────────────────────────────

async def yadisk_sync_once() -> dict:
    """Синхронизирует фото из сохранённой ссылки. Возвращает статистику."""
    if yadisk_sync_lock.locked():
        # Синхронизация уже идёт в другом вызове (например, фоновый таймер
        # и ручное "Проверить сейчас" совпали по времени) — не запускаем
        # вторую параллельно, просто сообщаем, что она уже выполняется.
        return {"added": 0, "errors": 0, "skipped": 0, "already_running": True}

    async with yadisk_sync_lock:
        db = get_db()
        row = db.execute("SELECT public_url FROM yadisk_watch WHERE id=1 AND enabled=1").fetchone()
        if not row:
            db.close()
            return {"added": 0, "errors": 0, "skipped": 0}
        public_url = row["public_url"]
        existing_names = {r["original_name"] for r in
                          db.execute("SELECT original_name FROM photos").fetchall()}
        db.close()

        try:
            all_files = await yadisk_list_files(public_url)
        except Exception as e:
            db = get_db()
            db.execute(
                "UPDATE yadisk_watch SET last_sync_at=?, last_sync_added=0, last_sync_errors=-1 WHERE id=1",
                (datetime.utcnow().isoformat(),)
            )
            db.commit(); db.close()
            return {"added": 0, "errors": -1, "error_msg": str(e)}

        new_files = [f for f in all_files if f["name"] not in existing_names]
        added = errors = 0
        new_photos = []  # (photo_id, full_path) — для автотегирования после вставки
        for file_info in new_files:
            try:
                data = await yadisk_download_file(public_url, file_info["path"])
                img = PILImage.open(io.BytesIO(data)); img.load()
                uid = str(uuid.uuid4()); filename = uid + ".jpg"
                path = os.path.join(PHOTOS_DIR, filename)
                img.convert("RGB").save(path, "JPEG", quality=92)
                db = get_db()
                count = db.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
                cur = db.execute("INSERT INTO photos (filename, original_name, position) VALUES (?,?,?)",
                           (filename, file_info["name"], count))
                db.commit(); db.close()
                new_photos.append((cur.lastrowid, path))
                added += 1
            except Exception:
                errors += 1

        db = get_db()
        db.execute(
            "UPDATE yadisk_watch SET last_sync_at=?, last_sync_added=?, last_sync_errors=? WHERE id=1",
            (datetime.utcnow().isoformat(), added, errors)
        )
        db.commit(); db.close()

        # Автотегирование WD14 — при ошибке/недоступности модели просто пропускаем,
        # фото всё равно уже синхронизированы выше.
        for photo_id, path in new_photos:
            await auto_tag_photo_async(photo_id, path)

        if added > 0:
            await ws_manager.broadcast({"type": "photos_updated", "added": added})

        return {"added": added, "errors": errors, "skipped": len(all_files) - len(new_files)}


async def yadisk_sync_loop():
    """Фоновая задача: проверяет Яндекс.Диск с заданным интервалом."""
    while True:
        try:
            db = get_db()
            row = db.execute(
                "SELECT interval_minutes, last_sync_at FROM yadisk_watch WHERE id=1 AND enabled=1"
            ).fetchone()
            db.close()
            if row:
                interval = (row["interval_minutes"] or 60) * 60
                last = row["last_sync_at"]
                if last:
                    elapsed = (datetime.utcnow() - datetime.fromisoformat(last)).total_seconds()
                    wait = max(0, interval - elapsed)
                else:
                    wait = 0
                if wait > 0:
                    await asyncio.sleep(min(wait, 60))
                    continue
                await yadisk_sync_once()
            else:
                await asyncio.sleep(60)
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(60)


@app.get("/api/admin/yadisk-watch")
def get_yadisk_watch(user=Depends(admin_user)):
    db = get_db()
    row = db.execute("SELECT * FROM yadisk_watch WHERE id=1").fetchone()
    db.close()
    return dict(row) if row else None


@app.post("/api/admin/yadisk-watch")
async def set_yadisk_watch(
    public_url: str = Form(...),
    interval_minutes: int = Form(60),
    user=Depends(admin_user)
):
    db = get_db()
    db.execute(
        "INSERT INTO yadisk_watch (id, public_url, interval_minutes, enabled) VALUES (1,?,?,1) "
        "ON CONFLICT(id) DO UPDATE SET public_url=excluded.public_url, "
        "interval_minutes=excluded.interval_minutes, enabled=1, last_sync_at=NULL",
        (public_url.strip(), max(5, interval_minutes))
    )
    db.commit(); db.close()
    # Запустить немедленную синхронизацию в фоне
    asyncio.create_task(yadisk_sync_once())
    return {"status": "ok"}


@app.delete("/api/admin/yadisk-watch")
def delete_yadisk_watch(user=Depends(admin_user)):
    db = get_db()
    db.execute("DELETE FROM yadisk_watch WHERE id=1")
    db.commit(); db.close()
    return {"status": "ok"}


@app.post("/api/admin/yadisk-watch/sync-now")
async def yadisk_watch_sync_now(user=Depends(admin_user)):
    result = await yadisk_sync_once()
    return result


# ── GOOGLE DRIVE: IMPORT & AUTO-SYNC ──────────────────────────────────────────
# Полный аналог блока Яндекс.Диска выше, только источник — публичная папка
# Google Drive (доступ "у кого есть ссылка"). Подпапки сворачиваются в общий
# плоский список — без сохранения структуры, как и договаривались.

@app.post("/api/admin/import-gdrive")
async def import_gdrive(folder_url: str = Form(...), user=Depends(admin_user)):
    """
    Сканирует публичную папку Google Drive (включая подпапки) и импортирует
    все изображения. Уже существующие файлы (по имени) пропускаются.
    """
    db = get_db()
    existing_names = {r["original_name"] for r in
                      db.execute("SELECT original_name FROM photos").fetchall()}

    try:
        all_files = await gdrive_list_files(folder_url)
    except HTTPException:
        db.close()
        raise
    except Exception as e:
        db.close()
        raise HTTPException(502, f"Ошибка получения списка файлов: {e}")

    new_files = [f for f in all_files if f["name"] not in existing_names]

    added = 0
    errors = 0
    new_photos = []  # (photo_id, full_path) — для автотегирования после вставки
    for file_info in new_files:
        try:
            data = await gdrive_download_file(file_info["id"])
            img = PILImage.open(io.BytesIO(data))
            img.load()
            uid = str(uuid.uuid4())
            filename = uid + ".jpg"
            path = os.path.join(PHOTOS_DIR, filename)
            img.convert("RGB").save(path, "JPEG", quality=92)
            count = db.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
            cur = db.execute(
                "INSERT INTO photos (filename, original_name, position) VALUES (?,?,?)",
                (filename, file_info["name"], count)
            )
            db.commit()
            new_photos.append((cur.lastrowid, path))
            added += 1
        except Exception:
            errors += 1
            continue

    db.close()

    # Автотегирование WD14 — при ошибке/недоступности модели просто пропускаем,
    # фото всё равно уже импортированы выше.
    for photo_id, path in new_photos:
        await auto_tag_photo_async(photo_id, path)

    return {
        "total_found": len(all_files),
        "skipped": len(all_files) - len(new_files),
        "added": added,
        "errors": errors,
    }


@app.get("/api/admin/preview-gdrive")
async def preview_gdrive(folder_url: str, user=Depends(admin_user)):
    """Возвращает список файлов в папке без скачивания."""
    try:
        files = await gdrive_list_files(folder_url)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, str(e))

    db = get_db()
    existing = {r["original_name"] for r in
                db.execute("SELECT original_name FROM photos").fetchall()}
    db.close()

    return {
        "total": len(files),
        "new": len([f for f in files if f["name"] not in existing]),
        "files": [{"name": f["name"], "size": f["size"],
                   "exists": f["name"] in existing} for f in files[:50]],
    }


async def gdrive_sync_once() -> dict:
    """Синхронизирует фото из сохранённой ссылки на папку Google Drive."""
    if gdrive_sync_lock.locked():
        # Синхронизация уже идёт в другом вызове (например, фоновый таймер
        # и ручное "Проверить сейчас" совпали по времени) — не запускаем
        # вторую параллельно, просто сообщаем, что она уже выполняется.
        return {"added": 0, "errors": 0, "skipped": 0, "already_running": True}

    async with gdrive_sync_lock:
        db = get_db()
        row = db.execute("SELECT folder_url FROM gdrive_watch WHERE id=1 AND enabled=1").fetchone()
        if not row:
            db.close()
            return {"added": 0, "errors": 0, "skipped": 0}
        folder_url = row["folder_url"]
        existing_names = {r["original_name"] for r in
                          db.execute("SELECT original_name FROM photos").fetchall()}
        db.close()

        try:
            all_files = await gdrive_list_files(folder_url)
        except Exception as e:
            db = get_db()
            db.execute(
                "UPDATE gdrive_watch SET last_sync_at=?, last_sync_added=0, last_sync_errors=-1 WHERE id=1",
                (datetime.utcnow().isoformat(),)
            )
            db.commit(); db.close()
            return {"added": 0, "errors": -1, "error_msg": str(e)}

        new_files = [f for f in all_files if f["name"] not in existing_names]
        added = errors = 0
        new_photos = []  # (photo_id, full_path) — для автотегирования после вставки
        for file_info in new_files:
            try:
                data = await gdrive_download_file(file_info["id"])
                img = PILImage.open(io.BytesIO(data)); img.load()
                uid = str(uuid.uuid4()); filename = uid + ".jpg"
                path = os.path.join(PHOTOS_DIR, filename)
                img.convert("RGB").save(path, "JPEG", quality=92)
                db = get_db()
                count = db.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
                cur = db.execute("INSERT INTO photos (filename, original_name, position) VALUES (?,?,?)",
                           (filename, file_info["name"], count))
                db.commit(); db.close()
                new_photos.append((cur.lastrowid, path))
                added += 1
            except Exception:
                errors += 1

        db = get_db()
        db.execute(
            "UPDATE gdrive_watch SET last_sync_at=?, last_sync_added=?, last_sync_errors=? WHERE id=1",
            (datetime.utcnow().isoformat(), added, errors)
        )
        db.commit(); db.close()

        # Автотегирование WD14 — при ошибке/недоступности модели просто пропускаем,
        # фото всё равно уже синхронизированы выше.
        for photo_id, path in new_photos:
            await auto_tag_photo_async(photo_id, path)

        if added > 0:
            await ws_manager.broadcast({"type": "photos_updated", "added": added})

        return {"added": added, "errors": errors, "skipped": len(all_files) - len(new_files)}


async def gdrive_sync_loop():
    """Фоновая задача: проверяет Google Drive с заданным интервалом."""
    while True:
        try:
            db = get_db()
            row = db.execute(
                "SELECT interval_minutes, last_sync_at FROM gdrive_watch WHERE id=1 AND enabled=1"
            ).fetchone()
            db.close()
            if row:
                interval = (row["interval_minutes"] or 60) * 60
                last = row["last_sync_at"]
                if last:
                    elapsed = (datetime.utcnow() - datetime.fromisoformat(last)).total_seconds()
                    wait = max(0, interval - elapsed)
                else:
                    wait = 0
                if wait > 0:
                    await asyncio.sleep(min(wait, 60))
                    continue
                await gdrive_sync_once()
            else:
                await asyncio.sleep(60)
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(60)


@app.get("/api/admin/gdrive-watch")
def get_gdrive_watch(user=Depends(admin_user)):
    db = get_db()
    row = db.execute("SELECT * FROM gdrive_watch WHERE id=1").fetchone()
    db.close()
    return dict(row) if row else None


@app.post("/api/admin/gdrive-watch")
async def set_gdrive_watch(
    folder_url: str = Form(...),
    interval_minutes: int = Form(60),
    user=Depends(admin_user)
):
    db = get_db()
    db.execute(
        "INSERT INTO gdrive_watch (id, folder_url, interval_minutes, enabled) VALUES (1,?,?,1) "
        "ON CONFLICT(id) DO UPDATE SET folder_url=excluded.folder_url, "
        "interval_minutes=excluded.interval_minutes, enabled=1, last_sync_at=NULL",
        (folder_url.strip(), max(5, interval_minutes))
    )
    db.commit(); db.close()
    # Запустить немедленную синхронизацию в фоне
    asyncio.create_task(gdrive_sync_once())
    return {"status": "ok"}


@app.delete("/api/admin/gdrive-watch")
def delete_gdrive_watch(user=Depends(admin_user)):
    db = get_db()
    db.execute("DELETE FROM gdrive_watch WHERE id=1")
    db.commit(); db.close()
    return {"status": "ok"}


@app.post("/api/admin/gdrive-watch/sync-now")
async def gdrive_watch_sync_now(user=Depends(admin_user)):
    result = await gdrive_sync_once()
    return result


# ── PHOTOS ────────────────────────────────────────────────────────────────────

@app.post("/api/admin/photos/upload")
async def upload_photos(files: list[UploadFile] = File(...), user=Depends(admin_user)):
    db = get_db(); added = 0
    new_photos = []  # (photo_id, full_path) — для автотегирования после вставки
    for f in files:
        data = await f.read()
        try:
            img = PILImage.open(io.BytesIO(data)); img.load()
        except: continue
        uid = str(uuid.uuid4()); filename = uid + ".jpg"
        path = os.path.join(PHOTOS_DIR, filename)
        img.convert("RGB").save(path, "JPEG", quality=92)
        count = db.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
        cur = db.execute("INSERT INTO photos (filename,original_name,position) VALUES (?,?,?)",
                   (filename, f.filename, count))
        new_photos.append((cur.lastrowid, path))
        added += 1
    db.commit(); db.close()

    # Автотегирование WD14 — по договорённости: при ошибке/недоступности модели
    # просто пропускаем, фото всё равно уже загружено выше.
    for photo_id, path in new_photos:
        await auto_tag_photo_async(photo_id, path)

    return {"added": added}

@app.get("/api/admin/photos")
def admin_photos(user=Depends(admin_user)):
    db = get_db()
    rows = db.execute("""
        SELECT p.*, COUNT(r.id) as vote_count
        FROM photos p LEFT JOIN ratings r ON r.photo_id=p.id
        GROUP BY p.id ORDER BY p.position
    """).fetchall()
    db.close(); return [dict(r) for r in rows]

@app.delete("/api/admin/photos/{pid}")
def delete_photo(pid: int, user=Depends(admin_user)):
    db = get_db()
    row = db.execute("SELECT filename FROM photos WHERE id=?", (pid,)).fetchone()
    if row:
        try: os.remove(os.path.join(PHOTOS_DIR, row["filename"]))
        except: pass
        db.execute("DELETE FROM photos WHERE id=?", (pid,))
        db.execute("DELETE FROM ratings WHERE photo_id=?", (pid,))
        db.execute("DELETE FROM photo_tags WHERE photo_id=?", (pid,))
        db.commit()
    db.close(); return {"ok": True}

@app.get("/api/admin/duplicate-photos")
def find_duplicate_photos(user=Depends(admin_user)):
    """
    Находит фото с одинаковым original_name — обычно следствие гонки при
    параллельном запуске синхронизации (см. merge_duplicate_photos). Не
    удаляет ничего сама, только показывает, что будет затронуто.
    """
    db = get_db()
    groups = db.execute("""
        SELECT original_name, GROUP_CONCAT(id) as ids, COUNT(*) as cnt
        FROM photos GROUP BY original_name HAVING cnt > 1
        ORDER BY cnt DESC
    """).fetchall()
    db.close()
    return {
        "groups": len(groups),
        "extra_photos": sum(g["cnt"] - 1 for g in groups),
        "details": [{"original_name": g["original_name"], "ids": g["ids"], "count": g["cnt"]} for g in groups],
    }

@app.post("/api/admin/duplicate-photos/merge")
def merge_duplicate_photos(user=Depends(admin_user)):
    """
    Объединяет дубли (фото с одинаковым original_name, обычно из-за гонки
    при параллельном запуске авто-синхронизации до того, как в коде
    появилась защита через Lock). Для каждой группы дублей оставляет самую
    раннюю запись (минимальный id), а голоса и теги с удаляемых копий
    переносит на неё — не теряет, если разные люди успели проголосовать
    на разных копиях одного и того же фото. Файлы лишних копий удаляются с диска.
    """
    db = get_db()
    groups = db.execute("""
        SELECT original_name, GROUP_CONCAT(id) as ids
        FROM photos GROUP BY original_name HAVING COUNT(*) > 1
    """).fetchall()

    merged_groups = 0
    removed_photos = 0
    for g in groups:
        ids = sorted(int(x) for x in g["ids"].split(","))
        keep_id = ids[0]
        remove_ids = ids[1:]

        for rid in remove_ids:
            # переносим голоса и теги, которых ещё нет у keep_id (не теряем чужие оценки)
            for r in db.execute("SELECT user_id, tier_id FROM ratings WHERE photo_id=?", (rid,)).fetchall():
                db.execute("INSERT OR IGNORE INTO ratings (photo_id, user_id, tier_id) VALUES (?,?,?)",
                           (keep_id, r["user_id"], r["tier_id"]))
            for t in db.execute("SELECT user_id, tag_id, is_suggestion FROM photo_tags WHERE photo_id=?", (rid,)).fetchall():
                db.execute("INSERT OR IGNORE INTO photo_tags (photo_id, user_id, tag_id, is_suggestion) VALUES (?,?,?,?)",
                           (keep_id, t["user_id"], t["tag_id"], t["is_suggestion"]))

            row = db.execute("SELECT filename FROM photos WHERE id=?", (rid,)).fetchone()
            if row:
                try: os.remove(os.path.join(PHOTOS_DIR, row["filename"]))
                except: pass

            db.execute("DELETE FROM ratings WHERE photo_id=?", (rid,))
            db.execute("DELETE FROM photo_tags WHERE photo_id=?", (rid,))
            db.execute("DELETE FROM photos WHERE id=?", (rid,))
            removed_photos += 1

        merged_groups += 1

    db.commit()
    db.close()
    return {"merged_groups": merged_groups, "removed_photos": removed_photos}

# ── VOTING ────────────────────────────────────────────────────────────────────

@app.get("/api/current-photo")
def current_photo(user=Depends(current_user)):
    db = get_db()
    photo_id = get_setting(db, "current_photo_id")
    voting_open = get_setting(db, "voting_open")
    tiers = get_tiers(db)
    if not photo_id:
        db.close()
        return {"photo": None, "voting_open": voting_open=="1", "tiers": tiers}
    row = db.execute("""
        SELECT p.*, COUNT(r.id) as vote_count
        FROM photos p LEFT JOIN ratings r ON r.photo_id=p.id
        WHERE p.id=? GROUP BY p.id
    """, (photo_id,)).fetchone()
    user_rating = db.execute(
        "SELECT tier_id FROM ratings WHERE photo_id=? AND user_id=?",
        (photo_id, user["id"])).fetchone()
    # per-tier counts
    tier_counts = {}
    for r in db.execute("SELECT tier_id, COUNT(*) as cnt FROM ratings WHERE photo_id=? GROUP BY tier_id", (photo_id,)).fetchall():
        tier_counts[r["tier_id"]] = r["cnt"]
    total_users = db.execute("SELECT COUNT(*) FROM users WHERE is_admin=0 AND is_system=0").fetchone()[0]
    auto_advance = get_setting(db, "auto_advance") == "1"

    # ── следующее/предыдущее фото (для предзагрузки в кэш на фронтенде) ───────
    next_row = db.execute(
        "SELECT filename FROM photos WHERE position>(SELECT position FROM photos WHERE id=?) ORDER BY position LIMIT 1",
        (photo_id,)).fetchone()
    prev_row = db.execute(
        "SELECT filename FROM photos WHERE position<(SELECT position FROM photos WHERE id=?) ORDER BY position DESC LIMIT 1",
        (photo_id,)).fetchone()

    db.close()
    return {
        "photo": dict(row) if row else None,
        "user_tier": user_rating["tier_id"] if user_rating else None,
        "voting_open": voting_open == "1",
        "total_users": total_users,
        "tiers": tiers,
        "tier_counts": tier_counts,
        "auto_advance": auto_advance,
        "next_filename": next_row["filename"] if next_row else None,
        "prev_filename": prev_row["filename"] if prev_row else None,
    }

@app.post("/api/rate")
async def rate_photo(photo_id: int = Form(...), tier_id: str = Form(...), user=Depends(current_user)):
    db = get_db()
    tiers = get_tiers(db)
    valid_ids = {t["id"] for t in tiers}
    if tier_id not in valid_ids:
        db.close(); raise HTTPException(400, "Invalid tier")
    if get_setting(db, "voting_open") != "1":
        db.close(); raise HTTPException(403, "Voting is closed")
    if str(photo_id) != str(get_setting(db, "current_photo_id")):
        db.close(); raise HTTPException(400, "Not the current photo")
    db.execute("""
        INSERT INTO ratings (photo_id, user_id, tier_id) VALUES (?,?,?)
        ON CONFLICT(photo_id, user_id) DO UPDATE SET tier_id=excluded.tier_id
    """, (photo_id, user["id"], tier_id))
    db.commit()

    # ── AUTO-ADVANCE CHECK ────────────────────────────────────────────────────
    auto_advance = get_setting(db, "auto_advance") == "1"
    if auto_advance:
        online_ids = ws_manager.online_user_ids()
        # Только не-админы считаются «участниками»
        non_admin_online = set(
            r["id"] for r in db.execute(
                "SELECT id FROM users WHERE is_admin=0 AND is_system=0 AND id IN ({})".format(
                    ",".join("?" * len(online_ids)) if online_ids else "NULL"
                ), tuple(online_ids)
            ).fetchall()
        ) if online_ids else set()

        if non_admin_online:
            voted_ids = set(
                r["user_id"] for r in db.execute(
                    "SELECT user_id FROM ratings WHERE photo_id=?", (photo_id,)
                ).fetchall()
            )
            all_voted = non_admin_online.issubset(voted_ids)
            if all_voted:
                # Переходим к следующему фото
                nxt = db.execute(
                    "SELECT id FROM photos WHERE position>(SELECT position FROM photos WHERE id=?) ORDER BY position LIMIT 1",
                    (photo_id,)
                ).fetchone()
                if nxt:
                    set_setting(db, "current_photo_id", str(nxt["id"]))
                    set_setting(db, "voting_open", "1")
                    db.close()
                    await ws_manager.broadcast({"type": "photo_change", "photo_id": nxt["id"], "auto": True, "direction": "next"})
                    return {"ok": True, "auto_advanced": True}
                else:
                    # Все фото просмотрены
                    db.close()
                    await ws_manager.broadcast({"type": "all_done"})
                    return {"ok": True, "auto_advanced": True}

    db.close()
    await ws_manager.broadcast({"type": "vote_update", "photo_id": photo_id})
    return {"ok": True}


@app.get("/api/tags/search")
def search_tags(q: str = "", limit: int = 20, user=Depends(current_user)):
    db = get_db()
    q = q.strip().replace("_", " ")
    pattern = "%" + q.replace("%","").replace("_","\_") + "%"
    rows = db.execute(
        "SELECT id, name, category FROM tags WHERE name LIKE ? ESCAPE '\\' ORDER BY length(name), name LIMIT ?",
        (pattern, min(limit, 50))).fetchall()
    db.close()
    return [{"id": r["id"], "name": r["name"], "category": r["category"]} for r in rows]

@app.get("/api/photo-tags/{photo_id}")
def get_photo_tags(photo_id: int, user=Depends(current_user)):
    db = get_db()
    rows = db.execute("""
        SELECT t.id as tag_id, t.name as tag_name, u.username, pt.user_id, pt.is_suggestion
        FROM photo_tags pt
        JOIN tags t ON t.id = pt.tag_id
        JOIN users u ON u.id = pt.user_id
        WHERE pt.photo_id = ?
        ORDER BY t.name
    """, (photo_id,)).fetchall()
    db.close()
    mine = [{"id": r["tag_id"], "name": r["tag_name"]} for r in rows if r["user_id"] == user["id"] and not r["is_suggestion"]]
    all_tags = [{"tag_id": r["tag_id"], "tag_name": r["tag_name"], "username": r["username"],
                 "is_suggestion": bool(r["is_suggestion"])} for r in rows]
    return {"mine": mine, "all": all_tags}

@app.post("/api/photo-tags/{photo_id}/add")
def add_photo_tag(photo_id: int, tag_name: str = Form(...), user=Depends(current_user)):
    db = get_db()
    # find by name (case-insensitive)
    tag = db.execute("SELECT id FROM tags WHERE LOWER(name)=LOWER(?)", (tag_name.strip(),)).fetchone()
    if not tag:
        # allow adding if name exists in any form
        db.close(); raise HTTPException(404, "Тег не найден")
    try:
        # is_suggestion=0 явно — это обычный ручной тег, а не предложение автотегирования.
        db.execute("INSERT INTO photo_tags (photo_id, user_id, tag_id, is_suggestion) VALUES (?,?,?,0)",
                   (photo_id, user["id"], tag["id"]))
        db.commit()
    except sqlite3.IntegrityError:
        pass
    db.close()
    return {"ok": True}

@app.post("/api/photo-tags/{photo_id}/confirm")
def confirm_suggested_tag(photo_id: int, tag_id: int = Form(...), user=Depends(current_user)):
    """
    Подтверждает предложенный автотегированием тег: снимает флаг is_suggestion,
    после чего тег сразу считается обычным — виден в тир-листе, фильтрах
    и счётчиках. По договорённости подтверждения любого ОДНОГО человека
    достаточно — без накопления нескольких голосов.
    """
    db = get_db()
    row = db.execute(
        "SELECT id FROM photo_tags WHERE photo_id=? AND tag_id=? AND is_suggestion=1",
        (photo_id, tag_id)
    ).fetchone()
    if not row:
        db.close()
        raise HTTPException(404, "Предложенный тег не найден (возможно, уже подтверждён)")
    db.execute("UPDATE photo_tags SET is_suggestion=0 WHERE id=?", (row["id"],))
    db.commit()
    db.close()
    return {"ok": True}

@app.post("/api/photo-tags/{photo_id}/reject")
def reject_suggested_tag(photo_id: int, tag_id: int = Form(...), user=Depends(current_user)):
    """
    Отклоняет предложенный автотегированием тег — полностью удаляет
    запись-предложение. Это явное "нет, это неверно" от любого человека,
    в отличие от простого игнорирования (когда предложение просто остаётся
    висеть непросмотренным до тех пор, пока кто-то не подтвердит или отклонит).
    """
    db = get_db()
    db.execute(
        "DELETE FROM photo_tags WHERE photo_id=? AND tag_id=? AND is_suggestion=1",
        (photo_id, tag_id)
    )
    db.commit()
    db.close()
    return {"ok": True}

@app.delete("/api/photo-tags/{photo_id}/{tag_id}")
def remove_photo_tag(photo_id: int, tag_id: int, user=Depends(current_user)):
    db = get_db()
    db.execute("DELETE FROM photo_tags WHERE photo_id=? AND user_id=? AND tag_id=?",
               (photo_id, user["id"], tag_id))
    db.commit(); db.close()
    return {"ok": True}

@app.get("/api/photo-votes/{photo_id}")
def photo_votes(photo_id: int, user=Depends(current_user)):
    db = get_db()
    tiers = get_tiers(db)
    tier_map = {t["id"]: t for t in tiers}
    rows = db.execute("""
        SELECT u.username, r.tier_id, r.created_at
        FROM ratings r JOIN users u ON u.id = r.user_id
        WHERE r.photo_id = ?
        ORDER BY r.created_at DESC
    """, (photo_id,)).fetchall()
    db.close()
    return [{"username": r["username"], "tier_id": r["tier_id"],
             "tier_label": tier_map.get(r["tier_id"], {}).get("label", r["tier_id"]),
             "tier_color": tier_map.get(r["tier_id"], {}).get("color", "#888"),
             "created_at": r["created_at"]} for r in rows]


@app.get("/api/photo-detail/{photo_id}")
def photo_detail(photo_id: int, user=Depends(current_user)):
    db = get_db()
    tiers = get_tiers(db)
    tier_map = {t["id"]: t for t in tiers}

    # photo info
    photo = db.execute("SELECT * FROM photos WHERE id=?", (photo_id,)).fetchone()
    if not photo:
        db.close(); raise HTTPException(404)

    # votes per user
    votes = db.execute("""
        SELECT u.username, r.tier_id
        FROM ratings r JOIN users u ON u.id=r.user_id
        WHERE r.photo_id=? ORDER BY u.username
    """, (photo_id,)).fetchall()

    # tags with user counts (только подтверждённые — suggestion'ы сюда не попадают)
    tags = db.execute("""
        SELECT t.name, COUNT(pt.user_id) as cnt,
               GROUP_CONCAT(u.username, ', ') as users
        FROM photo_tags pt
        JOIN tags t ON t.id=pt.tag_id
        JOIN users u ON u.id=pt.user_id
        WHERE pt.photo_id=? AND pt.is_suggestion=0
        GROUP BY t.id ORDER BY cnt DESC, t.name
    """, (photo_id,)).fetchall()

    db.close()
    return {
        "photo": dict(photo),
        "votes": [{"username": r["username"], "tier_id": r["tier_id"],
                   "tier_label": tier_map.get(r["tier_id"], {}).get("label", r["tier_id"]),
                   "tier_color": tier_map.get(r["tier_id"], {}).get("color", "#888")}
                  for r in votes],
        "tags": [{"name": r["name"], "count": r["cnt"], "users": r["users"]} for r in tags],
    }

# ── ADMIN NAV ─────────────────────────────────────────────────────────────────

@app.post("/api/admin/next-photo")
async def next_photo(user=Depends(admin_user)):
    db = get_db(); cur = get_setting(db, "current_photo_id")
    nxt = db.execute(
        "SELECT id FROM photos WHERE position>(SELECT position FROM photos WHERE id=?) ORDER BY position LIMIT 1",
        (cur,)).fetchone() if cur else db.execute("SELECT id FROM photos ORDER BY position LIMIT 1").fetchone()
    if not nxt: db.close(); return {"done": True}
    set_setting(db, "current_photo_id", str(nxt["id"]))
    set_setting(db, "voting_open", "1")
    db.close()
    await ws_manager.broadcast({"type": "photo_change", "photo_id": nxt["id"], "direction": "next"})
    return {"done": False, "photo_id": nxt["id"]}

@app.post("/api/admin/prev-photo")
async def prev_photo(user=Depends(admin_user)):
    db = get_db(); cur = get_setting(db, "current_photo_id")
    if not cur: raise HTTPException(400, "No current photo")
    prev = db.execute(
        "SELECT id FROM photos WHERE position<(SELECT position FROM photos WHERE id=?) ORDER BY position DESC LIMIT 1",
        (cur,)).fetchone()
    if not prev: raise HTTPException(400, "Already at first photo")
    set_setting(db, "current_photo_id", str(prev["id"]))
    set_setting(db, "voting_open", "1")
    db.close()
    await ws_manager.broadcast({"type": "photo_change", "photo_id": prev["id"], "direction": "prev"})
    return {"photo_id": prev["id"]}

@app.post("/api/admin/set-photo/{pid}")
async def set_photo(pid: int, user=Depends(admin_user)):
    db = get_db()
    if not db.execute("SELECT id FROM photos WHERE id=?", (pid,)).fetchone(): raise HTTPException(404)
    set_setting(db, "current_photo_id", str(pid))
    set_setting(db, "voting_open", "1")
    db.close()
    await ws_manager.broadcast({"type": "photo_change", "photo_id": pid})
    return {"ok": True}


@app.post("/api/admin/shuffle")
async def shuffle_photos(user=Depends(admin_user)):
    """Перемешивает порядок фотографий случайно."""
    import random
    db = get_db()
    ids = [r["id"] for r in db.execute("SELECT id FROM photos").fetchall()]
    random.shuffle(ids)
    for new_pos, pid in enumerate(ids):
        db.execute("UPDATE photos SET position=? WHERE id=?", (new_pos, pid))
    # перейти на первое фото в новом порядке
    first = db.execute("SELECT id FROM photos ORDER BY position LIMIT 1").fetchone()
    if first:
        set_setting(db, "current_photo_id", str(first["id"]))
        set_setting(db, "voting_open", "1")
    db.commit()
    db.close()
    if first:
        await ws_manager.broadcast({"type": "photo_change", "photo_id": first["id"]})
    return {"ok": True, "count": len(ids), "first_id": first["id"] if first else None}

@app.post("/api/admin/close-voting")
async def close_voting(user=Depends(admin_user)):
    db = get_db(); set_setting(db, "voting_open", "0"); db.close()
    await ws_manager.broadcast({"type": "voting_closed"})
    return {"ok": True}

@app.post("/api/admin/auto-advance")
async def set_auto_advance(enabled: bool = Form(...), user=Depends(admin_user)):
    db = get_db()
    set_setting(db, "auto_advance", "1" if enabled else "0")
    db.close()
    await ws_manager.broadcast({"type": "auto_advance_changed", "enabled": enabled})
    return {"enabled": enabled}

@app.get("/api/admin/auto-advance")
def get_auto_advance(user=Depends(admin_user)):
    db = get_db()
    val = get_setting(db, "auto_advance") == "1"
    db.close()
    return {"enabled": val}

@app.get("/api/admin/wd14-status")
def get_wd14_status(user=Depends(admin_user)):
    """
    Статус автотегирования для админ-панели: проверяет не только наличие
    файлов модели, но и их реальный размер (см. wd14_tagger.is_available) —
    чтобы сразу было видно частую ошибку, когда вместо ~388 МБ model.onnx
    скачался крошечный LFS/Xet pointer-файл (0 КБ).
    """
    available = wd14_tagger.is_available()
    model_size = 0
    tags_size = 0
    try:
        if os.path.exists(wd14_tagger.MODEL_PATH):
            model_size = os.path.getsize(wd14_tagger.MODEL_PATH)
        if os.path.exists(wd14_tagger.TAGS_CSV_PATH):
            tags_size = os.path.getsize(wd14_tagger.TAGS_CSV_PATH)
    except OSError:
        pass
    return {
        "available": available,
        "model_size_bytes": model_size,
        "tags_size_bytes": tags_size,
    }

@app.get("/api/online-users")
def online_users(user=Depends(current_user)):
    """Возвращает список онлайн-пользователей (не-админов)."""
    online_ids = ws_manager.online_user_ids()
    if not online_ids:
        return {"count": 0, "users": []}
    db = get_db()
    rows = db.execute(
        "SELECT id, username FROM users WHERE is_admin=0 AND is_system=0 AND id IN ({})".format(
            ",".join("?" * len(online_ids))
        ), tuple(online_ids)
    ).fetchall()
    db.close()
    return {"count": len(rows), "users": [dict(r) for r in rows]}

# ── TIERLIST ──────────────────────────────────────────────────────────────────

@app.get("/api/tierlist/tags")
def tierlist_tags(user=Depends(current_user)):
    """Возвращает все подтверждённые теги, которые есть хотя бы у одного оценённого фото."""
    db = get_db()
    rows = db.execute("""
        SELECT DISTINCT t.id, t.name
        FROM tags t
        JOIN photo_tags pt ON pt.tag_id = t.id
        JOIN ratings r ON r.photo_id = pt.photo_id
        WHERE pt.is_suggestion = 0
        ORDER BY t.name
    """).fetchall()
    db.close()
    return [{"id": r["id"], "name": r["name"]} for r in rows]


@app.get("/api/tierlist")
def tierlist(tag_ids: str = "", exclude_tag_ids: str = "", user=Depends(current_user)):
    """
    tag_ids — список id тегов через запятую для включения (фото должны иметь ВСЕ).
    exclude_tag_ids — список id тегов через запятую для исключения (фото не должны иметь НИ ОДНОГО).
    """
    db = get_db()
    tiers = get_tiers(db)
    tier_order = [t["id"] for t in tiers]
    tier_map = {t["id"]: t for t in tiers}

    n = len(tiers)
    tier_score = {t["id"]: n - i for i, t in enumerate(tiers)}

    # parse include filter
    selected_tag_ids = []
    if tag_ids.strip():
        try:
            selected_tag_ids = [int(x) for x in tag_ids.split(",") if x.strip()]
        except ValueError:
            pass

    # parse exclude filter
    excluded_tag_ids = []
    if exclude_tag_ids.strip():
        try:
            excluded_tag_ids = [int(x) for x in exclude_tag_ids.split(",") if x.strip()]
        except ValueError:
            pass

    # build allowed_ids from include filter
    if selected_tag_ids:
        placeholders = ",".join("?" * len(selected_tag_ids))
        filtered_ids = db.execute(f"""
            SELECT photo_id
            FROM photo_tags
            WHERE tag_id IN ({placeholders}) AND is_suggestion = 0
            GROUP BY photo_id
            HAVING COUNT(DISTINCT tag_id) = {len(selected_tag_ids)}
        """, selected_tag_ids).fetchall()
        allowed_ids = {r["photo_id"] for r in filtered_ids}
    else:
        # all rated photos
        all_rated = db.execute("SELECT DISTINCT photo_id FROM ratings").fetchall()
        allowed_ids = {r["photo_id"] for r in all_rated}

    # apply exclude filter — remove photos that have ANY of the excluded tags
    if excluded_tag_ids and allowed_ids:
        excl_placeholders = ",".join("?" * len(excluded_tag_ids))
        excl_rows = db.execute(f"""
            SELECT DISTINCT photo_id
            FROM photo_tags
            WHERE tag_id IN ({excl_placeholders}) AND is_suggestion = 0
        """, excluded_tag_ids).fetchall()
        excl_photo_ids = {r["photo_id"] for r in excl_rows}
        allowed_ids -= excl_photo_ids

    if not allowed_ids:
        db.close()
        return {"tiers": {tid: [] for tid in tier_order},
                "tier_order": tier_order, "tier_map": tier_map,
                "active_tag_ids": selected_tag_ids,
                "excluded_tag_ids": excluded_tag_ids}

    id_placeholders = ",".join("?" * len(allowed_ids))
    rows = db.execute(f"""
        SELECT p.id, p.filename, p.original_name,
               r.tier_id, COUNT(*) as cnt
        FROM ratings r JOIN photos p ON p.id=r.photo_id
        WHERE p.id IN ({id_placeholders})
        GROUP BY p.id, r.tier_id
    """, list(allowed_ids)).fetchall()

    from collections import defaultdict
    photo_info = {}
    photo_scores = defaultdict(list)
    for row in rows:
        pid = row["id"]
        photo_info[pid] = {"id": pid, "filename": row["filename"], "original_name": row["original_name"]}
        for _ in range(row["cnt"]):
            photo_scores[pid].append(tier_score.get(row["tier_id"], 1))

    result = {tid: [] for tid in tier_order}
    for pid, scores in photo_scores.items():
        avg = sum(scores) / len(scores)
        idx = round(n - avg)
        idx = max(0, min(n-1, idx))
        assigned_tier = tier_order[idx]
        result[assigned_tier].append({
            **photo_info[pid],
            "vote_count": len(scores),
            "avg_score": round(avg, 2),
            "tier_id": assigned_tier,
        })

    for tid in result:
        result[tid].sort(key=lambda x: -x["avg_score"])

    db.close()
    return {"tiers": result, "tier_order": tier_order, "tier_map": tier_map,
            "active_tag_ids": selected_tag_ids,
            "excluded_tag_ids": excluded_tag_ids}

@app.get("/api/stats")
def stats(user=Depends(admin_user)):
    db = get_db()
    r = {
        "total_photos": db.execute("SELECT COUNT(*) FROM photos").fetchone()[0],
        "rated_photos": db.execute("SELECT COUNT(DISTINCT photo_id) FROM ratings").fetchone()[0],
        "total_users": db.execute("SELECT COUNT(*) FROM users WHERE is_admin=0 AND is_system=0").fetchone()[0],
        "total_votes": db.execute("SELECT COUNT(*) FROM ratings").fetchone()[0],
    }
    db.close(); return r

@app.post("/api/admin/reset-db")
def reset_db(user=Depends(admin_user)):
    db = get_db()
    # delete all non-admin users
    db.execute("DELETE FROM users WHERE is_admin=0 AND is_system=0")
    # delete all photos from disk
    photos = db.execute("SELECT filename FROM photos").fetchall()
    for p in photos:
        try: os.remove(os.path.join(PHOTOS_DIR, p["filename"]))
        except: pass
    db.execute("DELETE FROM photos")
    db.execute("DELETE FROM ratings")
    db.execute("DELETE FROM photo_tags")
    db.execute("UPDATE settings SET value=NULL WHERE key='current_photo_id'")
    db.execute("UPDATE settings SET value='1' WHERE key='voting_open'")
    db.commit()
    db.close()
    return {"ok": True}

@app.get("/api/admin/users")
def list_users(user=Depends(admin_user)):
    db = get_db()
    rows = db.execute("SELECT id,username,is_admin,created_at FROM users ORDER BY id").fetchall()
    db.close(); return [dict(r) for r in rows]

@app.post("/api/admin/users/{uid}/make-admin")
def make_admin_user(uid: int, user=Depends(admin_user)):
    db = get_db(); db.execute("UPDATE users SET is_admin=1 WHERE id=?", (uid,)); db.commit(); db.close()
    return {"ok": True}


@app.get("/api/users-list")
def users_list(user=Depends(current_user)):
    db = get_db()
    rows = db.execute(
        "SELECT u.id, u.username, u.is_admin, COUNT(r.id) as vote_count "
        "FROM users u LEFT JOIN ratings r ON r.user_id=u.id "
        "WHERE u.is_system=0 "
        "GROUP BY u.id ORDER BY vote_count DESC, u.username"
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


@app.get("/api/user-stats/{uid}")
def user_stats(uid: int, user=Depends(current_user)):
    db = get_db()
    tiers = get_tiers(db)
    tier_map = {t["id"]: t for t in tiers}
    tier_order = [t["id"] for t in tiers]
    n = len(tiers)

    target = db.execute("SELECT id, username FROM users WHERE id=?", (uid,)).fetchone()
    if not target:
        db.close()
        raise HTTPException(404, "User not found")

    tier_counts = {}
    for row in db.execute("SELECT tier_id, COUNT(*) as cnt FROM ratings WHERE user_id=? GROUP BY tier_id", (uid,)).fetchall():
        tier_counts[row["tier_id"]] = row["cnt"]
    total_votes = sum(tier_counts.values())

    user_ratings = db.execute("SELECT photo_id, tier_id FROM ratings WHERE user_id=?", (uid,)).fetchall()
    agree_score = 0.0
    total_compared = 0
    for ur in user_ratings:
        pid = ur["photo_id"]
        others = db.execute(
            "SELECT tier_id, COUNT(*) as cnt FROM ratings WHERE photo_id=? AND user_id!=?",
            (pid, uid)
        ).fetchall()
        if not others:
            continue
        total_other = sum(o["cnt"] for o in others)
        if total_other == 0:   # <-- добавить эту проверку
            continue
        same = next((o["cnt"] for o in others if o["tier_id"] == ur["tier_id"]), 0)
        agree_score += same / total_other
        # Сколько из других проголосовало так же как этот пользователь
        same = next((o["cnt"] for o in others if o["tier_id"] == ur["tier_id"]), 0)
        agree_score += same / total_other
        total_compared += 1

    agreement_pct = round(agree_score / total_compared * 100) if total_compared else None

    top_n = max(1, n // 2)
    top_tier_ids = tier_order[:top_n]
    ph = ",".join("?" * len(top_tier_ids))
    top_photos = [r["photo_id"] for r in db.execute(
        f"SELECT photo_id FROM ratings WHERE user_id=? AND tier_id IN ({ph})",
        [uid] + top_tier_ids
    ).fetchall()]

    fav_tags = []
    if top_photos:
        pp = ",".join("?" * len(top_photos))
        fav_tags = [dict(r) for r in db.execute(
            f"SELECT t.name, COUNT(*) as cnt FROM photo_tags pt "
            f"JOIN tags t ON t.id=pt.tag_id WHERE pt.photo_id IN ({pp}) AND pt.is_suggestion=0 "
            f"GROUP BY t.id ORDER BY cnt DESC LIMIT 12",
            top_photos
        ).fetchall()]

    db.close()
    return {
        "user": {"id": target["id"], "username": target["username"]},
        "total_votes": total_votes,
        "tier_counts": [
            {"tier_id": tid, "label": tier_map.get(tid, {}).get("label", tid),
             "color": tier_map.get(tid, {}).get("color", "#888"),
             "count": tier_counts.get(tid, 0)}
            for tid in tier_order
        ],
        "agreement_pct": agreement_pct,
        "total_compared": total_compared,
        "fav_tags": fav_tags,
    }


@app.get("/api/compare/{uid1}/{uid2}")
def compare_users(uid1: int, uid2: int, user=Depends(current_user)):
    db = get_db()
    tiers = get_tiers(db)
    tier_order = [t["id"] for t in tiers]
    tier_map = {t["id"]: t for t in tiers}
    n = len(tiers)
    tier_score = {t["id"]: n - i for i, t in enumerate(tiers)}

    u1 = db.execute("SELECT id, username FROM users WHERE id=?", (uid1,)).fetchone()
    u2 = db.execute("SELECT id, username FROM users WHERE id=?", (uid2,)).fetchone()
    if not u1 or not u2:
        db.close()
        raise HTTPException(404, "User not found")

    r1 = {r["photo_id"]: r["tier_id"] for r in
          db.execute("SELECT photo_id, tier_id FROM ratings WHERE user_id=?", (uid1,)).fetchall()}
    r2 = {r["photo_id"]: r["tier_id"] for r in
          db.execute("SELECT photo_id, tier_id FROM ratings WHERE user_id=?", (uid2,)).fetchall()}

    common = set(r1.keys()) & set(r2.keys())
    exact_match = 0
    close_match = 0
    disagreements = []

    for pid in common:
        t1, t2 = r1[pid], r2[pid]
        diff = abs(tier_score.get(t1, 1) - tier_score.get(t2, 1))
        if diff == 0:
            exact_match += 1
        elif diff == 1:
            close_match += 1
        else:
            photo = db.execute("SELECT filename, original_name FROM photos WHERE id=?", (pid,)).fetchone()
            if photo:
                disagreements.append({
                    "photo_id": pid,
                    "filename": photo["filename"],
                    "original_name": photo["original_name"],
                    "tier1": t1, "label1": tier_map.get(t1, {}).get("label", t1),
                    "color1": tier_map.get(t1, {}).get("color", "#888"),
                    "tier2": t2, "label2": tier_map.get(t2, {}).get("label", t2),
                    "color2": tier_map.get(t2, {}).get("color", "#888"),
                    "diff": diff,
                })

    disagreements.sort(key=lambda x: -x["diff"])
    similarity = round((exact_match + close_match * 0.5) / len(common) * 100) if common else 0

    db.close()
    return {
        "user1": {"id": u1["id"], "username": u1["username"]},
        "user2": {"id": u2["id"], "username": u2["username"]},
        "common_photos": len(common),
        "exact_match": exact_match,
        "close_match": close_match,
        "similarity": similarity,
        "disagreements": disagreements[:20],
    }

app.mount("/photos", StaticFiles(directory=PHOTOS_DIR), name="photos")
app.mount("/static", StaticFiles(directory="/app/frontend/static"), name="static")

@app.get("/{path:path}")
def serve_frontend(path: str): return FileResponse("/app/frontend/index.html")

@app.get("/")
def root(): return FileResponse("/app/frontend/index.html")
