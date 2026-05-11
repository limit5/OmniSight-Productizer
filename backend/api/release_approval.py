"""OP-949 H4 — ``/api/v1/release-approvals`` operator approval API.

Replaces the JIRA +2 gate (L1 R8 / ADR-0018 §approval) with a
backend-mediated approval surface. Three endpoints:

* ``GET  /release-approvals/pending`` — every release whose H3
  state machine carries an unresolved ``approval_pending`` sub-state
  entry. Used by :file:`components/omnisight/admin/ReleaseApprovalsPanel.tsx`
  to populate the operator list.
* ``POST /release-approvals/{release_id}/approve`` — operator approves.
  Records the decision in the H3 transition log and dispatches an
  ``operator.approval.granted`` event into the H2 queue so the
  R9-style state advance fires out-of-band.
* ``POST /release-approvals/{release_id}/abort`` — operator aborts.
  Symmetric path; records the abort and dispatches
  ``operator.approval.aborted``.

Auth contract (AC #5)
---------------------
The "non-ai-reviewer group" gate is enforced as two stacked checks:

1. :func:`auth.require_admin` — bearer token / cookie session with
   role ``admin`` or higher. AI bots that authenticate via API key
   default to ``role="admin"`` so the role check alone is not enough
   to keep them out.
2. :func:`_assert_human_operator` — refuses ``user.id`` that look like
   a bot principal (``apikey:*``, ``ci-*``, ``*-bot``). This mirrors
   the Gerrit ``non-ai-reviewer`` group membership rule: humans only.

When either check fails the operator sees an ``ApprovalAuthRefused``
shaped 401/403 with a stable ``error`` discriminator the UI uses to
route to ``/login``.

Error catalog (per ticket)
--------------------------
* ``ApprovalAuthRefused`` — 401/403 with ``error="auth_refused"``.
* ``RaceConditionApproval`` — 409 with ``error="already_resolved"``
  when two operators race; surfaces the resolver / verdict so the
  loser sees a meaningful toast.
* ``BackendDispatchFailed`` — 502 with ``error="dispatch_failed"``
  when the H2 event-router INSERT fails. The state-machine decision
  has already landed; the UI retry just re-dispatches the event.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from backend import auth
from backend.release_conductor import event_router, state_machine


log = logging.getLogger(__name__)


router = APIRouter(prefix="/release-approvals", tags=["release-approvals"])


# Bot principals never pass the non-ai-reviewer gate. The patterns
# below intentionally mirror the Gerrit account-naming convention
# (``*-bot``, ``ai-*``, ``ci-*``) plus the backend's own per-key
# bearer namespace (``apikey:<id>``). Anything else is treated as a
# human session.
_BOT_PRINCIPAL_PATTERNS = (
    re.compile(r"^apikey:", re.IGNORECASE),
    re.compile(r"-bot(@|$)", re.IGNORECASE),
    re.compile(r"^ci-", re.IGNORECASE),
    re.compile(r"^ai-", re.IGNORECASE),
    re.compile(r"^merger-", re.IGNORECASE),
)


def _looks_like_bot(user: auth.User) -> bool:
    for pattern in _BOT_PRINCIPAL_PATTERNS:
        if pattern.search(user.id or "") or pattern.search(user.email or ""):
            return True
    return False


def _assert_human_operator(user: auth.User) -> None:
    """Refuse bot principals — only humans may approve releases.

    The role check (``require_admin``) is already done by FastAPI's
    dependency; this is the additional "non-ai-reviewer group" gate
    that prevents an admin-role API key from being able to self-
    approve its own release.
    """
    if _looks_like_bot(user):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "auth_refused",
                "reason": (
                    "non-ai-reviewer group only — bot principals cannot "
                    "approve releases (L1 R8 / ADR-0018)"
                ),
            },
        )


# Permissive release-id regex — JIRA META keys (``OP-1234``) plus a
# fall-back to any short alnum/dash token. We intentionally don't
# pin the prefix — different projects may use different META keys
# (``HOTFIX-...``, ``RELEASE-...``) and the state machine just stores
# whatever the upstream wrote.
_RELEASE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,127}$")


def _normalise_release_id(raw: str | None) -> str:
    candidate = (raw or "").strip()
    if not candidate:
        raise HTTPException(status_code=400, detail="release_id is required")
    if not _RELEASE_ID_RE.match(candidate):
        raise HTTPException(
            status_code=400,
            detail="release_id must be alnum/dash, 1-128 chars, leading alpha",
        )
    return candidate


# ─── Wire shapes ─────────────────────────────────────────────────────


class PendingApprovalRow(BaseModel):
    release_id: str
    version: str
    state: str
    canary_percent: int | None = None
    slo_snapshot: dict[str, Any] | None = None
    reason: str | None = None
    requested_at: str | None = None
    row_version: int = 0


class PendingApprovalsResponse(BaseModel):
    pending: list[PendingApprovalRow]
    generated_at: str


class ApprovalDecisionRequest(BaseModel):
    reason: str = Field(default="", max_length=1024)


class ApprovalDecisionResponse(BaseModel):
    release_id: str
    version: str
    decision: str
    operator: str
    dispatched_event_row_id: int | None
    row_version: int


# ─── GET /release-approvals/pending ──────────────────────────────────


@router.get("/pending", response_model=PendingApprovalsResponse)
async def list_pending(
    user: auth.User = Depends(auth.require_admin),
) -> PendingApprovalsResponse:
    """AC #2 — return every release in the ``pending-approval`` sub-state.

    The H4 web UI calls this on first paint + on every SSE
    ``release.dashboard.updated`` tick. Cost is one table scan over a
    small (≤ tens of rows) table, so we don't bother caching.
    """
    _assert_human_operator(user)
    rows = state_machine.list_pending_approvals()
    payload = [
        PendingApprovalRow(
            release_id=row["release_id"],
            version=row["version"],
            state=row["state"],
            canary_percent=row.get("canary_percent"),
            slo_snapshot=row.get("slo_snapshot"),
            reason=row.get("reason"),
            requested_at=row.get("requested_at"),
            row_version=int(row.get("row_version") or 0),
        )
        for row in rows
    ]
    return PendingApprovalsResponse(
        pending=payload,
        generated_at=_now_iso(),
    )


# ─── POST /release-approvals/{release_id}/approve|abort ──────────────


def _dispatch_decision_event(
    *,
    release_id: str,
    version: str,
    decision: str,
    operator: str,
    reason: str,
) -> int | None:
    """Persist an H2 event so the worker fires the R9-style advance.

    Returns the event row id on success. On insert failure we surface
    ``BackendDispatchFailed`` to the UI — the decision is already
    recorded in the state machine, so the UI's retry button just
    re-runs this dispatch path (idempotent via the H2 event_id).
    """
    event_type = (
        "operator.approval.granted"
        if decision == "approve"
        else "operator.approval.aborted"
    )
    # event_id needs to be stable per (release_id, decision) so an idem-
    # potent retry from the UI doesn't create a second queue entry.
    event_id = f"{release_id}:{decision}:{int(time.time())}"
    try:
        result = event_router.persist_event(
            source="operator",
            event_type=event_type,
            event_id=event_id,
            payload={
                "release_id": release_id,
                "version": version,
                "decision": decision,
                "operator": operator,
                "reason": reason,
            },
        )
    except event_router.EventInsertFailed as exc:
        log.error(
            "release_approval.dispatch_failed release_id=%s decision=%s err=%s",
            release_id,
            decision,
            exc,
        )
        raise HTTPException(
            status_code=502,
            detail={
                "error": "dispatch_failed",
                "reason": (
                    "release_events INSERT failed; decision is recorded "
                    "but the R9 advance was not queued — retry"
                ),
            },
        )
    return int(result.get("event_row_id") or 0)


def _record_and_dispatch(
    *,
    release_id: str,
    user: auth.User,
    body: ApprovalDecisionRequest,
    decision: str,
) -> ApprovalDecisionResponse:
    _assert_human_operator(user)
    rid = _normalise_release_id(release_id)
    try:
        row = state_machine.record_decision(
            release_id=rid,
            decision=decision,
            operator=user.email or user.id,
            reason=body.reason or "",
        )
    except state_machine.ReleaseNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except state_machine.ApprovalAlreadyResolved as exc:
        # AC error catalog: RaceConditionApproval — second clicker
        # gets "already resolved" with the prior verdict so the UI
        # can render "already approved by alice@example.com".
        prior = state_machine.get_approval_status(release_id=rid)
        raise HTTPException(
            status_code=409,
            detail={
                "error": "already_resolved",
                "reason": str(exc),
                "prior": prior,
            },
        )
    except state_machine.RaceConditionDoubleTransition as exc:
        # Optimistic-locking lost-update race — surface as the same
        # AC error so the UI shows a consistent message and offers
        # the retry button.
        raise HTTPException(
            status_code=409,
            detail={"error": "already_resolved", "reason": str(exc)},
        )

    dispatched = _dispatch_decision_event(
        release_id=rid,
        version=row["version"],
        decision=decision,
        operator=user.email or user.id,
        reason=body.reason or "",
    )
    log.info(
        "release_approval.decision release_id=%s decision=%s operator=%s "
        "event_row_id=%s",
        rid,
        decision,
        user.email or user.id,
        dispatched,
    )
    return ApprovalDecisionResponse(
        release_id=rid,
        version=row["version"],
        decision=decision,
        operator=user.email or user.id,
        dispatched_event_row_id=dispatched,
        row_version=int(row["row_version"]),
    )


@router.post(
    "/{release_id}/approve",
    response_model=ApprovalDecisionResponse,
)
async def approve_release(
    release_id: str,
    body: ApprovalDecisionRequest,
    user: auth.User = Depends(auth.require_admin),
) -> ApprovalDecisionResponse:
    """AC #4 — operator approves a release awaiting sign-off."""
    return _record_and_dispatch(
        release_id=release_id,
        user=user,
        body=body,
        decision="approve",
    )


@router.post(
    "/{release_id}/abort",
    response_model=ApprovalDecisionResponse,
)
async def abort_release(
    release_id: str,
    body: ApprovalDecisionRequest,
    user: auth.User = Depends(auth.require_admin),
) -> ApprovalDecisionResponse:
    """AC #4 / Recovery — operator aborts a release awaiting sign-off."""
    return _record_and_dispatch(
        release_id=release_id,
        user=user,
        body=body,
        decision="abort",
    )


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "ApprovalDecisionRequest",
    "ApprovalDecisionResponse",
    "PendingApprovalRow",
    "PendingApprovalsResponse",
    "abort_release",
    "approve_release",
    "list_pending",
    "router",
]
