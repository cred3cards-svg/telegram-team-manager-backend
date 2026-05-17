import os
import json
import anthropic
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from database import get_conn

router = APIRouter()

client = anthropic.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))

SYSTEM_PROMPT = """
Young Indian urban woman, Telegram support team.
Hinglish tone. Warm, witty, firm.
Product: OnlyWin - free legal cricket prediction market
@Owcric_bot = game, @Onlywinme = community
Rules:
- Max 1 handle per message, never both together
- No URLs ever, handles only
- Plug only when natural (cricket/money/boredom talk)
- Max 2-3 sentences, max 2 emojis
- Abuse: warn once then exit
- Never sound like an ad or bot

Output JSON only:
{{"draft":"...","plug_used":false,"handle_used":null,
"boundary_triggered":false,"suggested_action":"send"}}

Context: {chat_type} | plug_used_before: {plug_already_used}
History: {chat_history}
Message: {message_text}
"""


def _fetch_persona(account_id: int) -> dict:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT personality, job_description FROM accounts WHERE id=?", (account_id,)
        ).fetchone()
    return dict(row) if row else {"personality": "", "job_description": ""}


class DraftRequest(BaseModel):
    message_text: str
    chat_history: list[dict] = []
    chat_type: str = "dm"
    project_context: str = "General team communication"
    tone: str = "casual"
    message_id: int | None = None
    account_id: int | None = None
    plug_already_used: bool = False


class DraftResponse(BaseModel):
    draft: str
    plug_used: bool = False
    handle_used: str | None = None
    boundary_triggered: bool = False
    suggested_action: str = "send"


def build_draft(
    message_text: str,
    chat_history: list,
    chat_type: str,
    plug_already_used: bool = False,
    **kwargs,  # absorb legacy args (project_context, tone, personality, job_description)
) -> str:
    """Return formatted system prompt."""
    history_str = "\n".join(
        f"{m.get('sender', 'User')}: {m.get('text', '')}"
        for m in chat_history[-3:]
    )
    return SYSTEM_PROMPT.format(
        chat_type=chat_type,
        plug_already_used=plug_already_used,
        chat_history=history_str or "(no prior messages)",
        message_text=message_text,
    )


def parse_ai_response(raw: str) -> dict:
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    try:
        return json.loads(raw.strip())
    except json.JSONDecodeError:
        return {
            "draft": raw.strip(),
            "plug_used": False,
            "handle_used": None,
            "boundary_triggered": False,
            "suggested_action": "send",
        }


@router.post("/ai/draft", response_model=DraftResponse)
async def generate_draft(req: DraftRequest):
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not configured")

    system_prompt = build_draft(
        message_text=req.message_text,
        chat_history=req.chat_history,
        chat_type=req.chat_type,
        plug_already_used=req.plug_already_used,
    )

    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            system=system_prompt,
            messages=[{"role": "user", "content": req.message_text}],
        )
        print(f"Tokens used: {response.usage.input_tokens} in, {response.usage.output_tokens} out")
        data = parse_ai_response(response.content[0].text)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    result = DraftResponse(
        draft=data.get("draft", ""),
        plug_used=data.get("plug_used", False),
        handle_used=data.get("handle_used", None),
        boundary_triggered=data.get("boundary_triggered", False),
        suggested_action=data.get("suggested_action", "send"),
    )

    if req.message_id:
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO ai_drafts (message_id, draft_text, category, urgency) VALUES (?,?,?,?)",
                (req.message_id, result.draft, "general", "medium"),
            )

    return result


@router.post("/ai/mark-used")
async def mark_draft_used(message_id: int, was_used: bool = True):
    with get_conn() as conn:
        conn.execute(
            "UPDATE ai_drafts SET was_used=? WHERE message_id=? ORDER BY id DESC LIMIT 1",
            (1 if was_used else 0, message_id),
        )
    return {"status": "updated"}


@router.get("/projects/list")
async def list_projects():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM projects").fetchall()
    return [dict(r) for r in rows]


@router.post("/projects/create")
async def create_project(name: str, tone: str = "casual", context: str = ""):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO projects (name, tone, context) VALUES (?,?,?)",
            (name, tone, context),
        )
        row = conn.execute("SELECT * FROM projects WHERE id=last_insert_rowid()").fetchone()
    return dict(row)


@router.put("/projects/{project_id}")
async def update_project(project_id: int, name: str = None, tone: str = None, context: str = None):
    with get_conn() as conn:
        if name:
            conn.execute("UPDATE projects SET name=? WHERE id=?", (name, project_id))
        if tone:
            conn.execute("UPDATE projects SET tone=? WHERE id=?", (tone, project_id))
        if context is not None:
            conn.execute("UPDATE projects SET context=? WHERE id=?", (context, project_id))
        row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    return dict(row)
