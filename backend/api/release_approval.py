"""OP-949 H4 — Operator approval web API (replaces the JIRA +2 gate).

The Sprint H L3 release conductor (`docs/operations/release-conductor-
pattern.md`) gates the ``staging → canary_5`` transition on an explicit
operator approval — the L1 R8 ADR-0018 R8 control. Prior to H4 that
approval was a manual Gerrit +2 from a member of the ``non-ai-reviewer``
group; H4 moves the gate into the web UI so operators can scan the
queue, see canary % + SLO state per release, and approve / abort in one
click.

This module owns the HTTP surface:

* ``GET  /api/v1/release-conductor/approvals``
    Returns every release currently sitting in the ``pending_approval``
    sub-state (``release_state.state == staging``) + every release that
    was already approved into ``canary_5`` but is still abortable (AC
    #6 "approval reversible before canary advances"). Per-row payload:
    version, canary %, SLO snapshot, last-transition timestamp,
    ``row_version`` (for the optimistic-locking handshake on POST).

* ``POST /api/v1/release-conductor/approvals/approve``
    Operator clicks the approve button. Body: ``{release_id,
    row_version}``. On success transitions ``staging → canary_5`` and
    persists an audit event through the H2 event router (AC #4
    "Operator click → POST → H2 event → H3 transition").

* ``POST /api/v1/release-conductor/approvals/abort``
    Operator clicks the abort button. Reversible per AC #6:
    ``staging → failed`` (the operator never approved) or
    ``canary_5 → rolled_back`` (the operator changed their mind before
    canary advanced to 25%).

Auth contract (AC #5)
=====================
The Gerrit-side gate is "+2 from a member of the ``non-ai-reviewer``
group" — i.e. a human operator, never a bot. We mirror that here by
requiring at least the ``operator`` role *and* by refusing API-key
bearer-token callers (those are bots / service accounts that auth as
``id="apikey:..."`` in :func:`backend.auth.current_user`). This is the
L3 analogue of the ``non-ai-reviewer`` group membership check the
submit-rule applies in Gerrit.

Error catalog (per ticket description)
======================================
* ``ApprovalAuthRefused``     — caller fails the role / non-bot check;
                                 returned as HTTP 401 (no session) or
                                 403 (session but not non-ai-reviewer).
* ``RaceConditionApproval``   — two operators clicked at the same time
                                 (or the operator's view was stale);
                                 the loser sees HTTP 409 + the current
                                 state in ``detail`` so the UI can
                                 re-fetch and show "already approved".
* ``BackendDispatchFailed``   — audit event insert failed (Postgres
                                 unreachable, etc.); state transition
                                 has already committed so the UI must
                                 offer a retry that idempotently
                                 re-reads state rather than re-writing.

Out of scope
============
* No DB schema change — ``pending_approval`` is a Python-level
  sub-state alias for ``staging`` (see ``STATE_PENDING_APPROVAL`` in
  :mod:`backend.release_conductor.state_machine`); the ``db`` area is
  fenced off for OP-949.
* No new SSE event topic — the UI listens to the existing
  ``release.dashboard.updated`` event and re-fetches the list.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from backend import auth
from backend.release_conductor import event_router, state_machine
from backend.release_conductor.event_handlers import slo_handlers


logger = logging.getLogger(__name__)


router = APIRouter(prefix="/release-conductor", tags=["release-conductor"])


# ─── Constants ───────────────────────────────────────────────────────
EVENT_SOURCE_OPERATOR = "operator_approval"
EVENT_TYPE_APPROVED = "release.approved"
EVENT_TYPE_ABORTED = "release.aborted"

# State -> canary percent for the UI badge. ``None`` for non-canary
# states (the UI renders "—" then).
_CANARY_PCT: dict[str, int] = {
    state_machine.STATE_STAGING: 0,
    state_machine.STATE_CANARY_5: 5,
    state_machine.STATE_CANARY_25: 25,
    state_machine.STATE_CANARY_100: 100,
}


# Abort edges per AC #6 ("approval reversible before canary advances").
# staging  -> failed       : operator never approved; abort = mark failed.
# canary_5 -> rolled_back  : operator approved but is bailing before 25%.
_ABORT_EDGES: dict[str, str] = {
    state_machine.STATE_STAGING: state_machine.STATE_FAILED,
    state_machine.STATE_CANARY_5: state_machine.STATE_ROLLED_BACK,
}


# States surfaced by the list endpoint. ``staging`` is the canonical
# "pending approval" sub-state; ``canary_5`` rides along so the UI can
# offer the abort path before canary auto-advances.
_LISTED_STATES: tuple[str, ...] = (
    state_machine.STATE_STAGING,
    state_machine.STATE_CANARY_5,
)


# ─── Auth dependency (AC #5) ─────────────────────────────────────────
async def require_non_ai_reviewer(
    request: Request,
    user: auth.User = Depends(auth.current_user),
) -> auth.User:
    """Mirror the Gerrit ``non-ai-reviewer`` group gate in HTTP land.

    Two checks:

    1. ``user.role`` is at least ``operator`` (the application-level
       human-reviewer tier — viewer is read-only and cannot trigger
       state transitions).
    2. ``user.id`` does NOT start with ``apikey:`` — API-key bearer
       tokens are service accounts / bots. The Gerrit submit-rule
       refuses bot +2 votes from the ``non-ai-reviewer`` slot; we
       refuse bot approvals in the web UI for the same reason.

    Anything else surfaces as ``ApprovalAuthRefused``: 401 when there is
    no session at all, 403 when the session exists but fails one of the
    two checks.

    Also enforces CSRF for state-changing methods so the bearer-token
    refusal is consistent with the rest of the backend's mutation
    surface — without CSRF the cookie-based session is still vulnerable
    to a cross-origin POST.
    """
    if not auth.role_at_least(user.role, "operator"):
        raise HTTPException(
            status_code=403,
            detail=(
                "ApprovalAuthRefused — operator approvals require a "
                f"role of operator or higher (you are {user.role!r})"
            ),
        )
    if user.id.startswith("apikey:"):
        raise HTTPException(
            status_code=403,
            detail=(
                "ApprovalAuthRefused — operator approvals require a "
                "human session, not a bearer-token bot (non-ai-reviewer "
                "group analogue)"
            ),
        )

    # CSRF — mirrors require_role's session check for non-GET requests.
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        cookie = request.cookies.get(auth.SESSION_COOKIE) or ""
        sess = await auth.get_session(cookie) if cookie else None
        auth.csrf_check(request, sess)

    return user


# ─── Wire shapes ─────────────────────────────────────────────────────
class ApprovalRow(BaseModel):
    """One row in the ``GET /approvals`` response payload."""

    release_id: str
    version: str
    state: str
    sub_state: str  # "pending_approval" or "approved_canary"
    row_version: int
    canary_percent: int | None
    slo_snapshot: dict[str, Any]
    last_transition_at: str
    created_at: str


class ApprovalsListResponse(BaseModel):
    releases: list[ApprovalRow]
    generated_at: str


class ApprovalActionRequest(BaseModel):
    """Operator approve / abort wire envelope.

    ``row_version`` is the optimistic-locking handshake — the UI echoes
    back the value it saw on the last GET; the backend refuses if the
    state row has moved on (``RaceConditionApproval``).
    """

    release_id: str = Field(..., min_length=1, max_length=64)
    row_version: int = Field(..., ge=0)


class ApprovalActionResponse(BaseModel):
    release_id: str
    state: str
    row_version: int
    last_transition_at: str
    audit_event_row_id: int


# ─── Helpers ─────────────────────────────────────────────────────────
def _slo_snapshot_for(release_id: str) -> dict[str, Any]:
    """Read the in-process SLO halt registry for ``release_id``.

    Returns ``{halted: bool, breach: dict|None}``. The halt registry is
    the same one consulted by the runner pickup gate, so the operator
    sees the same signal that's blocking the conductor's advance loop.
    """
    halted_release = slo_handlers.is_halted(release_id)
    halted_global = slo_handlers.is_halted(None)
    breach: dict[str, Any] | None = None
    if halted_release or halted_global:
        # Access the private dict under the public lock — the halt-state
        # module is a sibling here, so reaching past the underscore is
        # acceptable (alternative would be adding a public getter; out
        # of scope for OP-949).
        with slo_handlers._HALT_LOCK:
            breach = (
                slo_handlers._HALT_STATE.get(release_id)
                or slo_handlers._HALT_STATE.get("__global__")
            )
    return {
        "halted": bool(halted_release or halted_global),
        "breach": breach,
    }


def _sub_state(state: str) -> str:
    """Human-readable sub-state label for the UI badge."""
    if state == state_machine.STATE_STAGING:
        return "pending_approval"
    if state == state_machine.STATE_CANARY_5:
        return "approved_canary"
    return state


def _now_iso() -> str:
    return state_machine._now_iso()


def _persist_audit_event(
    *,
    release_id: str,
    version: str,
    action: str,
    actor: str,
    row_version_before: int,
) -> dict[str, Any]:
    """Audit the operator action through the H2 event router (AC #4).

    Failure to persist the audit row raises EventInsertFailed; the
    caller surfaces it as ``BackendDispatchFailed`` (500 + retry hint).
    Idempotency is provided by ``(source, event_id)`` — the
    ``event_id`` embeds the actor + row_version + a monotonic
    timestamp so a duplicate POST from the same operator collapses on
    the UNIQUE constraint but two distinct approvals are kept.
    """
    event_id = (
        f"{release_id}:{action}:{row_version_before}:"
        f"{actor}:{int(time.time() * 1000)}"
    )
    event_type = (
        EVENT_TYPE_APPROVED if action == "approve" else EVENT_TYPE_ABORTED
    )
    return event_router.persist_event(
        source=EVENT_SOURCE_OPERATOR,
        event_type=event_type,
        event_id=event_id,
        payload={
            "release_id": release_id,
            "version": version,
            "action": action,
            "actor": actor,
            "row_version_before": row_version_before,
        },
    )


def _row_to_approval_row(row: dict[str, Any]) -> ApprovalRow:
    return ApprovalRow(
        release_id=row["release_id"],
        version=row["version"],
        state=row["state"],
        sub_state=_sub_state(row["state"]),
        row_version=int(row["row_version"]),
        canary_percent=_CANARY_PCT.get(row["state"]),
        slo_snapshot=_slo_snapshot_for(row["release_id"]),
        last_transition_at=str(row["last_transition_at"]),
        created_at=str(row["created_at"]),
    )


def _actor_label(user: auth.User) -> str:
    return user.email or user.name or user.id


# ─── Endpoints ───────────────────────────────────────────────────────
@router.get("/approvals", response_model=ApprovalsListResponse)
async def list_approvals(
    _user: auth.User = Depends(require_non_ai_reviewer),
) -> ApprovalsListResponse:
    """AC #2 — list every release in state ``pending_approval``.

    Also includes ``canary_5`` rows so the UI can offer the abort path
    per AC #6 ("approval reversible before canary advances"). Sorting
    is ``last_transition_at DESC`` so the most-recently-blocked release
    sits at the top of the list.
    """
    rows: list[dict[str, Any]] = []
    for state in _LISTED_STATES:
        rows.extend(state_machine.list_in_state(state))
    # Stable sort: freshest transition first, then by version for ties.
    rows.sort(
        key=lambda r: (str(r["last_transition_at"]), str(r["version"])),
        reverse=True,
    )
    return ApprovalsListResponse(
        releases=[_row_to_approval_row(r) for r in rows],
        generated_at=_now_iso(),
    )


@router.post(
    "/approvals/approve", response_model=ApprovalActionResponse
)
async def approve_release(
    body: ApprovalActionRequest,
    user: auth.User = Depends(require_non_ai_reviewer),
) -> ApprovalActionResponse:
    """AC #4 — operator approves; transitions ``staging → canary_5``.

    409 ``RaceConditionApproval`` if the row isn't in ``staging`` or
    the operator's ``row_version`` is stale (two operators clicked
    simultaneously / the operator's view is out of date).
    """
    try:
        row = state_machine.get_by_release_id(release_id=body.release_id)
    except state_machine.ReleaseNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if row["state"] != state_machine.STATE_PENDING_APPROVAL:
        raise HTTPException(
            status_code=409,
            detail=(
                f"RaceConditionApproval — release {body.release_id!r} is "
                f"in state {row['state']!r}, not pending approval; "
                "another operator may have already acted"
            ),
        )
    if int(row["row_version"]) != int(body.row_version):
        raise HTTPException(
            status_code=409,
            detail=(
                "RaceConditionApproval — your view is stale "
                f"(saw row_version={body.row_version}, current is "
                f"{row['row_version']}); please refresh and retry"
            ),
        )

    actor = _actor_label(user)
    try:
        result = state_machine.transition(
            release_id=body.release_id,
            from_state=state_machine.STATE_PENDING_APPROVAL,
            to_state=state_machine.STATE_CANARY_5,
            reason=f"operator_approved by {actor}",
        )
    except state_machine.RaceConditionDoubleTransition as exc:
        raise HTTPException(
            status_code=409,
            detail=f"RaceConditionApproval — {exc}",
        ) from exc
    except state_machine.IllegalStateTransition as exc:
        # Edge case: the row state moved between our pre-check SELECT
        # and the transition's atomic UPDATE. Surface as the same
        # error catalog entry so the UI handles it identically.
        raise HTTPException(
            status_code=409,
            detail=f"RaceConditionApproval — {exc}",
        ) from exc

    try:
        audit = _persist_audit_event(
            release_id=body.release_id,
            version=row["version"],
            action="approve",
            actor=actor,
            row_version_before=int(body.row_version),
        )
    except event_router.EventInsertFailed as exc:
        # State has already advanced; audit is best-effort but we still
        # want the UI to know the action's audit trail is broken so
        # ops can investigate. 500 + retry hint per the error catalog.
        logger.error(
            "release_approval.audit_insert_failed "
            "release_id=%s err=%s",
            body.release_id,
            exc,
        )
        raise HTTPException(
            status_code=500,
            detail=(
                "BackendDispatchFailed — state transition committed but "
                "audit event insert failed; please retry the page "
                "refresh and verify the release advanced"
            ),
        ) from exc

    logger.info(
        "release_approval.approved release_id=%s actor=%s "
        "row_version=%s -> %s",
        body.release_id,
        actor,
        body.row_version,
        result["row_version"],
    )
    return ApprovalActionResponse(
        release_id=body.release_id,
        state=result["state"],
        row_version=int(result["row_version"]),
        last_transition_at=result["last_transition_at"],
        audit_event_row_id=int(audit.get("event_row_id", 0)),
    )


@router.post(
    "/approvals/abort", response_model=ApprovalActionResponse
)
async def abort_release(
    body: ApprovalActionRequest,
    user: auth.User = Depends(require_non_ai_reviewer),
) -> ApprovalActionResponse:
    """AC #6 — operator aborts an in-flight approval.

    Two abort edges per the docstring constant ``_ABORT_EDGES``:
    ``staging → failed`` (un-approve) and
    ``canary_5 → rolled_back`` (bail before canary advances).
    """
    try:
        row = state_machine.get_by_release_id(release_id=body.release_id)
    except state_machine.ReleaseNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    current = row["state"]
    target = _ABORT_EDGES.get(current)
    if target is None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"RaceConditionApproval — cannot abort release "
                f"{body.release_id!r} in state {current!r}; abort is "
                f"only valid from {sorted(_ABORT_EDGES)}"
            ),
        )
    if int(row["row_version"]) != int(body.row_version):
        raise HTTPException(
            status_code=409,
            detail=(
                "RaceConditionApproval — your view is stale "
                f"(saw row_version={body.row_version}, current is "
                f"{row['row_version']}); please refresh and retry"
            ),
        )

    actor = _actor_label(user)
    try:
        result = state_machine.transition(
            release_id=body.release_id,
            from_state=current,
            to_state=target,
            reason=f"operator_aborted by {actor}",
        )
    except state_machine.RaceConditionDoubleTransition as exc:
        raise HTTPException(
            status_code=409,
            detail=f"RaceConditionApproval — {exc}",
        ) from exc
    except state_machine.IllegalStateTransition as exc:
        raise HTTPException(
            status_code=409,
            detail=f"RaceConditionApproval — {exc}",
        ) from exc

    try:
        audit = _persist_audit_event(
            release_id=body.release_id,
            version=row["version"],
            action="abort",
            actor=actor,
            row_version_before=int(body.row_version),
        )
    except event_router.EventInsertFailed as exc:
        logger.error(
            "release_approval.audit_insert_failed "
            "release_id=%s err=%s",
            body.release_id,
            exc,
        )
        raise HTTPException(
            status_code=500,
            detail=(
                "BackendDispatchFailed — state transition committed but "
                "audit event insert failed; please retry the page "
                "refresh and verify the release rolled back"
            ),
        ) from exc

    logger.info(
        "release_approval.aborted release_id=%s actor=%s "
        "from=%s -> %s",
        body.release_id,
        actor,
        current,
        target,
    )
    return ApprovalActionResponse(
        release_id=body.release_id,
        state=result["state"],
        row_version=int(result["row_version"]),
        last_transition_at=result["last_transition_at"],
        audit_event_row_id=int(audit.get("event_row_id", 0)),
    )


__all__ = [
    "ApprovalActionRequest",
    "ApprovalActionResponse",
    "ApprovalRow",
    "ApprovalsListResponse",
    "EVENT_SOURCE_OPERATOR",
    "EVENT_TYPE_ABORTED",
    "EVENT_TYPE_APPROVED",
    "abort_release",
    "approve_release",
    "list_approvals",
    "require_non_ai_reviewer",
    "router",
]
