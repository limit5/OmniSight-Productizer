"""Pipeline-coordinator personality-mode system (AUDIT-29f-5 / OP-1003).

ADR-0021 §2.3 + §7 mandate a *multi-mode personality*: the coordinator's
behavioral mode is selected **per-situation** from a 4-axis
``(urgency × risk × novelty × reversibility)`` :class:`SituationProfile`,
rather than running a single fixed personality. Phase 6 ships the three
modes flagged "Phase 6 initial" in ADR §7.2 — :data:`EXECUTION_MODE`,
:data:`INVESTIGATION_MODE`, :data:`RESCUE_MODE`. ``TriageMode`` and
``ArchitectureMode`` are declared (so override labels referencing them are
recognised) but their behavior overlays land "later as patterns crystallize".

What this module owns:

    SituationProfile     — the 4-axis decision profile (ADR §7.1)
    select_mode()        — pure profile (+ operator override) → ModeName (§7.2/§7.3)
    PersonalityMode      — base behavior-override carrier; one frozen instance
    ExecutionMode/…      — the 3 Phase-6 mode behavior overlays (§7.2 "Behavior")
    parse_mode_override()— reads a ``coord-mode:<mode>`` operator label (§7.3)
    ModeSelector         — the daemon-facing seam (kept from the 29f-coord
                           skeleton; ``.select()`` with no/neutral profile still
                           returns SKELETON_MODE so an *idle* tick logs "skeleton")

Per-situation, not per-tick
---------------------------
The coordinator only adopts a personality mode when it is actually deciding
about a *situation* (a ticket / event carrying a profile). An idle tick — no
situation, empty work-graph — has no personality and keeps logging
:data:`SKELETON_MODE`. This is why :class:`ModeSelector` distinguishes "no
profile / neutral skeleton profile" (→ ``skeleton``) from a real
:class:`SituationProfile` (→ a Phase-6 mode). The decision engine (29f-3)
calls the selector with the situation's profile before rule evaluation;
Tier-1 rules + Tier-2 LLM consultation then read the selected mode's
:class:`PersonalityMode` behavior overlay to know how hard to lean on rules
vs. LLM, how wide to make the LLM context, and whether to file a follow-up.

Module-global state audit (per project SOP)
-------------------------------------------
Immutable constants, frozen dataclasses, one frozen ``PersonalityMode``
instance per mode plus a frozen registry mapping, and stateless selector
functions/classes. No module globals mutated at runtime, no import-time I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping

# ── Mode vocabulary (ADR-0021 §7.2) ───────────────────────────────────

# The single mode an *idle* tick (no situation) selects. The empty engine
# stamps this into a no-op decision-log line (ADR-0021 §3.2 example record).
SKELETON_MODE = "skeleton"

# Phase-6 modes — the three flagged "Phase 6 initial" in ADR §7.2.
EXECUTION_MODE = "ExecutionMode"
INVESTIGATION_MODE = "InvestigationMode"
RESCUE_MODE = "RescueMode"

# Later-phase modes — declared so operator override labels referencing them
# are *recognised* (and routed to a graceful fallback) before their behavior
# overlays exist. ADR §7.2: "Triage + Architecture added later."
TRIAGE_MODE = "TriageMode"
ARCHITECTURE_MODE = "ArchitectureMode"

# Modes whose behavior overlay actually ships in Phase 6.
PHASE6_MODES: tuple[str, ...] = (EXECUTION_MODE, INVESTIGATION_MODE, RESCUE_MODE)

# Default mode for a profiled situation that matches neither the Execution
# nor the Investigation predicate and shows no rescue distress. Execution is
# the steady-state default (Tier-1 rules dominate — the cheap, fast path).
DEFAULT_MODE = EXECUTION_MODE

# All mode names the system knows (incl. skeleton + later-phase names). Kept
# as the stable name vocabulary callers may reference.
KNOWN_MODES: tuple[str, ...] = (
    SKELETON_MODE,
    EXECUTION_MODE,
    INVESTIGATION_MODE,
    RESCUE_MODE,
    TRIAGE_MODE,
    ARCHITECTURE_MODE,
)

# The operator-override label prefix (ADR §7.3 / §4.7 label vocabulary).
COORD_MODE_LABEL_PREFIX = "coord-mode:"

# Maps the short slug an operator types after ``coord-mode:`` to a canonical
# mode name. Accepts both the slug ("execution") and the canonical name
# ("ExecutionMode", case-insensitively).
_OVERRIDE_SLUGS: Mapping[str, str] = {
    "execution": EXECUTION_MODE,
    "investigation": INVESTIGATION_MODE,
    "rescue": RESCUE_MODE,
    "triage": TRIAGE_MODE,
    "architecture": ARCHITECTURE_MODE,
}


# ── 4-axis situation profile (ADR-0021 §7.1) ──────────────────────────


class Level(str, Enum):
    """Ordinal axis level. ADR §7.1 scores each axis low / medium / high.

    A ``str`` enum so a profile serialises straight into a decision-log
    JSON line as ``"low"`` / ``"medium"`` / ``"high"`` without a custom
    encoder, and so ``Level.HIGH == "high"`` holds for ergonomic tests.
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True)
class SituationProfile:
    """The ``(urgency × risk × novelty × reversibility)`` decision profile.

    The four axes the coordinator computes for each *situation* it decides
    about (ADR §7.1). Signals feeding each axis (sprint-deadline proximity,
    blast radius, Cognee similarity, undo-window) are gathered by the event
    layer (29f-8) / rules (29f-3); this dataclass is just the typed result
    they hand to :func:`select_mode`.

    ``runner_stuck_hours`` / ``revert_count`` are *distress signals* used
    only by the RescueMode trigger (ADR §7.2: "runner stuck > 24h OR > 5
    reverts on one ticket"); they are not decision axes and default to the
    no-distress values, so a profile constructed from the four axes alone is
    well-formed.
    """

    urgency: Level = Level.LOW
    risk: Level = Level.LOW
    novelty: Level = Level.LOW
    reversibility: Level = Level.HIGH
    runner_stuck_hours: float = 0.0
    revert_count: int = 0

    def __post_init__(self) -> None:
        for name in ("urgency", "risk", "novelty", "reversibility"):
            value = getattr(self, name)
            if not isinstance(value, Level):
                # Accept the raw string form ("high") for ergonomics, coerce
                # to the enum so downstream comparisons are total.
                object.__setattr__(self, name, Level(value))
        if self.runner_stuck_hours < 0:
            raise ValueError("runner_stuck_hours must be non-negative")
        if self.revert_count < 0:
            raise ValueError("revert_count must be non-negative")

    @property
    def in_distress(self) -> bool:
        """RescueMode trigger (ADR §7.2): stuck > 24h OR > 5 reverts."""
        return self.runner_stuck_hours > 24.0 or self.revert_count > 5

    def to_record(self) -> dict[str, object]:
        """Serialisable form embedded in a decision-log entry."""
        return {
            "urgency": self.urgency.value,
            "risk": self.risk.value,
            "novelty": self.novelty.value,
            "reversibility": self.reversibility.value,
            "runner_stuck_hours": self.runner_stuck_hours,
            "revert_count": self.revert_count,
        }


