"""Operation Mode + Decision Engine API (Phase 47A skeleton).

Endpoints land incrementally — 47A wires the mode + list/read; the full
approve/reject/undo action set is completed in 47D.
"""

from __future__ import annotations

from typing import Any

import os
import time
import threading
from collections import deque
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from backend import auth as _au
from backend import decision_engine as de
from backend import decision_rules as _dr
from backend.routers import _pagination as _pg

router = APIRouter(tags=["decisions"])


# R2-#13: sliding-window rate limit on decision mutators. Defends against
# brute-forcing a short OMNISIGHT_DECISION_BEARER and against an over-eager
# operator script flooding approve/reject. Window is per-client-ip;
# intentionally not keyed on the token itself so a credential-stuffer
# cannot use a cheap probe to learn rate limit window size.
_RL_WINDOW_S = float(os.environ.get("OMNISIGHT_DECISION_RL_WINDOW_S", "10"))
_RL_MAX = int(os.environ.get("OMNISIGHT_DECISION_RL_MAX", "30"))
_rl_hits: dict[str, deque[float]] = {}
_rl_lock = threading.Lock()


def _rate_limit(client_ip: str) -> None:
    now = time.monotonic()
    cutoff = now - _RL_WINDOW_S
    with _rl_lock:
        dq = _rl_hits.setdefault(client_ip, deque())
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= _RL_MAX:
            raise HTTPException(
                status_code=429,
                detail=f"rate limit: {_RL_MAX}/{int(_RL_WINDOW_S)}s on decision mutators",
                headers={"Retry-After": str(int(_RL_WINDOW_S))},
            )
        dq.append(now)


# N10: decision-action endpoints can destructively change agent state
# (e.g. approving a destructive-severity decision). Gate them behind an
# optional bearer token — if OMNISIGHT_DECISION_BEARER is unset we keep
# the current open-posture of the codebase; if set, mutators require it.
def _require_decision_token(
    request: Request,
    authorization: str | None = Header(default=None),
) -> None:
    client_ip = (request.client.host if request.client else "unknown") or "unknown"
    _rate_limit(client_ip)
    expected = os.environ.get("OMNISIGHT_DECISION_BEARER", "").strip()
    if not expected:
        return  # feature off — preserves pre-fix behavior
    presented = (authorization or "")
    if presented.startswith("Bearer "):
        presented = presented[len("Bearer "):]
    if not presented or presented != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing decision bearer token")


def _reset_rate_limit_for_tests() -> None:  # test hook
    with _rl_lock:
        _rl_hits.clear()


class ModeRequest(BaseModel):
    mode: str = Field(..., description="One of manual | supervised | full_auto | turbo")
    # H2 row 1514: operators running with h2_auto_derate=false must
    # explicitly confirm when switching into turbo — without the safety
    # net, a sustained CPU spike will NOT auto-shrink the budget.
    confirm_turbo: bool = Field(
        default=False,
        description="Required when h2_auto_derate=false and mode=turbo.",
    )


@router.get("/operation-mode")
async def get_mode(request: Request) -> dict[str, Any]:
    session_token = request.cookies.get(_au.SESSION_COOKIE) or None
    mode = await de.get_session_mode_async(session_token)
    return {
        "mode": mode.value,
        "parallel_cap": de._PARALLEL_BUDGET[mode],
        "in_flight": de.parallel_in_flight(),
        "modes": [m.value for m in de.OperationMode],
        "session_scoped": True,
    }


@router.put("/operation-mode")
async def put_mode(
    req: ModeRequest,
    request: Request,
    _auth: None = Depends(_require_decision_token),
    _user=Depends(_au.require_operator),
) -> dict[str, Any]:
    if req.mode == "turbo" and not _au.role_at_least(_user.role, "admin"):
        return JSONResponse(status_code=403,
                            content={"detail": "turbo mode requires admin role"})
    session_token = request.cookies.get(_au.SESSION_COOKIE) or None
    try:
        if session_token:
            mode = await de.set_session_mode(
                session_token, req.mode, confirm_turbo=req.confirm_turbo
            )
        else:
            mode = de.set_mode(req.mode, confirm_turbo=req.confirm_turbo)
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc)})
    except de.TurboConfirmRequired as exc:
        return JSONResponse(
            status_code=409,
            content={
                "detail": str(exc),
                "code": "turbo_confirm_required",
                "hint": "re-send the request with confirm_turbo=true",
            },
        )
    return {
        "mode": mode.value,
        "parallel_cap": de._PARALLEL_BUDGET[mode],
        "session_scoped": session_token is not None,
    }


