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
        # ws -> (user_id, session_id); user_id может быть None (анонимный токен истёк),
        # session_id может быть None (подключение к списку сессий, не к конкретной)
        self.connections: dict[WebSocket, tuple] = {}

    async def connect(self, ws: WebSocket, user_id: Optional[int] = None, session_id: Optional[int] = None):
        await ws.accept()
        self.connections[ws] = (user_id, session_id)

    def disconnect(self, ws: WebSocket):
        self.connections.pop(ws, None)

    def online_user_ids(self, session_id: Optional[int] = None) -> set[int]:
        """Возвращает множество user_id онлайн. Если session_id указан —
        только те, кто подключён именно к этой сессии."""
        result = set()
        for uid, sid in self.connections.values():
            if uid is None:
                continue
            if session_id is not None and sid != session_id:
                continue
            result.add(uid)
        return result

    async def broadcast(self, data: dict, session_id: Optional[int] = None):
        """Если session_id указан — рассылает только подключениям этой сессии.
        Если не указан — рассылает всем (используется для общих уведомлений,
        например 'появились новые фото после импорта')."""
        msg = json.dumps(data, ensure_ascii=False)
        dead = []
        for ws, (uid, sid) in self.connections.items():
            if session_id is not None and sid != session_id:
                continue
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

ws_manager = WSManager()

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket, token: Optional[str] = None, session_id: Optional[int] = None):
    user_id = None
    if token:
        try:
            d = jwt.decode(token, SECRET, algorithms=["HS256"])
            user_id = int(d["sub"])
        except Exception:
            pass
    await ws_manager.connect(ws, user_id, session_id)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(ws)

