"""RPG.W7.2 -- Tier S/M/L/X gate on agent level + skill level.

ADR-0008 §"Routing integration" line:

    "Tier X tasks require Lv ≥ 50 + relevant skill ≥ Lv 3"

That single line is the entire policy for W7.2. This module owns the
pure check + the async resolver that fetches the inputs from the W1
character-card store and the W12 skill-state store. The pure check is
the canonical reference; callers that already hold the inputs in hand
should reach for ``is_eligible_for_tier`` directly instead of
re-implementing the rule.

W7.1 (`prefer_agent_id` routing) lands the call site inside
``routing_policy.choose_provider`` later in the wave; W7.3 layers a
per-Tier minimum-level table on top (BP.C T-shirt size feeds it). The
gate here is the leaf that both will compose with. Tier S / M / L pass
unconditionally — they have no level / skill floor in ADR-0008 — and
this module deliberately stays narrow so W7.3 can drop in without
re-litigating Tier X's curve.

Module-global state audit (per project SOP)
-------------------------------------------
This module defines only frozen constants, dataclasses, and exception
classes. There is no mutable module-level state. All store reads run
through the injected ``card_store`` / ``skill_store`` arguments — no
singleton lookup, no environment knob, no clock read at import time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# ── Constants from ADR-0008 §"Routing integration" ────────────────

#: Tier X is the only tier with a level/skill floor in ADR-0008.
TIER_X = "X"

#: Minimum character-card level required to route a Tier X task.
TIER_X_MIN_AGENT_LEVEL = 50

#: Minimum per-(agent_id, skill_id) skill level required for Tier X.
TIER_X_MIN_SKILL_LEVEL = 3

#: Effective skill level used when the skill_state row is absent. A
#: missing row means the agent has never accrued XP on that skill, so
#: it sits below Lv 1 from a gate perspective.
ABSENT_SKILL_LEVEL = 0


# ── Errors ─────────────────────────────────────────────────────────


class TierGateViolation(RuntimeError):
    """Raised when a Tier X task is dispatched to an under-qualified agent.

    Carries the structured ``unmet_reasons`` tuple so callers (routing
    fallback, operator-facing logs) can surface *why* a candidate was
    rejected without re-deriving it.
    """

    def __init__(self, message: str, *, unmet_reasons: tuple[str, ...]):
        super().__init__(message)
        self.unmet_reasons = unmet_reasons


# ── Result ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TierGateDecision:
    """Outcome of one tier-gate evaluation for a (agent, tier, skill) triple."""

    eligible: bool
    tier: str
    agent_level: int
    skill_level: int
    skill_id: str | None
    unmet_reasons: tuple[str, ...]


# ── Pure helpers ───────────────────────────────────────────────────


def is_eligible_for_tier(
    *,
    tier: str,
    agent_level: int,
    skill_level: int,
) -> bool:
    """Return whether ``agent_level`` + ``skill_level`` clear the tier floor.

    Tier S / M / L return ``True`` unconditionally — ADR-0008 places no
    explicit floor on those (W7.3 layers the BP.C T-shirt minimum-level
    table on top later). Tier X requires
    :data:`TIER_X_MIN_AGENT_LEVEL` AND :data:`TIER_X_MIN_SKILL_LEVEL`.
    """
    _validate_levels(agent_level=agent_level, skill_level=skill_level)
    if _normalise_tier(tier) != TIER_X:
        return True
    return agent_level >= TIER_X_MIN_AGENT_LEVEL and skill_level >= TIER_X_MIN_SKILL_LEVEL


def tier_gate_unmet_reasons(
    *,
    tier: str,
    agent_level: int,
    skill_level: int,
) -> tuple[str, ...]:
    """Return the structured reasons the (agent_level, skill_level) pair
    fails the floor for ``tier``. Empty tuple ⇒ eligible.

    The reason strings are stable for telemetry — the routing fallback
    in W7.4 will log them and the operator-facing UI will rehydrate
    them. Format: ``"tier_x_agent_level_below_50:46"``.
    """
    _validate_levels(agent_level=agent_level, skill_level=skill_level)
    if _normalise_tier(tier) != TIER_X:
        return ()
    reasons: list[str] = []
    if agent_level < TIER_X_MIN_AGENT_LEVEL:
        reasons.append(f"tier_x_agent_level_below_{TIER_X_MIN_AGENT_LEVEL}:{agent_level}")
    if skill_level < TIER_X_MIN_SKILL_LEVEL:
        reasons.append(f"tier_x_skill_level_below_{TIER_X_MIN_SKILL_LEVEL}:{skill_level}")
    return tuple(reasons)


def assert_eligible_for_tier(
    *,
    tier: str,
    agent_level: int,
    skill_level: int,
    agent_id: str | None = None,
    skill_id: str | None = None,
) -> None:
    """Raise :class:`TierGateViolation` if the pair fails the tier floor."""
    reasons = tier_gate_unmet_reasons(tier=tier, agent_level=agent_level, skill_level=skill_level)
    if not reasons:
        return
    who = agent_id or "<unknown agent>"
    which = skill_id or "<unspecified skill>"
    tier_label = _normalise_tier(tier)
    raise TierGateViolation(
        f"agent {who!r} fails Tier {tier_label} gate on skill {which!r}: {list(reasons)}",
        unmet_reasons=reasons,
    )


# ── Async resolver ─────────────────────────────────────────────────


async def evaluate_tier_gate(
    card_store: Any,
    skill_store: Any,
    *,
    agent_id: str,
    tier: str,
    skill_id: str | None,
) -> TierGateDecision:
    """Fetch the character card + skill row and apply the tier gate.

    ``card_store`` is any object satisfying the
    :class:`backend.agents.character_card.CharacterCardStore` shape;
    ``skill_store`` matches
    :class:`backend.agents.skill_leveling.SkillStateStore`. The two
    stores are passed in (rather than constructed) so callers stay in
    charge of connection lifecycle and tests can inject the in-memory
    variants without monkey-patching.

    Non-X tiers short-circuit to ``eligible=True`` without touching
    either store — Tier S / M / L is a free pass per ADR-0008. A
    missing character card or a Tier X dispatch with no ``skill_id``
    is treated as ``eligible=False`` so under-leveled instances cannot
    sneak through a routing race.
    """
    agent_id = _required("agent_id", agent_id)
    tier_norm = _normalise_tier(tier)

    if tier_norm != TIER_X:
        return TierGateDecision(
            eligible=True,
            tier=tier_norm,
            agent_level=0,
            skill_level=0,
            skill_id=skill_id,
            unmet_reasons=(),
        )

    card = await card_store.get_card(agent_id)
    if card is None:
        reason = (f"tier_x_character_card_missing:{agent_id}",)
        return TierGateDecision(
            eligible=False,
            tier=tier_norm,
            agent_level=0,
            skill_level=ABSENT_SKILL_LEVEL,
            skill_id=skill_id,
            unmet_reasons=reason,
        )

    if not skill_id or not skill_id.strip():
        reason = ("tier_x_skill_id_missing",)
        return TierGateDecision(
            eligible=False,
            tier=tier_norm,
            agent_level=card.level,
            skill_level=ABSENT_SKILL_LEVEL,
            skill_id=skill_id,
            unmet_reasons=reason,
        )

    skill_id_clean = skill_id.strip()
    state = await skill_store.get_state(agent_id, skill_id_clean)
    skill_level = state.level if state is not None else ABSENT_SKILL_LEVEL

    reasons = tier_gate_unmet_reasons(
        tier=tier_norm,
        agent_level=card.level,
        skill_level=skill_level,
    )
    return TierGateDecision(
        eligible=not reasons,
        tier=tier_norm,
        agent_level=card.level,
        skill_level=skill_level,
        skill_id=skill_id_clean,
        unmet_reasons=reasons,
    )


# ── Internal helpers ───────────────────────────────────────────────


def _normalise_tier(tier: str) -> str:
    if not isinstance(tier, str):
        raise TypeError("tier must be a string")
    clean = tier.strip().upper()
    if not clean:
        raise ValueError("tier is required")
    return clean


def _validate_levels(*, agent_level: int, skill_level: int) -> None:
    for name, value in (("agent_level", agent_level), ("skill_level", skill_level)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"{name} must be an int")
        if value < 0:
            raise ValueError(f"{name} must be >= 0")


def _required(field: str, value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    clean = value.strip()
    if not clean:
        raise ValueError(f"{field} is required")
    return clean


__all__ = [
    "ABSENT_SKILL_LEVEL",
    "TIER_X",
    "TIER_X_MIN_AGENT_LEVEL",
    "TIER_X_MIN_SKILL_LEVEL",
    "TierGateDecision",
    "TierGateViolation",
    "assert_eligible_for_tier",
    "evaluate_tier_gate",
    "is_eligible_for_tier",
    "tier_gate_unmet_reasons",
]
