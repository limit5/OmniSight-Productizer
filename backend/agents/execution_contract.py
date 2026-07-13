"""U6-0 T9/T10 G5a-1 execution contract (dormant, stdlib-only).

Executors receive only immutable, server-stored action identity, target, and
arguments.  They must never use model, raw caller, or other caller-supplied
arguments, and must not re-default or merge the stored arguments.

``DefinitelyNotApplied`` means no externally observable mutation, including a
partial mutation, occurred.  Timeouts, cancellation, connection loss, and any
exception after a write must be reported as ``Unknown``.  This tagged outcome
contract and its transition mapping are the frozen boundary consumed by later
execution and recovery work.

This module is additive and dormant.  It imports nothing from ``backend``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class Applied:
    """The effect definitely happened and its result is available."""

    result: Mapping[str, object]
    evidence: str


@dataclass(frozen=True)
class DefinitelyNotApplied:
    """No externally observable mutation, including a partial one, occurred."""

    error: str
    evidence: str


@dataclass(frozen=True)
class Unknown:
    """The effect may or may not have happened."""

    error: str


ExecOutcome = Applied | DefinitelyNotApplied | Unknown


@dataclass(frozen=True)
class StoredAction:
    """The server-stored action passed to an executor.

    Identity, target, and executable arguments come exclusively from the
    immutable ``prepared_actions`` row.  Only ``grant_id``,
    ``idempotency_key``, and ``recovery_mode`` are grant capability metadata.
    """

    grant_id: str
    idempotency_key: str | None
    recovery_mode: str
    adapter_namespace: str
    tool_name: str
    schema_version: str
    canonical_target: str
    executable_args: Mapping[str, object]


GRANT_STATES = frozenset({
    "pending",
    "executing",
    "consumed",
    "expired",
    "failed",
    "manual",
})
RESUME_STATES = frozenset({"queued", "claimed", "done", "manual", "failed"})
ATTEMPT_OUTCOMES = frozenset({"applied", "definitely_not_applied", "unknown"})
RECOVERY_MODES = frozenset({
    "non_replayable",
    "sink_idempotency_key",
    "read_after_write",
})


GRANT_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"executing", "expired"}),
    "executing": frozenset({"consumed", "failed", "manual"}),
    "manual": frozenset({"consumed", "failed"}),
    "consumed": frozenset(),
    "expired": frozenset(),
    "failed": frozenset(),
}
RESUME_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"claimed"}),
    "claimed": frozenset({"done", "manual", "failed", "queued"}),
    "done": frozenset(),
    "manual": frozenset(),
    "failed": frozenset(),
}


def is_valid_grant_transition(cur: str, nxt: str) -> bool:
    """Return whether ``cur`` to ``nxt`` is a frozen grant transition."""
    return nxt in GRANT_TRANSITIONS.get(cur, frozenset())


def is_valid_resume_transition(cur: str, nxt: str) -> bool:
    """Return whether ``cur`` to ``nxt`` is a frozen resume transition."""
    return nxt in RESUME_TRANSITIONS.get(cur, frozenset())


@dataclass(frozen=True)
class TerminalPlan:
    """Safe persistence plan selected from an executor outcome."""

    grant_next: str
    resume_next: str
    write_result: bool
    record_attempt: bool


def resolve_terminal(outcome: ExecOutcome, recovery_mode: str) -> TerminalPlan:
    """Resolve an executor outcome to the frozen safe persistence mapping.

    A replayable unknown outcome deliberately leaves the grant in
    ``executing`` and re-queues the resume job.  Recovery code separately
    decides whether a replay is admissible.
    """
    if recovery_mode not in RECOVERY_MODES:
        raise ValueError(f"unknown recovery mode: {recovery_mode}")

    if isinstance(outcome, Applied):
        plan = TerminalPlan("consumed", "done", True, False)
    elif isinstance(outcome, DefinitelyNotApplied):
        plan = TerminalPlan("failed", "failed", False, True)
    elif isinstance(outcome, Unknown):
        if recovery_mode == "non_replayable":
            plan = TerminalPlan("manual", "manual", False, True)
        else:
            plan = TerminalPlan("executing", "queued", False, True)
    else:
        raise TypeError(f"unsupported executor outcome: {type(outcome).__name__}")

    # ``executing`` is a stable state for a queued recovery attempt, not a
    # transition edge.  Every actual grant state change must be legal.
    assert plan.grant_next == "executing" or is_valid_grant_transition(
        "executing",
        plan.grant_next,
    )
    return plan
