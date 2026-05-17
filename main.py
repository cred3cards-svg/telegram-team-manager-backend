import os
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

load_dotenv()

from database import init_db
from accounts import router as accounts_router, restore_all_sessions, keepalive_all_sessions, _sync_dialogs, _clients
from groups import router as groups_router
from inbox import router as inbox_router, register_event_handlers
from ai import router as ai_router
from away import router as away_router
from database import get_conn, rows_to_list


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    await restore_all_sessions()

    # Register event handlers + sync dialogs for all active accounts
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT a.id, a.project_id, a.phone FROM accounts a WHERE a.status='active'"
        ).fetchall()
    for r in rows_to_list(rows):
        await register_event_handlers(r["id"], r["project_id"])
        client = _clients.get(r["phone"])
        if client:
            asyncio.create_task(_sync_dialogs(client, r["id"]))

    # Keep all Telegram sessions alive for the lifetime of the server
    asyncio.create_task(keepalive_all_sessions())

    yield


app = FastAPI(title="Telegram Team Manager", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(accounts_router)
app.include_router(groups_router)
app.include_router(inbox_router)
app.include_router(ai_router)
app.include_router(away_router)


@app.get("/health")
async def health():
    return {"status": "ok"}
