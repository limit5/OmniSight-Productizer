"""Pipeline-coordinator runner-capacity seam (29f-coord skeleton).

ADR-0021 §2.2 mandates *sprint-level capacity-aware re-planning* because
subscription-tier vs API-tier runner capability + budget vary enormously.
The real capacity tracker (runner class strengths, TPM headroom, budget
burn) lands in a later phase (29f-3 rules / capacity refresh). This module
defines the importable seam that the daemon and the future rule engine
share *now* so that wiring the real implementation later does not change
the daemon contract.

Module-global state audit (per project SOP)
-------------------------------------------
Frozen dataclasses + a pure ``empty`` constructor only. No I/O, no module
import-time side effects, no mutable module globals. The daemon injects a
``CapacitySnapshot`` per tick; the skeleton hands the engine an empty one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Mapping


@dataclass(frozen=True)
class RunnerCapacity:
    """Per-runner-class capacity figures.

    ``free_slots`` is the headroom the coordinator may schedule into;
    ``budget_remaining`` is a coarse cost signal (units intentionally
    left to the later capacity phase — the skeleton never reads it).
    """

    runner_class: str
    free_slots: int = 0
    budget_remaining: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.runner_class, str) or not self.runner_class.strip():
            raise ValueError("runner_class must be a non-empty string")
        if self.free_slots < 0:
            raise ValueError("free_slots must be non-negative")


@dataclass(frozen=True)
class CapacitySnapshot:
    """Immutable snapshot of runner capacity at one coordinator tick.

    Keyed by runner class (``claude``, ``codex``, ``api``, ``local-llm``
    per ADR-0021 §1.3). The skeleton produces an empty snapshot every
    tick; 29f-3 fills it from live runner / quota state.
    """

    captured_at: datetime
    runners: Mapping[str, RunnerCapacity] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.captured_at.tzinfo is None:
            raise ValueError("captured_at must be timezone-aware")
        object.__setattr__(self, "runners", dict(self.runners))

    @classmethod
    def empty(cls, *, captured_at: datetime | None = None) -> "CapacitySnapshot":
        """An empty snapshot — the skeleton's only producer.

        ``captured_at`` defaults to ``now(UTC)``; tests pass an injected
        clock value for determinism.
        """
        return cls(captured_at=captured_at or datetime.now(timezone.utc), runners={})

    @property
    def total_free_slots(self) -> int:
        return sum(rc.free_slots for rc in self.runners.values())