# ── Behavior overlays (ADR-0021 §7.2 "Behavior" column) ────────────────


@dataclass(frozen=True)
class PersonalityMode:
    """A mode's behavior overrides — what Tier-1 rules + Tier-2 LLM read.

    Each field encodes one knob from the §7.2 "Behavior" cell so the rule
    engine (29f-3) and LLM-consultation layer (29f-6) can ask the *mode*
    (rather than hard-coding per-mode branches) how to behave:

    ``tier1_dominant``      — let deterministic Tier-1 rules decide; only
                              consult the LLM per ``tier2_policy``.
    ``tier2_policy``        — ``"only_on_conflict"`` | ``"always"``: when the
                              Tier-2 LLM consultation runs.
    ``llm_context_hops``    — graph-expansion radius for the LLM context bundle.
    ``llm_lessons_window``  — how many recalled lessons to include.
    ``requires_root_cause`` — RescueMode demands full root-cause analysis.
    ``files_followup_ticket``/``followup_ticket_kind`` — whether the mode
                              files a follow-up (research-spike / blocked-pattern)
                              and under what label.
    ``writes_lesson``       — mode writes a lesson to prevent recurrence.
    """

    name: str
    tier1_dominant: bool
    tier2_policy: str
    llm_context_hops: int
    llm_lessons_window: int
    requires_root_cause: bool = False
    files_followup_ticket: bool = False
    followup_ticket_kind: str = ""
    writes_lesson: bool = False

    def __post_init__(self) -> None:
        if self.tier2_policy not in ("only_on_conflict", "always"):
            raise ValueError(f"unknown tier2_policy: {self.tier2_policy!r}")

    @property
    def consults_llm_every_decision(self) -> bool:
        return self.tier2_policy == "always"


