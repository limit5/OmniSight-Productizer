"""RPG.W4.1 / W4.2 / W4.3 / W4.4 -- deterministic XP award calculation.

This module implements the ADR-0008 XP curve and outcome multipliers without
persistence. Callers pass the observed task outcome and receive the XP delta
that should be applied to the agent's character card by a later storage layer.

W4 sub-wave coverage in this module
-----------------------------------
- W4.1 (OP-132 / OP-1349): ``award_xp`` -- the deterministic XpDelta
  entry point.
- W4.2 (OP-133 / OP-1350): ``level_threshold`` / ``level_for_xp`` -- the
  ``100 * N**1.4`` cumulative-XP curve plus the ``MAX_LEVEL = 80`` hard cap
  that delivers the ADR-0008 "sigmoid late-game" property.
- W4.3 (OP-1351): ``OUTCOME_MULTIPLIERS`` + ``TIER_L_PLUS_MULTIPLIER`` +
  ``FIRST_TIME_SKILL_MULTIPLIER``.
- W4.4 (OP-135): ``DUPLICATE_TASK_MULTIPLIER`` anti-grind clamp.
- W18.2 (OP-1389): secondary-class XP earns 0.5× before Lv 30 and
  returns to 1.0× from Lv 30 onward.
- W18.3 (OP-1390): dual-class agents in party tasks earn the hybrid
  synergy XP bonus.

Module-global state audit (per project SOP)
-------------------------------------------
This module defines immutable constants only. It performs no database access,
no registry mutation, and no clock reads; anti-grinding is represented by an
explicit task-outcome flag supplied by the caller.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, fields, is_dataclass
from types import MappingProxyType
from typing import Any, Literal, Mapping

from backend.agents.buff_registry import xp_multiplier_for_buff_ids
from backend.agents.debuff_registry import (
    BURNOUT_DEBUFF_ID,
    DebuffContext,
    active_debuff_ids_for_context,
    xp_multiplier_for_debuff_ids,
)

OutcomeStatus = Literal["success", "partial", "fail", "failed"]
ClassXpTarget = Literal["primary", "secondary"]

BASE_TASK_XP = 100
LEVEL_CURVE_BASE_XP = 100
MAX_LEVEL = 80
LEVEL_CURVE_EXPONENT = 1.4
# W4.3 (OP-1351): Tier-L+ tasks earn a flat 2.0× XP bump on top of the
# outcome multiplier per ADR-0008 §"Outcome multipliers" -- stacks
# multiplicatively with success/partial/fail and with first-time-skill.
TIER_L_PLUS_MULTIPLIER = 2.0
# W4.3 (OP-1351): The first task that exercises a new (agent, skill) pair
# earns a 3.0× XP bump per ADR-0008 -- discovery reward, stacks with
# outcome and Tier-L+ multipliers.
FIRST_TIME_SKILL_MULTIPLIER = 3.0
# W4.4 (OP-135): Anti-grinding clamp per ADR-0008 §"XP curve" -- when the
# runner detects that the same canonical task hash has already been
# awarded to this agent within the last 24h, the XP delta for the repeat
# is multiplied by 0.2 (i.e. ×0.2, an 80% haircut). Stacks multiplicatively
# with the W4.3 outcome / Tier-L+ / first-time-skill multipliers and with
# the W15 buff/debuff multipliers. The "same task hash within 24h" decision
# is made by the runner before calling :func:`award_xp`; this module treats
# ``duplicate_task_within_24h`` as an explicit input flag and reads no
# clock or task history (deterministic, pure). The skill XP path (W12)
# carries a parallel ``ANTI_GRIND_MULTIPLIER`` constant with the same 0.2
# value -- both literals MUST stay in lock-step with this constant.
DUPLICATE_TASK_MULTIPLIER = 0.2
SECONDARY_CLASS_FULL_XP_LEVEL = 30
SECONDARY_CLASS_RAMP_MULTIPLIER = 0.5
HYBRID_SYNERGY_PARTY_XP_MULTIPLIER = 1.15

# W4.3 (OP-1351): outcome → XP multiplier per ADR-0008 §"Outcome
# multipliers". ``failed`` is an alias for ``fail`` (runner emits either
# spelling); they MUST stay in lock-step. Tier-L+ and first-time-skill
# bumps stack multiplicatively on top of this base multiplier inside
# :func:`_outcome_multiplier`.
OUTCOME_MULTIPLIERS: Mapping[str, float] = MappingProxyType(
    {
        "success": 1.0,
        "partial": 0.4,
        "fail": 0.1,
        "failed": 0.1,
    }
)


@dataclass(frozen=True)
class TaskOutcome:
    """Normalized task completion signal used by :func:`award_xp`."""

    status: OutcomeStatus
    base_xp: int = BASE_TASK_XP
    tier_l_plus: bool = False
    first_time_skill_use: bool = False
    duplicate_task_within_24h: bool = False
    active_buff_ids: tuple[str, ...] = ()
    consecutive_failures: int = 0
    active_debuff_ids: tuple[str, ...] = ()
    class_xp_target: ClassXpTarget = "primary"
    secondary_class_level: int = 1
    dual_class_agent: bool = False
    party_task: bool = False


@dataclass(frozen=True)
class XpDelta:
    """Computed XP award for one agent/task outcome."""

    agent_id: str
    xp: int
    status: str
    base_xp: int
    multiplier: float


def award_xp(
    agent_id: str,
    task_outcome: TaskOutcome | Mapping[str, Any] | Any,
) -> XpDelta:
    """Return the XP delta for ``agent_id`` and ``task_outcome``.

    ``task_outcome`` may be a :class:`TaskOutcome`, a mapping, or a dataclass /
    object with matching attributes. The calculation mirrors ADR-0008:
    outcome multiplier, optional Tier-L+ multiplier, optional first-time skill
    multiplier, optional W15 buff/debuff multipliers, and optional
    duplicate-task anti-grinding multiplier.
    """
    clean_agent_id = _clean_agent_id(agent_id)
    outcome = _normalise_task_outcome(task_outcome)
    multiplier = _outcome_multiplier(outcome)
    xp = max(0, math.floor(outcome.base_xp * multiplier))
    return XpDelta(
        agent_id=clean_agent_id,
        xp=xp,
        status=outcome.status,
        base_xp=outcome.base_xp,
        multiplier=round(multiplier, 6),
    )


def level_threshold(level: int) -> int:
    """RPG.W4.2 -- cumulative XP required to reach ``level`` per ADR-0008.

    Returns ``ceil(LEVEL_CURVE_BASE_XP * level ** LEVEL_CURVE_EXPONENT)``,
    i.e. the ``100 * N**1.4`` curve from the ADR. The per-level marginal cost
    ``level_threshold(N+1) - level_threshold(N)`` grows monotonically with
    ``N`` (per-level grind gets heavier), and the absolute cap from
    :func:`level_for_xp` flattens the curve past :data:`MAX_LEVEL` -- the
    two together implement ADR-0008's "sigmoid late-game" so Lv 80 -> 81
    is not trivially grindable (it is in fact unreachable).
    """
    if not isinstance(level, int):
        raise TypeError("level must be an int")
    if level < 1:
        raise ValueError("level must be >= 1")
    return math.ceil(LEVEL_CURVE_BASE_XP * (level ** LEVEL_CURVE_EXPONENT))


def level_for_xp(total_xp: int) -> int:
    """RPG.W4.2 -- capped character level for cumulative ``total_xp``.

    Walks the :func:`level_threshold` ladder up to :data:`MAX_LEVEL`. XP
    beyond ``level_threshold(MAX_LEVEL)`` is silently discarded by the
    level computation -- callers persisting ``xp`` may still store the
    raw total, but the derived level will never exceed the cap. This is
    the hard half of the ADR-0008 sigmoid late-game contract; the curve
    in :func:`level_threshold` is the soft half.
    """
    if not isinstance(total_xp, int):
        raise TypeError("total_xp must be an int")
    if total_xp < 0:
        raise ValueError("total_xp must be >= 0")

    level = 1
    while level < MAX_LEVEL and total_xp >= level_threshold(level + 1):
        level += 1
    return level


def secondary_class_xp_multiplier(secondary_class_level: int) -> float:
    """Return W18.2's XP multiplier for a secondary class at ``level``."""
    _clean_level(secondary_class_level, field="secondary_class_level")
    # RPG.W18.2 (OP-1389): secondary-class ramp is half-speed until
    # Lv 30, then returns to the normal class XP rate.
    if secondary_class_level >= SECONDARY_CLASS_FULL_XP_LEVEL:
        return 1.0
    return SECONDARY_CLASS_RAMP_MULTIPLIER


