from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from telethon.tl.functions.channels import JoinChannelRequest, LeaveChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest
from telethon.tl.types import Channel, Chat
from database import get_conn, rows_to_list, row_to_dict
from accounts import get_client_for_account

router = APIRouter()


class JoinGroupRequest(BaseModel):
    account_id: int
    group_link: str


class LeaveGroupRequest(BaseModel):
    account_id: int
    group_id: str


class ToggleMonitorRequest(BaseModel):
    account_id: int
    chat_id: str
    monitored: bool


@router.post("/groups/join")
async def join_group(req: JoinGroupRequest):
    client = await get_client_for_account(req.account_id)

    link = req.group_link.strip()
    try:
        if "joinchat" in link or "+" in link:
            # Private invite link
            hash_part = link.split("+")[-1] if "+" in link else link.split("joinchat/")[-1]
            result = await client(ImportChatInviteRequest(hash_part))
            entity = result.chats[0]
        else:
            # Public username
            username = link.split("/")[-1].lstrip("@")
            entity = await client.get_entity(username)
            await client(JoinChannelRequest(entity))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to join: {str(e)}")

    chat_name = getattr(entity, "title", str(entity.id))
    chat_id = str(entity.id)

    with get_conn() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO chats (account_id, chat_id, chat_name, type, monitored)
               VALUES (?,?,?,'group',1)""",
            (req.account_id, chat_id, chat_name),
        )
        row = conn.execute(
            "SELECT * FROM chats WHERE account_id=? AND chat_id=?",
            (req.account_id, chat_id),
        ).fetchone()

    return {"status": "joined", "chat": row_to_dict(row)}


@router.post("/groups/leave")
async def leave_group(req: LeaveGroupRequest):
    client = await get_client_for_account(req.account_id)

    try:
        entity = await client.get_entity(int(req.group_id))
        await client(LeaveChannelRequest(entity))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to leave: {str(e)}")

    with get_conn() as conn:
        conn.execute(
            "DELETE FROM chats WHERE account_id=? AND chat_id=? AND type='group'",
            (req.account_id, req.group_id),
        )

    return {"status": "left"}


@router.get("/groups/list")
async def list_groups(account_id: int):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM chats WHERE account_id=? AND type='group'", (account_id,)
        ).fetchall()
    return rows_to_list(rows)


@router.get("/groups/messages")
async def get_group_messages(account_id: int, group_id: str, limit: int = 50):
    client = await get_client_for_account(account_id)

    try:
        entity = await client.get_entity(int(group_id))
        msgs = await client.get_messages(entity, limit=limit)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    result = []
    for m in reversed(msgs):
        if m.text:
            sender = "You" if m.out else (getattr(m.sender, "first_name", "") or "Unknown")
            result.append({
                "sender": sender,
                "text": m.text,
                "timestamp": m.date.isoformat(),
                "is_outgoing": m.out,
            })
    return result


@router.post("/groups/toggle-monitor")
async def toggle_monitor(req: ToggleMonitorRequest):
    with get_conn() as conn:
        conn.execute(
            "UPDATE chats SET monitored=? WHERE account_id=? AND chat_id=?",
            (1 if req.monitored else 0, req.account_id, req.chat_id),
        )
    return {"status": "updated", "monitored": req.monitored}
