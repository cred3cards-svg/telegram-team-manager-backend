import asyncio
import json
import os
from datetime import datetime
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from telethon import events
from telethon.tl.types import User, Channel, Chat
import anthropic
from database import get_conn, rows_to_list, row_to_dict
from accounts import get_client_for_account, _clients

router = APIRouter()

# Connected WebSocket clients: project_id -> list of WebSocket
_ws_clients: dict[int, list[WebSocket]] = {}


class SendMessageRequest(BaseModel):
    account_id: int
    chat_id: str
    text: str


def _upsert_chat(conn, account_id: int, chat_id: str, chat_name: str, chat_type: str, last_message: str):
    conn.execute(
        """INSERT INTO chats (account_id, chat_id, chat_name, type, last_message, unread_count)
           VALUES (?,?,?,?,?,1)
           ON CONFLICT(account_id, chat_id) DO UPDATE SET
             last_message=excluded.last_message,
             unread_count=unread_count+1,
             chat_name=excluded.chat_name""",
        (account_id, chat_id, chat_name, chat_type, last_message),
    )
    return conn.execute(
        "SELECT * FROM chats WHERE account_id=? AND chat_id=?", (account_id, chat_id)
    ).fetchone()


def _insert_message(conn, chat_row_id: int, sender: str, text: str, is_outgoing: bool):
    ts = datetime.utcnow().isoformat()
    conn.execute(
        "INSERT INTO messages (chat_id, sender, text, timestamp, is_outgoing) VALUES (?,?,?,?,?)",
        (chat_row_id, sender, text, ts, 1 if is_outgoing else 0),
    )
    row = conn.execute(
        "SELECT id FROM messages WHERE chat_id=? AND timestamp=? AND sender=? ORDER BY id DESC LIMIT 1",
        (chat_row_id, ts, sender),
    ).fetchone()
    return row["id"] if row else None


async def _broadcast(project_id: int, payload: dict):
    dead = []
    for ws in _ws_clients.get(project_id, []):
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _ws_clients[project_id].remove(ws)


async def register_event_handlers(account_id: int, project_id: int):
    """Attach Telethon new-message handler to a connected client."""
    with get_conn() as conn:
        row = conn.execute("SELECT phone FROM accounts WHERE id=?", (account_id,)).fetchone()
    if not row:
        return
    phone = row["phone"]
    client = _clients.get(phone)
    if not client:
        return

    @client.on(events.NewMessage(incoming=True))
    async def handler(event):
        msg = event.message
        if not msg.text:
            return

        sender_entity = await event.get_sender()
        chat_entity = await event.get_chat()

        # Ignore bots entirely — no storage, no reply
        if getattr(sender_entity, "bot", False):
            return

        is_group = isinstance(chat_entity, (Channel, Chat))
        chat_type = "group" if is_group else "dm"
        chat_id = str(chat_entity.id)
        chat_name = getattr(chat_entity, "title", None) or getattr(chat_entity, "first_name", "Unknown")
        sender_name = getattr(sender_entity, "first_name", None) or getattr(sender_entity, "username", "Unknown")

        # For groups, only store if monitored
        with get_conn() as conn:
            existing_chat = conn.execute(
                "SELECT * FROM chats WHERE account_id=? AND chat_id=?", (account_id, chat_id)
            ).fetchone()

            if is_group and existing_chat and not existing_chat["monitored"]:
                return

            chat_row = _upsert_chat(conn, account_id, chat_id, chat_name, chat_type, msg.text[:100])
            msg_id = _insert_message(conn, chat_row["id"], sender_name, msg.text, False)

        await _broadcast(project_id, {
            "type": "new_message",
            "account_id": account_id,
            "chat_id": chat_id,
            "chat_name": chat_name,
            "chat_type": chat_type,
            "sender": sender_name,
            "text": msg.text,
            "message_id": msg_id,
            "timestamp": msg.date.isoformat(),
        })

        # Away mode: auto-reply to both DMs and groups
        asyncio.create_task(
            _maybe_away_reply(client, account_id, project_id, chat_entity, chat_id, msg.text, msg_id, chat_type)
        )