# ExecutionMode — high urgency + low novelty + low risk. "Tier 1 rules
# dominate. Skip Tier 2 unless rules conflict. Fast routing decisions."
ExecutionMode = PersonalityMode(
    name=EXECUTION_MODE,
    tier1_dominant=True,
    tier2_policy="only_on_conflict",
    llm_context_hops=2,
    llm_lessons_window=5,
)

# InvestigationMode — low urgency + high novelty. "Always Tier 2. LLM
# context wider (10-hop graph, 20 lessons). Files research / spike tickets."
InvestigationMode = PersonalityMode(
    name=INVESTIGATION_MODE,
    tier1_dominant=False,
    tier2_policy="always",
    llm_context_hops=10,
    llm_lessons_window=20,
    files_followup_ticket=True,
    followup_ticket_kind="research-spike",
)

# RescueMode — runner stuck > 24h OR > 5 reverts. "Full root-cause
# analysis. File runner-blocked-pattern ticket + write lesson. Aim:
# prevent recurrence, not just unstuck."
RescueMode = PersonalityMode(
    name=RESCUE_MODE,
    tier1_dominant=False,
    tier2_policy="always",
    llm_context_hops=10,
    llm_lessons_window=20,
    requires_root_cause=True,
    files_followup_ticket=True,
    followup_ticket_kind="runner-blocked-pattern",
    writes_lesson=True,
)

# Frozen registry: mode name → behavior overlay. Phase-6 modes only; the
# skeleton "idle" mode and later-phase modes have no overlay (None).
MODE_BEHAVIORS: Mapping[str, PersonalityMode] = {
    EXECUTION_MODE: ExecutionMode,
    INVESTIGATION_MODE: InvestigationMode,
    RESCUE_MODE: RescueMode,
}


def mode_behavior(mode: str) -> PersonalityMode | None:
    """Behavior overlay for a mode name, or ``None`` for idle/later-phase modes."""
    return MODE_BEHAVIORS.get(mode)


# ── Operator override (ADR-0021 §7.3) ─────────────────────────────────


def parse_mode_override(labels: object) -> str | None:
    """Resolve a ``coord-mode:<mode>`` operator label to a canonical mode.

    ADR §7.3: a ``coord-mode:<mode>`` label on a ticket forces that mode for
    any decision involving the ticket. Accepts the short slug
    (``coord-mode:execution``) or the canonical name
    (``coord-mode:ExecutionMode``), case-insensitively. Returns ``None`` when
    no recognised override label is present. If multiple are present, the
    first recognised one (sorted for determinism) wins.
    """
    if not labels:
        return None
    found: list[str] = []
    for label in sorted(str(x) for x in labels):
        if not label.lower().startswith(COORD_MODE_LABEL_PREFIX):
            continue
        value = label[len(COORD_MODE_LABEL_PREFIX):].strip()
        canonical = _canonical_override(value)
        if canonical is not None:
            found.append(canonical)
    return found[0] if found else None


