"""OP-2568 U4-D — dedicated HUMAN-ONLY memory-promotion approval.

The ONLY sanctioned way an approval row is created. Approval does NOT
publish — U4-C revalidates and publishes later. All DB work delegates
to the approvals writer module (zero ledger table names here).

Handler order is load-bearing: the human-principal assertion runs
UNCONDITIONALLY and FIRST — in "open" auth mode the anonymous synthetic
principal carries role=super_admin and would pass BOTH role gates, so
only the human check stops it. Never move it behind a role shortcut.
"""
from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from backend import auth as _au
from backend.learned_item_approval import (
    ApprovalValidationError,
    assert_human_principal,
    get_version_audience,
    record_memory_approval,
)

router = APIRouter(tags=["memory-promotions"])


class ApproveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    eval_run_id: str
    live_set_hash: str
    digest_id: str | None = None


async def handle_approve(
    user: Any, conn: Any, version_id: str, body: ApproveBody
) -> dict[str, Any]:
    """Plain handler body — tests drive it directly with fakes (no app
    boot, no pool init)."""
    assert_human_principal(user)  # BEFORE any DB access — see module doc
    audience = await get_version_audience(conn, version_id)
    # Freeze G6: global-audience items need super_admin; tenant-level
    # needs admin (already enforced by the route dependency). Audience
    # is server-derived — a body-supplied value is never trusted.
    if audience == "global" and not _au.role_at_least(user.role, "super_admin"):
        raise HTTPException(
            status_code=403,
            detail="global-audience memory promotions require super_admin",
        )
    approval_id = str(uuid4())
    await record_memory_approval(
        conn,
        approval_id=approval_id,
        version_id=version_id,
        eval_run_id=body.eval_run_id,
        live_set_hash=body.live_set_hash,
        approved_by=user.email,  # the REAL authenticated identity
        digest_id=body.digest_id,
    )
    return {"approval_id": approval_id}


@router.post("/memory-promotions/{version_id}/approve")
async def approve_memory_promotion(
    version_id: str,
    body: ApproveBody,
    user: _au.User = Depends(_au.require_admin),
) -> dict[str, Any]:
    # Human check before the pool is even touched.
    assert_human_principal(user)
    from backend.db_pool import get_pool  # lazy — import must stay side-effect-free

    async with get_pool().acquire() as conn:
        try:
            return await handle_approve(user, conn, version_id, body)
        except ApprovalValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.reason)
