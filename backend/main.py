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

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass

@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(yadisk_sync_loop())
    yield
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
            db.execute(
                "INSERT INTO photos (filename, original_name, position) VALUES (?,?,?)",
                (filename, file_info["name"], count)
            )
            db.commit()
            added += 1
        except Exception:
            errors += 1
            continue

    db.close()
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
    for file_info in new_files:
        try:
            data = await yadisk_download_file(public_url, file_info["path"])
            img = PILImage.open(io.BytesIO(data)); img.load()
            uid = str(uuid.uuid4()); filename = uid + ".jpg"
            path = os.path.join(PHOTOS_DIR, filename)
            img.convert("RGB").save(path, "JPEG", quality=92)
            db = get_db()
            count = db.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
            db.execute("INSERT INTO photos (filename, original_name, position) VALUES (?,?,?)",
                       (filename, file_info["name"], count))
            db.commit(); db.close()
            added += 1
        except Exception:
            errors += 1

    db = get_db()
    db.execute(
        "UPDATE yadisk_watch SET last_sync_at=?, last_sync_added=?, last_sync_errors=? WHERE id=1",
        (datetime.utcnow().isoformat(), added, errors)
    )
    db.commit(); db.close()

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


# ── PHOTOS ────────────────────────────────────────────────────────────────────

@app.post("/api/admin/photos/upload")
async def upload_photos(files: list[UploadFile] = File(...), user=Depends(admin_user)):
    db = get_db(); added = 0
    for f in files:
        data = await f.read()
        try:
            img = PILImage.open(io.BytesIO(data)); img.load()
        except: continue
        uid = str(uuid.uuid4()); filename = uid + ".jpg"
        img.convert("RGB").save(os.path.join(PHOTOS_DIR, filename), "JPEG", quality=92)
        count = db.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
        db.execute("INSERT INTO photos (filename,original_name,position) VALUES (?,?,?)",
                   (filename, f.filename, count))
        added += 1
    db.commit(); db.close()
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
        db.commit()
    db.close(); return {"ok": True}

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
    total_users = db.execute("SELECT COUNT(*) FROM users WHERE is_admin=0").fetchone()[0]
    auto_advance = get_setting(db, "auto_advance") == "1"
    db.close()
    return {
        "photo": dict(row) if row else None,
        "user_tier": user_rating["tier_id"] if user_rating else None,
        "voting_open": voting_open == "1",
        "total_users": total_users,
        "tiers": tiers,
        "tier_counts": tier_counts,
        "auto_advance": auto_advance,
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
                "SELECT id FROM users WHERE is_admin=0 AND id IN ({})".format(
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
                    await ws_manager.broadcast({"type": "photo_change", "photo_id": nxt["id"], "auto": True})
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
        SELECT t.id as tag_id, t.name as tag_name, u.username, pt.user_id
        FROM photo_tags pt
        JOIN tags t ON t.id = pt.tag_id
        JOIN users u ON u.id = pt.user_id
        WHERE pt.photo_id = ?
        ORDER BY t.name
    """, (photo_id,)).fetchall()
    db.close()
    mine = [{"id": r["tag_id"], "name": r["tag_name"]} for r in rows if r["user_id"] == user["id"]]
    all_tags = [{"tag_id": r["tag_id"], "tag_name": r["tag_name"], "username": r["username"]} for r in rows]
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
        db.execute("INSERT INTO photo_tags (photo_id, user_id, tag_id) VALUES (?,?,?)",
                   (photo_id, user["id"], tag["id"]))
        db.commit()
    except sqlite3.IntegrityError:
        pass
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

    # tags with user counts
    tags = db.execute("""
        SELECT t.name, COUNT(pt.user_id) as cnt,
               GROUP_CONCAT(u.username, ', ') as users
        FROM photo_tags pt
        JOIN tags t ON t.id=pt.tag_id
        JOIN users u ON u.id=pt.user_id
        WHERE pt.photo_id=?
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
    await ws_manager.broadcast({"type": "photo_change", "photo_id": nxt["id"]})
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
    await ws_manager.broadcast({"type": "photo_change", "photo_id": prev["id"]})
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

@app.get("/api/online-users")
def online_users(user=Depends(current_user)):
    """Возвращает список онлайн-пользователей (не-админов)."""
    online_ids = ws_manager.online_user_ids()
    if not online_ids:
        return {"count": 0, "users": []}
    db = get_db()
    rows = db.execute(
        "SELECT id, username FROM users WHERE is_admin=0 AND id IN ({})".format(
            ",".join("?" * len(online_ids))
        ), tuple(online_ids)
    ).fetchall()
    db.close()
    return {"count": len(rows), "users": [dict(r) for r in rows]}

# ── TIERLIST ──────────────────────────────────────────────────────────────────

@app.get("/api/tierlist/tags")
def tierlist_tags(user=Depends(current_user)):
    """Возвращает все теги, которые есть хотя бы у одного оценённого фото."""
    db = get_db()
    rows = db.execute("""
        SELECT DISTINCT t.id, t.name
        FROM tags t
        JOIN photo_tags pt ON pt.tag_id = t.id
        JOIN ratings r ON r.photo_id = pt.photo_id
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
            WHERE tag_id IN ({placeholders})
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
            WHERE tag_id IN ({excl_placeholders})
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
        "total_users": db.execute("SELECT COUNT(*) FROM users WHERE is_admin=0").fetchone()[0],
        "total_votes": db.execute("SELECT COUNT(*) FROM ratings").fetchone()[0],
    }
    db.close(); return r

@app.post("/api/admin/reset-db")
def reset_db(user=Depends(admin_user)):
    db = get_db()
    # delete all non-admin users
    db.execute("DELETE FROM users WHERE is_admin=0")
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
            f"JOIN tags t ON t.id=pt.tag_id WHERE pt.photo_id IN ({pp}) "
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