def _clean_agent_id(agent_id: str) -> str:
    if not isinstance(agent_id, str):
        raise TypeError("agent_id must be a string")
    clean = agent_id.strip()
    if not clean:
        raise ValueError("agent_id must be non-empty")
    return clean


def _normalise_task_outcome(
    task_outcome: TaskOutcome | Mapping[str, Any] | Any,
) -> TaskOutcome:
    if isinstance(task_outcome, TaskOutcome):
        outcome = task_outcome
    else:
        values = _outcome_values(task_outcome)
        status = values.get("status", values.get("outcome"))
        if status is None:
            raise ValueError("task_outcome must include status or outcome")
        outcome = TaskOutcome(
            status=_clean_status(status),
            base_xp=_clean_base_xp(values.get("base_xp", BASE_TASK_XP)),
            tier_l_plus=bool(
                values.get("tier_l_plus", values.get("tier_l_or_higher", False))
            ),
            first_time_skill_use=bool(values.get("first_time_skill_use", False)),
            duplicate_task_within_24h=bool(
                values.get(
                    "duplicate_task_within_24h",
                    values.get("same_task_hash_within_24h", False),
                )
            ),
            active_buff_ids=_clean_active_buff_ids(
                values.get("active_buff_ids", values.get("buff_ids", ()))
            ),
            consecutive_failures=_clean_consecutive_failures(
                values.get("consecutive_failures", 0)
            ),
            active_debuff_ids=_clean_active_debuff_ids(
                values.get("active_debuff_ids", values.get("debuff_ids", ()))
            ),
            class_xp_target=_class_xp_target_from_values(values),
            secondary_class_level=_clean_level(
                values.get("secondary_class_level", 1),
                field="secondary_class_level",
            ),
            dual_class_agent=bool(
                values.get(
                    "dual_class_agent",
                    values.get("is_dual_class", values.get("dual_class", False)),
                )
            ),
            party_task=bool(
                values.get(
                    "party_task",
                    values.get("in_party", values.get("party_member", False)),
                )
            ),
        )
    _validate_outcome(outcome)
    return outcome


