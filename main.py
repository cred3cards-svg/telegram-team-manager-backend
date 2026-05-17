import os
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

load_dotenv()

from database import init_db
from accounts import router as accounts_router, restore_all_sessions, keepalive_all_sessions
from groups import router as groups_router
from inbox import router as inbox_router
from ai import router as ai_router
from away import router as away_router
from discovery import router as discovery_router, run_discovery_loop


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    await restore_all_sessions()
    asyncio.create_task(keepalive_all_sessions())
    asyncio.create_task(run_discovery_loop())

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
app.include_router(discovery_router)


@app.get("/health")
async def health():
    return {"status": "ok"}
