from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import sqlite3, os, jwt, bcrypt, uuid, io
from datetime import datetime, timedelta
from typing import Optional
from PIL import Image as PILImage

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass

app = FastAPI(title="PhotoRank")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SECRET = os.getenv("JWT_SECRET", "change-me-in-production-please")
PHOTOS_DIR = "/app/photos"
DB_PATH = "/app/data/db.sqlite3"
os.makedirs(PHOTOS_DIR, exist_ok=True)
os.makedirs("/app/data", exist_ok=True)

security = HTTPBearer(auto_error=False)

# S=5, A=4, B=3, C=2, D=1
TIER_SCORE = {"S": 5, "A": 4, "B": 3, "C": 2, "D": 1}
SCORE_TIER = {5: "S", 4: "A", 3: "B", 2: "C", 1: "D"}
DEFAULT_TIER_LABELS = {"S": "S", "A": "A", "B": "B", "C": "C", "D": "D"}

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
        is_active INTEGER DEFAULT 0,
        uploaded_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS ratings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        photo_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        score INTEGER NOT NULL,
        created_at TEXT DEFAULT (datetime('now')),
        UNIQUE(photo_id, user_id)
    );
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    );
    INSERT OR IGNORE INTO settings VALUES ('current_photo_id', NULL);
    INSERT OR IGNORE INTO settings VALUES ('voting_open', '1');
    INSERT OR IGNORE INTO settings VALUES ('tier_labels', '{"S":"S","A":"A","B":"B","C":"C","D":"D"}');
    """)
    db.commit()
    db.close()

init_db()

def hash_pw(pw):
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()

def check_pw(pw, hashed):
    return bcrypt.checkpw(pw.encode(), hashed.encode())

def make_token(user_id, is_admin):
    payload = {"sub": str(user_id), "admin": is_admin,
               "exp": datetime.utcnow() + timedelta(days=30)}
    return jwt.encode(payload, SECRET, algorithm="HS256")

def current_user(creds: Optional[HTTPAuthorizationCredentials] = Depends(security)):
    if not creds:
        raise HTTPException(401, "Not authenticated")
    try:
        data = jwt.decode(creds.credentials, SECRET, algorithms=["HS256"])
        return {"id": int(data["sub"]), "admin": data.get("admin", False)}
    except Exception:
        raise HTTPException(401, "Invalid token")

def admin_user(user=Depends(current_user)):
    if not user["admin"]:
        raise HTTPException(403, "Admin only")
    return user

def get_setting(db, key):
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None

def set_setting(db, key, value):
    db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, value))
    db.commit()

def get_tier_labels(db):
    import json
    raw = get_setting(db, "tier_labels")
    try:
        return json.loads(raw) if raw else DEFAULT_TIER_LABELS.copy()
    except:
        return DEFAULT_TIER_LABELS.copy()

# ── AUTH ──────────────────────────────────────────────────────────────────────

@app.post("/api/register")
def register(username: str = Form(...), password: str = Form(...)):
    if len(username) < 3 or len(password) < 4:
        raise HTTPException(400, "Username ≥3 chars, password ≥4 chars")
    db = get_db()
    try:
        db.execute("INSERT INTO users (username, password_hash) VALUES (?,?)",
                   (username.strip(), hash_pw(password)))
        db.commit()
        row = db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        return {"token": make_token(row["id"], False), "username": username, "is_admin": False}
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Username already taken")
    finally:
        db.close()

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
    row = db.execute("SELECT id, username, is_admin FROM users WHERE id=?", (user["id"],)).fetchone()
    db.close()
    return dict(row)

# ── TIER LABELS ───────────────────────────────────────────────────────────────

@app.get("/api/tier-labels")
def api_get_tier_labels(user=Depends(current_user)):
    import json
    db = get_db()
    labels = get_tier_labels(db)
    db.close()
    return labels

@app.post("/api/admin/tier-labels")
def api_set_tier_labels(
    s: str = Form(...), a: str = Form(...), b: str = Form(...),
    c: str = Form(...), d: str = Form(...),
    user=Depends(admin_user)
):
    import json
    labels = {"S": s.strip()[:20], "A": a.strip()[:20], "B": b.strip()[:20],
              "C": c.strip()[:20], "D": d.strip()[:20]}
    db = get_db()
    set_setting(db, "tier_labels", json.dumps(labels, ensure_ascii=False))
    db.close()
    return labels

# ── PHOTOS ────────────────────────────────────────────────────────────────────

@app.post("/api/admin/photos/upload")
async def upload_photos(files: list[UploadFile] = File(...), user=Depends(admin_user)):
    db = get_db()
    added = 0
    for f in files:
        data = await f.read()
        try:
            img = PILImage.open(io.BytesIO(data))
            img.load()
        except Exception:
            continue
        uid = str(uuid.uuid4())
        filename = uid + ".jpg"
        path = os.path.join(PHOTOS_DIR, filename)
        img.convert("RGB").save(path, "JPEG", quality=92)
        count = db.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
        db.execute("INSERT INTO photos (filename, original_name, position) VALUES (?,?,?)",
                   (filename, f.filename, count))
        added += 1
    db.commit()
    db.close()
    return {"added": added}

@app.get("/api/admin/photos")
def admin_photos(user=Depends(admin_user)):
    db = get_db()
    rows = db.execute("""
        SELECT p.*, COUNT(r.id) as vote_count, ROUND(AVG(r.score),2) as avg_score
        FROM photos p LEFT JOIN ratings r ON r.photo_id=p.id
        GROUP BY p.id ORDER BY p.position
    """).fetchall()
    db.close()
    return [dict(r) for r in rows]

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
    db.close()
    return {"ok": True}

# ── VOTING ────────────────────────────────────────────────────────────────────

@app.get("/api/current-photo")
def current_photo(user=Depends(current_user)):
    db = get_db()
    photo_id = get_setting(db, "current_photo_id")
    voting_open = get_setting(db, "voting_open")
    labels = get_tier_labels(db)
    if not photo_id:
        db.close()
        return {"photo": None, "voting_open": voting_open == "1", "tier_labels": labels}
    row = db.execute("""
        SELECT p.*, COUNT(r.id) as vote_count, ROUND(AVG(r.score),2) as avg_score
        FROM photos p LEFT JOIN ratings r ON r.photo_id=p.id
        WHERE p.id=? GROUP BY p.id
    """, (photo_id,)).fetchone()
    user_rating = db.execute(
        "SELECT score FROM ratings WHERE photo_id=? AND user_id=?",
        (photo_id, user["id"])).fetchone()
    total_users = db.execute("SELECT COUNT(*) FROM users WHERE is_admin=0").fetchone()[0]
    db.close()

    user_tier = SCORE_TIER.get(user_rating["score"]) if user_rating else None
    return {
        "photo": dict(row) if row else None,
        "user_rating": user_rating["score"] if user_rating else None,
        "user_tier": user_tier,
        "voting_open": voting_open == "1",
        "total_users": total_users,
        "tier_labels": labels,
    }

@app.post("/api/rate")
def rate_photo(photo_id: int = Form(...), tier: str = Form(...), user=Depends(current_user)):
    tier = tier.upper()
    if tier not in TIER_SCORE:
        raise HTTPException(400, "Invalid tier")
    score = TIER_SCORE[tier]
    db = get_db()
    voting_open = get_setting(db, "voting_open")
    if voting_open != "1":
        db.close()
        raise HTTPException(403, "Voting is closed")
    cur = get_setting(db, "current_photo_id")
    if str(photo_id) != str(cur):
        db.close()
        raise HTTPException(400, "Not the current photo")
    db.execute("""
        INSERT INTO ratings (photo_id, user_id, score) VALUES (?,?,?)
        ON CONFLICT(photo_id, user_id) DO UPDATE SET score=excluded.score
    """, (photo_id, user["id"], score))
    db.commit()
    db.close()
    return {"ok": True, "score": score, "tier": tier}

# ── ADMIN NAVIGATION ──────────────────────────────────────────────────────────

@app.post("/api/admin/next-photo")
def next_photo(user=Depends(admin_user)):
    db = get_db()
    cur = get_setting(db, "current_photo_id")
    nxt = db.execute(
        "SELECT id FROM photos WHERE position > (SELECT position FROM photos WHERE id=?) ORDER BY position LIMIT 1",
        (cur,)).fetchone() if cur else db.execute("SELECT id FROM photos ORDER BY position LIMIT 1").fetchone()
    if not nxt:
        db.close()
        return {"done": True}
    set_setting(db, "current_photo_id", str(nxt["id"]))
    set_setting(db, "voting_open", "1")
    db.close()
    return {"done": False, "photo_id": nxt["id"]}

@app.post("/api/admin/prev-photo")
def prev_photo(user=Depends(admin_user)):
    db = get_db()
    cur = get_setting(db, "current_photo_id")
    if not cur:
        raise HTTPException(400, "No current photo")
    prev = db.execute(
        "SELECT id FROM photos WHERE position < (SELECT position FROM photos WHERE id=?) ORDER BY position DESC LIMIT 1",
        (cur,)).fetchone()
    if not prev:
        raise HTTPException(400, "Already at first photo")
    set_setting(db, "current_photo_id", str(prev["id"]))
    set_setting(db, "voting_open", "1")
    db.close()
    return {"photo_id": prev["id"]}

@app.post("/api/admin/set-photo/{pid}")
def set_photo(pid: int, user=Depends(admin_user)):
    db = get_db()
    if not db.execute("SELECT id FROM photos WHERE id=?", (pid,)).fetchone():
        raise HTTPException(404)
    set_setting(db, "current_photo_id", str(pid))
    set_setting(db, "voting_open", "1")
    db.close()
    return {"ok": True}

@app.post("/api/admin/close-voting")
def close_voting(user=Depends(admin_user)):
    db = get_db()
    set_setting(db, "voting_open", "0")
    db.close()
    return {"ok": True}

# ── TIERLIST ──────────────────────────────────────────────────────────────────

@app.get("/api/tierlist")
def tierlist(user=Depends(current_user)):
    import json
    db = get_db()
    labels = get_tier_labels(db)
    rows = db.execute("""
        SELECT p.id, p.filename, p.original_name,
               COUNT(r.id) as vote_count,
               ROUND(AVG(r.score), 2) as avg_score
        FROM photos p
        LEFT JOIN ratings r ON r.photo_id = p.id
        WHERE p.id IN (SELECT DISTINCT photo_id FROM ratings)
        GROUP BY p.id ORDER BY avg_score DESC
    """).fetchall()
    db.close()
    result = {"S": [], "A": [], "B": [], "C": [], "D": []}
    for row in rows:
        avg = row["avg_score"] or 1
        # round to nearest tier
        score = round(avg)
        score = max(1, min(5, score))
        tier = SCORE_TIER[score]
        result[tier].append({
            "id": row["id"],
            "filename": row["filename"],
            "original_name": row["original_name"],
            "avg_score": row["avg_score"],
            "vote_count": row["vote_count"],
            "tier": tier,
        })
    return {"tiers": result, "tier_order": ["S","A","B","C","D"], "tier_labels": labels}

@app.get("/api/stats")
def stats(user=Depends(admin_user)):
    db = get_db()
    r = {
        "total_photos": db.execute("SELECT COUNT(*) FROM photos").fetchone()[0],
        "rated_photos": db.execute("SELECT COUNT(DISTINCT photo_id) FROM ratings").fetchone()[0],
        "total_users": db.execute("SELECT COUNT(*) FROM users WHERE is_admin=0").fetchone()[0],
        "total_votes": db.execute("SELECT COUNT(*) FROM ratings").fetchone()[0],
    }
    db.close()
    return r

@app.get("/api/admin/users")
def list_users(user=Depends(admin_user)):
    db = get_db()
    rows = db.execute("SELECT id, username, is_admin, created_at FROM users ORDER BY id").fetchall()
    db.close()
    return [dict(r) for r in rows]

@app.post("/api/admin/users/{uid}/make-admin")
def make_admin_user(uid: int, user=Depends(admin_user)):
    db = get_db()
    db.execute("UPDATE users SET is_admin=1 WHERE id=?", (uid,))
    db.commit()
    db.close()
    return {"ok": True}

app.mount("/photos", StaticFiles(directory=PHOTOS_DIR), name="photos")
app.mount("/static", StaticFiles(directory="/app/frontend/static"), name="static")

@app.get("/{path:path}")
def serve_frontend(path: str):
    return FileResponse("/app/frontend/index.html")

@app.get("/")
def root():
    return FileResponse("/app/frontend/index.html")
