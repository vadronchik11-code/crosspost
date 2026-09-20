import hashlib
import json
import secrets
import sqlite3
from datetime import datetime, timezone

from . import config

ROLES = ("head", "teamlead", "media")          # руковод, тимлид медиа, медиа
MANAGERS = ("head", "teamlead")                 # могут управлять командой

COLUMNS = [
    "title", "common_text",
    "vk_enabled", "tg_enabled",
    "vk_text", "tg_text", "tg_html", "tg_as_file",
    "images", "vk_background", "tg_background",
    "scheduled_at", "status",
    "vk_status", "vk_result", "vk_error",
    "tg_status", "tg_result", "tg_error",
    "published_at", "created_at", "updated_at",
    "created_by", "updated_by", "remote_dirty",
]
BOOL_COLUMNS = ("vk_enabled", "tg_enabled", "tg_html", "tg_as_file", "remote_dirty")

# поля, по которым считаем «содержательные» изменения поста (для истории и синхронизации)
TRACKED = ("title", "common_text", "vk_text", "tg_text", "images", "vk_background", "tg_background",
           "scheduled_at", "status", "vk_enabled", "tg_enabled", "tg_as_file")
CONTENT_FIELDS = ("vk_text", "tg_text", "images", "vk_background", "tg_background", "tg_as_file", "scheduled_at")

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT DEFAULT '',
    common_text TEXT DEFAULT '',
    vk_enabled INTEGER DEFAULT 1,
    tg_enabled INTEGER DEFAULT 1,
    vk_text TEXT DEFAULT '',
    tg_text TEXT DEFAULT '',
    tg_html INTEGER DEFAULT 0,
    tg_as_file INTEGER DEFAULT 0,
    images TEXT DEFAULT '[]',
    vk_background TEXT,
    tg_background TEXT,
    scheduled_at TEXT,
    status TEXT DEFAULT 'draft',
    vk_status TEXT DEFAULT 'pending',
    vk_result TEXT,
    vk_error TEXT,
    tg_status TEXT DEFAULT 'pending',
    tg_result TEXT,
    tg_error TEXT,
    published_at TEXT,
    created_at TEXT,
    updated_at TEXT,
    created_by TEXT,
    updated_by TEXT,
    remote_dirty INTEGER DEFAULT 0,
    vk_postponed_id INTEGER,
    vk_postponed_info TEXT
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL,
    created_by TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id INTEGER NOT NULL,
    user TEXT,
    action TEXT NOT NULL,
    changes TEXT,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS history_post ON history(post_id);