def _outcome_values(task_outcome: Mapping[str, Any] | Any) -> Mapping[str, Any]:
    if isinstance(task_outcome, Mapping):
        return task_outcome
    if is_dataclass(task_outcome):
        return {
            field.name: getattr(task_outcome, field.name)
            for field in fields(task_outcome)
        }
    return {
        name: getattr(task_outcome, name)
        for name in (
            "status",
            "outcome",
            "base_xp",
            "tier_l_plus",
            "tier_l_or_higher",
            "first_time_skill_use",
            "duplicate_task_within_24h",
            "same_task_hash_within_24h",
            "active_buff_ids",
            "buff_ids",
            "consecutive_failures",
            "active_debuff_ids",
            "debuff_ids",
            "class_xp_target",
            "xp_target",
            "class_target",
            "secondary_class_xp",
            "is_secondary_class",
            "secondary_class",
            "secondary_class_level",
            "dual_class_agent",
            "is_dual_class",
            "dual_class",
            "party_task",
            "in_party",
            "party_member",
        )
        if hasattr(task_outcome, name)
    }


def _clean_status(status: Any) -> OutcomeStatus:
    if not isinstance(status, str):
        raise TypeError("task_outcome status must be a string")
    clean = status.strip().lower()
    if clean not in OUTCOME_MULTIPLIERS:
        allowed = ", ".join(sorted(OUTCOME_MULTIPLIERS))
        raise ValueError(
            f"unsupported task_outcome status {status!r}; expected one of {allowed}"
        )
    return clean  # type: ignore[return-value]


def _clean_base_xp(base_xp: Any) -> int:
    if isinstance(base_xp, bool) or not isinstance(base_xp, int):
        raise TypeError("task_outcome base_xp must be an int")
    if base_xp < 0:
        raise ValueError("task_outcome base_xp must be >= 0")
    return base_xp


