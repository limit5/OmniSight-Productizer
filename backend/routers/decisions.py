"""Operation Mode + Decision Engine API (Phase 47A skeleton).

Endpoints land incrementally — 47A wires the mode + list/read; the full
approve/reject/undo action set is completed in 47D.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from backend import decision_engine as de

router = APIRouter(tags=["decisions"])


class ModeRequest(BaseModel):
    mode: str = Field(..., description="One of manual | supervised | full_auto | turbo")


@router.get("/operation-mode")
async def get_mode() -> dict[str, Any]:
    mode = de.get_mode()
    return {
        "mode": mode.value,
        "parallel_cap": de._PARALLEL_BUDGET[mode],
        "modes": [m.value for m in de.OperationMode],
    }


@router.put("/operation-mode")
async def put_mode(req: ModeRequest) -> dict[str, Any]:
    try:
        mode = de.set_mode(req.mode)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"detail": str(exc)})
    return {
        "mode": mode.value,
        "parallel_cap": de._PARALLEL_BUDGET[mode],
    }


@router.get("/decisions")
async def list_decisions(status: str = "pending", limit: int = 100) -> dict[str, Any]:
    if status == "pending":
        items = [d.to_dict() for d in de.list_pending()]
    elif status == "history":
        items = [d.to_dict() for d in de.list_history(limit=limit)]
    else:
        return JSONResponse(
            status_code=400,
            content={"detail": "status must be 'pending' or 'history'"},
        )
    return {"items": items, "count": len(items)}


@router.get("/decisions/{decision_id}")
async def get_decision(decision_id: str) -> dict[str, Any]:
    d = de.get(decision_id)
    if d is None:
        return JSONResponse(status_code=404, content={"detail": "decision not found"})
    return d.to_dict()