"""

# колонки, добавленные позже: доливаем в старую базу
MIGRATIONS = {
    "posts": {
        "tg_as_file": "INTEGER DEFAULT 0",
        "created_by": "TEXT",
        "updated_by": "TEXT",
        "remote_dirty": "INTEGER DEFAULT 0",
        "vk_postponed_id": "INTEGER",
        "vk_postponed_info": "TEXT",
    },
}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


_conn = connect()
_conn.executescript(SCHEMA)
for table, cols in MIGRATIONS.items():
    existing = {r["name"] for r in _conn.execute(f"PRAGMA table_info({table})")}
    for col, ddl in cols.items():
        if col not in existing:
            _conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
_conn.commit()


# ---------------------------------------------------------------- posts
def _row(row) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    d["images"] = json.loads(d.get("images") or "[]")
    for k in BOOL_COLUMNS:
        d[k] = bool(d.get(k))
    for k in ("vk_result", "tg_result"):
        if isinstance(d.get(k), str):
            try:
                d[k] = json.loads(d[k])
            except ValueError:
                pass
    return d


def list_posts() -> list[dict]:
    rows = _conn.execute(
        "SELECT * FROM posts ORDER BY "
        "CASE status WHEN 'scheduled' THEN 0 WHEN 'error' THEN 1 WHEN 'draft' THEN 2 ELSE 3 END, "
        "COALESCE(scheduled_at, updated_at) DESC"
    ).fetchall()
    return [_row(r) for r in rows]


def get_post(post_id: int) -> dict | None:
    return _row(_conn.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone())


def _prepare(data: dict) -> dict:
    out = {}
    for k, v in data.items():
        if k not in COLUMNS:
            continue
        if k == "images":
            v = json.dumps(v or [], ensure_ascii=False)
        elif k in BOOL_COLUMNS:
            v = 1 if v else 0
        elif isinstance(v, (dict, list)):
            v = json.dumps(v, ensure_ascii=False)
        out[k] = v
    return out


def create_post(data: dict) -> dict:
    data = _prepare(data)
    data["created_at"] = data["updated_at"] = now_iso()
    keys = list(data.keys())
    cur = _conn.execute(
        f"INSERT INTO posts ({', '.join(keys)}) VALUES ({', '.join('?' for _ in keys)})",
        [data[k] for k in keys],
    )
    _conn.commit()
    return get_post(cur.lastrowid)


def update_post(post_id: int, data: dict) -> dict:
    data = _prepare(data)
    data["updated_at"] = now_iso()
    sets = ", ".join(f"{k}=?" for k in data)
    _conn.execute(f"UPDATE posts SET {sets} WHERE id=?", [*data.values(), post_id])
    _conn.commit()
    return get_post(post_id)


def delete_post(post_id: int) -> None:
    _conn.execute("DELETE FROM posts WHERE id=?", (post_id,))
    _conn.execute("DELETE FROM history WHERE post_id=?", (post_id,))
    _conn.commit()


def due_posts() -> list[dict]:
    rows = _conn.execute(
        "SELECT * FROM posts WHERE status='scheduled' AND scheduled_at IS NOT NULL AND scheduled_at <= ?",
        (now_iso(),),
    ).fetchall()
    return [_row(r) for r in rows]


def scheduled_tg_posts() -> list[dict]:
    """Посты, отданные в отложку Telegram, у которых ещё не найдены настоящие id."""
    rows = _conn.execute("SELECT * FROM posts WHERE tg_status='published' AND tg_result LIKE '%\"scheduled\": true%'").fetchall()
    return [_row(r) for r in rows]


def diff_post(old: dict, new: dict) -> dict:
    """Что изменилось: {поле: [было, стало]} по отслеживаемым полям, присутствующим в new."""
    changes = {}
    for k in TRACKED:
        if k not in new:
            continue
        a, b = old.get(k), new.get(k)
        if k in BOOL_COLUMNS:
            a, b = bool(a), bool(b)
        if a != b:
            changes[k] = [a, b]
    return changes


# ---------------------------------------------------------------- history
def add_history(post_id: int, user: str | None, action: str, changes: dict | None = None) -> None:
    _conn.execute(
        "INSERT INTO history (post_id, user, action, changes, created_at) VALUES (?, ?, ?, ?, ?)",
        (post_id, user, action, json.dumps(changes or {}, ensure_ascii=False), now_iso()),
    )
    _conn.commit()


def post_history(post_id: int) -> list[dict]:
    rows = _conn.execute("SELECT * FROM history WHERE post_id=? ORDER BY id DESC", (post_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["changes"] = json.loads(d.get("changes") or "{}")
        out.append(d)
    return out


# ---------------------------------------------------------------- users
def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000).hex()
    return f"{salt}${h}"


def check_password(password: str, stored: str) -> bool:
    try:
        salt, h = stored.split("$", 1)
    except ValueError:
        return False
    calc = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000).hex()
    return secrets.compare_digest(calc, h)


def _user(row) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    d.pop("password_hash", None)
    return d


def list_users() -> list[dict]:
    return [_user(r) for r in _conn.execute("SELECT * FROM users ORDER BY CASE role WHEN 'head' THEN 0 WHEN 'teamlead' THEN 1 ELSE 2 END, username").fetchall()]


def get_user(user_id: int) -> dict | None:
    return _user(_conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())


def get_user_by_name(username: str) -> dict | None:
    return _user(_conn.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE", (username,)).fetchone())


def verify_user(username: str, password: str) -> dict | None:
    row = _conn.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE", (username,)).fetchone()
    if row and check_password(password, row["password_hash"]):
        return _user(row)
    return None


def create_user(username: str, password: str, role: str, created_by: str | None) -> dict:
    cur = _conn.execute(
        "INSERT INTO users (username, password_hash, role, created_by, created_at) VALUES (?, ?, ?, ?, ?)",
        (username, hash_password(password), role, created_by, now_iso()),
    )
    _conn.commit()
    return get_user(cur.lastrowid)


def update_user(user_id: int, password: str | None = None, role: str | None = None) -> dict:
    if password:
        _conn.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(password), user_id))
    if role:
        _conn.execute("UPDATE users SET role=? WHERE id=?", (role, user_id))
    _conn.commit()
    return get_user(user_id)


def delete_user(user_id: int) -> None:
    _conn.execute("DELETE FROM users WHERE id=?", (user_id,))
    _conn.commit()


def users_count() -> int:
    return _conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
