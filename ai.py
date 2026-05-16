import os
import json
import anthropic
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from database import get_conn

router = APIRouter()

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))

SYSTEM_TEMPLATE = """You are a human team member replying to a Telegram message.

Project: {project_context}
Tone: {tone}
Chat type: {chat_type}
{persona_block}
Given the conversation, output ONLY valid JSON:
{{"draft":"...","category":"support|sales|general|spam","urgency":"high|medium|low"}}

Rules:
- Draft max 3 sentences
- Sound like the specific person described above, not a generic assistant
- Match their vocabulary, style, and role
- Never mention AI or that you're automated"""

USER_TEMPLATE = """Recent chat:
{chat_history}

Latest message: {message_text}"""


def _persona_block(personality: str, job_description: str) -> str:
    parts = []
    if job_description:
        parts.append(f"Role: {job_description}")
    if personality:
        parts.append(f"Personality & style: {personality}")
    if not parts:
        return ""
    return "\n".join(parts) + "\n"


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
    account_id: int | None = None   # used to fetch per-account persona


class DraftResponse(BaseModel):
    draft: str
    category: str
    urgency: str


def build_draft(
    message_text: str,
    chat_history: list,
    chat_type: str,
    project_context: str,
    tone: str,
    personality: str = "",
    job_description: str = "",
) -> tuple[str, str]:
    """Return (system_prompt, user_prompt)."""
    history_str = "\n".join(
        f"{m.get('sender', 'User')}: {m.get('text', '')}"
        for m in chat_history[-5:]
    )
    system_prompt = SYSTEM_TEMPLATE.format(
        project_context=project_context,
        tone=tone,
        chat_type=chat_type,
        persona_block=_persona_block(personality, job_description),
    )
    user_prompt = USER_TEMPLATE.format(
        chat_history=history_str or "(no prior messages)",
        message_text=message_text,
    )
    return system_prompt, user_prompt


def parse_ai_response(raw: str) -> dict:
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    try:
        return json.loads(raw.strip())
    except json.JSONDecodeError:
        return {"draft": raw.strip(), "category": "general", "urgency": "medium"}


@router.post("/ai/draft", response_model=DraftResponse)
async def generate_draft(req: DraftRequest):
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not configured")

    persona = _fetch_persona(req.account_id) if req.account_id else {"personality": "", "job_description": ""}

    system_prompt, user_prompt = build_draft(
        message_text=req.message_text,
        chat_history=req.chat_history,
        chat_type=req.chat_type,
        project_context=req.project_context,
        tone=req.tone,
        personality=persona.get("personality", ""),
        job_description=persona.get("job_description", ""),
    )

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=300,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        data = parse_ai_response(response.content[0].text)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    result = DraftResponse(
        draft=data.get("draft", ""),
        category=data.get("category", "general"),
        urgency=data.get("urgency", "medium"),
    )

    if req.message_id:
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO ai_drafts (message_id, draft_text, category, urgency) VALUES (?,?,?,?)",
                (req.message_id, result.draft, result.category, result.urgency),
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
