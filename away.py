import json
from datetime import datetime, timedelta
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List
from database import get_conn, row_to_dict, rows_to_list

router = APIRouter()

MAX_HOURS = 6


class AwayEnableRequest(BaseModel):
    project_id: int
    hours: float
    target_mode: str = "all"          # "all" or "selected"
    target_chat_ids: List[str] = []   # account_id:chat_id pairs when mode=selected


def get_away_status(project_id: int) -> dict:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM away_sessions WHERE project_id=?", (project_id,)
        ).fetchone()
    if not row:
        return {"active": False, "away_until": None, "seconds_remaining": 0,
                "target_mode": "all", "target_chat_ids": []}

    away_until = datetime.fromisoformat(row["away_until"])
    now = datetime.utcnow()
    if now >= away_until:
        with get_conn() as conn:
            conn.execute("DELETE FROM away_sessions WHERE project_id=?", (project_id,))
        return {"active": False, "away_until": None, "seconds_remaining": 0,
                "target_mode": "all", "target_chat_ids": []}

    try:
        target_chat_ids = json.loads(row["target_chat_ids"] or "[]")
    except Exception:
        target_chat_ids = []

    return {
        "active": True,
        "away_until": away_until.isoformat(),
        "seconds_remaining": int((away_until - now).total_seconds()),
        "target_mode": row["target_mode"] or "all",
        "target_chat_ids": target_chat_ids,
    }


def is_away_for_chat(project_id: int, account_id: int, chat_id: str) -> bool:
    status = get_away_status(project_id)
    if not status["active"]:
        return False
    if status["target_mode"] == "all":
        return True
    key = f"{account_id}:{chat_id}"
    return key in status["target_chat_ids"]


@router.post("/away/enable")
async def enable_away(req: AwayEnableRequest):
    hours = min(max(req.hours, 0.5), MAX_HOURS)
    now = datetime.utcnow()
    away_until = now + timedelta(hours=hours)

    with get_conn() as conn:
        conn.execute(
            """INSERT INTO away_sessions (project_id, away_until, enabled_at, target_mode, target_chat_ids)
               VALUES (?,?,?,?,?)
               ON CONFLICT(project_id) DO UPDATE SET
                 away_until=excluded.away_until,
                 enabled_at=excluded.enabled_at,
                 target_mode=excluded.target_mode,
                 target_chat_ids=excluded.target_chat_ids""",
            (req.project_id, away_until.isoformat(), now.isoformat(),
             req.target_mode, json.dumps(req.target_chat_ids)),
        )

    return {
        "status": "away",
        "away_until": away_until.isoformat(),
        "hours": hours,
        "seconds_remaining": int(hours * 3600),
        "target_mode": req.target_mode,
        "target_chat_ids": req.target_chat_ids,
    }


@router.post("/away/disable")
async def disable_away(project_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM away_sessions WHERE project_id=?", (project_id,))
    return {"status": "back"}


@router.get("/away/status")
async def away_status(project_id: int):
    return get_away_status(project_id)


@router.get("/away/log")
async def get_away_log(project_id: int, unreviewed_only: bool = False):
    with get_conn() as conn:
        query = "SELECT * FROM away_log WHERE project_id=?"
        params = [project_id]
        if unreviewed_only:
            query += " AND reviewed=0"
        query += " ORDER BY replied_at DESC LIMIT 100"
        rows = conn.execute(query, params).fetchall()
    return rows_to_list(rows)


@router.post("/away/log/{log_id}/review")
async def mark_reviewed(log_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE away_log SET reviewed=1 WHERE id=?", (log_id,))
    return {"status": "reviewed"}


@router.post("/away/log/review-all")
async def mark_all_reviewed(project_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE away_log SET reviewed=1 WHERE project_id=?", (project_id,))
    return {"status": "all_reviewed"}
