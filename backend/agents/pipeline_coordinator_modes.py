"""Pipeline-coordinator behavioral-mode seam (29f-coord skeleton).

ADR-0021 §2.3 mandates a *multi-mode personality*: behavioral mode is
selected per-situation from an ``(urgency × risk × novelty × reversibility)``
profile rather than a single fixed personality. The selection logic + the
mode-specific rule overlays are a later phase. This module defines the seam
the daemon calls each tick — :class:`ModeSelector.select` — so 29f-3 can
implement real mode arbitration without changing the daemon contract.

The skeleton always selects :data:`SKELETON_MODE`, matching the
``mode:"skeleton"`` field the empty engine writes to the decision log.

Module-global state audit (per project SOP)
-------------------------------------------
Immutable constants + a stateless selector class. No module globals, no
import-time I/O.
"""

from __future__ import annotations

from dataclasses import dataclass

# The single mode the skeleton ever selects. The empty engine stamps this
# into every decision-log line (ADR-0021 §3.2 example record).
SKELETON_MODE = "skeleton"

# The mode vocabulary 29f-3 will arbitrate between. Declared now so callers
# can reference stable names; the skeleton only ever returns SKELETON_MODE.
KNOWN_MODES: tuple[str, ...] = (
    SKELETON_MODE,
    "cautious",
    "decisive",
    "consultative",
    "conservative",
)


@dataclass(frozen=True)
class ModeProfile:
    """The ``(urgency × risk × novelty × reversibility)`` inputs to mode
    selection (ADR-0021 §2.3).

    Each axis is a 0.0–1.0 score. The skeleton constructs a neutral profile
    and ignores it; 29f-3 maps profiles → modes.
    """

    urgency: float = 0.0
    risk: float = 0.0
    novelty: float = 0.0
    reversibility: float = 1.0

    def __post_init__(self) -> None:
        for name in ("urgency", "risk", "novelty", "reversibility"):
            value = getattr(self, name)
            if not 0.0 <= float(value) <= 1.0:
                raise ValueError(f"{name} must be within [0.0, 1.0]")


class ModeSelector:
    """Skeleton mode selector — always returns :data:`SKELETON_MODE`.

    Real arbitration over :class:`ModeProfile` axes lands in 29f-3.
    """

    def select(self, profile: ModeProfile | None = None) -> str:
        """Return the behavioral mode for this tick. Skeleton: always skeleton."""
        return SKELETON_MODE
