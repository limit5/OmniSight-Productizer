"""OP-770 admin release approval page (OP-779 D18: reason text + audit)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from backend import auth
from backend.production_release import (
    approve_and_ship,
    cancel_release,
    latest_approval,
    load_approval,
    render_approval_html,
)


router = APIRouter(prefix="/admin/release-approval", tags=["release-approval"])


def _load_for_request(tag: str | None):
    if tag:
        try:
            return load_approval(tag)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    record = latest_approval()
    if record is None:
        raise HTTPException(status_code=404, detail="no release approvals found")
    return record


def _validate_reason(reason: str | None) -> str:
    """OP-779 D18 -- the approval form must carry a non-empty reason."""
    if reason is None or not reason.strip():
        raise HTTPException(
            status_code=400,
            detail="reason text is required (OP-779 audit requirement)",
        )
    return reason.strip()


@router.get("", response_class=HTMLResponse)
async def release_approval_page(
    tag: str | None = None,
    _user: auth.User = Depends(auth.require_admin),
) -> HTMLResponse:
    record = _load_for_request(tag)
    return HTMLResponse(render_approval_html(record))


@router.get("/api")
async def release_approval_api(
    tag: str | None = None,
    _user: auth.User = Depends(auth.require_admin),
) -> JSONResponse:
    record = _load_for_request(tag)
    return JSONResponse(record.__dict__)


@router.post("/ship")
async def ship_release(
    request: Request,
    tag: str,
    reason: str = Form(default=""),
    user: auth.User = Depends(auth.require_admin),
) -> Response:
    reason_text = _validate_reason(reason)
    record = await approve_and_ship(tag, actor=user.email, reason=reason_text)
    accept = request.headers.get("accept", "")
    if "application/json" in accept:
        return JSONResponse(record.__dict__)
    return RedirectResponse(
        url=f"/admin/release-approval?tag={record.tag}",
        status_code=303,
    )


@router.post("/cancel")
async def cancel_release_approval(
    request: Request,
    tag: str,
    reason: str = Form(default=""),
    user: auth.User = Depends(auth.require_admin),
) -> Response:
    reason_text = _validate_reason(reason)
    record = await cancel_release(tag, actor=user.email, reason=reason_text)
    accept = request.headers.get("accept", "")
    if "application/json" in accept:
        return JSONResponse(record.__dict__)
    return RedirectResponse(
        url=f"/admin/release-approval?tag={record.tag}",
        status_code=303,
    )