def _validate_outcome(outcome: TaskOutcome) -> None:
    _clean_status(outcome.status)
    _clean_base_xp(outcome.base_xp)
    _clean_active_buff_ids(outcome.active_buff_ids)
    _clean_consecutive_failures(outcome.consecutive_failures)
    _clean_active_debuff_ids(outcome.active_debuff_ids)
    _clean_class_xp_target(outcome.class_xp_target)
    _clean_level(outcome.secondary_class_level, field="secondary_class_level")


def _outcome_multiplier(outcome: TaskOutcome) -> float:
    """RPG.W4.3 (OP-1351) -- compose the per-task XP multiplier.

    Stacking order is multiplicative and stable: outcome → Tier-L+ →
    first-time-skill → W15 buffs → W15 debuffs → W4.4 anti-grind →
    W18.2 secondary-class ramp → W18.3 hybrid synergy. The W4.3 contract
    only constrains the *first three* terms (success/partial/fail/
    Tier-L+/first-time-skill); later terms are layered by W4.4 / W15 /
    W17 / W18 and documented in their own waves.
    """
    multiplier = OUTCOME_MULTIPLIERS[outcome.status]
    if outcome.tier_l_plus:
        multiplier *= TIER_L_PLUS_MULTIPLIER
    if outcome.first_time_skill_use:
        multiplier *= FIRST_TIME_SKILL_MULTIPLIER
    multiplier *= xp_multiplier_for_buff_ids(
        _clean_active_buff_ids(outcome.active_buff_ids)
    )
    multiplier *= xp_multiplier_for_debuff_ids(_effective_debuff_ids(outcome))
    # W4.4 (OP-135): anti-grinding clamp -- same canonical task hash
    # repeated within 24h takes a flat 0.2× haircut on top of every
    # earlier multiplier. The flag is computed runner-side and passed
    # in; see :data:`DUPLICATE_TASK_MULTIPLIER` for the contract.
    if outcome.duplicate_task_within_24h:
        multiplier *= DUPLICATE_TASK_MULTIPLIER
    if outcome.class_xp_target == "secondary":
        multiplier *= secondary_class_xp_multiplier(outcome.secondary_class_level)
    if outcome.dual_class_agent and outcome.party_task:
        # RPG.W18.3 (OP-1390): dual-class agents receive the hybrid
        # synergy bump only while participating in a party task.
        multiplier *= HYBRID_SYNERGY_PARTY_XP_MULTIPLIER
    return multiplier


def _clean_class_xp_target(value: Any) -> ClassXpTarget:
    if not isinstance(value, str):
        raise TypeError("task_outcome class_xp_target must be a string")
    clean = value.strip().lower()
    if clean not in ("primary", "secondary"):
        raise ValueError(
            "task_outcome class_xp_target must be 'primary' or 'secondary'"
        )
    return clean  # type: ignore[return-value]


def _class_xp_target_from_values(values: Mapping[str, Any]) -> ClassXpTarget:
    for key in ("class_xp_target", "xp_target", "class_target"):
        if key in values:
            return _clean_class_xp_target(values[key])
    for key in ("secondary_class_xp", "is_secondary_class", "secondary_class"):
        if bool(values.get(key, False)):
            return "secondary"
    return "primary"


def _clean_level(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"task_outcome {field} must be an int")
    if value < 1:
        raise ValueError(f"task_outcome {field} must be >= 1")
    return value


