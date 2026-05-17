"""OP-778 deployment dashboard backend API."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from backend import auth, canary_rollout
from backend import release_dashboard as dashboard


router = APIRouter(prefix="/admin/releases", tags=["release-dashboard"])

# Minimum length for free-text request fields — pydantic shorthand for "non-empty after parsing".
MIN_NONEMPTY_LENGTH = 1
# Progress percent bounds for synthetic deploy progress events (inclusive).
MIN_PROGRESS_PERCENT = 0
MAX_PROGRESS_PERCENT = 100


class RollbackRequest(BaseModel):
    reason: str = Field(min_length=MIN_NONEMPTY_LENGTH)
    tag: str | None = None


class CanaryControlRequest(BaseModel):
    command: str = Field(pattern="^(pause|resume|advance|abort)$")
    reason: str = Field(default="operator", min_length=MIN_NONEMPTY_LENGTH)


class SyntheticProgressRequest(BaseModel):
    tag: str = Field(min_length=MIN_NONEMPTY_LENGTH)
    progress_percent: int = Field(ge=MIN_PROGRESS_PERCENT, le=MAX_PROGRESS_PERCENT)
    status: str = Field(default="deploying", min_length=MIN_NONEMPTY_LENGTH)


@router.get("")
async def release_dashboard_snapshot(
    _user: auth.User = Depends(auth.require_admin),
) -> JSONResponse:
    return JSONResponse(dashboard.snapshot())


@router.post("/rollback")
async def rollback_release(
    req: RollbackRequest,
    user: auth.User = Depends(auth.require_admin),
) -> JSONResponse:
    try:
        result = await dashboard.rollback_current(
            actor=user.email,
            reason=req.reason.strip(),
            tag=req.tag,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(result)


@router.post("/canary/control")
async def control_canary(
    req: CanaryControlRequest,
    _user: auth.User = Depends(auth.require_admin),
) -> JSONResponse:
    controller = canary_rollout.CanaryController()
    try:
        state = canary_rollout.load_state()
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
    payload = {
        "control": req.command,
        "canary": dashboard.canary_snapshot(),
        "updated_at": new_state.updated_at,
    }
    dashboard.publish_update(payload)
    return JSONResponse(payload)


@router.post("/synthetic-progress")
async def synthetic_progress(
    req: SyntheticProgressRequest,
    _user: auth.User = Depends(auth.require_admin),
) -> JSONResponse:
    try:
        payload = dashboard.synthetic_progress_event(
            tag=req.tag,
            progress_percent=req.progress_percent,
            status=req.status,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(payload)
