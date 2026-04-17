"""L1 — Bootstrap wizard REST endpoints.

Exposes the admin-only finalize transition that closes the first-install
wizard. Finalize is guarded by the same four-gate contract driven by
:func:`backend.bootstrap.get_bootstrap_status`: if any gate is still red
OR any required step is missing from ``bootstrap_state``, the call
returns HTTP 409 with the offending signal so the wizard can surface
which step the operator still owes.

The route lives under ``/bootstrap/*`` so the global bootstrap gate
middleware in :mod:`backend.main` lets it through before the app is
finalized — otherwise finalize itself would be redirected to the wizard.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from backend import auth as _au
from backend import audit
from backend import bootstrap as _boot

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/bootstrap", tags=["bootstrap"])


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  L2 — Step 1 (force admin password rotation)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class AdminPasswordRequest(BaseModel):
    """Request body for the wizard's Step 1 password rotation.

    ``current_password`` is verified against the default admin row (the
    one still flagged ``must_change_password=1``). ``new_password`` is
    re-validated server-side using :func:`auth.validate_password_strength`
    — the 12-char + zxcvbn ≥ 3 bar owned by K7/K1.
    """

    current_password: str = Field(min_length=1, max_length=512)
    new_password: str = Field(min_length=12, max_length=512)


class AdminPasswordResponse(BaseModel):
    status: str
    admin_password_default: bool
    user_id: str


@router.post("/admin-password", response_model=AdminPasswordResponse)
async def bootstrap_admin_password(req: AdminPasswordRequest) -> AdminPasswordResponse:
    """Rotate the shipping default admin credential during the wizard.

    This endpoint is intentionally unauthenticated — during L2 Step 1 no
    admin is logged in yet. It identifies the target user as the single
    admin row carrying ``must_change_password=1`` (i.e. the one
    :func:`auth.ensure_default_admin` created with the bundled
    ``omnisight-admin`` fallback). The operator's ``current_password``
    must still verify against that row, so an attacker without access
    to the default password cannot trigger this flow.

    On success:
      * rotates the password (clears ``must_change_password``)
      * records ``bootstrap_state.admin_password_set`` with the admin's
        user id as actor
      * writes audit action ``bootstrap.admin_password_set``

    Error contract:
      * 409 if no admin still requires a password change (already done)
      * 401 if current_password is wrong
      * 422 if new_password fails the strength check
    """
    target = await _au.find_admin_requiring_password_change()
    if target is None:
        return JSONResponse(  # type: ignore[return-value]
            status_code=409,
            content={
                "detail": "No admin currently requires a password change — "
                          "default credential has already been rotated.",
                "admin_password_default": False,
            },
        )

    verified = await _au.authenticate_password(target.email, req.current_password)
    if verified is None:
        return JSONResponse(  # type: ignore[return-value]
            status_code=401,
            content={"detail": "current password is incorrect"},
        )

    strength_err = _au.validate_password_strength(req.new_password)
    if strength_err:
        return JSONResponse(  # type: ignore[return-value]
            status_code=422,
            content={"detail": strength_err},
        )

    # Rotate (clears must_change_password atomically).
    await _au.change_password(target.id, req.new_password)

    # Record the wizard step — drives the L1 finalize gate.
    await _boot.record_bootstrap_step(
        _boot.STEP_ADMIN_PASSWORD,
        actor_user_id=target.id,
        metadata={"email": target.email, "source": "wizard"},
    )

    try:
        await audit.log(
            action="bootstrap.admin_password_set",
            entity_kind="bootstrap",
            entity_id=_boot.STEP_ADMIN_PASSWORD,
            before={"must_change_password": True},
            after={"must_change_password": False, "user_id": target.id},
            actor=target.email,
        )
    except Exception as exc:
        logger.debug("bootstrap.admin_password_set audit emit failed: %s", exc)

    logger.info(
        "bootstrap: admin password rotated for user=%s via wizard Step 1",
        target.email,
    )
    return AdminPasswordResponse(
        status="password_changed",
        admin_password_default=False,
        user_id=target.id,
    )


class FinalizeRequest(BaseModel):
    reason: str | None = Field(
        default=None,
        description="Optional free-text note persisted with the finalize row.",
        max_length=500,
    )


class FinalizeResponse(BaseModel):
    finalized: bool
    status: dict
    actor_user_id: str


@router.get("/status")
async def bootstrap_status() -> dict:
    """Public read of the four-gate status + finalized flag.

    Exempt from auth so the wizard UI can poll it during install before
    the admin has even logged in. No secrets leak — each field is a
    boolean derived from already-public server state.
    """
    status = await _boot.get_bootstrap_status()
    missing = await _boot.missing_required_steps()
    return {
        "status": status.to_dict(),
        "all_green": status.all_green,
        "finalized": _boot.is_bootstrap_finalized_flag(),
        "missing_steps": missing,
    }


@router.post("/finalize", response_model=FinalizeResponse)
async def bootstrap_finalize(
    req: FinalizeRequest | None = None,
    admin: _au.User = Depends(_au.require_admin),
):
    """Close out the wizard — admin only, requires every gate green.

    409 conditions (the wizard should keep the operator on the current
    step):
      * any live gate is still red (password default, no LLM key,
        CF tunnel unprovisioned, smoke not green)
      * any required step row is missing from ``bootstrap_state``
    On success, writes a ``finalized`` audit row into
    ``bootstrap_state`` and flips the persisted
    ``bootstrap_finalized=true`` app-setting flag.
    """
    metadata: dict = {"reason": (req.reason if req else None) or ""}

    try:
        status = await _boot.mark_bootstrap_finalized(
            actor_user_id=admin.id,
            metadata=metadata,
        )
    except RuntimeError as exc:
        live_status = await _boot.get_bootstrap_status()
        missing = await _boot.missing_required_steps()
        logger.warning(
            "bootstrap: finalize refused for admin=%s: %s", admin.email, exc,
        )
        return JSONResponse(
            status_code=409,
            content={
                "detail": str(exc),
                "status": live_status.to_dict(),
                "missing_steps": missing,
            },
        )

    try:
        await audit.log(
            action="bootstrap_finalized",
            entity_kind="bootstrap",
            entity_id=_boot.STEP_FINALIZED,
            before=None,
            after={"status": status.to_dict(), **metadata},
            actor=admin.email,
        )
    except Exception as exc:
        logger.debug("bootstrap: audit log failed (non-fatal): %s", exc)

    return FinalizeResponse(
        finalized=True,
        status=status.to_dict(),
        actor_user_id=admin.id,
    )
