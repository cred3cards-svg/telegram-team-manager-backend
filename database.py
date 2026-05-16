import sqlite3
import os
from contextlib import contextmanager
from cryptography.fernet import Fernet

DB_PATH = os.getenv("DATABASE_URL", "app.db").replace("sqlite:///./", "")

# Session string encryption — key must be stable across restarts
_raw_key = os.getenv("SESSION_ENCRYPTION_KEY", "")
if _raw_key:
    # Re-add padding that env vars / copy-paste sometimes strip
    _key_bytes = _raw_key.encode() if isinstance(_raw_key, str) else _raw_key
    _key_bytes += b"=" * (-len(_key_bytes) % 4)
    _fernet = Fernet(_key_bytes)
else:
    # Fallback: derive from API hash so it's at least consistent per deployment
    import base64, hashlib
    _seed = os.getenv("TELEGRAM_API_HASH", "fallback-key-change-me")
    _fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(_seed.encode()).digest()))


def encrypt_session(session_string: str) -> str:
    """Encrypt before storing in DB."""
    if not session_string:
        return ""
    return _fernet.encrypt(session_string.encode()).decode()


def decrypt_session(encrypted: str) -> str:
    """Decrypt when reading from DB."""
    if not encrypted:
        return ""
    try:
        return _fernet.decrypt(encrypted.encode()).decode()
    except Exception:
        # Already plaintext (legacy rows before encryption was added)
        return encrypted


def init_db():
    with get_conn() as conn:
        conn.executescript("""
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
        """)


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
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
