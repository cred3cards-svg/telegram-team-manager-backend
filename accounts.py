import os
import asyncio
import shutil
import tempfile
from datetime import datetime
from typing import List
from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from pydantic import BaseModel
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError
from telethon.tl.types import User, Channel, Chat
from database import get_conn, rows_to_list, row_to_dict, encrypt_session, decrypt_session

router = APIRouter()

API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")

# In-memory map: phone -> TelegramClient (kept alive)
_clients: dict[str, TelegramClient] = {}
# phone -> pending OTP state
_pending: dict[str, dict] = {}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _scrub(account: dict) -> dict:
    """Never expose session_string in API responses."""
    return {k: v for k, v in account.items() if k != "session_string"}


async def get_or_create_client(phone: str, session_string: str = "") -> TelegramClient:
    if phone in _clients and _clients[phone].is_connected():
        return _clients[phone]
    client = TelegramClient(
        StringSession(session_string), API_ID, API_HASH,
        connection_retries=-1,   # retry forever on drops
        retry_delay=5,           # 5s between retries
        auto_reconnect=True,
    )
    await client.connect()
    _clients[phone] = client
    return client


async def get_client_for_account(account_id: int) -> TelegramClient:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT phone, session_string FROM accounts WHERE id=?", (account_id,)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Account not found")
    acc = row_to_dict(row)
    session = decrypt_session(acc["session_string"])
    return await get_or_create_client(acc["phone"], session)


async def _activate_account(account_id: int, project_id: int, client: TelegramClient):
    """Common post-auth steps: sync dialogs + register event handler."""
    asyncio.create_task(_sync_dialogs(client, account_id))
    from inbox import register_event_handlers
    asyncio.create_task(register_event_handlers(account_id, project_id))


async def keepalive_all_sessions():
    """Background loop: ping every 4 min, reconnect + re-register if dropped."""
    from inbox import register_event_handlers
    while True:
        await asyncio.sleep(240)  # every 4 minutes
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT id, phone, session_string, project_id FROM accounts WHERE status='active'"
            ).fetchall()
        for r in rows_to_list(rows):
            phone = r["phone"]
            try:
                client = _clients.get(phone)
                if client and client.is_connected():
                    await client.get_me()   # lightweight ping
                    print(f"[keepalive] OK: {phone}")
                else:
                    print(f"[keepalive] Reconnecting {phone}...")
                    session = decrypt_session(r["session_string"])
                    client = await get_or_create_client(phone, session)
                    if await client.is_user_authorized():
                        await register_event_handlers(r["id"], r["project_id"])
                        asyncio.create_task(_sync_dialogs(client, r["id"]))
                        print(f"[keepalive] Reconnected {phone}")
                    else:
                        print(f"[keepalive] Session expired for {phone}")
            except Exception as e:
                print(f"[keepalive] Error for {phone}: {e}")


async def restore_all_sessions():
    from inbox import register_event_handlers
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, phone, session_string, project_id FROM accounts WHERE status='active'"
        ).fetchall()
    for r in rows_to_list(rows):
        if not r["session_string"]:
            continue
        try:
            session = decrypt_session(r["session_string"])
            client = await get_or_create_client(r["phone"], session)
            if await client.is_user_authorized():
                # Save refreshed session string back to DB
                fresh = client.session.save()
                if fresh != session:
                    with get_conn() as conn:
                        conn.execute(
                            "UPDATE accounts SET session_string=? WHERE phone=?",
                            (encrypt_session(fresh), r["phone"]),
                        )
                await register_event_handlers(r["id"], r["project_id"])
                asyncio.create_task(_sync_dialogs(client, r["id"]))
                print(f"[startup] Restored {r['phone']}")
            else:
                print(f"[startup] Session invalid for {r['phone']} — needs re-login")
                with get_conn() as conn:
                    conn.execute("UPDATE accounts SET status='pending' WHERE phone=?", (r["phone"],))
        except Exception as e:
            print(f"[startup] Failed to restore {r['phone']}: {e}")


async def _sync_dialogs(client: TelegramClient, account_id: int):
    try:
        dialogs = await client.get_dialogs(limit=100)
    except Exception as e:
        print(f"[accounts] Dialog sync failed: {e}")
        return
    with get_conn() as conn:
        for d in dialogs:
            entity = d.entity
            is_group = isinstance(entity, (Channel, Chat))
            chat_type = "group" if is_group else "dm"
            chat_id = str(entity.id)
            chat_name = getattr(entity, "title", None) or getattr(entity, "first_name", "") or "Unknown"
            last_msg = d.message.message if d.message and d.message.message else ""
            conn.execute(
                """INSERT INTO chats (account_id, chat_id, chat_name, type, last_message, unread_count)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(account_id, chat_id) DO UPDATE SET
                     chat_name=excluded.chat_name,
                     last_message=excluded.last_message""",
                (account_id, chat_id, chat_name, chat_type, last_msg[:200], d.unread_count or 0),
            )
    print(f"[accounts] Synced {len(dialogs)} dialogs for account {account_id}")