# ── Mode selection (ADR-0021 §7.2 + §7.3) ─────────────────────────────


def select_mode(profile: SituationProfile, *, override: str | None = None) -> str:
    """Select the behavioral mode for one situation. Pure: profile → ModeName.

    Selection order (ADR §7):

    1. **Operator override wins** (§7.3): a recognised ``coord-mode:*`` override
       forces that mode. An override naming a later-phase mode (Triage /
       Architecture) is honoured by *name* even though its behavior overlay is
       not implemented yet — the daemon logs the forced name and the rule
       layer falls back to default behavior.
    2. **RescueMode** (§7.2): the distress trigger (stuck > 24h OR > 5 reverts)
       short-circuits — a stuck/thrashing situation is a rescue regardless of
       the other axes.
    3. **ExecutionMode**: high urgency + low novelty + low risk.
    4. **InvestigationMode**: low urgency + high novelty.
    5. **Default** (:data:`DEFAULT_MODE`): everything else — the steady-state
       fast path.
    """
    if override is not None:
        canonical = _canonical_override(override)
        if canonical is not None:
            return canonical
        # Unrecognised override: ignore it and fall through to profiling
        # rather than crash on an operator typo.

    if profile.in_distress:
        return RESCUE_MODE

    if (
        profile.urgency is Level.HIGH
        and profile.novelty is Level.LOW
        and profile.risk is Level.LOW
    ):
        return EXECUTION_MODE

    if profile.urgency is Level.LOW and profile.novelty is Level.HIGH:
        return INVESTIGATION_MODE

    return DEFAULT_MODE


def _canonical_override(override: str) -> str | None:
    """Canonicalise an override value (slug or full name) to a known mode."""
    text = override.strip()
    slug = text.lower()
    if slug in _OVERRIDE_SLUGS:
        return _OVERRIDE_SLUGS[slug]
    for known in KNOWN_MODES:
        if known.lower() == slug:
            return known
    return None


# ── Daemon-facing seam (kept from the 29f-coord skeleton) ──────────────


@dataclass(frozen=True)
class ModeProfile:
    """Legacy neutral profile from the 29f-coord skeleton seam.

    Retained so the skeleton's idle-tick contract is unchanged:
    ``ModeSelector().select(ModeProfile())`` still returns
    :data:`SKELETON_MODE`. New code computes a :class:`SituationProfile`
    instead; this float-axis stub maps to the idle/skeleton mode.
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
    """Daemon-facing mode-selection seam.

    Distinguishes an *idle* tick from a *profiled situation*:

    - ``select()`` / ``select(None)`` / ``select(ModeProfile())`` — no real
      situation → :data:`SKELETON_MODE` (an idle tick logs "skeleton").
    - ``select(SituationProfile(...), override=...)`` — a real situation →
      a Phase-6 mode via :func:`select_mode`.

    The engine (29f-3) calls ``select`` with the situation's profile + any
    operator override **before** rule evaluation; the chosen mode's
    :func:`mode_behavior` overlay then steers Tier-1 / Tier-2 behavior.
    """

    def select(
        self,
        profile: SituationProfile | ModeProfile | None = None,
        *,
        override: str | None = None,
    ) -> str:
        """Return the behavioral mode. See class docstring for dispatch."""
        if isinstance(profile, SituationProfile):
            return select_mode(profile, override=override)
        # Idle tick / legacy neutral profile. An operator override still wins
        # even with no situation profile (operator authority is absolute).
        if override is not None:
            canonical = _canonical_override(override)
            if canonical is not None:
                return canonical
        return SKELETON_MODE
