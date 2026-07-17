"""Human operator decisions for dormant action challenges.

Only a cookie-authenticated human tenant administrator can inspect, confirm,
or reject a challenge.  Confirming delegates to ``db.confirm_challenge``,
which records the decision, issues the grant, and queues the resume job in one
PostgreSQL transaction.  This router never executes the prepared action.

Module-global state audit: the constants below are immutable and derive to the
same values in every worker; all mutable decision state is coordinated by
PostgreSQL.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from backend import auth, db
from backend.api.release_approval import _assert_human_operator
from backend.db_pool import get_pool


router = APIRouter(prefix="/action-challenges", tags=["action-challenges"])
logger = logging.getLogger(__name__)

GRANT_TTL_SECONDS = 3600
_MAX_REASON = 1024


def _require_reason(reason: str) -> str:
    reason_text = (reason or "").strip()
    if not reason_text:
        raise HTTPException(
            status_code=400,
            detail="reason text is required (audit).",
        )
    return reason_text[:_MAX_REASON]


async def _require_human_admin(
    request: Request,
    user: auth.User = Depends(auth.current_user),
) -> auth.User:
    """Require a cookie-bound human with authoritative tenant authority."""
    # Reject header presence, including an empty value.  Besides excluding all
    # bearer principals, this prevents a stale bearer header from making
    # csrf_check exempt an otherwise cookie-authenticated request.
    if request.headers.get("authorization") is not None:
        raise HTTPException(
            status_code=403,
            detail={"error": "cookie_session_required"},
        )

    session = getattr(request.state, "session", None)
    if session is None:
        raise HTTPException(
            status_code=403,
            detail={"error": "human_session_required"},
        )
    if getattr(session, "user_id", None) != user.id:
        raise HTTPException(
            status_code=403,
            detail={"error": "session_user_mismatch"},
        )
    if user.id == "anonymous":
        raise HTTPException(
            status_code=403,
            detail={"error": "human_operator_required"},
        )

    auth.csrf_check(request, session)
    _assert_human_operator(user)

    if not auth.role_at_least(user.role, "super_admin"):
        async with get_pool().acquire() as conn:
            row = await conn.fetchrow(
                "SELECT role, status FROM user_tenant_memberships "
                "WHERE user_id = $1 AND tenant_id = $2",
                user.id,
                user.tenant_id,
            )
        if (
            row is None
            or row["status"] != "active"
            or row["role"] not in ("owner", "admin")
        ):
            raise HTTPException(
                status_code=403,
                detail={"error": "tenant_admin_required"},
            )
    return user


@router.get("/{challenge_id}")
async def get_challenge(
    challenge_id: str,
    request: Request,
    user: auth.User = Depends(_require_human_admin),
) -> JSONResponse:
    """Return the challenge and structured action details for review."""
    async with get_pool().acquire() as conn:
        challenge = await db.get_challenge(
            conn,
            challenge_id,
            tenant_id=user.tenant_id,
        )
        if challenge is None:
            raise HTTPException(
                status_code=404,
                detail=f"no challenge {challenge_id}",
            )
        prepared_action = await db.get_prepared_action(
            conn,
            challenge["action_instance_id"],
            tenant_id=user.tenant_id,
        )

    if prepared_action is None:
        logger.error(
            "challenge %s has no prepared_action (integrity)",
            challenge_id,
        )
        raise HTTPException(
            status_code=500,
            detail="challenge integrity error",
        )

    review = {
        "executable_args": json.loads(prepared_action["executable_args"]),
        "human_rendering": json.loads(prepared_action["human_rendering"]),
        "prepared_action_digest": prepared_action["prepared_action_digest"],
    }
    payload = {**challenge, "prepared_action": review}
    return JSONResponse(jsonable_encoder(payload))


@router.post("/{challenge_id}/reject")
async def reject(
    challenge_id: str,
    request: Request,
    reason: str = Form(default=""),
    user: auth.User = Depends(_require_human_admin),
) -> JSONResponse:
    """Reject one live pending challenge without issuing work."""
    reason_text = _require_reason(reason)
    async with get_pool().acquire() as conn:
        status = await db.reject_challenge(
            conn,
            tenant_id=user.tenant_id,
            challenge_id=challenge_id,
            confirmer_actor=user.email,
            confirmer_principal_type="human",
            confirmer_auth_event_id=auth.session_id_from_token(
                request.state.session.token
            ),
            reason=reason_text,
        )
    if status == "not_rejectable":
        raise HTTPException(
            status_code=409,
            detail=(
                f"challenge {challenge_id} is not a live pending challenge"
            ),
        )
    return JSONResponse(
        {
            "challenge_id": challenge_id,
            "status": status,
            "rejected_by": user.email,
        }
    )


@router.post("/{challenge_id}/confirm")
async def confirm(
    challenge_id: str,
    request: Request,
    reason: str = Form(default=""),
    user: auth.User = Depends(_require_human_admin),
) -> JSONResponse:
    """Confirm one live challenge and mint dormant grant/resume records."""
    reason_text = _require_reason(reason)
    grant_id = "grant-" + uuid.uuid4().hex
    resume_id = "resume-" + uuid.uuid4().hex
    grant_expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=GRANT_TTL_SECONDS
    )

    async with get_pool().acquire() as conn:
        try:
            status = await db.confirm_challenge(
                conn,
                tenant_id=user.tenant_id,
                challenge_id=challenge_id,
                confirmer_actor=user.email,
                confirmer_principal_type="human",
                confirmer_auth_event_id=auth.session_id_from_token(
                    request.state.session.token
                ),
                reason=reason_text,
                grant_id=grant_id,
                resume_id=resume_id,
                grant_expires_at=grant_expires_at,
            )
        except RuntimeError as exc:
            logger.exception(
                "confirm_challenge integrity error challenge_id=%s",
                challenge_id,
            )
            raise HTTPException(
                status_code=500,
                detail="confirm failed",
            ) from exc

    if status == "not_confirmable":
        raise HTTPException(
            status_code=409,
            detail=(
                f"challenge {challenge_id} is not a live pending challenge"
            ),
        )
    return JSONResponse(
        {
            "challenge_id": challenge_id,
            "status": status,
            "grant_id": grant_id,
            "resume_id": resume_id,
            "confirmed_by": user.email,
        }
    )
