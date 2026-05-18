"""
Group Discovery Engine
Runs in background when accounts are idle.
Searches Telegram for public Indian groups (1000+ members),
stores suggestions, and exposes API for dashboard + one-click join.
"""
import asyncio
from datetime import datetime
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from telethon.tl.functions.contacts import SearchRequest
from telethon.tl.functions.channels import JoinChannelRequest, GetFullChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest
from telethon.tl.types import Channel, Chat
from database import get_conn, rows_to_list
from accounts import _clients, get_client_for_account

router = APIRouter()

# ── Keywords covering Indian group categories ─────────────────────────────────

KEYWORDS = [
    # Cricket
    "india cricket", "IPL", "cricket fans india", "cricket prediction",
    "cricket discussion india", "T20 india",
    # Bollywood / Entertainment
    "bollywood", "hindi movies", "desi memes", "indian movies",
    "bollywood gossip", "web series india",
    # Adult / 18+
    "desi adult", "indian adult", "18+ india", "desi hot",
    # General India chat
    "india chat", "indian group", "desi chat", "india talks",
    "india memes", "india news", "indian students",
    # Finance / Trading
    "india stocks", "indian trading", "nifty banknifty",
]

MIN_MEMBERS = 1000
DISCOVERY_INTERVAL = 6 * 3600   # run every 6 hours per account
_last_run: dict[str, float] = {}  # phone -> last discovery timestamp


# ── DB helpers ────────────────────────────────────────────────────────────────

def _init_discovery_table():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS suggested_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT NOT NULL,
                username TEXT DEFAULT '',
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                members INTEGER DEFAULT 0,
                online_members INTEGER DEFAULT 0,
                category TEXT DEFAULT 'general',
                discovered_at TEXT NOT NULL,
                UNIQUE(group_id)
            )
        """) if False else None  # handled in init_db
        # Try creating — harmless if exists
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS suggested_groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL UNIQUE,
                    username TEXT DEFAULT '',
                    name TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    members INTEGER DEFAULT 0,
                    online_members INTEGER DEFAULT 0,
                    category TEXT DEFAULT 'general',
                    discovered_at TEXT NOT NULL
                )
            """)
        except Exception:
            pass


def _categorise(name: str, description: str) -> str:
    text = (name + " " + description).lower()
    if any(w in text for w in ["cricket", "ipl", "t20", "prediction"]):
        return "cricket"
    if any(w in text for w in ["bollywood", "movie", "film", "web series", "hindi"]):
        return "bollywood"
    if any(w in text for w in ["adult", "18+", "hot", "desi"]):
        return "adult"
    if any(w in text for w in ["stock", "trading", "nifty", "finance"]):
        return "finance"
    if any(w in text for w in ["meme", "funny", "comedy"]):
        return "memes"
    return "general"


# ── Core discovery logic ──────────────────────────────────────────────────────

def _dynamic_keywords(account_id: int) -> list:
    """Merge base KEYWORDS with words from already-joined group names for targeted search."""
    try:
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT chat_name FROM chats WHERE account_id=? AND type='group' LIMIT 60",
                (account_id,)
            ).fetchall()
        stop = {"the", "and", "for", "chat", "group", "india", "indian", "with", "your",
                "this", "that", "from", "have", "only", "like", "more", "will", "into"}
        extra = set()
        for r in rows:
            for word in r["chat_name"].lower().split():
                if len(word) > 3 and word not in stop:
                    extra.add(word)
        combined = list(set(KEYWORDS + list(extra)))
        return combined[:40]
    except Exception:
        return KEYWORDS


def _already_joined_ids(account_id: int) -> set:
    """Return chat_ids already joined by this account so we don't re-suggest them."""
    try:
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT chat_id FROM chats WHERE account_id=? AND type='group'", (account_id,)
            ).fetchall()
        return {r["chat_id"] for r in rows}
    except Exception:
        return set()