def _clean_active_buff_ids(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        items = (value,)
    else:
        try:
            items = tuple(value)
        except TypeError as exc:
            raise TypeError("task_outcome active_buff_ids must be iterable") from exc
    out: list[str] = []
    for item in items:
        if not isinstance(item, str):
            raise TypeError("task_outcome active_buff_ids entries must be strings")
        clean = item.strip()
        if not clean:
            raise ValueError("task_outcome active_buff_ids entries must be non-empty")
        out.append(clean)
    return tuple(out)


def _clean_consecutive_failures(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("task_outcome consecutive_failures must be an int")
    if value < 0:
        raise ValueError("task_outcome consecutive_failures must be >= 0")
    return value


def _clean_active_debuff_ids(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        items = (value,)
    else:
        try:
            items = tuple(value)
        except TypeError as exc:
            raise TypeError("task_outcome active_debuff_ids must be iterable") from exc
    out: list[str] = []
    for item in items:
        if not isinstance(item, str):
            raise TypeError("task_outcome active_debuff_ids entries must be strings")
        clean = item.strip()
        if not clean:
            raise ValueError("task_outcome active_debuff_ids entries must be non-empty")
        out.append(clean)
    return tuple(out)


def _effective_debuff_ids(outcome: TaskOutcome) -> tuple[str, ...]:
    explicit = _clean_active_debuff_ids(outcome.active_debuff_ids)
    inferred = active_debuff_ids_for_context(
        DebuffContext(
            consecutive_failures=outcome.consecutive_failures,
            outcome_status=outcome.status,
        )
    )
    if outcome.status == "success":
        explicit = tuple(
            debuff_id for debuff_id in explicit if debuff_id != BURNOUT_DEBUFF_ID
        )
    return tuple(dict.fromkeys(explicit + inferred))


def party_total_xp_pool(
    base_xp_per_member: int,
    party_size: int,
    *,
    synergy_xp_bonus: float = 0.0,
) -> int:
    """RPG.W17.5 (OP-196) -- compute the total XP pool that the party's
    task completion distributes across members.

    Mirrors the ADR-0008 §"Party / Synergy system (W17)" rule: each
    member contributes their own base XP to the pool, and the entire
    pool is multiplied by ``(1 + synergy_xp_bonus)`` before being
    split. The pool is the *party-side* half of the W17.5 payout
    contract — the per-member ``personal_xp`` half is accrued
    separately and is **not** folded into this number. The pool
    flows into :func:`backend.agents.party.compute_party_xp_distribution`,
    which performs the even-split and adds the personal-XP additive
    on top per W17.5. This helper lives in ``xp_engine`` so the W4.1
    curve constants stay co-located with their callers.

    Returns 0 for ``party_size <= 0`` or non-positive ``base_xp_per_member``.
    """
    if not isinstance(base_xp_per_member, int) or isinstance(base_xp_per_member, bool):
        raise TypeError("base_xp_per_member must be an int")
    if not isinstance(party_size, int) or isinstance(party_size, bool):
        raise TypeError("party_size must be an int")
    if base_xp_per_member <= 0 or party_size <= 0:
        return 0
    if not isinstance(synergy_xp_bonus, (int, float)) or isinstance(
        synergy_xp_bonus, bool
    ):
        raise TypeError("synergy_xp_bonus must be a number")
    multiplier = 1.0 + max(0.0, float(synergy_xp_bonus))
    pool = base_xp_per_member * party_size
    return max(0, math.floor(pool * multiplier))


def skill_xp_delta_for(task_outcome: TaskOutcome | Mapping[str, Any] | Any) -> int:
    """RPG.W12 -- compute the per-skill XP delta for ``task_outcome``.

    Bridges the W4.1 outcome shape into the W12 ``award_skill_xp`` call
    site. Reads the same outcome / Tier-L+ / first-time-skill /
    anti-grind flags but ignores the W4.1-only buff/debuff multipliers
    (W12 skill XP is intentionally simpler — see ADR-0008
    §"Skill leveling (W12)"). Buff/debuff still apply to the W4.1
    character-level XP path returned by :func:`award_xp`.
    """
    from backend.agents.skill_leveling import compute_xp_delta

    outcome = _normalise_task_outcome(task_outcome)
    return compute_xp_delta(
        outcome.base_xp,
        outcome.status,
        tier_l_plus=outcome.tier_l_plus,
        first_time_skill_use=outcome.first_time_skill_use,
        same_task_hash_within_24h=outcome.duplicate_task_within_24h,
    )


__all__ = [
    "BASE_TASK_XP",
    "ClassXpTarget",
    "DUPLICATE_TASK_MULTIPLIER",
    "FIRST_TIME_SKILL_MULTIPLIER",
    "HYBRID_SYNERGY_PARTY_XP_MULTIPLIER",
    "LEVEL_CURVE_BASE_XP",
    "LEVEL_CURVE_EXPONENT",
    "MAX_LEVEL",
    "OUTCOME_MULTIPLIERS",
    "SECONDARY_CLASS_FULL_XP_LEVEL",
    "SECONDARY_CLASS_RAMP_MULTIPLIER",
    "TIER_L_PLUS_MULTIPLIER",
    "OutcomeStatus",
    "TaskOutcome",
    "XpDelta",
    "award_xp",
    "level_for_xp",
    "level_threshold",
    "party_total_xp_pool",
    "secondary_class_xp_multiplier",
    "skill_xp_delta_for",
]
