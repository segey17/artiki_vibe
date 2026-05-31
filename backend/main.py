from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import sqlite3, os, jwt, bcrypt, uuid, io, json
from datetime import datetime, timedelta
from typing import Optional
from PIL import Image as PILImage

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass

app = FastAPI(title="PhotoRank")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])

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
    db.close()
    return {
        "photo": dict(row) if row else None,
        "user_tier": user_rating["tier_id"] if user_rating else None,
        "voting_open": voting_open == "1",
        "total_users": total_users,
        "tiers": tiers,
        "tier_counts": tier_counts,
    }

@app.post("/api/rate")
def rate_photo(photo_id: int = Form(...), tier_id: str = Form(...), user=Depends(current_user)):
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
    db.commit(); db.close()
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
def next_photo(user=Depends(admin_user)):
    db = get_db(); cur = get_setting(db, "current_photo_id")
    nxt = db.execute(
        "SELECT id FROM photos WHERE position>(SELECT position FROM photos WHERE id=?) ORDER BY position LIMIT 1",
        (cur,)).fetchone() if cur else db.execute("SELECT id FROM photos ORDER BY position LIMIT 1").fetchone()
    if not nxt: db.close(); return {"done": True}
    set_setting(db, "current_photo_id", str(nxt["id"]))
    set_setting(db, "voting_open", "1")
    db.close(); return {"done": False, "photo_id": nxt["id"]}

@app.post("/api/admin/prev-photo")
def prev_photo(user=Depends(admin_user)):
    db = get_db(); cur = get_setting(db, "current_photo_id")
    if not cur: raise HTTPException(400, "No current photo")
    prev = db.execute(
        "SELECT id FROM photos WHERE position<(SELECT position FROM photos WHERE id=?) ORDER BY position DESC LIMIT 1",
        (cur,)).fetchone()
    if not prev: raise HTTPException(400, "Already at first photo")
    set_setting(db, "current_photo_id", str(prev["id"]))
    set_setting(db, "voting_open", "1")
    db.close(); return {"photo_id": prev["id"]}

@app.post("/api/admin/set-photo/{pid}")
def set_photo(pid: int, user=Depends(admin_user)):
    db = get_db()
    if not db.execute("SELECT id FROM photos WHERE id=?", (pid,)).fetchone(): raise HTTPException(404)
    set_setting(db, "current_photo_id", str(pid))
    set_setting(db, "voting_open", "1")
    db.close(); return {"ok": True}

@app.post("/api/admin/close-voting")
def close_voting(user=Depends(admin_user)):
    db = get_db(); set_setting(db, "voting_open", "0"); db.close(); return {"ok": True}

# ── TIERLIST ──────────────────────────────────────────────────────────────────

@app.get("/api/tierlist")
def tierlist(user=Depends(current_user)):
    db = get_db()
    tiers = get_tiers(db)
    tier_order = [t["id"] for t in tiers]
    tier_map = {t["id"]: t for t in tiers}

    # assign score by position: first tier = highest
    n = len(tiers)
    tier_score = {t["id"]: n - i for i, t in enumerate(tiers)}

    # get photos with ratings
    rows = db.execute("""
        SELECT p.id, p.filename, p.original_name,
               r.tier_id, COUNT(*) as cnt
        FROM ratings r JOIN photos p ON p.id=r.photo_id
        GROUP BY p.id, r.tier_id
    """).fetchall()

    # accumulate weighted score per photo
    from collections import defaultdict
    photo_info = {}
    photo_scores = defaultdict(list)
    for row in rows:
        pid = row["id"]
        photo_info[pid] = {"id": pid, "filename": row["filename"], "original_name": row["original_name"]}
        for _ in range(row["cnt"]):
            photo_scores[pid].append(tier_score.get(row["tier_id"], 1))

    # compute avg and assign tier
    result = {tid: [] for tid in tier_order}
    for pid, scores in photo_scores.items():
        avg = sum(scores) / len(scores)
        # map avg score back to tier index
        idx = round(n - avg)
        idx = max(0, min(n-1, idx))
        assigned_tier = tier_order[idx]
        result[assigned_tier].append({
            **photo_info[pid],
            "vote_count": len(scores),
            "avg_score": round(avg, 2),
            "tier_id": assigned_tier,
        })

    # sort each tier by avg desc
    for tid in result:
        result[tid].sort(key=lambda x: -x["avg_score"])

    db.close()
    return {"tiers": result, "tier_order": tier_order, "tier_map": tier_map}


@app.get("/api/photo-detail/{photo_id}")
def photo_detail(photo_id: int, user=Depends(current_user)):
    db = get_db()
    tiers = get_tiers(db)
    tier_map = {t["id"]: t for t in tiers}

    # ratings with usernames
    ratings = db.execute("""
        SELECT u.username, r.tier_id
        FROM ratings r JOIN users u ON u.id=r.user_id
        WHERE r.photo_id=?
        ORDER BY u.username
    """, (photo_id,)).fetchall()

    # tags with count
    tags = db.execute("""
        SELECT t.name, COUNT(*) as cnt, GROUP_CONCAT(u.username, ', ') as users
        FROM photo_tags pt
        JOIN tags t ON t.id=pt.tag_id
        JOIN users u ON u.id=pt.user_id
        WHERE pt.photo_id=?
        GROUP BY t.id
        ORDER BY cnt DESC, t.name
    """, (photo_id,)).fetchall()

    db.close()
    return {
        "ratings": [{"username": r["username"], "tier_id": r["tier_id"],
                     "tier_label": tier_map.get(r["tier_id"],{}).get("label", r["tier_id"]),
                     "tier_color": tier_map.get(r["tier_id"],{}).get("color","#888")} for r in ratings],
        "tags": [{"name": t["name"], "count": t["cnt"], "users": t["users"]} for t in tags],
    }

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

@app.get("/api/admin/users")
def list_users(user=Depends(admin_user)):
    db = get_db()
    rows = db.execute("SELECT id,username,is_admin,created_at FROM users ORDER BY id").fetchall()
    db.close(); return [dict(r) for r in rows]

@app.post("/api/admin/users/{uid}/make-admin")
def make_admin_user(uid: int, user=Depends(admin_user)):
    db = get_db(); db.execute("UPDATE users SET is_admin=1 WHERE id=?", (uid,)); db.commit(); db.close()
    return {"ok": True}

app.mount("/photos", StaticFiles(directory=PHOTOS_DIR), name="photos")
app.mount("/static", StaticFiles(directory="/app/frontend/static"), name="static")

@app.get("/{path:path}")
def serve_frontend(path: str): return FileResponse("/app/frontend/index.html")

@app.get("/")
def root(): return FileResponse("/app/frontend/index.html")