async def _discover_for_client(client, phone: str, account_id: int = 0):
    seen_ids: set[str] = set()
    found = []
    keywords = _dynamic_keywords(account_id)
    already_joined = _already_joined_ids(account_id)

    for keyword in keywords:
        try:
            result = await client(SearchRequest(q=keyword, limit=50))
            for chat in result.chats:
                # Only supergroups (megagroup=True) — skip channels
                if not isinstance(chat, Channel):
                    continue
                if not getattr(chat, "megagroup", False):
                    continue
                if getattr(chat, "restricted", False) or getattr(chat, "private", False):
                    continue
                members = getattr(chat, "participants_count", 0) or 0
                if members < MIN_MEMBERS:
                    continue
                gid = str(chat.id)
                if gid in seen_ids or gid in already_joined:
                    continue
                seen_ids.add(gid)
                found.append(chat)
            await asyncio.sleep(2)   # be polite to Telegram rate limits
        except Exception as e:
            print(f"[discovery] keyword '{keyword}' failed: {e}")
            await asyncio.sleep(5)

    # Fetch online count for top 20 groups (GetFullChannelRequest is heavier)
    enriched = []
    for chat in found[:40]:
        try:
            full = await client(GetFullChannelRequest(chat))
            online = getattr(full.full_chat, "online_count", 0) or 0
        except Exception:
            online = 0

        username = getattr(chat, "username", "") or ""
        description = getattr(
            getattr(chat, "about", None) or full.full_chat if "full" in dir() else chat,
            "about", ""
        ) or ""
        enriched.append({
            "group_id": str(chat.id),
            "username": username,
            "name": chat.title,
            "description": description[:200],
            "members": getattr(chat, "participants_count", 0) or 0,
            "online_members": online,
            "category": _categorise(chat.title, description),
            "discovered_at": datetime.utcnow().isoformat(),
        })
        await asyncio.sleep(1)

    # Upsert into DB
    with get_conn() as conn:
        for g in enriched:
            try:
                conn.execute(
                    """INSERT INTO suggested_groups
                       (group_id, username, name, description, members, online_members, category, discovered_at)
                       VALUES (?,?,?,?,?,?,?,?)
                       ON CONFLICT(group_id) DO UPDATE SET
                         members=EXCLUDED.members,
                         online_members=EXCLUDED.online_members,
                         discovered_at=EXCLUDED.discovered_at""",
                    (g["group_id"], g["username"], g["name"], g["description"],
                     g["members"], g["online_members"], g["category"], g["discovered_at"]),
                )
            except Exception:
                pass

    print(f"[discovery] {phone}: found {len(enriched)} groups, saved to DB")


async def run_discovery_loop():
    """Background loop — runs discovery for each active account every 6 hours."""
    import time
    _init_discovery_table()
    await asyncio.sleep(30)   # let startup settle first

    while True:
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT id, phone FROM accounts WHERE status='active'"
            ).fetchall()


        for r in rows:
            phone = r["phone"]
            now = time.time()
            if now - _last_run.get(phone, 0) < DISCOVERY_INTERVAL:
                continue
            client = _clients.get(phone)
            if not client or not client.is_connected():
                continue
            _last_run[phone] = now
            print(f"[discovery] Starting group discovery for {phone}...")
            asyncio.create_task(_discover_for_client(client, phone, r["id"]))

        await asyncio.sleep(3600)   # check every hour, run every 6h per account


# ── API endpoints ─────────────────────────────────────────────────────────────

@router.get("/discovery/suggested")
async def get_suggested_groups(category: str = "all", limit: int = 50):
    with get_conn() as conn:
        if category == "all":
            rows = conn.execute(
                "SELECT * FROM suggested_groups ORDER BY members DESC LIMIT ?",
                (limit,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM suggested_groups WHERE category=? ORDER BY members DESC LIMIT ?",
                (category, limit)
            ).fetchall()
    return rows_to_list(rows)


class JoinSuggestedRequest(BaseModel):
    account_id: int
    group_id: str


@router.post("/discovery/join")
async def join_suggested_group(req: JoinSuggestedRequest):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM suggested_groups WHERE group_id=?", (req.group_id,)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Group not found in suggestions")

    group = dict(row)
    client = await get_client_for_account(req.account_id)

    try:
        if group["username"]:
            entity = await client.get_entity(group["username"])
        else:
            entity = await client.get_entity(int(group["group_id"]))
        await client(JoinChannelRequest(entity))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to join: {e}")

    chat_name = group["name"]
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO chats (account_id, chat_id, chat_name, type, monitored)
               VALUES (?,?,?,'group',1)
               ON CONFLICT(account_id, chat_id) DO NOTHING""",
            (req.account_id, group["group_id"], chat_name),
        )
        # Persist at project level so it survives account changes
        acc_row = conn.execute("SELECT project_id FROM accounts WHERE id=?", (req.account_id,)).fetchone()
        if acc_row:
            from datetime import datetime
            conn.execute(
                """INSERT INTO project_groups
                       (project_id, account_id, group_id, group_name, username, category, monitored, joined_at)
                   VALUES (?,?,?,?,?,?,1,?)
                   ON CONFLICT(project_id, group_id) DO UPDATE SET
                     account_id=EXCLUDED.account_id""",
                (acc_row["project_id"], req.account_id, group["group_id"], chat_name,
                 group.get("username", ""), group.get("category", "general"),
                 datetime.utcnow().isoformat()),
            )

    return {"status": "joined", "group": chat_name}


@router.post("/discovery/run-now")
async def trigger_discovery_now(account_id: int):
    """Manually trigger discovery for one account."""
    from accounts import get_client_for_account as gca
    client = await gca(account_id)
    with get_conn() as conn:
        row = conn.execute("SELECT phone FROM accounts WHERE id=?", (account_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Account not found")
    asyncio.create_task(_discover_for_client(client, row["phone"], account_id))
    return {"status": "discovery started"}
