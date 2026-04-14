"""Phase 58 — Decision Profile + Auto-decision Postmortem API.

GET   /profile                       current profile + list of all 4
PUT   /profile {id: STRICT|...}      switch profile (gated)
GET   /auto-decisions[?since=&undone=&limit=]
POST  /decisions/bulk-undo {ids:[]}  bulk mark undone (calls audit)
"""

from __future__ import annotations

import os
import time
from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from backend import decision_profiles as dp

router = APIRouter(tags=["profile"])


def _require_token(authorization: str | None = Header(default=None)) -> None:
    expected = os.environ.get("OMNISIGHT_DECISION_BEARER", "").strip()
    if not expected:
        return
    presented = (authorization or "")
    if presented.startswith("Bearer "):
        presented = presented[len("Bearer "):]
    if presented != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing decision bearer token")


@router.get("/profile")
async def get_profile() -> dict:
    return {
        "current": dp.get_current_id(),
        "details": dp.get_profile().to_dict(),
        "available": dp.list_profiles(),
        "critical_kinds": sorted(dp.CRITICAL_KINDS),
    }


class ProfileRequest(BaseModel):
    id: str = Field(..., description="STRICT | BALANCED | AUTONOMOUS | GHOST")


@router.put("/profile")
async def put_profile(req: ProfileRequest, _auth: None = Depends(_require_token)) -> dict:
    try:
        prof = dp.set_profile(req.id)
    except dp.GhostNotAllowed as exc:
        return JSONResponse(status_code=403, content={"detail": str(exc)})
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc)})
    return {"current": prof.id, "details": prof.to_dict()}


@router.get("/auto-decisions")
async def list_auto_decisions(
    since: float | None = None,
    undone: bool | None = None,
    limit: int = 200,
) -> dict:
    """Postmortem feed."""
    from backend import db
    where = []
    params: list = []
    if since is not None:
        where.append("auto_executed_at >= ?"); params.append(since)
    if undone is True:
        where.append("undone_at IS NOT NULL")
    elif undone is False:
        where.append("undone_at IS NULL")
    sql = ("SELECT id, decision_id, kind, severity, chosen_option, confidence, "
           "rationale, profile_id, auto_executed_at, undone_at, undone_by "
           "FROM auto_decision_log")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY auto_executed_at DESC LIMIT ?"
    params.append(int(limit))
    async with db._conn().execute(sql, tuple(params)) as cur:
        rows = await cur.fetchall()
    return {"items": [
        {
            "id": r["id"], "decision_id": r["decision_id"],
            "kind": r["kind"], "severity": r["severity"],
            "chosen_option": r["chosen_option"],
            "confidence": r["confidence"], "rationale": r["rationale"],
            "profile_id": r["profile_id"],
            "auto_executed_at": r["auto_executed_at"],
            "undone_at": r["undone_at"], "undone_by": r["undone_by"],
        } for r in rows
    ], "count": len(rows)}


class BulkUndoRequest(BaseModel):
    ids: list[int] = Field(default_factory=list)
    actor: str = "user"


@router.post("/decisions/bulk-undo")
async def bulk_undo(req: BulkUndoRequest, _auth: None = Depends(_require_token)) -> dict:
    """Mark a batch of auto_decision_log rows as undone. The matching
    Decision in DecisionEngine._history is also flipped to undone via
    decision_engine.undo() (best-effort — we don't fail if the in-memory
    record was already evicted by the 500-row history cap)."""
    if not req.ids:
        return {"undone": 0}
    from backend import db, decision_engine as _de
    undone_at = time.time()
    placeholders = ",".join("?" * len(req.ids))
    async with db._conn().execute(
        f"SELECT id, decision_id FROM auto_decision_log WHERE id IN ({placeholders}) "
        "AND undone_at IS NULL",
        tuple(req.ids),
    ) as cur:
        targets = await cur.fetchall()
    if not targets:
        return {"undone": 0}
    decision_ids = [r["decision_id"] for r in targets]
    await db._conn().execute(
        f"UPDATE auto_decision_log SET undone_at=?, undone_by=? "
        f"WHERE id IN ({placeholders}) AND undone_at IS NULL",
        (undone_at, req.actor, *req.ids),
    )
    await db._conn().commit()
    flipped = 0
    for did in decision_ids:
        try:
            if _de.undo(did) is not None:
                flipped += 1
        except Exception:
            pass
    return {"undone": len(targets), "in_memory_flipped": flipped}
