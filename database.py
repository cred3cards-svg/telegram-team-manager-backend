import os
import sqlite3
from contextlib import contextmanager
from cryptography.fernet import Fernet

# ── Fernet encryption ─────────────────────────────────────────────────────────

_raw_key = os.getenv("SESSION_ENCRYPTION_KEY", "")
if _raw_key:
    _key_bytes = _raw_key.encode() if isinstance(_raw_key, str) else _raw_key
    _key_bytes += b"=" * (-len(_key_bytes) % 4)
    _fernet = Fernet(_key_bytes)
else:
    import base64, hashlib
    _seed = os.getenv("TELEGRAM_API_HASH", "fallback-key-change-me")
    _fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(_seed.encode()).digest()))


def encrypt_session(session_string: str) -> str:
    if not session_string:
        return ""
    return _fernet.encrypt(session_string.encode()).decode()


def decrypt_session(encrypted: str) -> str:
    if not encrypted:
        return ""
    try:
        return _fernet.decrypt(encrypted.encode()).decode()
    except Exception:
        return encrypted  # legacy plaintext


# ── Backend detection ─────────────────────────────────────────────────────────

_DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data/app.db")
USE_POSTGRES = _DATABASE_URL.startswith("postgres")

if USE_POSTGRES:
    import psycopg2
    import psycopg2.extras
    # Railway sometimes uses postgres:// — psycopg2 needs postgresql://
    _PG_DSN = _DATABASE_URL.replace("postgres://", "postgresql://", 1)
    print(f"[db] Using PostgreSQL")
else:
    _DB_PATH = _DATABASE_URL.replace("sqlite:///./", "").replace("sqlite:///", "")
    os.makedirs(os.path.dirname(os.path.abspath(_DB_PATH)), exist_ok=True)
    print(f"[db] Using SQLite at {_DB_PATH}")


# ── PostgreSQL connection wrapper (looks like sqlite3 to the rest of the app) ─

class _PGConn:
    """Wraps a psycopg2 connection to mimic sqlite3's interface."""

    def __init__(self):
        self._conn = psycopg2.connect(_PG_DSN)
        self._cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        self._last_id = None

    # ── Core execute ──────────────────────────────────────────────────────────

    def execute(self, sql: str, params=()):
        sql = sql.replace("?", "%s")
        # last_insert_rowid() → use stored id from previous INSERT
        if "last_insert_rowid()" in sql:
            sql = sql.replace("last_insert_rowid()", str(self._last_id or 0))
        self._cur.execute(sql, params)
        # Track last inserted ID for SERIAL columns
        if sql.strip().upper().startswith("INSERT"):
            try:
                self._cur.execute("SELECT lastval()")
                row = self._cur.fetchone()
                self._last_id = row["lastval"] if row else None
            except Exception:
                pass
        return self

    def executescript(self, sql: str):
        """Run multiple semicolon-separated statements."""
        for stmt in sql.split(";"):
            stmt = stmt.strip()
            if stmt:
                try:
                    self._cur.execute(stmt)
                except psycopg2.errors.DuplicateTable:
                    self._conn.rollback()
                    # Re-open cursor after rollback
                    self._cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
                except Exception:
                    pass  # IF NOT EXISTS handles most cases

    # ── Fetch ─────────────────────────────────────────────────────────────────

    def fetchone(self):
        try:
            return self._cur.fetchone()
        except Exception:
            return None

    def fetchall(self):
        try:
            return self._cur.fetchall()
        except Exception:
            return []

    # ── Transaction ───────────────────────────────────────────────────────────

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        try:
            self._cur.close()
            self._conn.close()
        except Exception:
            pass

    # ── Utility ───────────────────────────────────────────────────────────────

    def __getitem__(self, key):
        return self._cur.fetchone()[key]


# ── Schema ────────────────────────────────────────────────────────────────────