async def _maybe_away_reply(client, account_id: int, project_id: int, chat_entity, chat_id: str, message_text: str, msg_id: int, chat_type: str = "dm"):
    from away import is_away_for_chat
    if not is_away_for_chat(project_id, account_id, chat_id):
        return

    # Only auto-reply once per chat per away session — check last outgoing message time vs away_until
    with get_conn() as conn:
        away_row = conn.execute("SELECT enabled_at FROM away_sessions WHERE project_id=?", (project_id,)).fetchone()
        if not away_row:
            return
        enabled_at = away_row["enabled_at"]

        chat_row = conn.execute("SELECT * FROM chats WHERE account_id=? AND chat_id=?", (account_id, chat_id)).fetchone()
        if not chat_row:
            return

        # Cap auto-replies at 10 per chat per away session
        reply_count = conn.execute(
            """SELECT COUNT(*) as n FROM messages WHERE chat_id=? AND is_outgoing=1 AND timestamp >= ?""",
            (chat_row["id"], enabled_at),
        ).fetchone()["n"]

    if reply_count >= 10:
        return

    # Fetch project context for AI
    with get_conn() as conn:
        project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        recent_msgs = conn.execute(
            "SELECT sender, text FROM messages WHERE chat_id=? ORDER BY timestamp DESC LIMIT 3",
            (chat_row["id"],),
        ).fetchall()

    project = dict(project) if project else {}
    history = [{"sender": r["sender"], "text": r["text"]} for r in reversed(recent_msgs)]

    # Generate AI draft using shared build_draft() so persona is respected
    try:
        from ai import build_draft, parse_ai_response
        system_prompt = build_draft(
            message_text=message_text,
            chat_history=history,
            chat_type=chat_type,
        )
        ai_client = anthropic.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
        response = await ai_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            system=system_prompt,
            messages=[{"role": "user", "content": message_text}],
        )
        print(f"[away] Tokens used: {response.usage.input_tokens} in, {response.usage.output_tokens} out")
        data = parse_ai_response(response.content[0].text)
        draft_text = data.get("draft", "")
    except Exception as e:
        print(f"[away] AI draft failed: {e}")
        return

    if not draft_text:
        return

    # Send the reply
    try:
        await client.send_message(chat_entity, draft_text)
    except Exception as e:
        print(f"[away] Send failed: {e}")
        return

    # Store as outgoing message, log the away reply
    chat_name_str = getattr(chat_entity, "title", None) or getattr(chat_entity, "first_name", "") or chat_id
    with get_conn() as conn:
        chat_row = conn.execute("SELECT * FROM chats WHERE account_id=? AND chat_id=?", (account_id, chat_id)).fetchone()
        if chat_row:
            _insert_message(conn, chat_row["id"], "You", draft_text, True)
            conn.execute(
                "INSERT INTO ai_drafts (message_id, draft_text, category, urgency, was_used) VALUES (?,?,?,?,1)",
                (msg_id, draft_text, data.get("category", "general"), data.get("urgency", "medium")),
            )
            conn.execute("UPDATE chats SET last_message=? WHERE id=?", (draft_text[:200], chat_row["id"]))
        conn.execute(
            """INSERT INTO away_log
               (project_id, account_id, chat_id, chat_name, incoming_text, reply_text, category, urgency, replied_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (project_id, account_id, chat_id, chat_name_str, message_text, draft_text,
             data.get("category", "general"), data.get("urgency", "medium"),
             datetime.utcnow().isoformat()),
        )

    await _broadcast(project_id, {
        "type": "away_reply_sent",
        "account_id": account_id,
        "chat_id": chat_id,
        "text": draft_text,
        "timestamp": datetime.utcnow().isoformat(),
    })
    print(f"[away] Auto-replied to {chat_id}: {draft_text[:60]}...")


@router.get("/inbox/{project_id}")
async def get_inbox(project_id: int):
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT c.*, a.phone, a.project_id
               FROM chats c
               JOIN accounts a ON c.account_id = a.id
               WHERE a.project_id=?
               ORDER BY c.unread_count DESC, c.last_message DESC""",
            (project_id,),
        ).fetchall()
    return rows_to_list(rows)


@router.get("/chat/{account_id}/{chat_id}")
async def get_chat_history(account_id: int, chat_id: str):
    with get_conn() as conn:
        chat_row = conn.execute(
            "SELECT * FROM chats WHERE account_id=? AND chat_id=?", (account_id, chat_id)
        ).fetchone()

    # Check if we have messages stored
    has_messages = False
    if chat_row:
        with get_conn() as conn:
            count = conn.execute(
                "SELECT COUNT(*) as n FROM messages WHERE chat_id=?", (chat_row["id"],)
            ).fetchone()["n"]
            has_messages = count > 0

    if not chat_row or not has_messages:
        # Fetch live from Telegram and cache
        client = await get_client_for_account(account_id)
        try:
            entity = await client.get_entity(int(chat_id))
            msgs = await client.get_messages(entity, limit=50)
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))

        chat_name = getattr(entity, "title", None) or getattr(entity, "first_name", "Unknown")
        is_group = isinstance(entity, (Channel, Chat))
        chat_type = "group" if is_group else "dm"

        result = []
        with get_conn() as conn:
            chat_row = _upsert_chat(conn, account_id, chat_id, chat_name, chat_type, "")
            for m in reversed(msgs):
                if m.text:
                    sender_name = "You" if m.out else (getattr(m.sender, "first_name", "") or "Unknown")
                    _insert_message(conn, chat_row["id"], sender_name, m.text, m.out)
                    result.append({"sender": sender_name, "text": m.text, "timestamp": m.date.isoformat(), "is_outgoing": m.out})
        return result

    with get_conn() as conn:
        # Mark as read
        conn.execute(
            "UPDATE chats SET unread_count=0 WHERE account_id=? AND chat_id=?",
            (account_id, chat_id),
        )
        msgs = conn.execute(
            "SELECT * FROM messages WHERE chat_id=? ORDER BY timestamp ASC",
            (chat_row["id"],),
        ).fetchall()
    return rows_to_list(msgs)


@router.post("/messages/send")
async def send_message(req: SendMessageRequest):
    client = await get_client_for_account(req.account_id)

    try:
        entity = await client.get_entity(int(req.chat_id))
        await client.send_message(entity, req.text)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    with get_conn() as conn:
        chat_row = conn.execute(
            "SELECT * FROM chats WHERE account_id=? AND chat_id=?",
            (req.account_id, req.chat_id),
        ).fetchone()
        if chat_row:
            msg_id = _insert_message(conn, chat_row["id"], "You", req.text, True)
            conn.execute(
                "UPDATE chats SET last_message=? WHERE id=?",
                (req.text[:100], chat_row["id"]),
            )
        else:
            msg_id = None

    return {"status": "sent", "message_id": msg_id}


@router.websocket("/ws/{project_id}")
async def websocket_endpoint(websocket: WebSocket, project_id: int):
    await websocket.accept()
    _ws_clients.setdefault(project_id, []).append(websocket)
    try:
        while True:
            await websocket.receive_text()  # keep alive
    except WebSocketDisconnect:
        if project_id in _ws_clients:
            try:
                _ws_clients[project_id].remove(websocket)
            except ValueError:
                pass