@router.get("/decisions")
async def list_decisions(status: str = "pending", limit: int = _pg.Limit(default=100)) -> dict[str, Any]:
    if status == "pending":
        items = [d.to_dict() for d in de.list_pending()]
    elif status == "history":
        items = [d.to_dict() for d in de.list_history(limit=limit)]
    else:
        return JSONResponse(
            status_code=422,
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
                           _auth: None = Depends(_require_decision_token),
                           _user=Depends(_au.require_operator)) -> dict[str, Any]:
    # Validate option_id belongs to the decision
    existing = de.get(decision_id)
    if existing is None:
        return JSONResponse(status_code=404, content={"detail": "decision not found"})
    if existing.status != de.DecisionStatus.pending:
        return JSONResponse(status_code=409, content={"detail": f"not pending (status={existing.status.value})"})
    # Phase 54: destructive approvals require admin role.
    if (existing.severity == de.DecisionSeverity.destructive
            and not _au.role_at_least(_user.role, "admin")):
        return JSONResponse(
            status_code=403,
            content={"detail": "destructive decisions require admin role to approve"},
        )
    valid_ids = {o["id"] for o in existing.options}
    if req.option_id not in valid_ids:
        return JSONResponse(status_code=422, content={"detail": "unknown option_id"})
    out = de.resolve(decision_id, req.option_id, resolver="user",
                     status=de.DecisionStatus.approved)
    assert out is not None  # we checked pending above
    return out.to_dict()


@router.post("/decisions/{decision_id}/reject")
async def reject_decision(decision_id: str,
                          _auth: None = Depends(_require_decision_token),
                          _user=Depends(_au.require_operator)) -> dict[str, Any]:
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
                        _auth: None = Depends(_require_decision_token),
                        _user=Depends(_au.require_operator)) -> dict[str, Any]:
    out = de.undo(decision_id)
    if out is None:
        return JSONResponse(status_code=404, content={"detail": "no resolved decision with that id"})
    return out.to_dict()


@router.post("/decisions/sweep")
async def trigger_sweep(_auth: None = Depends(_require_decision_token),
                        _user=Depends(_au.require_operator)) -> dict[str, Any]:
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
                              _auth: None = Depends(_require_decision_token),
                              _user=Depends(_au.require_operator)) -> dict[str, Any]:
    try:
        tuning = _bs.set_strategy(req.strategy)
    except ValueError as exc:
        # L#45: 422 per REST/Pydantic convention (validation), not 400.
        return JSONResponse(status_code=422, content={"detail": str(exc)})
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
                             _auth: None = Depends(_require_decision_token),
                             _user=Depends(_au.require_operator)) -> dict[str, Any]:
    try:
        rules = _dr.replace_rules(payload.rules)
    except ValueError as exc:
        # L#45: 422 per REST/Pydantic convention (validation), not 400.
        return JSONResponse(status_code=422, content={"detail": str(exc)})
    return {"rules": rules}


@router.post("/decision-rules/test")
async def test_decision_rules(payload: RulesTestPayload) -> dict[str, Any]:
    """Dry-run: for each sample kind, which rule (if any) fires under
    the current (or requested) mode."""
    mode = payload.mode or de.get_mode().value
    return {"mode": mode, "hits": _dr.test_against(payload.kinds, mode)}


# ─── H3 row 1527: Force Turbo manual override ───────────────────────
#
# Operators can override the H2 auto-derate state machine + the DRF
# sandbox capacity derate to restore full turbo budget immediately,
# bypassing the 120-s cooldown. Useful when an operator knows the CPU
# spike was caused by an external process (e.g. a one-shot benchmark)
# and wants to resume at turbo budget without waiting for the cooldown.
#
# Safety layers (required, NOT optional):
#   1. Request body `confirm=true` is mandatory — the frontend confirm
#      dialog surfaces the OOM warning; the server enforces the boolean
#      so a CLI / curl caller can't skip the human-in-the-loop step.
#   2. Admin role is required (matches the existing `PUT /operation-mode`
#      gate for turbo — an operator without admin can't flip the mode
#      switch directly, so they can't escape the same gate here).
#   3. Phase-53 hash-chain audit row is written with action
#      `coordinator.force_turbo_override`, entity_kind `force_turbo_override`,
#      entity_id `applied` so post-hoc inspection can reconstruct who
#      overrode a derate and when. The `before` dict captures the
#      pre-override state (derate_active, derate_ratio, derate_reason);
#      `after` captures the result (restored_to_budget, manual_override=true).
#   4. SSE event `coordinator.force_turbo_override` broadcasts to the
#      UI so every open dashboard shows the override immediately.


