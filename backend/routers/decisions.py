"""Operation Mode + Decision Engine API (Phase 47A skeleton).

Endpoints land incrementally — 47A wires the mode + list/read; the full
approve/reject/undo action set is completed in 47D.
"""

from __future__ import annotations

from typing import Any

import os
from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from backend import decision_engine as de
from backend import decision_rules as _dr

router = APIRouter(tags=["decisions"])


# N10: decision-action endpoints can destructively change agent state
# (e.g. approving a destructive-severity decision). Gate them behind an
# optional bearer token — if OMNISIGHT_DECISION_BEARER is unset we keep
# the current open-posture of the codebase; if set, mutators require it.
def _require_decision_token(authorization: str | None = Header(default=None)) -> None:
    expected = os.environ.get("OMNISIGHT_DECISION_BEARER", "").strip()
    if not expected:
        return  # feature off — preserves pre-fix behavior
    presented = (authorization or "")
    if presented.startswith("Bearer "):
        presented = presented[len("Bearer "):]
    if not presented or presented != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing decision bearer token")


class ModeRequest(BaseModel):
    mode: str = Field(..., description="One of manual | supervised | full_auto | turbo")


@router.get("/operation-mode")
async def get_mode() -> dict[str, Any]:
    mode = de.get_mode()
    return {
        "mode": mode.value,
        "parallel_cap": de._PARALLEL_BUDGET[mode],
        "in_flight": de.parallel_in_flight(),
        "modes": [m.value for m in de.OperationMode],
    }


@router.put("/operation-mode")
async def put_mode(req: ModeRequest, _auth: None = Depends(_require_decision_token)) -> dict[str, Any]:
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


class ResolveRequest(BaseModel):
    option_id: str = Field(..., description="Which option the user picked")


@router.post("/decisions/{decision_id}/approve")
async def approve_decision(decision_id: str, req: ResolveRequest,
                           _auth: None = Depends(_require_decision_token)) -> dict[str, Any]:
    # Validate option_id belongs to the decision
    existing = de.get(decision_id)
    if existing is None:
        return JSONResponse(status_code=404, content={"detail": "decision not found"})
    if existing.status != de.DecisionStatus.pending:
        return JSONResponse(status_code=409, content={"detail": f"not pending (status={existing.status.value})"})
    valid_ids = {o["id"] for o in existing.options}
    if req.option_id not in valid_ids:
        return JSONResponse(status_code=400, content={"detail": "unknown option_id"})
    out = de.resolve(decision_id, req.option_id, resolver="user",
                     status=de.DecisionStatus.approved)
    assert out is not None  # we checked pending above
    return out.to_dict()


@router.post("/decisions/{decision_id}/reject")
async def reject_decision(decision_id: str,
                          _auth: None = Depends(_require_decision_token)) -> dict[str, Any]:
    existing = de.get(decision_id)
    if existing is None:
        return JSONResponse(status_code=404, content={"detail": "decision not found"})
    if existing.status != de.DecisionStatus.pending:
        return JSONResponse(status_code=409, content={"detail": f"not pending (status={existing.status.value})"})
    # N8: rejection uses the sentinel "__rejected__" rather than an empty
    # string so downstream consumers / SSE subscribers can branch on a
    # non-null id without null-deref surprises.
    out = de.resolve(decision_id, "__rejected__", resolver="user",
                     status=de.DecisionStatus.rejected)
    return out.to_dict() if out else {}


@router.post("/decisions/{decision_id}/undo")
async def undo_decision(decision_id: str,
                        _auth: None = Depends(_require_decision_token)) -> dict[str, Any]:
    out = de.undo(decision_id)
    if out is None:
        return JSONResponse(status_code=404, content={"detail": "no resolved decision with that id"})
    return out.to_dict()


@router.post("/decisions/sweep")
async def trigger_sweep(_auth: None = Depends(_require_decision_token)) -> dict[str, Any]:
    """Manually trigger the timeout sweep (testing + manual nudge)."""
    resolved = de.sweep_timeouts()
    return {"resolved": len(resolved), "ids": [d.id for d in resolved]}


# ─── Phase 47C: Budget Strategy ───

from backend import budget_strategy as _bs


class StrategyRequest(BaseModel):
    strategy: str = Field(..., description="One of quality | balanced | cost_saver | sprint")


@router.get("/budget-strategy")
async def get_budget_strategy() -> dict[str, Any]:
    return {
        "strategy": _bs.get_strategy().value,
        "tuning": _bs.get_tuning().to_dict(),
        "available": _bs.list_strategies(),
    }


@router.put("/budget-strategy")
async def put_budget_strategy(req: StrategyRequest,
                              _auth: None = Depends(_require_decision_token)) -> dict[str, Any]:
    try:
        tuning = _bs.set_strategy(req.strategy)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"detail": str(exc)})
    return {"strategy": tuning.strategy.value, "tuning": tuning.to_dict()}


# ─── Phase 50B: Decision Rules Editor ───────────────────────────────

class RulesPayload(BaseModel):
    rules: list[dict[str, Any]] = Field(default_factory=list)


class RulesTestPayload(BaseModel):
    kinds: list[str] = Field(default_factory=list)
    mode: str | None = None


@router.get("/decision-rules")
async def get_decision_rules() -> dict[str, Any]:
    """Return the ordered rule list + available severity/mode vocab."""
    return {
        "rules": _dr.list_rules(),
        "severities": [s.value for s in de.DecisionSeverity],
        "modes": [m.value for m in de.OperationMode],
    }


@router.put("/decision-rules")
async def put_decision_rules(payload: RulesPayload,
                             _auth: None = Depends(_require_decision_token)) -> dict[str, Any]:
    try:
        rules = _dr.replace_rules(payload.rules)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"detail": str(exc)})
    return {"rules": rules}


@router.post("/decision-rules/test")
async def test_decision_rules(payload: RulesTestPayload) -> dict[str, Any]:
    """Dry-run: for each sample kind, which rule (if any) fires under
    the current (or requested) mode."""
    mode = payload.mode or de.get_mode().value
    return {"mode": mode, "hits": _dr.test_against(payload.kinds, mode)}
