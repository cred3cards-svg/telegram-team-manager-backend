from datetime import datetime
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


def _upsert_project_group(conn, project_id: int, account_id: int, group_id: str,
                           group_name: str, username: str = "", category: str = "general"):
    """Write/update the project-level group record (persists across account changes)."""
    conn.execute(
        """INSERT INTO project_groups
               (project_id, account_id, group_id, group_name, username, category, monitored, joined_at)
           VALUES (?,?,?,?,?,?,1,?)
           ON CONFLICT(project_id, group_id) DO UPDATE SET
             account_id=EXCLUDED.account_id,
             group_name=EXCLUDED.group_name,
             username=COALESCE(NULLIF(EXCLUDED.username,''), project_groups.username)""",
        (project_id, account_id, group_id, group_name, username, category,
         datetime.utcnow().isoformat()),
    )


@router.post("/groups/join")
async def join_group(req: JoinGroupRequest):
    client = await get_client_for_account(req.account_id)

    link = req.group_link.strip()
    try:
        if "joinchat" in link or link.startswith("+") or (
            "t.me/+" in link
        ):
            hash_part = link.split("+")[-1] if "+" in link else link.split("joinchat/")[-1]
            result = await client(ImportChatInviteRequest(hash_part))
            entity = result.chats[0]
        else:
            username = link.split("/")[-1].lstrip("@")
            entity = await client.get_entity(username)
            await client(JoinChannelRequest(entity))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to join: {str(e)}")

    chat_name = getattr(entity, "title", str(entity.id))
    chat_id = str(entity.id)
    username = getattr(entity, "username", "") or ""

    with get_conn() as conn:
        # Get project_id for this account
        acc_row = conn.execute("SELECT project_id FROM accounts WHERE id=?", (req.account_id,)).fetchone()
        project_id = acc_row["project_id"] if acc_row else None

        # Write to chats (account-level, for messaging)
        conn.execute(
            """INSERT INTO chats (account_id, chat_id, chat_name, type, monitored)
               VALUES (?,?,?,'group',1)
               ON CONFLICT(account_id, chat_id) DO NOTHING""",
            (req.account_id, chat_id, chat_name),
        )
        row = conn.execute(
            "SELECT * FROM chats WHERE account_id=? AND chat_id=?",
            (req.account_id, chat_id),
        ).fetchone()

        # Write to project_groups (project-level, persists forever)
        if project_id:
            _upsert_project_group(conn, project_id, req.account_id, chat_id, chat_name, username)

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
        acc_row = conn.execute("SELECT project_id FROM accounts WHERE id=?", (req.account_id,)).fetchone()
        project_id = acc_row["project_id"] if acc_row else None

        conn.execute(
            "DELETE FROM chats WHERE account_id=? AND chat_id=? AND type='group'",
            (req.account_id, req.group_id),
        )
        if project_id:
            conn.execute(
                "DELETE FROM project_groups WHERE project_id=? AND group_id=?",
                (project_id, req.group_id),
            )

    return {"status": "left"}


@router.get("/groups/list")
async def list_groups(project_id: int):
    """List all groups for a project — persists even if managing account changes."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT pg.*, a.phone as account_phone
               FROM project_groups pg
               LEFT JOIN accounts a ON pg.account_id = a.id
               WHERE pg.project_id=?
               ORDER BY pg.group_name""",
            (project_id,)
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
    val = 1 if req.monitored else 0
    with get_conn() as conn:
        conn.execute(
            "UPDATE chats SET monitored=? WHERE account_id=? AND chat_id=?",
            (val, req.account_id, req.chat_id),
        )
        acc_row = conn.execute("SELECT project_id FROM accounts WHERE id=?", (req.account_id,)).fetchone()
        if acc_row:
            conn.execute(
                "UPDATE project_groups SET monitored=? WHERE project_id=? AND group_id=?",
                (val, acc_row["project_id"], req.chat_id),
            )
    return {"status": "updated", "monitored": req.monitored}
