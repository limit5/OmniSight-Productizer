"""OP-771 canary rollout operator controls.

The D17 dashboard can call these backend endpoints to pause, advance,
resume, or abort a progressive canary without this ticket touching the
frontend implementation.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend import auth
from backend import canary_rollout as _canary


router = APIRouter(prefix="/canary-rollout", tags=["canary-rollout"])


class StartCanaryRequest(BaseModel):
    rollout_id: str = Field(min_length=1)
    stable_color: Literal["blue", "green"]
    canary_color: Literal["blue", "green"]


class ManualControlRequest(BaseModel):
    command: Literal["pause", "resume", "advance", "abort"]
    reason: str = Field(default="operator", min_length=1)


def _payload(
    state: _canary.CanaryState,
    decision: _canary.CanaryDecision | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "rollout_id": state.rollout_id,
        "status": state.status,
        "stable_color": state.stable_color,
        "canary_color": state.canary_color,
        "stage": {
            "name": state.stage.name,
            "canary_percent": state.stage.canary_percent,
            "observe_seconds": state.stage.observe_seconds,
        },
        "stage_index": state.stage_index,
        "reason": state.reason,
        "updated_at": state.updated_at,
    }
    if decision is not None:
        body["decision"] = {
            "action": decision.action,
            "stage": decision.stage,
            "reason": decision.reason,
            "snapshot": None if decision.snapshot is None else {
                "error_rate": decision.snapshot.error_rate,
                "p95_latency_ms": decision.snapshot.p95_latency_ms,
                "availability": decision.snapshot.availability,
                "source": decision.snapshot.source,
            },
        }
    return body


def _controller() -> _canary.CanaryController:
    return _canary.CanaryController()


@router.post("/start")
async def start_canary(
    req: StartCanaryRequest,
    _actor: auth.User = Depends(auth.require_admin),
) -> dict[str, Any]:
    controller = _controller()
    try:
        state = controller.start(
            rollout_id=req.rollout_id,
            stable_color=req.stable_color,
            canary_color=req.canary_color,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _payload(state)


@router.post("/evaluate")
async def evaluate_canary(
    _actor: auth.User = Depends(auth.require_admin),
) -> dict[str, Any]:
    controller = _controller()
    try:
        state = _canary.load_state()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="canary state not found") from exc
    new_state, decision = controller.evaluate(state)
    return _payload(new_state, decision)


@router.post("/control")
async def control_canary(
    req: ManualControlRequest,
    _actor: auth.User = Depends(auth.require_admin),
) -> dict[str, Any]:
    controller = _controller()
    try:
        state = _canary.load_state()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="canary state not found") from exc
    try:
        new_state = controller.manual_control(
            state,
            req.command,
            reason=req.reason,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _payload(new_state)


@router.get("/assignment/{tenant_id:path}")
async def tenant_assignment(
    tenant_id: str,
    _actor: auth.User = Depends(auth.require_viewer),
) -> dict[str, Any]:
    try:
        state = _canary.load_state()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="canary state not found") from exc
    assigned = _canary.stable_canary_assignment(
        tenant_id,
        state.stage.canary_percent,
    )
    return {
        "tenant_id": tenant_id,
        "canary": assigned,
        "target_color": state.canary_color if assigned else state.stable_color,
        "canary_percent": state.stage.canary_percent,
        "rollout_id": state.rollout_id,
    }
