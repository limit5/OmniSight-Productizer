"""P5 operator gate — approve/reject Sora's dangerous-action proposals.

Sora files 'pending' proposals (propose_action). This router is the HUMAN side:
an admin lists them and approves/rejects. Approving atomically flips the row and
— only for the narrow, allowlisted, flag-gated cases — triggers execution via
backend.agents.action_executor (restart of an allowlisted user unit; deploy/
promote/rollback stay proposal-only). Every decision is admin-authed and carries
a required reason. Sora cannot reach any of this — she has no execute tool.
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import JSONResponse

from backend import auth
from backend.agents import action_executor
from backend.api.release_approval import _assert_human_operator  # human-only gate (rejects apikey/bot)
from backend.db_pool import get_pool

router = APIRouter(prefix="/proposed-actions", tags=["proposed-actions"])


def _require_reason(reason: str | None) -> str:
    if reason is None or not reason.strip():
        raise HTTPException(status_code=400, detail="reason text is required (audit).")
    return reason.strip()


@router.get("")
async def list_actions(
    status: str | None = None,
    _user: auth.User = Depends(auth.require_admin),
) -> JSONResponse:
    """List proposals, optionally filtered by status (default: pending)."""
    from backend import db
    async with get_pool().acquire() as conn:
        rows = await db.list_proposed_actions(conn, status=status or "pending", limit=100)
    return JSONResponse({"status": status or "pending", "count": len(rows), "actions": rows})


@router.get("/{action_id}")
async def get_action(
    action_id: str,
    _user: auth.User = Depends(auth.require_admin),
) -> JSONResponse:
    from backend import db
    async with get_pool().acquire() as conn:
        row = await db.get_proposed_action(conn, action_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"no proposal {action_id}")
    return JSONResponse(row)


@router.post("/{action_id}/reject")
async def reject_action(
    action_id: str,
    reason: str = Form(default=""),
    user: auth.User = Depends(auth.require_admin),
) -> JSONResponse:
    _assert_human_operator(user)   # bot/API-key principals cannot decide (audit r3 BLOCKER)
    reason_text = _require_reason(reason)
    from backend import db
    async with get_pool().acquire() as conn:
        row = await db.get_proposed_action(conn, action_id)
        if not row:
            raise HTTPException(status_code=404, detail=f"no proposal {action_id}")
        won = await db.decide_proposed_action(
            conn, action_id, decision="rejected", decided_by=user.email,
            reason=reason_text, at=time.time())
    if not won:
        raise HTTPException(status_code=409, detail=f"proposal {action_id} is not pending (already decided)")
    return JSONResponse({"id": action_id, "status": "rejected", "decided_by": user.email, "reason": reason_text})


@router.post("/{action_id}/approve")
async def approve_action(
    action_id: str,
    reason: str = Form(default=""),
    dry_run: bool = Form(default=False),   # audit r3: was a query param → form 'dry_run=true' was silently ignored
    user: auth.User = Depends(auth.require_admin),
) -> JSONResponse:
    """Approve a pending proposal. Atomically flips pending→approved (so a
    double-approve can't race), then attempts execution via the narrow executor.
    Real execution runs ONLY for an allowlisted restart with OMNISIGHT_P5_EXECUTE
    on; otherwise it's a dry-run and the row stays 'approved'."""
    _assert_human_operator(user)   # bot/API-key principals cannot approve (audit r3 BLOCKER)
    reason_text = _require_reason(reason)
    from backend import db
    async with get_pool().acquire() as conn:
        row = await db.get_proposed_action(conn, action_id)
        if not row:
            raise HTTPException(status_code=404, detail=f"no proposal {action_id}")
        won = await db.decide_proposed_action(
            conn, action_id, decision="approved", decided_by=user.email,
            reason=reason_text, at=time.time())
        if not won:
            raise HTTPException(status_code=409, detail=f"proposal {action_id} is not pending (already decided)")

    # Execute (narrow + gated). ``row`` carries action_kind/params. Any executor
    # raise (r3 EXEC-01) is caught so the row never strands without a result.
    try:
        result = await action_executor.execute_approved_action(row, actor=user.email, dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001
        result = {"ok": False, "executed": False, "dry_run": False, "detail": f"executor error: {exc}"}

    # DEFER path (audit r5 DRR-01 — the important fix): ONLY a real, enabled,
    # allowlisted restart is deferred to the host. We mark 'executing' HERE —
    # AFTER the gate decided to defer — NOT unconditionally before it. So a
    # dry-run / OMNISIGHT_P5_EXECUTE-off / refused approval can NEVER transiently
    # sit in 'executing' and be picked up + really restarted by the host (which
    # would defeat the dry-run flag AND the master off-switch). Post-fix,
    # 'executing'+action_kind='restart' means EXACTLY "a real deferred restart the
    # host should run". A crash before this mark leaves the row 'approved' → the
    # host never touches it (fail-safe).
    if result.get("deferred_to_host"):
        async with get_pool().acquire() as conn:
            await db.mark_proposed_action_executing(conn, action_id, at=time.time())
        return JSONResponse({
            "id": action_id, "approved_by": user.email, "reason": reason_text,
            "status": "executing", "execution": result,
        })

    # Non-deferred → a terminal status written FROM 'approved' (r5 CIR-02: the
    # expected_prior guard means this can never clobber a host-written terminal).
    # Distinguish a genuine REFUSAL (off-allowlist / bad-param / executor error)
    # from a benign dry-run or a proposal-only 'run via release-train' (r5 DRR-05).
    if result.get("dry_run") or result.get("proposal_only"):
        new_status = "approved"          # intent recorded; nothing ran here
    elif not result.get("ok"):
        new_status = "refused"           # off-allowlist / bad param / executor error
    else:
        new_status = "approved"
    async with get_pool().acquire() as conn:
        await db.set_proposed_action_result(
            conn, action_id, status=new_status, result=result.get("detail", ""),
            at=time.time(), expected_prior="approved")

    return JSONResponse({
        "id": action_id, "approved_by": user.email, "reason": reason_text,
        "status": new_status, "execution": result,
    })
