"""OP-741 dead-letter dashboard backend.

Surfaces patchsets paused by the CI recovery loop guard and lets an
admin perform the four supported manual exits. The durable source of
truth is the JIRA/Gerrit label set in production; this router keeps the
same in-process registry shape as other operator dashboards so tests and
local drills can exercise the contract without live services.
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from backend import auth
from backend.agents.ci_recovery import (
    Change,
    DeadLetterEntry,
    InMemoryRecoveryBackend,
)


router = APIRouter(prefix="/admin/ci-dead-letter", tags=["admin", "ci-recovery"])

_backend = InMemoryRecoveryBackend()


def register_dead_letter(entry: DeadLetterEntry) -> None:
    """Called by the CI recovery worker after applying loop-pause."""

    _backend.dead_letters[entry.change.jira_key] = entry


def _reset_for_tests() -> None:
    _backend.labels.clear()
    _backend.calls.clear()
    _backend.dead_letters.clear()


class DeadLetterActionRequest(BaseModel):
    jira_key: str = Field(min_length=3)
    action: str = Field(pattern="^(retrigger-ci|abandon-ps|mark-quarantine|manual-review)$")
    reason: str = Field(default="", max_length=500)


@router.get("")
async def list_ci_dead_letters(
    _request: Request,
    actor: auth.User = Depends(auth.require_admin),
) -> JSONResponse:
    rows = [entry.to_payload() for entry in _backend.list_dead_letters()]
    return JSONResponse(
        status_code=200,
        content={
            "items": rows,
            "operator": actor.email,
            "fetched_at": time.time(),
        },
    )


@router.post("/action")
async def apply_ci_dead_letter_action(
    req: DeadLetterActionRequest,
    request: Request,
    actor: auth.User = Depends(auth.require_admin),
) -> JSONResponse:
    from backend import audit as _audit

    ok = _backend.dead_letter_action(req.jira_key, req.action, req.reason)
    if ok:
        try:
            await _audit.log(
                action="ci_dead_letter_operator_escape",
                entity_kind="gerrit_change",
                entity_id=req.jira_key,
                before={"status": "runner-loop-paused-pending-review"},
                after={"action": req.action, "reason": req.reason},
                actor=actor.email,
            )
        except Exception:
            pass

    return JSONResponse(
        status_code=200,
        content={
            "ok": ok,
            "jira_key": req.jira_key,
            "action": req.action,
            "operator": actor.email,
        },
    )


def seed_dead_letter_for_tests(
    *,
    jira_key: str = "OP-741",
    change_number: int = 741,
    attempt_count: int = 6,
) -> None:
    change = Change(
        number=change_number,
        jira_key=jira_key,
        patchset=1,
        commit_sha="abc123456789",
    )
    register_dead_letter(
        DeadLetterEntry(
            change=change,
            reason="loop guard hard-stop",
            paused_at=time.time(),
            attempt_count=attempt_count,
            audit_trail=[{"action": "ci_recovery_loop_hard_stop"}],
        )
    )