def _insert_account(conn, project_id: int, phone: str, session_string: str, status: str) -> dict:
    encrypted = encrypt_session(session_string)
    conn.execute(
        """INSERT INTO accounts (project_id, phone, session_string, status) VALUES (?,?,?,?)
           ON CONFLICT(phone) DO UPDATE SET
             project_id=EXCLUDED.project_id,
             session_string=EXCLUDED.session_string,
             status=EXCLUDED.status""",
        (project_id, phone, encrypted, status),
    )
    row = conn.execute("SELECT * FROM accounts WHERE phone=?", (phone,)).fetchone()
    return _scrub(row_to_dict(row))


# ── Method 1: OTP ────────────────────────────────────────────────────────────

class AddOTPRequest(BaseModel):
    phone: str
    project_id: int


class VerifyOTPRequest(BaseModel):
    phone: str
    otp_code: str
    password: str = ""


@router.post("/accounts/add/otp")
@router.post("/accounts/add")          # backward compat
async def add_account_otp(req: AddOTPRequest):
    if not API_ID or not API_HASH:
        raise HTTPException(status_code=500, detail="TELEGRAM_API_ID/HASH not configured")

    with get_conn() as conn:
        existing = conn.execute("SELECT id FROM accounts WHERE phone=?", (req.phone,)).fetchone()
        if existing:
            raise HTTPException(status_code=400, detail="Phone already added")
        conn.execute(
            "INSERT INTO accounts (project_id, phone, status) VALUES (?,?,?)",
            (req.project_id, req.phone, "pending"),
        )

    client = await get_or_create_client(req.phone)
    try:
        result = await client.send_code_request(req.phone)
    except Exception as e:
        with get_conn() as conn:
            conn.execute("DELETE FROM accounts WHERE phone=? AND status='pending'", (req.phone,))
        err = str(e)
        if "FloodWait" in err:
            import re
            secs = re.search(r"wait of (\d+)", err)
            wait = int(secs.group(1)) if secs else 0
            raise HTTPException(
                status_code=429,
                detail=f"Telegram rate limit: wait {wait//3600}h {(wait%3600)//60}m before requesting another OTP."
            )
        raise HTTPException(status_code=400, detail=err)

    _pending[req.phone] = {"phone_code_hash": result.phone_code_hash, "project_id": req.project_id}
    return {"status": "otp_sent", "phone": req.phone}


@router.post("/accounts/verify/otp")
@router.post("/accounts/verify")       # backward compat
async def verify_account_otp(req: VerifyOTPRequest):
    if req.phone not in _pending:
        raise HTTPException(status_code=400, detail="No pending OTP. Call /accounts/add/otp first.")

    client = await get_or_create_client(req.phone)
    phone_code_hash = _pending[req.phone]["phone_code_hash"]
    project_id = _pending[req.phone].get("project_id", 1)

    try:
        await client.sign_in(req.phone, req.otp_code, phone_code_hash=phone_code_hash)
    except SessionPasswordNeededError:
        if not req.password:
            raise HTTPException(status_code=400, detail="2FA password required")
        await client.sign_in(password=req.password)

    session_string = client.session.save()
    _pending.pop(req.phone, None)

    with get_conn() as conn:
        encrypted = encrypt_session(session_string)
        conn.execute(
            "UPDATE accounts SET session_string=?, status='active' WHERE phone=?",
            (encrypted, req.phone),
        )
        row = conn.execute("SELECT * FROM accounts WHERE phone=?", (req.phone,)).fetchone()
    account = _scrub(row_to_dict(row))
    await _activate_account(account["id"], account["project_id"], client)
    return {"status": "connected", "account": account}


# ── Method 2: Session String ──────────────────────────────────────────────────

class AddSessionRequest(BaseModel):
    session_string: str
    project_id: int


@router.post("/accounts/add/session")
async def add_account_session(req: AddSessionRequest):
    if not API_ID or not API_HASH:
        raise HTTPException(status_code=500, detail="TELEGRAM_API_ID/HASH not configured")

    client = TelegramClient(StringSession(req.session_string), API_ID, API_HASH)
    try:
        await client.connect()
        me = await client.get_me()
        if not me:
            raise HTTPException(status_code=400, detail="Invalid session string — could not authenticate")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Session validation failed: {e}")

    phone = getattr(me, "phone", None) or str(me.id)
    if not phone.startswith("+"):
        phone = f"+{phone}"

    with get_conn() as conn:
        existing = conn.execute("SELECT id FROM accounts WHERE phone=?", (phone,)).fetchone()
        if existing:
            raise HTTPException(status_code=400, detail=f"Account {phone} already added")
        account = _insert_account(conn, req.project_id, phone, req.session_string, "active")

    _clients[phone] = client
    await _activate_account(account["id"], req.project_id, client)
    return {"status": "connected", "account": account}


