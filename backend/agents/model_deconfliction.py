"""OP-1117 (v2-Ⅹ-5e) — Model deconfliction policy for failover pickup.

When a runner instance pinned to model A (e.g. ``subscription-codex``)
fails on a ticket, this module governs whether a different runner
instance pinned to model B (e.g. ``subscription-claude``) is allowed to
pick up the same ticket. Without this gate, a ticket whose problem is
content-bound — a real merge conflict, a broken ``area:*`` label, an
infrastructure drift — thrashes through every available model in
sequence, burning N parallel cost-bearing quotas to reproduce the same
failure.

The Codex Package 2 finding cited in the OP-1117 ticket described the
shape exactly: "failover model MUST NOT pick up a ticket that another
model just failed UNLESS the failure class is one that fallback is
expected to improve". This module is that gate.

Public API
----------

* :class:`FailoverDisposition` — two-valued classification of each
  :class:`backend.agents.failure_class.FailureClass`.
* :data:`FAILOVER_DISPOSITION` — the canonical decision matrix (AC #1).
* :func:`failover_disposition` — single-class lookup.
* :func:`should_pickup_after_prior_failure` — main entry point used at
  pickup time. Consults the runner-incident buffer and decides whether
  the current runner class is allowed to pick up.
* :class:`DispatchDecision` — return value carrying allowed/refused +
  human-readable reason + the blocking incident (if any).

Integration seam
----------------

``jira_dispatch.fetch_pickable_tickets`` calls this gate per candidate
before returning. The gate is opt-out via the
``OMNISIGHT_RUNNER_DECONFLICTION_DISABLE=1`` env var so an operator can
disable it during incident response without a deploy. The same gate
applies regardless of which runner ``agent_class`` is calling — the
catalog and the decision matrix are symmetric across providers.

Scope notes
-----------

* This module only consults the in-process runner-incident buffer
  exposed by :func:`incident_recorder.get_runner_incidents`. Cross-
  process visibility (codex-bot's failures observable by claude-bot's
  pickup loop) becomes automatic once that function is reshaped to
  query the ``runner_incidents`` Postgres surface from migration 0206
  — wiring that up is owned by separate tickets and is intentionally
  out of OP-1117's area boundary (db).
* Same-model retry semantics ("codex fails, codex retries") are out of
  scope: the LLM-loop guard / repeat-2-error guard owns that path.
  This policy looks specifically at cross-model failover.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from enum import Enum
from typing import Mapping

from backend.agents.failure_class import FailureClass
from backend.agents.incident_recorder import (
    RunnerIncidentRecord,
    get_runner_incidents,
)

log = logging.getLogger(__name__)


__all__ = [
    "DECONFLICTION_DISABLE_ENV",
    "DEFAULT_LOOKBACK_SECONDS",
    "DispatchDecision",
    "FAILOVER_DISPOSITION",
    "FailoverDisposition",
    "failover_disposition",
    "is_deconfliction_disabled",
    "should_pickup_after_prior_failure",
]


# ── Failure-class catalog (AC #1) ──────────────────────────────────


class FailoverDisposition(str, Enum):
    """Two-valued classification of :class:`FailureClass` for failover.

    ``TRY_NEXT_MODEL``
        The failure is model-specific in nature — a different model is
        plausibly able to succeed where this one failed. Examples: an
        LLM reflection loop is a property of the failing model's
        reasoning, not the ticket; a checkpatch lint failure can come
        from a model's stylistic preferences; a wall-clock timeout
        often correlates with a particular model's latency profile.

    ``DONT_RETRY_OTHER_MODEL``
        The failure is ticket-content or environment bound — running a
        different model against the same input will reproduce the
        failure. Examples: an unresolvable merge conflict is an
        artifact of the repo state; a bad ``area:*`` label is an
        authoring defect; a Gerrit↔JIRA bridge drift is infrastructure
        and platform-neutral.
    """

    TRY_NEXT_MODEL = "TRY_NEXT_MODEL"
    DONT_RETRY_OTHER_MODEL = "DONT_RETRY_OTHER_MODEL"


# Canonical decision matrix. Every member of
# :class:`FailureClass` (except ``MEMORY_RECALL_AUDIT``, which is the C6
# audit slot — not a runner failure) carries an explicit disposition,
# so adding a new failure class is a co-change that surfaces via the
# completeness test in ``test_model_deconfliction``.
FAILOVER_DISPOSITION: Mapping[FailureClass, FailoverDisposition] = {
    # Model-specific — fallback model can plausibly do better.
    FailureClass.LINT_FAILURE:            FailoverDisposition.TRY_NEXT_MODEL,
    FailureClass.TEST_FAILURE:            FailoverDisposition.TRY_NEXT_MODEL,
    FailureClass.LLM_LOOP_DETECTED:       FailoverDisposition.TRY_NEXT_MODEL,
    FailureClass.RUNNER_TIMEOUT:          FailoverDisposition.TRY_NEXT_MODEL,
    FailureClass.OUTCOMES_GRADER_REFUSED: FailoverDisposition.TRY_NEXT_MODEL,
    # Ticket-content / environment / infra bound — swapping models
    # reproduces the same failure.
    FailureClass.MERGE_CONFLICT:          FailoverDisposition.DONT_RETRY_OTHER_MODEL,
    FailureClass.WORKTREE_DIRTY:          FailoverDisposition.DONT_RETRY_OTHER_MODEL,
    FailureClass.UNKNOWN_AREA_LABEL:      FailoverDisposition.DONT_RETRY_OTHER_MODEL,
    FailureClass.BRIDGE_DESYNC:           FailoverDisposition.DONT_RETRY_OTHER_MODEL,
    FailureClass.MUTEX_CONTENTION:        FailoverDisposition.DONT_RETRY_OTHER_MODEL,
    # Catch-all stays conservative: if we cannot classify the failure
    # there is no evidence a different model will fix it.
    FailureClass.OTHER:                   FailoverDisposition.DONT_RETRY_OTHER_MODEL,
}


def failover_disposition(failure_class: FailureClass) -> FailoverDisposition:
    """Return whether ``failure_class`` warrants a cross-model retry.

    Unknown classes (defensive — should not happen, the matrix is
    completeness-checked in tests) coerce to
    :attr:`FailoverDisposition.DONT_RETRY_OTHER_MODEL` so an
    unrecognised failure never accidentally widens the thrash window.
    """
    return FAILOVER_DISPOSITION.get(
        failure_class, FailoverDisposition.DONT_RETRY_OTHER_MODEL
    )


# ── Decision (AC #2 dispatch matrix; AC #3 thrash prevention) ──────


@dataclass(frozen=True)
class DispatchDecision:
    """Outcome of consulting the deconfliction policy at pickup time."""

    allowed: bool
    reason: str
    blocking_incident: RunnerIncidentRecord | None = None


# How recent a prior failure must be for the gate to count it. The AC
# wording is "another model JUST failed"; a 24-hour window is short
# enough that a ticket whose state has since changed (e.g. operator
# resolved the merge conflict offline) can be re-picked up, and long
# enough to catch real thrash (codex → claude within minutes).
DEFAULT_LOOKBACK_SECONDS = 24 * 3600

DECONFLICTION_DISABLE_ENV = "OMNISIGHT_RUNNER_DECONFLICTION_DISABLE"


def is_deconfliction_disabled() -> bool:
    """Operator escape hatch — set ``OMNISIGHT_RUNNER_DECONFLICTION_DISABLE=1``."""
    return os.environ.get(DECONFLICTION_DISABLE_ENV, "").strip() == "1"


def should_pickup_after_prior_failure(
    *,
    ticket_key: str,
    current_runner_class: str,
    now: float | None = None,
    lookback_seconds: int = DEFAULT_LOOKBACK_SECONDS,
) -> DispatchDecision:
    """Return whether ``current_runner_class`` may pick up ``ticket_key``.

    Algorithm:

    1. If the operator escape hatch is set, allow unconditionally.
    2. Fetch the runner-incident buffer for ``ticket_key``.
    3. Drop incidents older than ``lookback_seconds`` (recency gate).
    4. Drop incidents whose ``runner_class`` equals
       ``current_runner_class`` (same-model retry is out of scope; the
       reflection-loop guard owns that path).
    5. If no cross-model prior failures remain → allow.
    6. Pick the most recent cross-model failure. Its ``failure_class``
       drives the decision matrix:

       * :attr:`FailoverDisposition.TRY_NEXT_MODEL` → allow (the
         fallback is the right tool).
       * :attr:`FailoverDisposition.DONT_RETRY_OTHER_MODEL` → refuse
         (thrash-prevention; the failure is content-bound).

    The function is pure with respect to the in-memory buffer + a
    clock; a future Postgres-backed buffer changes only the call to
    :func:`get_runner_incidents` and not this decision logic.
    """
    if is_deconfliction_disabled():
        return DispatchDecision(
            allowed=True,
            reason="deconfliction-disabled (env opt-out)",
        )

    incidents = get_runner_incidents(ticket_key=ticket_key)
    if not incidents:
        return DispatchDecision(allowed=True, reason="no-prior-incidents")

    now_ts = now if now is not None else time.time()
    cutoff = now_ts - lookback_seconds

    cross_model = [
        inc
        for inc in incidents
        if inc.runner_class != current_runner_class and inc.created_at >= cutoff
    ]
    if not cross_model:
        return DispatchDecision(
            allowed=True,
            reason=(
                f"no-recent-cross-model-failures "
                f"(lookback={lookback_seconds}s)"
            ),
        )

    cross_model.sort(key=lambda inc: inc.created_at, reverse=True)
    most_recent = cross_model[0]
    disposition = failover_disposition(most_recent.failure_class)

    if disposition is FailoverDisposition.TRY_NEXT_MODEL:
        return DispatchDecision(
            allowed=True,
            reason=(
                f"prior {most_recent.runner_class} failure "
                f"{most_recent.failure_class.value} ∈ TRY_NEXT_MODEL"
            ),
            blocking_incident=most_recent,
        )

    return DispatchDecision(
        allowed=False,
        reason=(
            f"prior {most_recent.runner_class} failure "
            f"{most_recent.failure_class.value} ∈ DONT_RETRY_OTHER_MODEL "
            "— thrash-prevention"
        ),
        blocking_incident=most_recent,
    )