_PARALLEL_BUDGET_TURBO = de._PARALLEL_BUDGET[de.OperationMode.turbo]


class ForceTurboRequest(BaseModel):
    confirm: bool = Field(
        default=False,
        description=(
            "Must be true. Frontend confirm dialog must warn about "
            "possible OOM under sustained load before setting this flag."
        ),
    )
    reason: str | None = Field(
        default=None,
        max_length=200,
        description="Optional operator rationale — persisted to the audit row.",
    )


@router.post("/coordinator/force-turbo")
async def force_turbo(
    req: ForceTurboRequest,
    _auth: None = Depends(_require_decision_token),
    _user=Depends(_au.require_operator),
) -> dict[str, Any]:
    if not _au.role_at_least(_user.role, "admin"):
        return JSONResponse(
            status_code=403,
            content={"detail": "force turbo requires admin role"},
        )
    if not req.confirm:
        return JSONResponse(
            status_code=422,
            content={
                "detail": (
                    "confirm=true is required — force turbo bypasses the "
                    "OOM safety net and MUST be acknowledged by an operator"
                ),
                "code": "force_turbo_confirm_required",
            },
        )

    from backend import sandbox_capacity as _sc
    from backend import audit as _audit
    from backend.events import bus as _bus

    # Capture before-state so the audit row describes what was overridden.
    turbo_before = de.turbo_derate_snapshot()
    cap_before = _sc.snapshot()
    before = {
        "turbo_derate_active": bool(turbo_before.get("derate_active", False)),
        "capacity_derate_ratio": cap_before.get("derate_ratio", 1.0),
        "capacity_derate_reason": cap_before.get("derate_reason"),
        "effective_capacity_max": cap_before.get("effective_capacity_max"),
        "capacity_max": cap_before.get("capacity_max"),
    }

    # Clear H2 turbo auto-derate (returns True iff it was active).
    cleared_turbo_derate = de.clear_turbo_derate()
    # Reset the DRF sandbox capacity derate ratio back to 1.0 so the
    # effective budget returns to CAPACITY_MAX.
    reset_capacity_derate = bool(before["capacity_derate_ratio"] < 1.0)
    if reset_capacity_derate:
        _sc.set_derate(1.0, reason=None)

    after_snap = _sc.snapshot()
    after = {
        "turbo_derate_active": de.is_turbo_derated(),
        "capacity_derate_ratio": after_snap.get("derate_ratio", 1.0),
        "effective_capacity_max": after_snap.get("effective_capacity_max"),
        "restored_to_budget": _PARALLEL_BUDGET_TURBO,
        "manual_override": True,
        "operator_reason": (req.reason or "").strip() or None,
        "at": time.time(),
    }

    # SSE broadcast — every open dashboard sees the override instantly.
    try:
        _bus.publish(
            "coordinator.force_turbo_override",
            {
                "cleared_turbo_derate": cleared_turbo_derate,
                "reset_capacity_derate": reset_capacity_derate,
                "actor": getattr(_user, "email", None) or getattr(_user, "id", None) or "operator",
                "reason": after["operator_reason"],
                **after,
            },
        )
    except Exception:
        pass

    # Phase-53 hash-chain audit row.
    try:
        _audit.log_sync(
            action="coordinator.force_turbo_override",
            entity_kind="force_turbo_override",
            entity_id="applied",
            before=before,
            after=after,
            actor=getattr(_user, "email", None) or getattr(_user, "id", None) or "operator",
        )
    except Exception:
        pass

    return {
        "applied": True,
        "cleared_turbo_derate": cleared_turbo_derate,
        "reset_capacity_derate": reset_capacity_derate,
        "before": before,
        "after": after,
    }