_SCHEMA_SQLITE = """
    CREATE TABLE IF NOT EXISTS projects (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        tone TEXT DEFAULT 'casual',
        context TEXT DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER REFERENCES projects(id),
        phone TEXT NOT NULL UNIQUE,
        session_string TEXT DEFAULT '',
        status TEXT DEFAULT 'pending',
        personality TEXT DEFAULT '',
        job_description TEXT DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS chats (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER REFERENCES accounts(id),
        chat_id TEXT NOT NULL,
        chat_name TEXT DEFAULT '',
        type TEXT DEFAULT 'dm',
        last_message TEXT DEFAULT '',
        unread_count INTEGER DEFAULT 0,
        monitored INTEGER DEFAULT 1,
        UNIQUE(account_id, chat_id)
    );
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER REFERENCES chats(id),
        sender TEXT NOT NULL,
        text TEXT NOT NULL,
        timestamp TEXT NOT NULL,
        is_outgoing INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS ai_drafts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        message_id INTEGER REFERENCES messages(id),
        draft_text TEXT NOT NULL,
        category TEXT DEFAULT 'general',
        urgency TEXT DEFAULT 'medium',
        was_used INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS away_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER REFERENCES projects(id),
        account_id INTEGER REFERENCES accounts(id),
        chat_id TEXT NOT NULL,
        chat_name TEXT DEFAULT '',
        incoming_text TEXT NOT NULL,
        reply_text TEXT NOT NULL,
        category TEXT DEFAULT 'general',
        urgency TEXT DEFAULT 'medium',
        replied_at TEXT NOT NULL,
        reviewed INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS away_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER UNIQUE REFERENCES projects(id),
        away_until TEXT NOT NULL,
        enabled_at TEXT NOT NULL,
        target_mode TEXT DEFAULT 'all',
        target_chat_ids TEXT DEFAULT '[]'
    );
    INSERT OR IGNORE INTO projects (id, name, tone, context)
    VALUES (1, 'Default Project', 'casual', 'General team communication project');
"""

_SCHEMA_PG = """
    CREATE TABLE IF NOT EXISTS projects (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL,
        tone TEXT DEFAULT 'casual',
        context TEXT DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS accounts (
        id SERIAL PRIMARY KEY,
        project_id INTEGER REFERENCES projects(id),
        phone TEXT NOT NULL UNIQUE,
        session_string TEXT DEFAULT '',
        status TEXT DEFAULT 'pending',
        personality TEXT DEFAULT '',
        job_description TEXT DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS chats (
        id SERIAL PRIMARY KEY,
        account_id INTEGER REFERENCES accounts(id),
        chat_id TEXT NOT NULL,
        chat_name TEXT DEFAULT '',
        type TEXT DEFAULT 'dm',
        last_message TEXT DEFAULT '',
        unread_count INTEGER DEFAULT 0,
        monitored INTEGER DEFAULT 1,
        UNIQUE(account_id, chat_id)
    );
    CREATE TABLE IF NOT EXISTS messages (
        id SERIAL PRIMARY KEY,
        chat_id INTEGER REFERENCES chats(id),
        sender TEXT NOT NULL,
        text TEXT NOT NULL,
        timestamp TEXT NOT NULL,
        is_outgoing INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS ai_drafts (
        id SERIAL PRIMARY KEY,
        message_id INTEGER REFERENCES messages(id),
        draft_text TEXT NOT NULL,
        category TEXT DEFAULT 'general',
        urgency TEXT DEFAULT 'medium',
        was_used INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS away_log (
        id SERIAL PRIMARY KEY,
        project_id INTEGER REFERENCES projects(id),
        account_id INTEGER REFERENCES accounts(id),
        chat_id TEXT NOT NULL,
        chat_name TEXT DEFAULT '',
        incoming_text TEXT NOT NULL,
        reply_text TEXT NOT NULL,
        category TEXT DEFAULT 'general',
        urgency TEXT DEFAULT 'medium',
        replied_at TEXT NOT NULL,
        reviewed INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS away_sessions (
        id SERIAL PRIMARY KEY,
        project_id INTEGER UNIQUE REFERENCES projects(id),
        away_until TEXT NOT NULL,
        enabled_at TEXT NOT NULL,
        target_mode TEXT DEFAULT 'all',
        target_chat_ids TEXT DEFAULT '[]'
    );
    INSERT INTO projects (id, name, tone, context)
    VALUES (1, 'Default Project', 'casual', 'General team communication project')
    ON CONFLICT (id) DO NOTHING;
"""


def init_db():
    with get_conn() as conn:
        if USE_POSTGRES:
            conn.executescript(_SCHEMA_PG)
        else:
            conn.executescript(_SCHEMA_SQLITE)


# ── Connection context manager ────────────────────────────────────────────────

@contextmanager
def get_conn():
    if USE_POSTGRES:
        conn = _PGConn()
    else:
        conn = sqlite3.connect(_DB_PATH)
        conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Helpers ───────────────────────────────────────────────────────────────────

def row_to_dict(row):
    return dict(row) if row else None


def rows_to_list(rows):
    return [dict(r) for r in rows]
