"""Pipeline-coordinator decision-engine seam (29f-coord skeleton).

ADR-0021 §2.1 specifies a *hybrid* decision engine: deterministic Python
Tier-1 rules handle ≥90 % of decisions, with LLM consultation reserved for
novel / multi-plausible cases. None of that logic exists yet — this is the
foundation ticket (29f-coord). What ships here is the **contract** the real
rule set (29f-3), cold-start orchestrator (29f-7) and event subscribers
(29f-8) all plug into without changing the daemon:

    DecisionContext  — everything the engine is allowed to read for a tick
    DecisionEngine.evaluate(ctx) -> DecisionResult
    Action / NoopAction          — the action vocabulary
    DecisionResult               — actions + reason + decision_id

The skeleton engine is intentionally empty: it returns a single
``NoopAction`` with reason ``skeleton_noop`` and ``dry_run=True`` so the
daemon can run end-to-end (heartbeat + decision-log) before any real
decision logic exists.

Module-global state audit (per project SOP)
-------------------------------------------
Frozen dataclasses, a Protocol, and a stateless engine class. No module
globals, no import-time I/O. ``engine_version`` is a class constant so the
decision-log records which engine produced each tick.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Protocol, runtime_checkable

from backend.agents.pipeline_coordinator_capacity import CapacitySnapshot

# Bumped when the engine's decision semantics change. 0.x = skeleton era.
SKELETON_ENGINE_VERSION = "0.1.0-skeleton"

# Stable reason string the skeleton emits each tick (asserted by tests and
# grep-able in the decision log per ADR-0021 §9 L4).
SKELETON_NOOP_REASON = "skeleton_noop"


@dataclass(frozen=True)
class Action:
    """A single thing the coordinator decides to do.

    The skeleton only ever emits :class:`NoopAction`; real action kinds
    (relabel, transition, escalate-to-operator — ADR-0021 §6) subclass or
    extend ``kind`` in 29f-3 without changing this envelope. ``dry_run``
    defaults to True so a half-wired daemon can never mutate JIRA/Gerrit.
    """

    kind: str
    target: str = ""
    params: Mapping[str, Any] = field(default_factory=dict)
    dry_run: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not self.kind.strip():
            raise ValueError("Action.kind must be a non-empty string")
        object.__setattr__(self, "params", dict(self.params))

    def to_record(self) -> dict[str, Any]:
        """Serialisable form embedded in a decision-log ``actions`` array."""
        return {
            "kind": self.kind,
            "target": self.target,
            "params": dict(self.params),
            "dry_run": self.dry_run,
        }


@dataclass(frozen=True)
class NoopAction(Action):
    """The do-nothing action the skeleton engine returns every tick."""

    def __init__(self) -> None:
        super().__init__(kind="noop", target="", params={}, dry_run=True)


@dataclass(frozen=True)
class DecisionContext:
    """Everything the engine may read to make one decision (a "tick").

    Stateless-across-restarts (ADR-0021 §3.2): the daemon re-builds this
    each tick from the injected clock + work-graph cache + capacity
    snapshot. The skeleton populates only ``now`` and ``capacity``; later
    phases add the JIRA work graph, bridge-log tail and event payload.
    """

    now: datetime
    capacity: CapacitySnapshot
    mode: str = "skeleton"
    # Open-ended bag for later phases (event payload, work-graph cache,
    # bridge-log tail). Skeleton leaves it empty.
    work_graph: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.now.tzinfo is None:
            raise ValueError("DecisionContext.now must be timezone-aware")
        object.__setattr__(self, "work_graph", dict(self.work_graph))


@dataclass(frozen=True)
class DecisionResult:
    """Outcome of one ``evaluate`` call.

    ``decision_id`` is a fresh UUID per tick so a decision-log line can be
    correlated with downstream action records (and L6 crash-recovery can
    pair ``shutdown_began`` / ``shutdown_complete`` with the right tick).
    """

    decision_id: str
    actions: tuple[Action, ...]
    reason: str
    mode: str
    engine_version: str

    @property
    def is_noop(self) -> bool:
        return all(isinstance(a, NoopAction) for a in self.actions) or not self.actions


@runtime_checkable
class DecisionEngineProtocol(Protocol):
    """The seam 29f-3 implements; the daemon depends on this, not the class."""

    engine_version: str

    def evaluate(self, ctx: DecisionContext) -> DecisionResult: ...


class DecisionEngine:
    """Skeleton decision engine — logs one no-op per tick, decides nothing.

    Real Tier-1 rules + Tier-2 LLM consultation land in 29f-3. Keeping the
    skeleton a concrete class (rather than only a Protocol) lets the daemon
    default-construct an engine and run end-to-end today.
    """

    engine_version: str = SKELETON_ENGINE_VERSION

    def evaluate(self, ctx: DecisionContext) -> DecisionResult:
        """Return a single :class:`NoopAction`. Pure — reads nothing, no I/O."""
        return DecisionResult(
            decision_id=uuid.uuid4().hex,
            actions=(NoopAction(),),
            reason=SKELETON_NOOP_REASON,
            mode=ctx.mode,
            engine_version=self.engine_version,
        )
