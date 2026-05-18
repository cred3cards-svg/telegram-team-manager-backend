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


def encrypt_session(s: str) -> str:
    return _fernet.encrypt(s.encode()).decode() if s else ""


def decrypt_session(s: str) -> str:
    if not s:
        return ""
    try:
        return _fernet.decrypt(s.encode()).decode()
    except Exception:
        return s  # legacy plaintext


# ── Backend detection ─────────────────────────────────────────────────────────

_DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data/app.db")
USE_POSTGRES = _DATABASE_URL.startswith("postgres")

if USE_POSTGRES:
    import psycopg2
    import psycopg2.extras
    _PG_DSN = _DATABASE_URL.replace("postgres://", "postgresql://", 1)
    print("[db] Backend: PostgreSQL")
else:
    _DB_PATH = _DATABASE_URL.replace("sqlite:///./", "").replace("sqlite:///", "")
    os.makedirs(os.path.dirname(os.path.abspath(_DB_PATH)), exist_ok=True)
    print(f"[db] Backend: SQLite at {_DB_PATH}")


# ── Schema statements (each runs independently) ───────────────────────────────

_TABLES = [
    """CREATE TABLE IF NOT EXISTS projects (
        id {serial} PRIMARY KEY,
        name TEXT NOT NULL,
        tone TEXT DEFAULT 'casual',
        context TEXT DEFAULT ''
    )""",
    """CREATE TABLE IF NOT EXISTS accounts (
        id {serial} PRIMARY KEY,
        project_id INTEGER REFERENCES projects(id),
        phone TEXT NOT NULL UNIQUE,
        session_string TEXT DEFAULT '',
        status TEXT DEFAULT 'pending',
        personality TEXT DEFAULT '',
        job_description TEXT DEFAULT ''
    )""",
    """CREATE TABLE IF NOT EXISTS chats (
        id {serial} PRIMARY KEY,
        account_id INTEGER REFERENCES accounts(id),
        chat_id TEXT NOT NULL,
        chat_name TEXT DEFAULT '',
        type TEXT DEFAULT 'dm',
        last_message TEXT DEFAULT '',
        unread_count INTEGER DEFAULT 0,
        monitored INTEGER DEFAULT 1,
        UNIQUE(account_id, chat_id)
    )""",
    """CREATE TABLE IF NOT EXISTS messages (
        id {serial} PRIMARY KEY,
        chat_id INTEGER REFERENCES chats(id),
        sender TEXT NOT NULL,
        text TEXT NOT NULL,
        timestamp TEXT NOT NULL,
        is_outgoing INTEGER DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS ai_drafts (
        id {serial} PRIMARY KEY,
        message_id INTEGER REFERENCES messages(id),
        draft_text TEXT NOT NULL,
        category TEXT DEFAULT 'general',
        urgency TEXT DEFAULT 'medium',
        was_used INTEGER DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS away_log (
        id {serial} PRIMARY KEY,
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
    )""",
    """CREATE TABLE IF NOT EXISTS away_sessions (
        id {serial} PRIMARY KEY,
        project_id INTEGER UNIQUE REFERENCES projects(id),
        away_until TEXT NOT NULL,
        enabled_at TEXT NOT NULL,
        target_mode TEXT DEFAULT 'all',
        target_chat_ids TEXT DEFAULT '[]'
    )""",
    """CREATE TABLE IF NOT EXISTS suggested_groups (
        id {serial} PRIMARY KEY,
        group_id TEXT NOT NULL UNIQUE,
        username TEXT DEFAULT '',
        name TEXT NOT NULL,
        description TEXT DEFAULT '',
        members INTEGER DEFAULT 0,
        online_members INTEGER DEFAULT 0,
        category TEXT DEFAULT 'general',
        discovered_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS project_groups (
        id {serial} PRIMARY KEY,
        project_id INTEGER REFERENCES projects(id),
        account_id INTEGER,
        group_id TEXT NOT NULL,
        group_name TEXT DEFAULT '',
        username TEXT DEFAULT '',
        category TEXT DEFAULT 'general',
        monitored INTEGER DEFAULT 1,
        joined_at TEXT NOT NULL,
        UNIQUE(project_id, group_id)
    )""",
]