# ── Method 3: TData Folder ────────────────────────────────────────────────────

@router.post("/accounts/add/tdata")
async def add_account_tdata(
    project_id: int = Form(...),
    files: List[UploadFile] = File(...),
    paths: List[str] = Form(...),
):
    if not API_ID or not API_HASH:
        raise HTTPException(status_code=500, detail="TELEGRAM_API_ID/HASH not configured")

    tmp_dir = tempfile.mkdtemp(prefix="tdata_")
    try:
        # Reconstruct folder structure from uploaded files + their relative paths
        for upload, rel_path in zip(files, paths):
            dest = os.path.join(tmp_dir, rel_path)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            content = await upload.read()
            with open(dest, "wb") as f:
                f.write(content)

        # The tdata folder itself is the first path component
        tdata_root = os.path.join(tmp_dir, paths[0].split("/")[0])

        session_string = await _tdata_to_session(tdata_root)

        # Validate the converted session
        client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
        await client.connect()
        me = await client.get_me()
        if not me:
            raise HTTPException(status_code=400, detail="TData conversion succeeded but session is invalid")

        phone = getattr(me, "phone", None) or str(me.id)
        if not phone.startswith("+"):
            phone = f"+{phone}"

        with get_conn() as conn:
            existing = conn.execute("SELECT id FROM accounts WHERE phone=?", (phone,)).fetchone()
            if existing:
                raise HTTPException(status_code=400, detail=f"Account {phone} already added")
            account = _insert_account(conn, project_id, phone, session_string, "active")

        _clients[phone] = client
        await _activate_account(account["id"], project_id, client)
        return {"status": "connected", "method": "tdata", "account": account}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"TData import failed: {e}")
    finally:
        # Always delete tdata files immediately
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def _tdata_to_session(tdata_path: str) -> str:
    try:
        from opentele.td import TDesktop
        from opentele.api import UseCurrentSession
    except ImportError:
        raise HTTPException(status_code=500, detail="opentele not installed. Add it to requirements.txt and rebuild.")

    tdesk = TDesktop(tdata_path)
    if not tdesk.isLoaded():
        raise ValueError("TData folder could not be loaded — check the folder structure")

    client = await tdesk.ToTelethon(session=StringSession(), flag=UseCurrentSession)
    await client.connect()
    session_string = client.session.save()
    await client.disconnect()
    return session_string


# ── Shared endpoints ──────────────────────────────────────────────────────────

class RemoveRequest(BaseModel):
    account_id: int


@router.delete("/accounts/remove")
async def remove_account(req: RemoveRequest):
    with get_conn() as conn:
        row = conn.execute("SELECT phone FROM accounts WHERE id=?", (req.account_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Account not found")
        phone = row["phone"]
        conn.execute("DELETE FROM accounts WHERE id=?", (req.account_id,))

    if phone in _clients:
        try:
            await _clients[phone].disconnect()
        except Exception:
            pass
        del _clients[phone]

    return {"status": "removed"}


@router.get("/accounts/list")
async def list_accounts(project_id: int):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM accounts WHERE project_id=?", (project_id,)
        ).fetchall()
    return [_scrub(dict(r)) for r in rows]


@router.get("/accounts/{account_id}/persona")
async def get_persona(account_id: int):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, phone, personality, job_description FROM accounts WHERE id=?", (account_id,)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Account not found")
    return row_to_dict(row)


class PersonaRequest(BaseModel):
    personality: str = ""
    job_description: str = ""


@router.put("/accounts/{account_id}/persona")
async def update_persona(account_id: int, req: PersonaRequest):
    with get_conn() as conn:
        conn.execute(
            "UPDATE accounts SET personality=?, job_description=? WHERE id=?",
            (req.personality, req.job_description, account_id),
        )
        row = conn.execute(
            "SELECT id, phone, personality, job_description FROM accounts WHERE id=?", (account_id,)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Account not found")
    return row_to_dict(row)


@router.post("/accounts/sync/{account_id}")
async def sync_account_dialogs(account_id: int):
    client = await get_client_for_account(account_id)
    with get_conn() as conn:
        row = conn.execute("SELECT project_id FROM accounts WHERE id=?", (account_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Account not found")

    await _sync_dialogs(client, account_id)
    from inbox import register_event_handlers
    asyncio.create_task(register_event_handlers(account_id, row["project_id"]))

    with get_conn() as conn:
        count = conn.execute(
            "SELECT COUNT(*) as n FROM chats WHERE account_id=?", (account_id,)
        ).fetchone()["n"]

    return {"status": "synced", "chats_found": count}