SECRET = os.getenv("JWT_SECRET", "change-me-in-production-please")
PHOTOS_DIR = "/app/photos"
THUMBS_DIR = "/app/photos/_thumbs"  # уменьшенные превью для сетки/карточек — считаются лениво, по первому запросу
THUMB_MAX_SIZE = 320  # px по длинной стороне
DB_PATH = "/app/data/db.sqlite3"
AUTO_TAGGER_USERNAME = "auto-tagger"  # системный "пользователь", от имени которого пишутся автотеги
GOOGLE_DRIVE_API_KEY = os.getenv("GOOGLE_DRIVE_API_KEY", "")  # для доступа к публичным папкам Google Drive
os.makedirs(PHOTOS_DIR, exist_ok=True)
os.makedirs(THUMBS_DIR, exist_ok=True)
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

    # ── МИГРАЦИЯ НА СЕССИИ: если ratings уже существует со старой схемой
    # (без колонки session_id — то есть это база до введения системы сессий),
    # переименовываем её, чтобы дальше создать новую таблицу с правильной
    # схемой, а старые данные перенесём ниже в "Сессию №1".
    old_ratings_exists = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='ratings'"
    ).fetchone()
    needs_ratings_migration = False
    if old_ratings_exists:
        existing_cols = [r[1] for r in db.execute("PRAGMA table_info(ratings)").fetchall()]
        if 'session_id' not in existing_cols:
            needs_ratings_migration = True
            db.execute("ALTER TABLE ratings RENAME TO ratings_old_pre_sessions")
            db.commit()

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
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    );
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
        last_sync_error_msg TEXT,
        enabled INTEGER DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT UNIQUE NOT NULL,
        title TEXT NOT NULL,
        creator_user_id INTEGER NOT NULL,
        tiers TEXT NOT NULL,
        current_photo_id INTEGER,
        voting_open INTEGER DEFAULT 1,
        auto_advance INTEGER DEFAULT 0,
        is_active INTEGER DEFAULT 1,
        created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS idx_sessions_code ON sessions(code);
    CREATE TABLE IF NOT EXISTS session_photos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER NOT NULL,
        photo_id INTEGER NOT NULL,
        position INTEGER DEFAULT 0,
        UNIQUE(session_id, photo_id)
    );
    CREATE INDEX IF NOT EXISTS idx_session_photos_session ON session_photos(session_id);
    CREATE TABLE IF NOT EXISTS ratings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER NOT NULL,
        photo_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        tier_id TEXT NOT NULL,
        created_at TEXT DEFAULT (datetime('now')),
        UNIQUE(session_id, photo_id, user_id)
    );
    CREATE INDEX IF NOT EXISTS idx_ratings_session ON ratings(session_id);
    CREATE TABLE IF NOT EXISTS published_tierlist (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        source_session_id INTEGER,
        title TEXT,
        snapshot_json TEXT NOT NULL,
        published_by INTEGER,
        published_at TEXT DEFAULT (datetime('now'))
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
    gdrive_watch_cols = [r[1] for r in db.execute("PRAGMA table_info(gdrive_watch)").fetchall()]
    if 'last_sync_error_msg' not in gdrive_watch_cols:
        db.execute("ALTER TABLE gdrive_watch ADD COLUMN last_sync_error_msg TEXT")
    photos_cols = [r[1] for r in db.execute("PRAGMA table_info(photos)").fetchall()]
    if 'phash' not in photos_cols:
        # перцептивный хеш файла (dHash, 64 бита в hex) — для поиска дублей
        # по содержимому картинки, а не по имени файла. Считается лениво по
        # кнопке в "Управление фото", не на каждой загрузке — см. scan_photo_hashes.
        db.execute("ALTER TABLE photos ADD COLUMN phash TEXT")
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

    # ── МИГРАЦИЯ НА СЕССИИ (продолжение) ────────────────────────────────────
    # Если выше была обнаружена старая таблица ratings_old_pre_sessions —
    # переносим все накопленные голоса и текущие глобальные настройки
    # (тиры, текущее фото, открыто/закрыто голосование) в новую "Сессию №1",
    # чтобы старые данные не потерялись при переходе на систему сессий.
    if needs_ratings_migration:
        old_rows = db.execute("SELECT * FROM ratings_old_pre_sessions").fetchall()

        old_tiers_raw = db.execute("SELECT value FROM settings WHERE key='tiers'").fetchone()
        try:
            old_tiers = json.loads(old_tiers_raw["value"]) if old_tiers_raw and old_tiers_raw["value"] else None
        except Exception:
            old_tiers = None
        if not old_tiers:
            old_tiers = DEFAULT_TIERS[:]

        old_current_photo_row = db.execute("SELECT value FROM settings WHERE key='current_photo_id'").fetchone()
        old_current_photo_id = old_current_photo_row["value"] if old_current_photo_row else None
        old_voting_open_row = db.execute("SELECT value FROM settings WHERE key='voting_open'").fetchone()
        old_voting_open = 1 if (not old_voting_open_row or old_voting_open_row["value"] == "1") else 0
        old_auto_advance_row = db.execute("SELECT value FROM settings WHERE key='auto_advance'").fetchone()
        old_auto_advance = 1 if (old_auto_advance_row and old_auto_advance_row["value"] == "1") else 0

        # Создателем "Сессии №1" делаем первого зарегистрированного админа сайта
        # (если такого нет — первого зарегистрированного пользователя вообще).
        owner_row = db.execute(
            "SELECT id FROM users WHERE is_admin=1 AND is_system=0 ORDER BY id LIMIT 1"
        ).fetchone() or db.execute(
            "SELECT id FROM users WHERE is_system=0 ORDER BY id LIMIT 1"
        ).fetchone()

        if owner_row:
            import secrets, string
            _alphabet = string.ascii_lowercase + string.digits
            code = "".join(secrets.choice(_alphabet) for _ in range(8))
            for _ in range(5):
                if not db.execute("SELECT 1 FROM sessions WHERE code=?", (code,)).fetchone():
                    break
                code = "".join(secrets.choice(_alphabet) for _ in range(8))

            cur = db.execute(
                "INSERT INTO sessions (code, title, creator_user_id, tiers, current_photo_id, voting_open, auto_advance) "
                "VALUES (?,?,?,?,?,?,?)",
                (code, "Сессия №1", owner_row["id"], json.dumps(old_tiers, ensure_ascii=False),
                 old_current_photo_id, old_voting_open, old_auto_advance)
            )
            session_id = cur.lastrowid

            # Все существующие фото входят в Сессию №1 в их текущем глобальном порядке.
            photo_rows = db.execute("SELECT id FROM photos ORDER BY position").fetchall()
            db.executemany(
                "INSERT OR IGNORE INTO session_photos (session_id, photo_id, position) VALUES (?,?,?)",
                [(session_id, r["id"], i) for i, r in enumerate(photo_rows)]
            )

            # Переносим все старые голоса как голоса в Сессии №1.
            db.executemany(
                "INSERT OR IGNORE INTO ratings (session_id, photo_id, user_id, tier_id, created_at) VALUES (?,?,?,?,?)",
                [(session_id, r["photo_id"], r["user_id"], r["tier_id"], r["created_at"]) for r in old_rows]
            )
            print(f"[migration] Старые данные перенесены в 'Сессию №1' (id={session_id}, "
                  f"код={code}): {len(photo_rows)} фото, {len(old_rows)} голосов.")
        else:
            print("[migration] Не найдено ни одного пользователя для создания владельца "
                  "'Сессии №1' — старые голоса остались только в ratings_old_pre_sessions.")

        db.execute("DROP TABLE IF EXISTS ratings_old_pre_sessions")

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

def get_tiers_for_session(session_row):
    """Парсит JSON тиров конкретной сессии (sessions.tiers). Сессия передаётся
    как уже полученная Row/dict — чтобы не делать лишний SELECT там, где
    сессия уже на руках."""
    try:
        t = json.loads(session_row["tiers"]) if session_row["tiers"] else None
        if t: return sorted(t, key=lambda x: x.get("order", 0))
    except: pass
    return DEFAULT_TIERS[:]

def get_top_rated_photo_ids_for_user(db, user_id: int) -> set:
    """
    Общая часть для "любимых тегов": собирает id фото, которым пользователь
    поставил оценку из верхней половины тиров — по ВСЕМ его сессиям сразу.
    "Верхняя половина" считается в рамках КАЖДОЙ сессии отдельно (тиры в
    разных сессиях разные, но относительная позиция сопоставима).
    """
    sessions_rated = db.execute(
        "SELECT DISTINCT session_id FROM ratings WHERE user_id=?", (user_id,)
    ).fetchall()
    top_photo_ids = set()
    for srow in sessions_rated:
        sid = srow["session_id"]
        session = db.execute("SELECT tiers FROM sessions WHERE id=?", (sid,)).fetchone()
        if not session:
            continue
        tiers = get_tiers_for_session(session)
        n = len(tiers)
        top_n = max(1, n // 2)
        top_tier_ids = [t["id"] for t in tiers[:top_n]]
        if not top_tier_ids:
            continue
        ph = ",".join("?" * len(top_tier_ids))
        rows = db.execute(
            f"SELECT photo_id FROM ratings WHERE session_id=? AND user_id=? AND tier_id IN ({ph})",
            [sid, user_id] + top_tier_ids
        ).fetchall()
        top_photo_ids.update(r["photo_id"] for r in rows)
    return top_photo_ids

def get_favorite_tag_ids_for_user(db, user_id: int, limit: int = 20) -> list:
    """
    Любимые теги пользователя по ВСЕМ его сессиям сразу — те же принципы,
    что в /api/sessions/{id}/user-stats/{uid} (топ-теги по высоко оценённым
    фото), только не ограничены одной сессией. Используется для приоритизации
    списка тегов в форме создания новой сессии: тегам, которые человек
    регулярно ставил высокую оценку, нужно показываться первыми.
    """
    top_photo_ids = get_top_rated_photo_ids_for_user(db, user_id)
    if not top_photo_ids:
        return []
    pp = ",".join("?" * len(top_photo_ids))
    rows = db.execute(
        f"SELECT tag_id, COUNT(*) as cnt FROM photo_tags "
        f"WHERE photo_id IN ({pp}) AND is_suggestion=0 "
        f"GROUP BY tag_id ORDER BY cnt DESC LIMIT ?",
        list(top_photo_ids) + [limit]
    ).fetchall()
    return [r["tag_id"] for r in rows]

def get_favorite_tags_detailed_for_user(db, user_id: int, limit: int = 12) -> list:
    """
    То же самое, что get_favorite_tag_ids_for_user, но с именем тега,
    счётчиком и категорией (персонаж/общий) — для показа на странице
    профиля пользователя, а не только как список id для внутренней логики.
    """
    top_photo_ids = get_top_rated_photo_ids_for_user(db, user_id)
    if not top_photo_ids:
        return []
    pp = ",".join("?" * len(top_photo_ids))
    rows = db.execute(
        f"SELECT t.id, t.name, t.category, COUNT(*) as cnt "
        f"FROM photo_tags pt JOIN tags t ON t.id = pt.tag_id "
        f"WHERE pt.photo_id IN ({pp}) AND pt.is_suggestion=0 "
        f"GROUP BY t.id ORDER BY cnt DESC LIMIT ?",
        list(top_photo_ids) + [limit]
    ).fetchall()
    return [{"id": r["id"], "name": r["name"], "count": r["cnt"], "is_character": r["category"] == 4} for r in rows]

def get_session_or_404(db, session_id: int):
    row = db.execute("SELECT * FROM sessions WHERE id=? AND is_active=1", (session_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Сессия не найдена или была завершена")
    return row

def generate_session_code() -> str:
    """Короткий код для ссылки на сессию — достаточно энтропии, чтобы не
    угадать чужую сессию перебором, но короче UUID для удобства ввода вручную."""
    import secrets, string
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(8))

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

    Для ОДНОГО фото за раз (ручная загрузка через интерфейс). Для массовой
    обработки (импорт, синхронизация, сканирование папки) используйте
    auto_tag_photos_batch_async — она прогоняет все фото через модель одним
    батчем вместо по одному, что на CPU заметно быстрее суммарно.
    """
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, auto_tag_photo, photo_id, photo_path)
    except Exception as e:
        print(f"[auto_tag_photo_async] Пропускаем автотегирование фото {photo_id}: {e}")

def _auto_tag_photos_batch(new_photos: list):
    """
    Синхронная часть батчевого автотегирования — прогоняет СРАЗУ ВСЕ
    переданные фото через WD14 одним проходом (см. wd14_tagger.predict_tag_ids_batch),
    а не по одному, и пишет все найденные предложения тегов в БД одним
    заходом. Выполняется в отдельном треде (см. auto_tag_photos_batch_async),
    как и одиночная версия auto_tag_photo.
    """
    if not new_photos:
        return
    photo_ids = [pid for pid, _ in new_photos]
    paths = [path for _, path in new_photos]
    tag_ids_per_photo = wd14_tagger.predict_tag_ids_batch(paths)

    db = get_db()
    try:
        tagger_id = _get_auto_tagger_user_id(db)
        if tagger_id is None:
            return
        # какие из предложенных моделью tag_id реально есть в таблице tags —
        # проверяем один раз по объединению всех id со всех фото сразу,
        # а не отдельным запросом на каждое фото.
        all_ids = sorted(set(tid for tags in tag_ids_per_photo for tid in tags))
        if not all_ids:
            return
        existing = set(
            r["id"] for r in db.execute(
                "SELECT id FROM tags WHERE id IN ({})".format(",".join("?" * len(all_ids))),
                all_ids
            ).fetchall()
        )
        rows = [
            (photo_id, tagger_id, tid)
            for photo_id, tags in zip(photo_ids, tag_ids_per_photo)
            for tid in tags if tid in existing
        ]
        if rows:
            db.executemany(
                "INSERT OR IGNORE INTO photo_tags (photo_id, user_id, tag_id, is_suggestion) VALUES (?,?,?,1)",
                rows
            )
            db.commit()
    finally:
        db.close()

async def auto_tag_photos_batch_async(new_photos: list):
    """
    Асинхронная обёртка над _auto_tag_photos_batch — используется во всех
    точках массового добавления фото (импорт с Яндекс/Google Диска,
    авто-синхронизация, сканирование папки на сервере) вместо цикла с
    отдельным auto_tag_photo_async на каждое фото.
    """
    if not new_photos:
        return
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, _auto_tag_photos_batch, new_photos)
    except Exception as e:
        print(f"[auto_tag_photos_batch_async] Пропускаем автотегирование партии из {len(new_photos)} фото: {e}")

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

# ── SESSIONS ────────────────────────────────────────────────────────────────────
# Любой зарегистрированный пользователь может создать сессию оценивания —
# внутри неё он становится "админом" (создателем) этой конкретной сессии,
# без глобальных прав на сайте. Другие подключаются по коду/ссылке или
# выбирают сессию из общего списка активных. Голоса, тиры и текущее фото
# полностью изолированы между сессиями; теги на фото остаются общими.

@app.get("/api/sessions")
def list_sessions(user=Depends(current_user)):
    """Список активных сессий — для экрана 'Активные сессии', куда можно
    зайти без кода по клику."""
    db = get_db()
    rows = db.execute("""
        SELECT s.id, s.code, s.title, s.created_at, s.voting_open,
               u.username as creator_username,
               (SELECT COUNT(*) FROM session_photos sp WHERE sp.session_id=s.id) as photo_count,
               (SELECT COUNT(DISTINCT user_id) FROM ratings r WHERE r.session_id=s.id) as participant_count
        FROM sessions s
        JOIN users u ON u.id = s.creator_user_id
        WHERE s.is_active = 1
        ORDER BY s.created_at DESC
    """).fetchall()
    db.close()
    return [dict(r) for r in rows]

def get_published_photo_ids(db) -> set:
    """
    Множество id фото, которые входят в ТЕКУЩИЙ опубликованный на главной
    странице снимок тир-листа (если ничего не опубликовано — пустое
    множество). Используется для альбома "Новые фото" при создании сессии —
    чтобы выбрать фото, которых ещё нет в опубликованном тир-листе.
    """
    row = db.execute("SELECT snapshot_json FROM published_tierlist WHERE id=1").fetchone()
    if not row:
        return set()
    snapshot = json.loads(row["snapshot_json"])
    ids = set()
    for tier_photos in snapshot.get("tiers", {}).values():
        for p in tier_photos:
            ids.add(p["id"])
    return ids

@app.post("/api/sessions")
def create_session(body: dict, user=Depends(current_user)):
    """
    Создаёт новую сессию оценивания.
    body:
      title: str — название сессии
      tiers: list — тиры (как в /api/sessions/{id}/tiers), минимум 2
      photo_filter: "all" | "tag" | "new" — какая подборка фото войдёт в сессию
                    ("new" — фото, которых ещё нет в опубликованном на главной
                    тир-листе)
      tag_ids: list[int] (если photo_filter == "tag") — id тегов для фильтра;
               фото попадает в сессию, только если у него есть ВСЕ
               перечисленные теги (логика "И", а не "ИЛИ")
      include_suggestions: bool — включать ли фото, у которых тег есть
                            только как неподтверждённое предложение AI
      shuffle: bool — перемешать порядок фото в сессии (по умолчанию — да)
    """
    title = (body.get("title") or "").strip()[:80] or "Без названия"
    tiers = body.get("tiers") or DEFAULT_TIERS[:]
    if len(tiers) < 2:
        raise HTTPException(400, "Минимум 2 тира")
    if len(tiers) > 10:
        raise HTTPException(400, "Максимум 10 тиров")
    for i, t in enumerate(tiers):
        if not (t.get("label") or "").strip():
            raise HTTPException(400, f"Тир {i+1}: пустое название")
        t["id"] = t.get("id") or str(uuid.uuid4())[:8]
        t["order"] = i
        t["label"] = t["label"].strip()[:30]
        t["color"] = t.get("color", "#888")

    photo_filter = body.get("photo_filter", "all")
    tag_ids = [int(x) for x in (body.get("tag_ids") or []) if x is not None]
    include_suggestions = bool(body.get("include_suggestions"))
    do_shuffle = body.get("shuffle", True)

    db = get_db()

    if photo_filter == "tag" and tag_ids:
        suggestion_clause = "" if include_suggestions else "AND is_suggestion=0"
        placeholders = ",".join("?" * len(tag_ids))
        rows = db.execute(f"""
            SELECT photo_id FROM photo_tags
            WHERE tag_id IN ({placeholders}) {suggestion_clause}
            GROUP BY photo_id
            HAVING COUNT(DISTINCT tag_id) = ?
        """, (*tag_ids, len(tag_ids))).fetchall()
        photo_ids = [r["photo_id"] for r in rows]
    elif photo_filter == "new":
        published_ids = get_published_photo_ids(db)
        rows = db.execute("SELECT id FROM photos ORDER BY position").fetchall()
        photo_ids = [r["id"] for r in rows if r["id"] not in published_ids]
    else:
        rows = db.execute("SELECT id FROM photos ORDER BY position").fetchall()
        photo_ids = [r["id"] for r in rows]

    if not photo_ids:
        db.close()
        raise HTTPException(400, "По выбранному фильтру не найдено ни одного фото")

    if do_shuffle:
        import random
        random.shuffle(photo_ids)

    code = generate_session_code()
    # на случай редчайшей коллизии кода — перегенерируем пару раз
    for _ in range(5):
        if not db.execute("SELECT 1 FROM sessions WHERE code=?", (code,)).fetchone():
            break
        code = generate_session_code()

    cur = db.execute(
        "INSERT INTO sessions (code, title, creator_user_id, tiers, voting_open) VALUES (?,?,?,?,1)",
        (code, title, user["id"], json.dumps(tiers, ensure_ascii=False))
    )
    session_id = cur.lastrowid

    db.executemany(
        "INSERT INTO session_photos (session_id, photo_id, position) VALUES (?,?,?)",
        [(session_id, pid, i) for i, pid in enumerate(photo_ids)]
    )
    first_photo_id = photo_ids[0]
    db.execute("UPDATE sessions SET current_photo_id=? WHERE id=?", (first_photo_id, session_id))
    db.commit()
    db.close()

    return {"id": session_id, "code": code, "title": title, "photo_count": len(photo_ids)}

@app.get("/api/sessions/{session_id}")
def get_session_info(session_id: int, user=Depends(current_user)):
    db = get_db()
    session = get_session_or_404(db, session_id)
    photo_count = db.execute(
        "SELECT COUNT(*) FROM session_photos WHERE session_id=?", (session_id,)
    ).fetchone()[0]
    participant_count = db.execute(
        "SELECT COUNT(DISTINCT user_id) FROM ratings WHERE session_id=?", (session_id,)
    ).fetchone()[0]
    db.close()
    d = dict(session)
    d["tiers"] = get_tiers_for_session(session)
    d["photo_count"] = photo_count
    d["participant_count"] = participant_count
    d["is_owner"] = session["creator_user_id"] == user["id"]
    return d

@app.get("/api/sessions/by-code/{code}")
def get_session_by_code(code: str, user=Depends(current_user)):
    """Резолвит код сессии (из ссылки или введённый вручную) в session_id —
    для перехода на экран голосования по короткой ссылке/коду."""
    db = get_db()
    row = db.execute("SELECT id FROM sessions WHERE code=? AND is_active=1", (code.strip().lower(),)).fetchone()
    db.close()
    if not row:
        raise HTTPException(404, "Сессия с таким кодом не найдена или завершена")
    return {"session_id": row["id"]}

@app.post("/api/sessions/{session_id}/end")
def end_session(session_id: int, user=Depends(current_user)):
    """Завершает сессию (мягко — is_active=0, данные не удаляются). Доступно
    только создателю сессии."""
    db = get_db()
    session = get_session_or_404(db, session_id)
    if session["creator_user_id"] != user["id"] and not user["is_admin"]:
        db.close()
        raise HTTPException(403, "Завершить сессию может только её создатель")
    db.execute("UPDATE sessions SET is_active=0 WHERE id=?", (session_id,))
    db.commit()
    db.close()
    return {"ok": True}


# ── PUBLIC GALLERY ────────────────────────────────────────────────────────────
# Просмотр всех фото вне контекста сессий — без возможности голосовать или
# ставить теги, только смотреть и фильтровать. Плюс витрина "опубликованного"
# тир-листа — замороженный снимок тир-листа какой-то сессии, который
# сайт-админ явно публикует на главную (см. ниже).

@app.get("/api/gallery/photos")
def gallery_photos(tag_ids: Optional[str] = None, include_suggestions: bool = False,
                    page: int = 1, page_size: int = 30, user=Depends(current_user)):
    """
    Список фото для галереи, постранично ("выпусками") — чтобы не листать
    тысячи фото одной бесконечной лентой. tag_ids — необязательный фильтр,
    список id тегов через запятую (например "3,7,12"); если задано несколько —
    фото должно иметь ВСЕ перечисленные теги (логика "И", а не "ИЛИ").
    include_suggestions согласуется с тем же переключателем, что в
    /api/gallery/tags — если включён, фильтр учитывает и неподтверждённые
    AI-предложения (иначе выбор AI-тега в фильтре вернул бы пустой список,
    хотя сам тег показывается).
    """
    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    offset = (page - 1) * page_size
    db = get_db()
    ids = [int(x) for x in tag_ids.split(",") if x.strip().isdigit()] if tag_ids else []
    if ids:
        suggestion_clause = "" if include_suggestions else "AND is_suggestion = 0"
        placeholders = ",".join("?" * len(ids))
        match_subquery = f"""
            SELECT photo_id FROM photo_tags
            WHERE tag_id IN ({placeholders}) {suggestion_clause}
            GROUP BY photo_id
            HAVING COUNT(DISTINCT tag_id) = ?
        """
        total = db.execute(f"SELECT COUNT(*) as c FROM ({match_subquery})", (*ids, len(ids))).fetchone()["c"]
        rows = db.execute(f"""
            SELECT id, filename, original_name, position FROM photos
            WHERE id IN ({match_subquery})
            ORDER BY position
            LIMIT ? OFFSET ?
        """, (*ids, len(ids), page_size, offset)).fetchall()
    else:
        total = db.execute("SELECT COUNT(*) as c FROM photos").fetchone()["c"]
        rows = db.execute(
            "SELECT id, filename, original_name FROM photos ORDER BY position LIMIT ? OFFSET ?",
            (page_size, offset)
        ).fetchall()
    db.close()
    total_pages = max(1, (total + page_size - 1) // page_size)
    return {
        "photos": [dict(r) for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
    }

@app.get("/api/gallery/tags")
def gallery_tags(include_suggestions: bool = False, user=Depends(current_user)):
    """
    Полный список тегов для фильтра в галерее, с количеством фото у каждого.
    В отличие от /api/tierlist/tags (витрина топ-10 для формы создания сессии),
    здесь нужен ПОЛНЫЙ список без лимита, отсортированный по алфавиту — это
    фильтр-справочник, а не подборка карточек.

    include_suggestions=False (по умолчанию): только теги с хотя бы одним
        ПОДТВЕРЖДЁННЫМ фото.
    include_suggestions=True: учитываются также теги, которые есть только
        как неподтверждённое AI-предложение — отдельный переключатель в
        галерее, а не смешивание с обычными тегами в одном списке.
    """
    db = get_db()
    suggestion_clause = "" if include_suggestions else "AND pt.is_suggestion = 0"
    rows = db.execute(f"""
        SELECT t.id, t.name, t.category, COUNT(DISTINCT pt.photo_id) as photo_count,
               MAX(CASE WHEN pt.is_suggestion = 0 THEN 1 ELSE 0 END) as has_confirmed
        FROM tags t JOIN photo_tags pt ON pt.tag_id = t.id
        WHERE 1=1 {suggestion_clause}
        GROUP BY t.id
        HAVING photo_count > 0
        ORDER BY t.name
    """).fetchall()
    db.close()
    return [{"id": r["id"], "name": r["name"], "photo_count": r["photo_count"],
             "has_confirmed": bool(r["has_confirmed"]), "is_character": r["category"] == 4} for r in rows]

@app.get("/api/gallery/published-tierlist")
def get_published_tierlist(user=Depends(current_user)):
    """Текущий опубликованный на главной снимок тир-листа, если есть."""
    db = get_db()
    row = db.execute("""
        SELECT pt.*, u.username as published_by_username
        FROM published_tierlist pt LEFT JOIN users u ON u.id = pt.published_by
        WHERE pt.id = 1
    """).fetchone()
    db.close()
    if not row:
        return None
    snapshot = json.loads(row["snapshot_json"])
    return {
        "title": row["title"],
        "published_by": row["published_by_username"],
        "published_at": row["published_at"],
        "source_session_id": row["source_session_id"],
        **snapshot,
    }

@app.get("/api/admin/publishable-sessions")
def list_publishable_sessions(user=Depends(admin_user)):
    """
    Список ВСЕХ сессий (активных и завершённых) с превью тир-листа — для
    экрана выбора "какую сессию опубликовать на главную". Доступно только
    сайт-админу.
    """
    db = get_db()
    rows = db.execute("""
        SELECT s.id, s.title, s.is_active, s.created_at,
               u.username as creator_username,
               (SELECT COUNT(*) FROM ratings r WHERE r.session_id=s.id) as vote_count,
               (SELECT COUNT(DISTINCT r.photo_id) FROM ratings r WHERE r.session_id=s.id) as rated_photo_count
        FROM sessions s JOIN users u ON u.id = s.creator_user_id
        ORDER BY s.created_at DESC
    """).fetchall()
    db.close()
    return [dict(r) for r in rows]

@app.post("/api/admin/publish-tierlist/{session_id}")
def publish_tierlist_snapshot(session_id: int, user=Depends(admin_user)):
    """
    Публикует ЗАМОРОЖЕННЫЙ снимок тир-листа выбранной сессии на главную
    страницу (галерею). Снимок не обновляется сам, даже если в исходной
    сессии продолжат голосовать — чтобы повторно опубликовать актуальную
    версию, нужно вызвать этот эндпоинт снова. Заменяет предыдущую публикацию,
    если она была (на главной всегда не более одного опубликованного тир-листа).
    """
    db = get_db()
    session = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not session:
        db.close()
        raise HTTPException(404, "Сессия не найдена")

    snapshot = compute_session_tierlist(db, session_id, session_row=session)
    db.execute("""
        INSERT INTO published_tierlist (id, source_session_id, title, snapshot_json, published_by, published_at)
        VALUES (1, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            source_session_id=excluded.source_session_id,
            title=excluded.title,
            snapshot_json=excluded.snapshot_json,
            published_by=excluded.published_by,
            published_at=excluded.published_at
    """, (session_id, session["title"], json.dumps(snapshot, ensure_ascii=False),
          user["id"], datetime.utcnow().isoformat()))
    db.commit()
    db.close()
    return {"ok": True}

@app.delete("/api/admin/publish-tierlist")
def unpublish_tierlist_snapshot(user=Depends(admin_user)):
    """Снимает текущий опубликованный тир-лист с главной страницы."""
    db = get_db()
    db.execute("DELETE FROM published_tierlist WHERE id=1")
    db.commit()
    db.close()
    return {"ok": True}


# ── TIERS CONFIG (per session) ─────────────────────────────────────────────────

@app.get("/api/sessions/{session_id}/tiers")
def api_get_session_tiers(session_id: int, user=Depends(current_user)):
    db = get_db()
    session = get_session_or_404(db, session_id)
    t = get_tiers_for_session(session)
    db.close()
    return t

@app.post("/api/sessions/{session_id}/tiers")
def api_set_session_tiers(session_id: int, body: dict, user=Depends(current_user)):
    db = get_db()
    session = get_session_or_404(db, session_id)
    if session["creator_user_id"] != user["id"] and not user["is_admin"]:
        db.close()
        raise HTTPException(403, "Менять тиры может только создатель сессии")

    tiers = body.get("tiers", [])
    if not tiers or len(tiers) < 2:
        db.close()
        raise HTTPException(400, "Минимум 2 тира")
    if len(tiers) > 10:
        db.close()
        raise HTTPException(400, "Максимум 10 тиров")
    ids = set()
    for i, t in enumerate(tiers):
        if not t.get("label", "").strip():
            db.close()
            raise HTTPException(400, f"Тир {i+1}: пустое название")
        t["id"] = t.get("id") or str(uuid.uuid4())[:8]
        t["order"] = i
        t["label"] = t["label"].strip()[:30]
        t["color"] = t.get("color", "#888")
        ids.add(t["id"])
    db.execute("UPDATE sessions SET tiers=? WHERE id=?",
               (json.dumps(tiers, ensure_ascii=False), session_id))
    db.commit()
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
    await auto_tag_photos_batch_async(new_photos)

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
        await auto_tag_photos_batch_async(new_photos)

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
    await auto_tag_photos_batch_async(new_photos)

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
                "UPDATE gdrive_watch SET last_sync_at=?, last_sync_added=0, last_sync_errors=-1, last_sync_error_msg=? WHERE id=1",
                (datetime.utcnow().isoformat(), str(e))
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
            "UPDATE gdrive_watch SET last_sync_at=?, last_sync_added=?, last_sync_errors=?, last_sync_error_msg=NULL WHERE id=1",
            (datetime.utcnow().isoformat(), added, errors)
        )
        db.commit(); db.close()

        # Автотегирование WD14 — при ошибке/недоступности модели просто пропускаем,
        # фото всё равно уже синхронизированы выше.
        await auto_tag_photos_batch_async(new_photos)

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
    await auto_tag_photos_batch_async(new_photos)

    return {"added": added}

@app.post("/api/admin/photos/scan-folder")
async def scan_photos_folder(user=Depends(admin_user)):
    """
    Сканирует папку /app/photos и регистрирует в БД файлы, которых там ещё
    нет — для случая, когда фото скопировали на сервер напрямую (scp, docker cp
    и т.п.), в обход загрузки через интерфейс. В отличие от такого копирования
    "вручную", здесь позиция новых фото корректно продолжает уже существующие
    (а не начинается заново с нуля), и для каждого нового файла запускается
    то же автотегирование WD14, что и при обычной загрузке. Запускается
    только по кнопке в "Управление фото", не автоматически при каждом запуске
    сервера — сканирование тысяч файлов может занять время.
    """
    db = get_db()
    existing = {r["filename"] for r in db.execute("SELECT filename FROM photos").fetchall()}
    try:
        all_files = sorted(os.listdir(PHOTOS_DIR))
    except FileNotFoundError:
        db.close()
        return {"added": 0, "skipped": 0, "total_found": 0}

    candidates = [f for f in all_files
                  if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
                  and f not in existing]

    new_photos = []  # (photo_id, full_path) — для автотегирования после вставки
    skipped = 0
    for f in candidates:
        path = os.path.join(PHOTOS_DIR, f)
        try:
            img = PILImage.open(path); img.load()
        except Exception:
            skipped += 1
            continue
        count = db.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
        cur = db.execute("INSERT INTO photos (filename, original_name, position) VALUES (?,?,?)",
                          (f, f, count))
        new_photos.append((cur.lastrowid, path))
    db.commit(); db.close()

    await auto_tag_photos_batch_async(new_photos)

    return {"added": len(new_photos), "skipped": skipped, "total_found": len(candidates)}

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

def generate_thumbnail(filename: str):
    """
    Создаёт уменьшенную копию фото (до THUMB_MAX_SIZE px по длинной стороне,
    JPEG) в THUMBS_DIR — считается лениво, по первому запросу превью,
    результат кэшируется на диске навсегда (пока исходный файл не удалят).
    Раздельный от оригинала JPEG нужен, чтобы сетка/карточки не тянули
    полноразмерные файлы там, где реально показывается миниатюра 140-320px.
    Возвращает путь к готовому превью или None, если файл не читается как
    изображение.
    """
    thumb_path = os.path.join(THUMBS_DIR, filename + ".jpg")
    if os.path.exists(thumb_path):
        return thumb_path
    src_path = os.path.join(PHOTOS_DIR, filename)
    try:
        img = PILImage.open(src_path)
        img = img.convert("RGB")
        img.thumbnail((THUMB_MAX_SIZE, THUMB_MAX_SIZE), PILImage.LANCZOS)
        img.save(thumb_path, "JPEG", quality=80)
        return thumb_path
    except Exception:
        return None

def compute_dhash(path: str, hash_size: int = 8):
    """
    Перцептивный хеш изображения (difference hash) — в отличие от простого
    хеша файла (md5/sha), устойчив к пересохранению в другом качестве/формате:
    два визуально одинаковых фото дадут одинаковый (или очень близкий) хеш,
    даже если байты файлов разные. Возвращает 16-символьную hex-строку
    (64 бита) или None, если файл не удалось прочитать как изображение.
    """
    try:
        from PIL import Image
        img = Image.open(path).convert("L").resize((hash_size + 1, hash_size), Image.LANCZOS)
        pixels = list(img.getdata())
        bits = 0
        w = hash_size + 1
        for row in range(hash_size):
            for col in range(hash_size):
                left = pixels[row * w + col]
                right = pixels[row * w + col + 1]
                bits = (bits << 1) | (1 if left > right else 0)
        return format(bits, "016x")
    except Exception:
        return None

def ensure_photo_hashes(db) -> int:
    """
    Считает phash для всех фото, у которых он ещё не посчитан (NULL).
    Возвращает число реально обработанных файлов. Вызывается только по
    явному нажатию кнопки в "Управление фото" — не автоматически при
    каждой загрузке фото, чтобы не тормозить обычную загрузку/импорт.
    """
    rows = db.execute("SELECT id, filename FROM photos WHERE phash IS NULL").fetchall()
    computed = 0
    for r in rows:
        h = compute_dhash(os.path.join(PHOTOS_DIR, r["filename"]))
        if h:
            db.execute("UPDATE photos SET phash=? WHERE id=?", (h, r["id"]))
            computed += 1
        else:
            # не удалось прочитать как изображение — ставим заведомо
            # неповторимый маркер, чтобы не пересчитывать его снова и снова
            # и чтобы он точно не попал ни в одну группу дублей
            db.execute("UPDATE photos SET phash=? WHERE id=?", (f"ERR:{r['id']}", r["id"]))
    db.commit()
    return computed

def merge_photo_groups(db, id_groups: list):
    """
    Общая логика объединения дублей: принимает список групп id фото
    (в каждой группе оставляет самую раннюю запись — минимальный id — и
    переносит на неё голоса/теги/присутствие в сессиях с удаляемых копий,
    не теряя чужие оценки, если разные люди успели проголосовать на разных
    копиях одного и того же фото). Используется и для дублей по имени файла,
    и для дублей по содержимому картинки — сам механизм слияния одинаков,
    отличается только то, как эти группы были найдены.
    """
    merged_groups = 0
    removed_photos = 0
    for ids in id_groups:
        ids = sorted(ids)
        keep_id = ids[0]
        remove_ids = ids[1:]

        for rid in remove_ids:
            for r in db.execute("SELECT session_id, user_id, tier_id FROM ratings WHERE photo_id=?", (rid,)).fetchall():
                db.execute("INSERT OR IGNORE INTO ratings (session_id, photo_id, user_id, tier_id) VALUES (?,?,?,?)",
                           (r["session_id"], keep_id, r["user_id"], r["tier_id"]))
            for t in db.execute("SELECT user_id, tag_id, is_suggestion FROM photo_tags WHERE photo_id=?", (rid,)).fetchall():
                db.execute("INSERT OR IGNORE INTO photo_tags (photo_id, user_id, tag_id, is_suggestion) VALUES (?,?,?,?)",
                           (keep_id, t["user_id"], t["tag_id"], t["is_suggestion"]))
            for sp in db.execute("SELECT session_id FROM session_photos WHERE photo_id=?", (rid,)).fetchall():
                db.execute("INSERT OR IGNORE INTO session_photos (session_id, photo_id, position) "
                           "SELECT session_id, ?, position FROM session_photos WHERE session_id=? AND photo_id=?",
                           (keep_id, sp["session_id"], rid))
                db.execute("DELETE FROM session_photos WHERE session_id=? AND photo_id=?", (sp["session_id"], rid))

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
    return merged_groups, removed_photos

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
        SELECT GROUP_CONCAT(id) as ids
        FROM photos GROUP BY original_name HAVING COUNT(*) > 1
    """).fetchall()
    id_groups = [[int(x) for x in g["ids"].split(",")] for g in groups]
    merged_groups, removed_photos = merge_photo_groups(db, id_groups)
    db.close()
    return {"merged_groups": merged_groups, "removed_photos": removed_photos}

@app.post("/api/admin/duplicate-photos/scan-by-image")
def scan_duplicate_photos_by_image(user=Depends(admin_user)):
    """
    Поиск дублей ПО СОДЕРЖИМОМУ картинки (перцептивный хеш), а не по имени
    файла — находит одно и то же изображение, загруженное под разными
    именами. Запускается только по нажатию кнопки в "Управление фото"
    (не автоматически при каждом открытии экрана, в отличие от проверки
    по имени файла) — вычисление хешей может занять время на первом прогоне
    для большого количества фото, дальше значения кэшируются в БД.
    """
    db = get_db()
    scanned = ensure_photo_hashes(db)
    groups = db.execute("""
        SELECT phash, GROUP_CONCAT(id) as ids, GROUP_CONCAT(filename) as filenames, COUNT(*) as cnt
        FROM photos
        WHERE phash IS NOT NULL AND phash NOT LIKE 'ERR:%'
        GROUP BY phash HAVING cnt > 1
        ORDER BY cnt DESC
    """).fetchall()
    db.close()
    return {
        "scanned": scanned,
        "groups": len(groups),
        "extra_photos": sum(g["cnt"] - 1 for g in groups),
        "details": [{"ids": g["ids"], "count": g["cnt"],
                     "preview_filename": g["filenames"].split(",")[0]} for g in groups],
    }

@app.post("/api/admin/duplicate-photos/merge-by-image")
def merge_duplicate_photos_by_image(user=Depends(admin_user)):
    """Объединяет дубли, найденные scan_duplicate_photos_by_image (по содержимому картинки)."""
    db = get_db()
    ensure_photo_hashes(db)
    groups = db.execute("""
        SELECT GROUP_CONCAT(id) as ids
        FROM photos
        WHERE phash IS NOT NULL AND phash NOT LIKE 'ERR:%'
        GROUP BY phash HAVING COUNT(*) > 1
    """).fetchall()
    id_groups = [[int(x) for x in g["ids"].split(",")] for g in groups]
    merged_groups, removed_photos = merge_photo_groups(db, id_groups)
    db.close()
    return {"merged_groups": merged_groups, "removed_photos": removed_photos}

# ── VOTING ────────────────────────────────────────────────────────────────────

@app.get("/api/sessions/{session_id}/current-photo")
def current_photo(session_id: int, user=Depends(current_user)):
    db = get_db()
    session = get_session_or_404(db, session_id)
    photo_id = session["current_photo_id"]
    voting_open = bool(session["voting_open"])
    tiers = get_tiers_for_session(session)
    if not photo_id:
        db.close()
        return {"photo": None, "voting_open": voting_open, "tiers": tiers}
    row = db.execute("""
        SELECT p.*, COUNT(r.id) as vote_count
        FROM photos p LEFT JOIN ratings r ON r.photo_id=p.id AND r.session_id=?
        WHERE p.id=? GROUP BY p.id
    """, (session_id, photo_id)).fetchone()
    user_rating = db.execute(
        "SELECT tier_id FROM ratings WHERE session_id=? AND photo_id=? AND user_id=?",
        (session_id, photo_id, user["id"])).fetchone()
    # per-tier counts
    tier_counts = {}
    for r in db.execute(
        "SELECT tier_id, COUNT(*) as cnt FROM ratings WHERE session_id=? AND photo_id=? GROUP BY tier_id",
        (session_id, photo_id)).fetchall():
        tier_counts[r["tier_id"]] = r["cnt"]
    # "Сколько людей сейчас в сессии" — считаем по живым WS-подключениям к
    # этой конкретной сессии (исключая создателя как "админа сессии" не делаем —
    # он тоже участник, как и в исходном глобальном режиме админ тоже голосовал).
    total_users = len(ws_manager.online_user_ids(session_id))
    auto_advance = bool(session["auto_advance"])

    # ── следующее/предыдущее фото в порядке ЭТОЙ сессии (для предзагрузки в кэш) ──
    next_row = db.execute("""
        SELECT p.filename FROM session_photos sp JOIN photos p ON p.id = sp.photo_id
        WHERE sp.session_id=? AND sp.position > (
            SELECT position FROM session_photos WHERE session_id=? AND photo_id=?
        ) ORDER BY sp.position LIMIT 1
    """, (session_id, session_id, photo_id)).fetchone()
    prev_row = db.execute("""
        SELECT p.filename FROM session_photos sp JOIN photos p ON p.id = sp.photo_id
        WHERE sp.session_id=? AND sp.position < (
            SELECT position FROM session_photos WHERE session_id=? AND photo_id=?
        ) ORDER BY sp.position DESC LIMIT 1
    """, (session_id, session_id, photo_id)).fetchone()

    db.close()
    return {
        "photo": dict(row) if row else None,
        "user_tier": user_rating["tier_id"] if user_rating else None,
        "voting_open": voting_open,
        "total_users": total_users,
        "tiers": tiers,
        "tier_counts": tier_counts,
        "auto_advance": auto_advance,
        "next_filename": next_row["filename"] if next_row else None,
        "prev_filename": prev_row["filename"] if prev_row else None,
    }

@app.post("/api/sessions/{session_id}/rate")
async def rate_photo(session_id: int, photo_id: int = Form(...), tier_id: str = Form(...), user=Depends(current_user)):
    db = get_db()
    session = get_session_or_404(db, session_id)
    tiers = get_tiers_for_session(session)
    valid_ids = {t["id"] for t in tiers}
    if tier_id not in valid_ids:
        db.close(); raise HTTPException(400, "Invalid tier")
    if not session["voting_open"]:
        db.close(); raise HTTPException(403, "Voting is closed")
    if photo_id != session["current_photo_id"]:
        db.close(); raise HTTPException(400, "Not the current photo")
    db.execute("""
        INSERT INTO ratings (session_id, photo_id, user_id, tier_id) VALUES (?,?,?,?)
        ON CONFLICT(session_id, photo_id, user_id) DO UPDATE SET tier_id=excluded.tier_id
    """, (session_id, photo_id, user["id"], tier_id))
    db.commit()

    # ── AUTO-ADVANCE CHECK ────────────────────────────────────────────────────
    auto_advance = bool(session["auto_advance"])
    if auto_advance:
        online_ids = ws_manager.online_user_ids(session_id)
        # Только не-админы считаются «участниками» (создатель сессии тоже может
        # голосовать как обычный участник — это не отличается от глобального
        # режима, где обычный сайт-админ тоже мог голосовать)
        non_admin_online = set(
            r["id"] for r in db.execute(
                "SELECT id FROM users WHERE is_system=0 AND id IN ({})".format(
                    ",".join("?" * len(online_ids)) if online_ids else "NULL"
                ), tuple(online_ids)
            ).fetchall()
        ) if online_ids else set()

        if non_admin_online:
            voted_ids = set(
                r["user_id"] for r in db.execute(
                    "SELECT user_id FROM ratings WHERE session_id=? AND photo_id=?", (session_id, photo_id)
                ).fetchall()
            )
            all_voted = non_admin_online.issubset(voted_ids)
            if all_voted:
                # Переходим к следующему фото в порядке этой сессии
                nxt = db.execute("""
                    SELECT sp.photo_id as id FROM session_photos sp
                    WHERE sp.session_id=? AND sp.position > (
                        SELECT position FROM session_photos WHERE session_id=? AND photo_id=?
                    ) ORDER BY sp.position LIMIT 1
                """, (session_id, session_id, photo_id)).fetchone()
                if nxt:
                    db.execute("UPDATE sessions SET current_photo_id=?, voting_open=1 WHERE id=?",
                               (nxt["id"], session_id))
                    db.commit()
                    db.close()
                    await ws_manager.broadcast(
                        {"type": "photo_change", "photo_id": nxt["id"], "auto": True, "direction": "next"},
                        session_id
                    )
                    return {"ok": True, "auto_advanced": True}
                else:
                    # Все фото просмотрены
                    db.close()
                    await ws_manager.broadcast({"type": "all_done"}, session_id)
                    return {"ok": True, "auto_advanced": True}

    db.close()
    await ws_manager.broadcast({"type": "vote_update", "photo_id": photo_id}, session_id)
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
        SELECT t.id as tag_id, t.name as tag_name, t.category, u.username, pt.user_id, pt.is_suggestion
        FROM photo_tags pt
        JOIN tags t ON t.id = pt.tag_id
        JOIN users u ON u.id = pt.user_id
        WHERE pt.photo_id = ?
        ORDER BY t.name
    """, (photo_id,)).fetchall()
    db.close()
    mine = [{"id": r["tag_id"], "name": r["tag_name"], "is_character": r["category"] == 4}
            for r in rows if r["user_id"] == user["id"] and not r["is_suggestion"]]
    all_tags = [{"tag_id": r["tag_id"], "tag_name": r["tag_name"], "username": r["username"],
                 "is_suggestion": bool(r["is_suggestion"]), "is_character": r["category"] == 4} for r in rows]
    return {"mine": mine, "all": all_tags}

@app.post("/api/photo-tags/{photo_id}/add")
async def add_photo_tag(photo_id: int, tag_name: str = Form(...), user=Depends(current_user)):
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
    await ws_manager.broadcast({"type": "tags_updated", "photo_id": photo_id})
    return {"ok": True}

@app.post("/api/photo-tags/{photo_id}/confirm")
async def confirm_suggested_tag(photo_id: int, tag_id: int = Form(...), user=Depends(current_user)):
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
    await ws_manager.broadcast({"type": "tags_updated", "photo_id": photo_id})
    return {"ok": True}

@app.post("/api/photo-tags/{photo_id}/reject")
async def reject_suggested_tag(photo_id: int, tag_id: int = Form(...), user=Depends(current_user)):
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
    await ws_manager.broadcast({"type": "tags_updated", "photo_id": photo_id})
    return {"ok": True}

@app.delete("/api/photo-tags/{photo_id}/{tag_id}")
async def remove_photo_tag(photo_id: int, tag_id: int, user=Depends(current_user)):
    db = get_db()
    db.execute("DELETE FROM photo_tags WHERE photo_id=? AND user_id=? AND tag_id=?",
               (photo_id, user["id"], tag_id))
    db.commit(); db.close()
    await ws_manager.broadcast({"type": "tags_updated", "photo_id": photo_id})
    return {"ok": True}

@app.get("/api/sessions/{session_id}/photo-votes/{photo_id}")
def photo_votes(session_id: int, photo_id: int, user=Depends(current_user)):
    db = get_db()
    session = get_session_or_404(db, session_id)
    tiers = get_tiers_for_session(session)
    tier_map = {t["id"]: t for t in tiers}
    rows = db.execute("""
        SELECT u.username, r.tier_id, r.created_at
        FROM ratings r JOIN users u ON u.id = r.user_id
        WHERE r.session_id = ? AND r.photo_id = ?
        ORDER BY r.created_at DESC
    """, (session_id, photo_id)).fetchall()
    db.close()
    return [{"username": r["username"], "tier_id": r["tier_id"],
             "tier_label": tier_map.get(r["tier_id"], {}).get("label", r["tier_id"]),
             "tier_color": tier_map.get(r["tier_id"], {}).get("color", "#888"),
             "created_at": r["created_at"]} for r in rows]


@app.get("/api/sessions/{session_id}/photo-detail/{photo_id}")
def photo_detail(session_id: int, photo_id: int, user=Depends(current_user)):
    db = get_db()
    session = get_session_or_404(db, session_id)
    tiers = get_tiers_for_session(session)
    tier_map = {t["id"]: t for t in tiers}

    # photo info
    photo = db.execute("SELECT * FROM photos WHERE id=?", (photo_id,)).fetchone()
    if not photo:
        db.close(); raise HTTPException(404)

    # votes per user (в рамках этой сессии)
    votes = db.execute("""
        SELECT u.username, r.tier_id
        FROM ratings r JOIN users u ON u.id=r.user_id
        WHERE r.session_id=? AND r.photo_id=? ORDER BY u.username
    """, (session_id, photo_id)).fetchall()

    # tags with user counts (теги общие для фото, не зависят от сессии;
    # только подтверждённые — suggestion'ы сюда не попадают)
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

def require_session_owner(db, session_id: int, user) -> sqlite3.Row:
    """Возвращает сессию, если вызывающий — её создатель (или сайт-админ),
    иначе бросает 403. Используется во всех управляющих сессией endpoint'ах
    (next/prev/set photo, shuffle, auto-advance, tiers)."""
    session = get_session_or_404(db, session_id)
    if session["creator_user_id"] != user["id"] and not user["is_admin"]:
        db.close()
        raise HTTPException(403, "Управлять сессией может только её создатель")
    return session

@app.post("/api/sessions/{session_id}/next-photo")
async def next_photo(session_id: int, user=Depends(current_user)):
    db = get_db()
    session = require_session_owner(db, session_id, user)
    cur = session["current_photo_id"]
    nxt = db.execute("""
        SELECT sp.photo_id as id FROM session_photos sp
        WHERE sp.session_id=? AND sp.position > (
            SELECT position FROM session_photos WHERE session_id=? AND photo_id=?
        ) ORDER BY sp.position LIMIT 1
    """, (session_id, session_id, cur)).fetchone() if cur else db.execute(
        "SELECT photo_id as id FROM session_photos WHERE session_id=? ORDER BY position LIMIT 1", (session_id,)
    ).fetchone()
    if not nxt:
        db.close()
        return {"done": True}
    db.execute("UPDATE sessions SET current_photo_id=?, voting_open=1 WHERE id=?", (nxt["id"], session_id))
    db.commit()
    db.close()
    await ws_manager.broadcast({"type": "photo_change", "photo_id": nxt["id"], "direction": "next"}, session_id)
    return {"done": False, "photo_id": nxt["id"]}

@app.post("/api/sessions/{session_id}/prev-photo")
async def prev_photo(session_id: int, user=Depends(current_user)):
    db = get_db()
    session = require_session_owner(db, session_id, user)
    cur = session["current_photo_id"]
    if not cur:
        db.close()
        raise HTTPException(400, "No current photo")
    prev = db.execute("""
        SELECT sp.photo_id as id FROM session_photos sp
        WHERE sp.session_id=? AND sp.position < (
            SELECT position FROM session_photos WHERE session_id=? AND photo_id=?
        ) ORDER BY sp.position DESC LIMIT 1
    """, (session_id, session_id, cur)).fetchone()
    if not prev:
        db.close()
        raise HTTPException(400, "Already at first photo")
    db.execute("UPDATE sessions SET current_photo_id=?, voting_open=1 WHERE id=?", (prev["id"], session_id))
    db.commit()
    db.close()
    await ws_manager.broadcast({"type": "photo_change", "photo_id": prev["id"], "direction": "prev"}, session_id)
    return {"photo_id": prev["id"]}

@app.post("/api/sessions/{session_id}/set-photo/{pid}")
async def set_photo(session_id: int, pid: int, user=Depends(current_user)):
    db = get_db()
    require_session_owner(db, session_id, user)
    if not db.execute("SELECT 1 FROM session_photos WHERE session_id=? AND photo_id=?", (session_id, pid)).fetchone():
        db.close()
        raise HTTPException(404, "Это фото не входит в данную сессию")
    db.execute("UPDATE sessions SET current_photo_id=?, voting_open=1 WHERE id=?", (pid, session_id))
    db.commit()
    db.close()
    await ws_manager.broadcast({"type": "photo_change", "photo_id": pid}, session_id)
    return {"ok": True}


@app.post("/api/sessions/{session_id}/shuffle")
async def shuffle_photos(session_id: int, user=Depends(current_user)):
    """Перемешивает порядок фотографий случайно — только внутри этой сессии,
    не затрагивает другие параллельные сессии и глобальный порядок photos."""
    import random
    db = get_db()
    require_session_owner(db, session_id, user)
    ids = [r["photo_id"] for r in db.execute(
        "SELECT photo_id FROM session_photos WHERE session_id=?", (session_id,)
    ).fetchall()]
    random.shuffle(ids)
    for new_pos, pid in enumerate(ids):
        db.execute("UPDATE session_photos SET position=? WHERE session_id=? AND photo_id=?",
                   (new_pos, session_id, pid))
    first_id = ids[0] if ids else None
    if first_id:
        db.execute("UPDATE sessions SET current_photo_id=?, voting_open=1 WHERE id=?", (first_id, session_id))
    db.commit()
    db.close()
    if first_id:
        await ws_manager.broadcast({"type": "photo_change", "photo_id": first_id}, session_id)
    return {"ok": True, "count": len(ids), "first_id": first_id}

@app.post("/api/sessions/{session_id}/auto-advance")
async def set_auto_advance(session_id: int, enabled: bool = Form(...), user=Depends(current_user)):
    db = get_db()
    require_session_owner(db, session_id, user)
    db.execute("UPDATE sessions SET auto_advance=? WHERE id=?", (1 if enabled else 0, session_id))
    db.commit()
    db.close()
    await ws_manager.broadcast({"type": "auto_advance_changed", "enabled": enabled}, session_id)
    return {"enabled": enabled}

@app.get("/api/sessions/{session_id}/auto-advance")
def get_auto_advance(session_id: int, user=Depends(current_user)):
    db = get_db()
    session = get_session_or_404(db, session_id)
    db.close()
    return {"enabled": bool(session["auto_advance"])}

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

@app.get("/api/sessions/{session_id}/online-users")
def online_users(session_id: int, user=Depends(current_user)):
    """Возвращает список онлайн-пользователей именно в этой сессии."""
    online_ids = ws_manager.online_user_ids(session_id)
    if not online_ids:
        return {"count": 0, "users": []}
    db = get_db()
    rows = db.execute(
        "SELECT id, username FROM users WHERE is_system=0 AND id IN ({})".format(
            ",".join("?" * len(online_ids))
        ), tuple(online_ids)
    ).fetchall()
    db.close()
    return {"count": len(rows), "users": [dict(r) for r in rows]}

# ── TIERLIST ──────────────────────────────────────────────────────────────────

@app.get("/api/photos/random-preview")
def random_photo_preview(user=Depends(current_user)):
    """
    Одно случайное фото из общего пула — для обложки плитки "Все фото" в
    форме создания сессии (по аналогии с тем, как альбом "Все фото" в
    галерее телефона показывает превью одного из снимков). Лёгкий запрос,
    не тянет весь список фото, как /api/admin/photos.
    """
    db = get_db()
    row = db.execute("SELECT filename FROM photos ORDER BY RANDOM() LIMIT 1").fetchone()
    db.close()
    return {"filename": row["filename"] if row else None}

@app.get("/api/photos/new-info")
def new_photos_info(user=Depends(current_user)):
    """
    Сколько фото ещё не входят в текущий опубликованный на главной тир-лист
    (плюс превью одного из них) — для карточки-альбома "Новые фото" в форме
    создания сессии. Если на главной ничего не опубликовано, "новыми"
    считаются все фото на сайте.
    """
    db = get_db()
    published_ids = get_published_photo_ids(db)
    if published_ids:
        placeholders = ",".join("?" * len(published_ids))
        rows = db.execute(
            f"SELECT id, filename FROM photos WHERE id NOT IN ({placeholders}) ORDER BY RANDOM()",
            list(published_ids)
        ).fetchall()
    else:
        rows = db.execute("SELECT id, filename FROM photos ORDER BY RANDOM()").fetchall()
    db.close()
    return {"count": len(rows), "preview_filename": rows[0]["filename"] if rows else None}

@app.get("/api/tierlist/tags")
def tierlist_tags(include_suggestions: bool = False, q: str = "", user=Depends(current_user)):
    """
    Возвращает теги для формы СОЗДАНИЯ сессии — отдельных альбомов-карточек.
    Список не зависит от конкретной сессии, теги общие атрибуты фото.

    Без поискового запроса (q=""): топ-10 самых популярных тегов (по числу
        фото) — витрина для дефолтного показа сетки альбомов.
    С поисковым запросом (q="..."): ищет по ВСЕМ тегам сайта, у которых есть
        хотя бы одно подходящее фото — без ограничения в 10, чтобы поиск не
        был "слепым" к тегам за пределами топ-10 витрины.

    include_suggestions=False (по умолчанию): считаются и показываются
        только теги с хотя бы одним ПОДТВЕРЖДЁННЫМ фото — неподтверждённые
        AI-предложения не видны вообще, пока человек явно не попросит их
        показать.
    include_suggestions=True: учитываются также фото, у которых тег есть
        только как неподтверждённое AI-предложение.

    Для каждого тега возвращаем:
      - has_confirmed: есть ли хотя бы одно ПОДТВЕРЖДЁННОЕ фото (для пометки
        "только AI" у тегов, которые пока никто не подтвердил)
      - preview_filename: имя файла одного репрезентативного фото — для
        карточки-обложки тега в UI
      - is_favorite: входит ли тег в любимые теги текущего пользователя
        (по высоко оценённым им фото во всех его сессиях) — такие теги
        отдаются первыми в списке.
    """
    db = get_db()

    suggestion_clause = "" if include_suggestions else "AND pt.is_suggestion = 0"
    q = q.strip()
    if q:
        pattern = "%" + q.replace("%", "").replace("_", "\\_") + "%"
        name_clause = "AND t.name LIKE ? ESCAPE '\\'"
        params = [pattern]
        limit_clause = "LIMIT 50"
    else:
        name_clause = ""
        params = []
        limit_clause = ""

    rows = db.execute(f"""
        SELECT t.id, t.name, t.category,
               COUNT(DISTINCT pt.photo_id) as photo_count,
               MAX(CASE WHEN pt.is_suggestion = 0 THEN 1 ELSE 0 END) as has_confirmed
        FROM tags t
        JOIN photo_tags pt ON pt.tag_id = t.id
        WHERE 1=1 {suggestion_clause} {name_clause}
        GROUP BY t.id
        HAVING photo_count > 0
        ORDER BY photo_count DESC
        {limit_clause}
    """, params).fetchall()

    favorite_ids = set(get_favorite_tag_ids_for_user(db, user["id"]))

    # Один батч-запрос превью для всех тегов сразу (вместо N отдельных) —
    # для каждого tag_id берём filename одного фото, предпочитая подтверждённое.
    preview_rows = db.execute("""
        SELECT pt.tag_id, p.filename, pt.is_suggestion
        FROM photo_tags pt JOIN photos p ON p.id = pt.photo_id
        ORDER BY pt.tag_id, pt.is_suggestion ASC
    """).fetchall()
    preview_by_tag = {}
    for pr in preview_rows:
        preview_by_tag.setdefault(pr["tag_id"], pr["filename"])  # первое вхождение на tag_id — уже отсортировано

    result = []
    for r in rows:
        result.append({
            "id": r["id"], "name": r["name"],
            "photo_count": r["photo_count"],
            "has_confirmed": bool(r["has_confirmed"]),
            "preview_filename": preview_by_tag.get(r["id"]),
            "is_favorite": r["id"] in favorite_ids,
            "is_character": r["category"] == 4,
        })

    db.close()
    # Сначала любимые (по убыванию популярности среди них), затем остальные
    # по убыванию популярности. Лимит — 10 карточек суммарно, чтобы список
    # не превращался в бесконечную простыню тегов от WD14.
    result.sort(key=lambda t: (not t["is_favorite"], -t["photo_count"]))
    return result if q else result[:10]


def compute_session_tierlist(db, session_id: int, session_row=None):
    """
    Считает тир-лист сессии по голосам (ratings) — общая логика, используемая
    и в обычном GET /api/sessions/{id}/tierlist, и при публикации статичного
    снимка на главную страницу (см. publish_tierlist_snapshot). Принимает уже
    открытое соединение db, ничего не закрывает сама.
    """
    session = session_row or get_session_or_404(db, session_id)
    tiers = get_tiers_for_session(session)
    tier_order = [t["id"] for t in tiers]
    tier_map = {t["id"]: t for t in tiers}

    n = len(tiers)
    tier_score = {t["id"]: n - i for i, t in enumerate(tiers)}

    allowed_ids = {r["photo_id"] for r in db.execute(
        "SELECT DISTINCT photo_id FROM ratings WHERE session_id=?", (session_id,)
    ).fetchall()}

    if not allowed_ids:
        return {"tiers": {tid: [] for tid in tier_order},
                "tier_order": tier_order, "tier_map": tier_map}

    id_placeholders = ",".join("?" * len(allowed_ids))
    rows = db.execute(f"""
        SELECT p.id, p.filename, p.original_name,
               r.tier_id, COUNT(*) as cnt
        FROM ratings r JOIN photos p ON p.id=r.photo_id
        WHERE r.session_id=? AND p.id IN ({id_placeholders})
        GROUP BY p.id, r.tier_id
    """, [session_id] + list(allowed_ids)).fetchall()

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

    return {"tiers": result, "tier_order": tier_order, "tier_map": tier_map}

@app.get("/api/sessions/{session_id}/tierlist")
def tierlist(session_id: int, user=Depends(current_user)):
    """
    Тир-лист конкретной сессии: считается по голосам (ratings) этой сессии,
    с тирами, заданными при её создании. Список фото фиксирован при создании
    сессии (см. /api/sessions создание) — повторная фильтрация по тегам внутри
    уже идущей сессии не нужна.
    """
    db = get_db()
    result = compute_session_tierlist(db, session_id)
    db.close()
    return result

@app.get("/api/stats")
def stats(user=Depends(current_user)):
    db = get_db()
    r = {
        "total_photos": db.execute("SELECT COUNT(*) FROM photos").fetchone()[0],
        "rated_photos": db.execute("SELECT COUNT(DISTINCT photo_id) FROM ratings").fetchone()[0],
        "total_users": db.execute("SELECT COUNT(*) FROM users WHERE is_admin=0 AND is_system=0").fetchone()[0],
        "total_votes": db.execute("SELECT COUNT(*) FROM ratings").fetchone()[0],
        "active_sessions": db.execute("SELECT COUNT(*) FROM sessions WHERE is_active=1").fetchone()[0],
    }
    db.close(); return r

@app.post("/api/admin/reset-db")
def reset_db(user=Depends(admin_user)):
    """
    Полный сброс сайта: удаляет всех обычных пользователей, все фото с диска,
    все голоса, теги, ВСЕ сессии (включая активные) и опубликованный на
    главной странице тир-лист. Это разрушительная операция уровня всего
    сайта — доступна только сайт-админу, не владельцам отдельных сессий
    (для завершения своей сессии есть /api/sessions/{id}/end).
    """
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
    db.execute("DELETE FROM session_photos")
    db.execute("DELETE FROM sessions")
    db.execute("DELETE FROM published_tierlist")
    db.commit()
    db.close()
    return {"ok": True}

@app.get("/api/users")
def list_all_users(user=Depends(current_user)):
    """
    Лёгкий список всех обычных (не системных) пользователей сайта —
    для страницы "Участники", откуда можно открыть чей-то профиль прямо
    с главной страницы, не заходя в сессию. В отличие от /api/admin/users
    (панель администратора, полное управление аккаунтами), этот эндпоинт
    открыт любому залогиненному человеку и отдаёт только базовые публичные
    поля.
    """
    db = get_db()
    rows = db.execute(
        "SELECT id, username, is_admin FROM users WHERE is_system=0 ORDER BY username COLLATE NOCASE"
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]

@app.get("/api/admin/users")
def list_users(user=Depends(admin_user)):
    db = get_db()
    rows = db.execute("SELECT id,username,is_admin,created_at FROM users ORDER BY id").fetchall()
    db.close(); return [dict(r) for r in rows]

@app.post("/api/admin/users/{uid}/make-admin")
def make_admin_user(uid: int, user=Depends(admin_user)):
    db = get_db(); db.execute("UPDATE users SET is_admin=1 WHERE id=?", (uid,)); db.commit(); db.close()
    return {"ok": True}

@app.get("/api/users/{uid}/profile")
def get_user_profile(uid: int, user=Depends(current_user)):
    """
    Глобальный профиль пользователя — агрегирует активность по ВСЕМ его
    сессиям сразу (в отличие от /api/sessions/{id}/user-stats/{uid}, который
    считает статистику только в рамках одной конкретной сессии). Доступен
    любому залогиненному человеку, не только владельцу профиля — так же,
    как список участников и сравнение уже открыты всем внутри сессии.
    """
    db = get_db()
    profile_user = db.execute(
        "SELECT id, username, is_admin, created_at FROM users WHERE id=? AND is_system=0", (uid,)
    ).fetchone()
    if not profile_user:
        db.close()
        raise HTTPException(404, "Пользователь не найден")

    total_votes = db.execute("SELECT COUNT(*) as c FROM ratings WHERE user_id=?", (uid,)).fetchone()["c"]
    sessions_participated = db.execute(
        "SELECT COUNT(DISTINCT session_id) as c FROM ratings WHERE user_id=?", (uid,)
    ).fetchone()["c"]
    sessions_created = db.execute(
        "SELECT COUNT(*) as c FROM sessions WHERE creator_user_id=?", (uid,)
    ).fetchone()["c"]
    tags_added = db.execute(
        "SELECT COUNT(*) as c FROM photo_tags WHERE user_id=? AND is_suggestion=0", (uid,)
    ).fetchone()["c"]
    last_active = db.execute(
        "SELECT MAX(created_at) as m FROM ratings WHERE user_id=?", (uid,)
    ).fetchone()["m"]

    # "щедрость" оценок — какая доля голосов пришлась на верхнюю половину
    # тиров (своих же, в рамках каждой сессии) — просто любопытная метрика,
    # не влияет ни на что в самом приложении.
    top_photo_ids = get_top_rated_photo_ids_for_user(db, uid)
    top_half_pct = round(100 * len(top_photo_ids) / total_votes) if total_votes else 0

    favorite_tags = get_favorite_tags_detailed_for_user(db, uid, limit=12)
    db.close()

    return {
        "id": profile_user["id"],
        "username": profile_user["username"],
        "is_admin": bool(profile_user["is_admin"]),
        "member_since": profile_user["created_at"],
        "total_votes": total_votes,
        "sessions_participated": sessions_participated,
        "sessions_created": sessions_created,
        "tags_added": tags_added,
        "last_active": last_active,
        "top_half_pct": top_half_pct,
        "favorite_tags": favorite_tags,
    }


@app.get("/api/sessions/{session_id}/users-list")
def users_list(session_id: int, user=Depends(current_user)):
    """Участники именно этой сессии (у кого есть хоть один голос в ней) —
    для боковой панели экрана 'Статистика'."""
    db = get_db()
    get_session_or_404(db, session_id)
    rows = db.execute(
        "SELECT u.id, u.username, u.is_admin, COUNT(r.id) as vote_count "
        "FROM users u JOIN ratings r ON r.user_id=u.id AND r.session_id=? "
        "WHERE u.is_system=0 "
        "GROUP BY u.id ORDER BY vote_count DESC, u.username",
        (session_id,)
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


@app.get("/api/sessions/{session_id}/user-stats/{uid}")
def user_stats(session_id: int, uid: int, user=Depends(current_user)):
    db = get_db()
    session = get_session_or_404(db, session_id)
    tiers = get_tiers_for_session(session)
    tier_map = {t["id"]: t for t in tiers}
    tier_order = [t["id"] for t in tiers]
    n = len(tiers)

    target = db.execute("SELECT id, username FROM users WHERE id=?", (uid,)).fetchone()
    if not target:
        db.close()
        raise HTTPException(404, "User not found")

    tier_counts = {}
    for row in db.execute(
        "SELECT tier_id, COUNT(*) as cnt FROM ratings WHERE session_id=? AND user_id=? GROUP BY tier_id",
        (session_id, uid)
    ).fetchall():
        tier_counts[row["tier_id"]] = row["cnt"]
    total_votes = sum(tier_counts.values())

    user_ratings = db.execute(
        "SELECT photo_id, tier_id FROM ratings WHERE session_id=? AND user_id=?", (session_id, uid)
    ).fetchall()
    agree_score = 0.0
    total_compared = 0
    for ur in user_ratings:
        pid = ur["photo_id"]
        others = db.execute(
            "SELECT tier_id, COUNT(*) as cnt FROM ratings WHERE session_id=? AND photo_id=? AND user_id!=?",
            (session_id, pid, uid)
        ).fetchall()
        if not others:
            continue
        total_other = sum(o["cnt"] for o in others)
        if total_other == 0:
            continue
        # Сколько из других проголосовало так же как этот пользователь
        same = next((o["cnt"] for o in others if o["tier_id"] == ur["tier_id"]), 0)
        agree_score += same / total_other
        total_compared += 1

    agreement_pct = round(agree_score / total_compared * 100) if total_compared else None

    top_n = max(1, n // 2)
    top_tier_ids = tier_order[:top_n]
    ph = ",".join("?" * len(top_tier_ids))
    top_photos = [r["photo_id"] for r in db.execute(
        f"SELECT photo_id FROM ratings WHERE session_id=? AND user_id=? AND tier_id IN ({ph})",
        [session_id, uid] + top_tier_ids
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


@app.get("/api/sessions/{session_id}/compare/{uid1}/{uid2}")
def compare_users(session_id: int, uid1: int, uid2: int, user=Depends(current_user)):
    db = get_db()
    session = get_session_or_404(db, session_id)
    tiers = get_tiers_for_session(session)
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
          db.execute("SELECT photo_id, tier_id FROM ratings WHERE session_id=? AND user_id=?",
                     (session_id, uid1)).fetchall()}
    r2 = {r["photo_id"]: r["tier_id"] for r in
          db.execute("SELECT photo_id, tier_id FROM ratings WHERE session_id=? AND user_id=?",
                     (session_id, uid2)).fetchall()}

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

@app.get("/thumbs/{filename}")
def get_thumbnail(filename: str):
    """
    Уменьшенное превью фото — используется везде, где в интерфейсе нужна
    маленькая миниатюра (сетка галереи, тир-лист, карточки альбомов, таблица
    в "Управление фото"), вместо того чтобы гонять по сети полноразмерный
    оригинал ради картинки 140-320px. Генерируется лениво при первом
    обращении и дальше отдаётся уже готовым файлом с длинным кэшем.
    """
    safe_name = os.path.basename(filename)  # защита от path traversal (../..)
    thumb_path = generate_thumbnail(safe_name)
    if not thumb_path:
        # не удалось прочитать как изображение — отдаём оригинал как есть,
        # чтобы миниатюра не была просто "битой картинкой" в интерфейсе
        orig_path = os.path.join(PHOTOS_DIR, safe_name)
        if not os.path.exists(orig_path):
            raise HTTPException(404, "Фото не найдено")
        return FileResponse(orig_path)
    return FileResponse(thumb_path, headers={"Cache-Control": "public, max-age=604800, immutable"})

app.mount("/photos", StaticFiles(directory=PHOTOS_DIR), name="photos")
app.mount("/static", StaticFiles(directory="/app/frontend/static"), name="static")

@app.get("/{path:path}")
def serve_frontend(path: str): return FileResponse("/app/frontend/index.html")

@app.get("/")
def root(): return FileResponse("/app/frontend/index.html")
