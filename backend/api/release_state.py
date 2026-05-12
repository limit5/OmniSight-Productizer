"""OP-943 G7 — ``/api/v1/release-state`` aggregator for the D17
dashboard's "Pending releases" tab.

Read-only fan-in over :mod:`backend.release_conductor.state_machine`:
one row per open RELEASE-* / HOTFIX-* release, carrying

* ``version``
* ``state`` — the current H3 state (≈ which release child / step is in
  progress)
* ``blocking_seconds`` — how long the release has been parked in that
  state
* the operator-approval-pending sub-state (R8 / H2 gate, OP-949) so the
  tab can highlight it and light up the 1-click Approve button
  (``can_approve`` — true only when the ``approval_pending`` marker is
  set *and* the caller is an authenticated human operator, mirroring
  the human-only gate that ``POST /release-approvals/{id}/approve``
  actually enforces).

The Approve click itself is handled by the existing H4 surface
(``POST /api/v1/release-approvals/{release_id}/approve``); this module
is purely the list/aggregation half consumed by
:file:`components/omnisight/admin/PendingReleasesTable.tsx`.

Real-time: the panel re-fetches this endpoint on every shared-SSE
``release.dashboard.updated`` tick (the H3 state machine publishes that
event on each transition / approval-log write).

Auth is the standard backend chain — any logged-in user can read the
list; ``can_approve`` is the only field that depends on the caller. No
write paths are exposed here (writes go through the H4 router).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from backend import auth
from backend.release_conductor import state_machine


log = logging.getLogger(__name__)


router = APIRouter(prefix="/release-state", tags=["release-state"])


# Mirrors ``backend.api.release_approval._BOT_PRINCIPAL_PATTERNS`` —
# kept in sync so ``can_approve`` here matches the human-only gate the
# approve POST enforces. (Duplicated rather than imported: the approval
# module's copy is module-private and importing it would couple two
# otherwise-independent API surfaces.)
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


def _is_hotfix(release_id: str, version: str) -> bool:
    """Heuristic: a hotfix release carries a ``HOTFIX-`` META key or a
    ``-hotfix`` version suffix (``vX.Y.Z-hotfixN``)."""
    rid = (release_id or "").upper()
    if rid.startswith("HOTFIX-") or rid.startswith("HOTFIX/"):
        return True
    return "hotfix" in (version or "").lower()


def _parse_iso(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def _seconds_in_current_state(row: dict[str, Any], now: datetime) -> int:
    """How long the release has been parked in its current main state.

    Prefers the ``at`` of the most recent transition-log entry whose
    ``to`` equals the current state; falls back to
    ``last_transition_at`` when the log has no usable entry (defensive
    — ``create()`` always seeds a genesis entry).
    """
    current = row.get("state")
    entered_at: Any = None
    for entry in row.get("transition_log", []) or []:
        if entry.get("to") == current and entry.get("at"):
            entered_at = entry["at"]
    ts = _parse_iso(entered_at) or _parse_iso(row.get("last_transition_at"))
    if ts is None:
        return 0
    return max(0, int((now - ts).total_seconds()))


# ─── Wire shapes ─────────────────────────────────────────────────────


class PendingReleaseRow(BaseModel):
    release_id: str
    version: str
    state: str
    is_hotfix: bool = False
    last_transition_at: str | None = None
    blocking_seconds: int = 0
    approval_pending: bool = False
    approval_reason: str | None = None
    approval_requested_at: str | None = None
    canary_percent: int | None = None
    slo_snapshot: dict[str, Any] | None = None
    can_approve: bool = False


class PendingReleasesResponse(BaseModel):
    releases: list[PendingReleaseRow]
    generated_at: str


# ─── GET /release-state/pending-releases ─────────────────────────────


@router.get("/pending-releases", response_model=PendingReleasesResponse)
async def list_pending_releases(
    include_done: bool = Query(
        default=False,
        description="Include releases in a terminal state (done / failed).",
    ),
    user: auth.User = Depends(auth.current_user),
) -> PendingReleasesResponse:
    """AC #2 — every open RELEASE-* / HOTFIX-* release with version,
    current state, blocking duration, and the operator-approval gate.

    The G7 web UI calls this on first paint + on every SSE
    ``release.dashboard.updated`` tick. Cost is one table scan over a
    small (≤ tens of rows) table, so we don't bother caching.
    """
    now = datetime.now(timezone.utc)
    caller_is_human = not _looks_like_bot(user)
    rows = state_machine.list_releases(include_terminal=include_done)

    out: list[PendingReleaseRow] = []
    for row in rows:
        approval = row.get("approval") or {}
        pending = approval.get("kind") == state_machine.APPROVAL_KIND_PENDING
        version = str(row.get("version") or "")
        release_id = str(row.get("release_id") or "")
        out.append(
            PendingReleaseRow(
                release_id=release_id,
                version=version,
                state=str(row.get("state") or "unknown"),
                is_hotfix=_is_hotfix(release_id, version),
                last_transition_at=(
                    str(row["last_transition_at"])
                    if row.get("last_transition_at")
                    else None
                ),
                blocking_seconds=_seconds_in_current_state(row, now),
                approval_pending=pending,
                approval_reason=approval.get("reason") if pending else None,
                approval_requested_at=approval.get("at") if pending else None,
                canary_percent=(
                    approval.get("canary_percent") if pending else None
                ),
                slo_snapshot=approval.get("slo_snapshot") if pending else None,
                can_approve=pending and caller_is_human,
            )
        )
    log.debug(
        "release_state.pending_releases count=%d include_done=%s human=%s",
        len(out),
        include_done,
        caller_is_human,
    )
    return PendingReleasesResponse(
        releases=out,
        generated_at=now.isoformat(),
    )


__all__ = [
    "PendingReleaseRow",
    "PendingReleasesResponse",
    "list_pending_releases",
    "router",
]
