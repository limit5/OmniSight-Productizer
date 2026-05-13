"""State-authority precedence resolver — SP-B-X-012 / OP-1070.

When the same ticket has divergent state across the runner's three
authoritative sources, this module decides which one wins and (optionally)
writes a row to ``release_audit`` so a post-mortem can reconstruct what
the runner saw at the conflict moment.

Precedence rules (per ``docs/sop/runner-state-authority-precedence.md``)
=======================================================================

1. **Gerrit merged state > JIRA workflow state.** If Gerrit reports a
   change as ``MERGED`` for a ticket the runner believes is still ``To Do``
   or ``In Progress``, the truth is on Gerrit. The JIRA-Gerrit bridge
   force-walks the JIRA state to match (the bridge already does this in
   ``gerrit_jira_bridge.py``; this module codifies the rule).
2. **JIRA workflow state > runner-internal state (``progress.txt``).** If
   the runner's progress.txt says ``working``-complete but JIRA says the
   ticket is ``Under Review`` (because a different runner picked it up
   between this runner's phases), JIRA wins.
3. **Runner-internal state > runner cached snapshots.** If the in-memory
   pickup snapshot disagrees with progress.txt, the on-disk progress is
   the durable truth.

Why this exists
===============

Before SP-B-X-012, the precedence was *implicit* in three different
places: the bridge's force-walk, the runner's TOCTOU re-read, and the
recovery probe in ``runner_progress.find_recovered_snapshot``. The rules
were consistent but never written down, so contributors editing any one
of them risked drift. This module makes the rule explicit + emits a
single ``[state-authority-resolved]`` audit marker so operators can
trace any resolution.

Non-goals
=========

- This module does NOT actively force-walk Gerrit/JIRA state. The bridge
  daemon already owns that. ``resolve()`` returns the decision; callers
  (bridge or runner FSM) act on it.
- This module does NOT cache its results. Each call resolves fresh from
  the inputs. Callers wanting caching should layer it on top.
- The audit-log write is non-fatal: if the ``release_audit`` write fails
  (DB down, schema drift), ``resolve()`` still returns the decision and
  logs a WARN. The decision is the authoritative output; the audit row
  is observability.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Literal, Optional, Protocol

log = logging.getLogger(__name__)


# ── Inputs ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class StateView:
    """Snapshot of one ticket's state across the three authoritative
    sources, captured at a single resolution point.

    Any field may be ``None`` if the source is unreachable or has no
    opinion for this ticket — ``resolve()`` treats ``None`` as "no
    constraint from this source" and falls through to the next source.
    """

    ticket_key: str
    # Source 1: Gerrit (highest authority)
    gerrit_change_status: Optional[Literal["NEW", "MERGED", "ABANDONED"]] = None
    gerrit_change_url: Optional[str] = None
    # Source 2: JIRA (middle authority)
    jira_status_name: Optional[str] = None
    # Source 3: runner-internal (lowest authority)
    runner_progress_phase: Optional[str] = None
    runner_snapshot_phase: Optional[str] = None


# ── Decision ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ResolveResult:
    """Outcome of one ``resolve()`` call.

    ``action`` is what the caller should DO with this result:

    - ``noop``: all sources agree, no state change needed.
    - ``force_walk_jira_to_published``: Gerrit says merged but JIRA isn't
      ``公開済み``/``Under Review`` yet. Caller should walk JIRA forward
      (the bridge daemon owns the actual transition call).
    - ``abort_runner_pickup``: JIRA workflow says the ticket already
      moved past ``進行中`` (e.g. ``Under Review`` or beyond). A new
      pickup is racing with an older completion — abort.
    - ``refresh_runner_snapshot``: runner_progress disagrees with
      runner_snapshot. Caller should re-read progress.txt.
    """

    action: Literal[
        "noop",
        "force_walk_jira_to_published",
        "abort_runner_pickup",
        "refresh_runner_snapshot",
    ]
    rule_id: Literal[1, 2, 3, 0]  # 0 = noop
    detail: dict[str, Any] = field(default_factory=dict)


# ── Audit log sink (optional) ─────────────────────────────────────────


class AuditSink(Protocol):
    """Pluggable audit-log writer. Production caller uses Postgres
    ``release_audit`` table (alembic 0207). Tests inject an in-memory
    sink that just appends to a list.
    """

    def write_release_audit_row(
        self,
        *,
        outcome: str,
        fix_version: Optional[str],
        develop_sha: str,
        main_sha: str,
        detail: dict[str, Any],
    ) -> None: ...


class _NullSink:
    def write_release_audit_row(self, **kwargs: Any) -> None:
        return None


# ── Resolver ──────────────────────────────────────────────────────────


_JIRA_PUBLISHED_STATUS_NAMES = frozenset({"公開済み", "Done", "Released", "Completed"})
_JIRA_REVIEW_STATUS_NAMES = frozenset({"Under Review", "公開済み", "Done"})
_JIRA_PROGRESS_STATUS_NAMES = frozenset({"進行中", "In Progress"})


def resolve(
    view: StateView,
    *,
    audit_sink: AuditSink | None = None,
    develop_sha: str = "",
    main_sha: str = "",
    idem_key: str | None = None,
) -> ResolveResult:
    """Apply the three precedence rules in order and return the decision.

    ``audit_sink`` is optional — if provided, the resolution is also
    written to release_audit. The audit write is non-fatal; failures
    log a WARN and do not affect the return value.

    ``idem_key`` is a deterministic token (per SP-B-X-001 convention)
    embedded in the audit row's ``detail`` JSON so replay can be
    detected.
    """
    # Rule 1: Gerrit merged > JIRA workflow
    if view.gerrit_change_status == "MERGED" and view.jira_status_name is not None:
        if view.jira_status_name not in _JIRA_PUBLISHED_STATUS_NAMES:
            result = ResolveResult(
                action="force_walk_jira_to_published",
                rule_id=1,
                detail={
                    "reason": "Gerrit merged but JIRA not in published state",
                    "gerrit_change_url": view.gerrit_change_url,
                    "jira_status_name": view.jira_status_name,
                },
            )
            _maybe_audit(result, view, audit_sink, develop_sha, main_sha, idem_key)
            return result

    # Rule 2: JIRA workflow > runner-internal
    # A new pickup attempts ``transition_to_in_progress`` only if the
    # ticket is in ``To Do``. If JIRA already shows ``Under Review`` or
    # later, an older runner is finishing the same ticket — abort.
    if (
        view.runner_progress_phase in ("picking_up", "working")
        and view.jira_status_name is not None
        and view.jira_status_name not in (_JIRA_PROGRESS_STATUS_NAMES | {"To Do"})
    ):
        result = ResolveResult(
            action="abort_runner_pickup",
            rule_id=2,
            detail={
                "reason": "JIRA workflow advanced past 進行中; concurrent pickup detected",
                "runner_progress_phase": view.runner_progress_phase,
                "jira_status_name": view.jira_status_name,
            },
        )
        _maybe_audit(result, view, audit_sink, develop_sha, main_sha, idem_key)
        return result

    # Rule 3: runner_progress > runner_snapshot
    if (
        view.runner_progress_phase is not None
        and view.runner_snapshot_phase is not None
        and view.runner_progress_phase != view.runner_snapshot_phase
    ):
        result = ResolveResult(
            action="refresh_runner_snapshot",
            rule_id=3,
            detail={
                "reason": "runner snapshot diverged from on-disk progress.txt",
                "snapshot_phase": view.runner_snapshot_phase,
                "progress_phase": view.runner_progress_phase,
            },
        )
        _maybe_audit(result, view, audit_sink, develop_sha, main_sha, idem_key)
        return result

    # No conflict
    return ResolveResult(action="noop", rule_id=0, detail={})


def _maybe_audit(
    result: ResolveResult,
    view: StateView,
    sink: AuditSink | None,
    develop_sha: str,
    main_sha: str,
    idem_key: str | None,
) -> None:
    if sink is None:
        return
    try:
        sink.write_release_audit_row(
            outcome="noop",
            fix_version=None,
            develop_sha=develop_sha or os.environ.get("OMNISIGHT_DEVELOP_SHA", ""),
            main_sha=main_sha or os.environ.get("OMNISIGHT_MAIN_SHA", ""),
            detail={
                "kind": "state-authority-resolved",
                "ticket_key": view.ticket_key,
                "rule_id": result.rule_id,
                "action": result.action,
                "idem_key": idem_key,
                **result.detail,
            },
        )
    except Exception as exc:  # noqa: BLE001 — audit must not block decision
        log.warning(
            "state_authority_resolver: audit-row write failed (%s); decision returned anyway",
            exc,
        )


__all__ = ["StateView", "ResolveResult", "AuditSink", "resolve"]