def init_db():
    """Create all tables. Each statement runs in its own transaction so one
    failure never blocks the rest."""
    if USE_POSTGRES:
        serial = "SERIAL"
        seed_sql = """INSERT INTO projects (id, name, tone, context)
                      VALUES (1, 'Default Project', 'casual', 'General team communication project')
                      ON CONFLICT (id) DO NOTHING"""
    else:
        serial = "INTEGER"
        seed_sql = """INSERT OR IGNORE INTO projects (id, name, tone, context)
                      VALUES (1, 'Default Project', 'casual', 'General team communication project')"""

    for tpl in _TABLES:
        sql = tpl.replace("{serial}", serial)
        try:
            with get_conn() as conn:
                conn.execute(sql)
        except Exception as e:
            print(f"[db] init warning: {e}")

    try:
        with get_conn() as conn:
            conn.execute(seed_sql)
    except Exception as e:
        print(f"[db] seed warning: {e}")

    # Add new columns to existing tables (safe to re-run)
    _migrate_columns()

    # Backfill project_groups from existing chats so old joins aren't lost
    _backfill_project_groups()

    # Log account count so we know the DB is alive
    try:
        with get_conn() as conn:
            n = conn.execute("SELECT COUNT(*) as n FROM accounts").fetchone()["n"]
            print(f"[db] init complete — {n} accounts in database")
    except Exception as e:
        print(f"[db] count check failed: {e}")


def _migrate_columns():
    """Add new columns introduced after initial deploy. Safe to re-run — errors are suppressed."""
    migrations = [
        "ALTER TABLE projects ADD COLUMN system_prompt TEXT DEFAULT ''",
        "ALTER TABLE chats ADD COLUMN write_restricted INTEGER DEFAULT 0",
        "ALTER TABLE project_groups ADD COLUMN write_restricted INTEGER DEFAULT 0",
    ]
    for sql in migrations:
        try:
            with get_conn() as conn:
                conn.execute(sql)
        except Exception:
            pass  # column already exists — fine


def _backfill_project_groups():
    """One-time migration: copy any chats (type=group) not yet in project_groups."""
    import datetime as _dt
    now = _dt.datetime.utcnow().isoformat()
    try:
        with get_conn() as conn:
            rows = conn.execute(
                """SELECT c.chat_id, c.chat_name, c.account_id, c.monitored, a.project_id
                   FROM chats c
                   JOIN accounts a ON c.account_id = a.id
                   WHERE c.type = 'group'"""
            ).fetchall()
        if not rows:
            return
        with get_conn() as conn:
            for r in rows:
                try:
                    conn.execute(
                        """INSERT INTO project_groups
                               (project_id, account_id, group_id, group_name, monitored, joined_at)
                           VALUES (?,?,?,?,?,?)
                           ON CONFLICT(project_id, group_id) DO NOTHING""",
                        (r["project_id"], r["account_id"], r["chat_id"],
                         r["chat_name"], r["monitored"], now),
                    )
                except Exception:
                    pass
        print(f"[db] backfilled {len(rows)} group(s) into project_groups")
    except Exception as e:
        print(f"[db] backfill warning: {e}")


# ── PostgreSQL row wrapper (makes psycopg2 rows behave like sqlite3.Row) ──────

class _PGConn:
    def __init__(self):
        self._conn = psycopg2.connect(_PG_DSN)
        self._conn.autocommit = False
        self._cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    def execute(self, sql: str, params=()):
        sql = sql.replace("?", "%s")
        self._cur.execute(sql, params)
        return self

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


def row_to_dict(row):
    return dict(row) if row else None


def rows_to_list(rows):
    return [dict(r) for r in rows]
