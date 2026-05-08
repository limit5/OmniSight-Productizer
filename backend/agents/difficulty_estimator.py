"""L4.9.2 -- Meta-LLM task difficulty estimator for dynamic provider routing.

Predicts the cognitive / contextual difficulty of a ``TaskSpec`` at task
dispatch boundaries so the routing policy can prefer cheap-but-adequate
providers (Ollama, OpenAI) for trivial / easy tasks and reserve expensive
capable providers (Anthropic, Gemini) for hard / expert ones.

The estimator is intentionally heuristic and side-effect free: no LLM call
on the routing hot path, no database access, no provider health checks.
The "Meta-LLM" framing in L4.9 is the *encoded* reasoning model -- the
features and weight table below stand in for the LLM's classification
function so routing stays sub-millisecond and deterministic.

Module-global state audit (per implement_phase_step.md SOP)
-----------------------------------------------------------
This module defines immutable constants only.  Difficulty classification
inputs come from the per-call ``TaskSpec`` payload; no caller-visible
state is mutated.

Routing wiring contract
-----------------------
``routing_policy._routing_priority_score()`` reads
``provider_preference_multiplier()`` when ``is_enabled()`` returns True.
When the feature flag is off, the multiplier is exactly ``1.0`` for every
provider family so legacy quota-first ordering is preserved bit-for-bit.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

from backend import feature_flags

META_LLM_ROUTING_ENV = "OMNISIGHT_MP_META_LLM_DIFFICULTY_ROUTING"


class DifficultyClass(str, Enum):
    """Five-bucket difficulty classification used by the routing policy."""

    TRIVIAL = "trivial"
    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"
    EXPERT = "expert"


# Provider family ranking per difficulty class. Ordering is best-first:
# index 0 is the preferred family, index -1 is the least preferred. The
# cascade matches the L4.9 Cost-per-Decision design intent: cheap-and-
# adequate up front for trivial / easy tasks, capable-and-expensive up
# front for hard / expert tasks.
DIFFICULTY_PROVIDER_PREFERENCE: dict[DifficultyClass, tuple[str, ...]] = {
    DifficultyClass.TRIVIAL: ("ollama", "openai", "gemini", "anthropic"),
    DifficultyClass.EASY: ("openai", "gemini", "anthropic", "ollama"),
    DifficultyClass.MEDIUM: ("gemini", "openai", "anthropic", "ollama"),
    DifficultyClass.HARD: ("anthropic", "gemini", "openai", "ollama"),
    DifficultyClass.EXPERT: ("anthropic", "gemini", "openai", "ollama"),
}

# Score thresholds (lower bound, inclusive) for bucketing the heuristic
# score into a ``DifficultyClass``. Tuned so a default ``TaskSpec`` with
# tier=M, agent_class=subscription-codex, and a 200-char prompt lands in
# MEDIUM (matching the 13-epic ADR-0007 baseline assumption).
_DIFFICULTY_THRESHOLDS: tuple[tuple[float, DifficultyClass], ...] = (
    (10.0, DifficultyClass.EXPERT),
    (7.0, DifficultyClass.HARD),
    (4.0, DifficultyClass.MEDIUM),
    (1.5, DifficultyClass.EASY),
    (0.0, DifficultyClass.TRIVIAL),
)

# Multiplier applied to the routing-priority score for each rank in the
# difficulty preference list. Top-recommended family gets a 1.6x boost,
# descending to a 0.7x suppression for the least-preferred. Unknown
# families (rank >= len(preference)) fall through to 1.0 so adapters
# outside the {anthropic, openai, gemini, ollama} family stay neutral.
_DIFFICULTY_BOOST_BY_RANK: tuple[float, ...] = (1.6, 1.2, 1.0, 0.7)
_NEUTRAL_BOOST = 1.0

# Tier scoring contribution: harder tiers add more score units.
_TIER_BASE_SCORE: dict[str, float] = {
    "S": 0.0,
    "M": 2.0,
    "L": 4.0,
    "X": 6.0,
}

# T-shirt size scoring contribution (mirrors cost_estimator's S/M/XL).
_T_SHIRT_SIZE_SCORE: dict[str, float] = {
    "S": 0.0,
    "M": 1.0,
    "XL": 3.0,
}

# Agent-class hint score: api-* subscriptions imply tasks that have
# already been routed to the high-cost lane and are typically harder.
_AGENT_CLASS_SCORE: dict[str, float] = {
    "subscription-codex": 1.0,
    "api-openai": 1.0,
    "subscription-claude": 2.0,
    "api-anthropic": 2.0,
    "subscription-gemini": 1.0,
    "api-gemini": 1.0,
    "subscription-xai": 1.0,
    "api-xai": 1.0,
}

# Prompt-content keyword signals. HARD bumps difficulty up by 1.5 each
# (capped at 4.5); TRIVIAL clamps it down by 1.0 each (capped at 3.0).
_HARD_KEYWORDS = re.compile(
    r"\b("
    r"architect\w*|design|ADR|migrat\w+|refactor\w*|race[- ]?condition|"
    r"deadlock|threading|concurren\w+|distributed|consensus|cryptograph\w+|"
    r"verilog|kernel|driver|FPGA|simd|vectoriz\w+|optimi[sz]\w+|"
    r"performance|benchmark|profil\w+"
    r")\b",
    re.IGNORECASE,
)
_TRIVIAL_KEYWORDS = re.compile(
    r"\b("
    r"rename|typo|comment|docstring|format|lint|whitespace|"
    r"capitali[sz]\w+|punctuation|spelling"
    r")\b",
    re.IGNORECASE,
)
_HARD_KEYWORD_WEIGHT = 1.5
_HARD_KEYWORD_CAP = 4.5
_TRIVIAL_KEYWORD_WEIGHT = 1.0
_TRIVIAL_KEYWORD_CAP = 3.0

# Prompt-length contribution: log-scaled so a 200-char prompt adds ~1.0
# score units, a 2k-char prompt ~2.0, and a 20k-char prompt ~3.0.
_PROMPT_LENGTH_DIVISOR = 200.0
_PROMPT_LENGTH_MAX = 4.0


@dataclass(frozen=True)
class TaskDifficultyEstimate:
    """Heuristic difficulty estimate for one ``TaskSpec``."""

    difficulty: DifficultyClass
    score: float
    recommended_provider_families: tuple[str, ...]


def estimate_difficulty(task: Any) -> TaskDifficultyEstimate:
    """Return a heuristic difficulty estimate for ``task``.

    The estimator combines tier, t-shirt size, agent-class hint, prompt
    length, and keyword signals into a single non-negative score, then
    buckets the score into one of five ``DifficultyClass`` values.
    """
    score = 0.0
    score += _tier_score(task)
    score += _t_shirt_score(task)
    score += _agent_class_score(task)
    score += _prompt_length_score(task)
    score += _keyword_score(task)
    score = max(score, 0.0)

    difficulty = _score_to_difficulty(score)
    return TaskDifficultyEstimate(
        difficulty=difficulty,
        score=round(score, 3),
        recommended_provider_families=DIFFICULTY_PROVIDER_PREFERENCE[difficulty],
    )


def provider_preference_index(
    difficulty: DifficultyClass,
    provider_family: str,
) -> int:
    """Return the rank of ``provider_family`` (0 = best) for ``difficulty``.

    Returns ``len(preference)`` for families not in the preference list so
    they sort after every recommended family.
    """
    preference = DIFFICULTY_PROVIDER_PREFERENCE[difficulty]
    family = provider_family.strip().lower()
    try:
        return preference.index(family)
    except ValueError:
        return len(preference)


def provider_preference_multiplier(
    difficulty: DifficultyClass,
    provider_family: str,
) -> float:
    """Return the routing-priority multiplier for one provider family.

    The multiplier is applied on top of the existing buff / debuff /
    quota-ratio product in ``routing_policy._routing_priority_score()``.
    Unknown families receive ``1.0`` (neutral) so non-LLM provider
    adapters keep their legacy ordering.
    """
    rank = provider_preference_index(difficulty, provider_family)
    if rank >= len(_DIFFICULTY_BOOST_BY_RANK):
        return _NEUTRAL_BOOST
    return _DIFFICULTY_BOOST_BY_RANK[rank]


def is_enabled() -> bool:
    """Return whether Meta-LLM difficulty routing is active for this worker.

    Default-disabled: this is opt-in via the ``OMNISIGHT_META_LLM_DIFFICULTY_ROUTING``
    knob (or the equivalent ``feature_flags`` registry row) until the
    L4.9.3 fallback chain and L4.9.4 retrospective learning land.
    """
    return feature_flags.resolve_env_backed_feature_flag(
        META_LLM_ROUTING_ENV,
        default_enabled=False,
        env_mode="true_values",
    )


def _tier_score(task: Any) -> float:
    tier = _first_str_attr(task, ("tier",))
    if tier is None:
        return 0.0
    return _TIER_BASE_SCORE.get(tier.strip().upper(), 0.0)


def _t_shirt_score(task: Any) -> float:
    size = _first_str_attr(task, ("size", "t_shirt_size", "tshirt_size"))
    if size is None:
        return 0.0
    return _T_SHIRT_SIZE_SCORE.get(size.strip().upper(), 0.0)


def _agent_class_score(task: Any) -> float:
    agent_class = _first_str_attr(task, ("agent_class",))
    if agent_class is None:
        return 0.0
    return _AGENT_CLASS_SCORE.get(agent_class.strip(), 0.0)


def _prompt_length_score(task: Any) -> float:
    prompt = _prompt_text(task)
    if not prompt:
        return 0.0
    chars = len(prompt)
    if chars <= 0:
        return 0.0
    raw = math.log10(max(chars / _PROMPT_LENGTH_DIVISOR, 1.0) + 1.0) * 2.0
    return min(raw, _PROMPT_LENGTH_MAX)


def _keyword_score(task: Any) -> float:
    prompt = _prompt_text(task)
    if not prompt:
        return 0.0
    hard_hits = len(_HARD_KEYWORDS.findall(prompt))
    trivial_hits = len(_TRIVIAL_KEYWORDS.findall(prompt))
    bumps = min(hard_hits * _HARD_KEYWORD_WEIGHT, _HARD_KEYWORD_CAP)
    clamps = min(trivial_hits * _TRIVIAL_KEYWORD_WEIGHT, _TRIVIAL_KEYWORD_CAP)
    return bumps - clamps


def _score_to_difficulty(score: float) -> DifficultyClass:
    for threshold, label in _DIFFICULTY_THRESHOLDS:
        if score >= threshold:
            return label
    return DifficultyClass.TRIVIAL


def _prompt_text(task: Any) -> str:
    prompt = getattr(task, "prompt", None)
    if isinstance(prompt, str):
        return prompt
    if isinstance(task, dict):
        value = task.get("prompt")
        if isinstance(value, str):
            return value
    return ""


def _first_str_attr(task: Any, attrs: tuple[str, ...]) -> str | None:
    for attr in attrs:
        if isinstance(task, dict):
            value = task.get(attr)
        else:
            value = getattr(task, attr, None)
        if isinstance(value, str) and value.strip():
            return value
    return None


__all__ = [
    "DIFFICULTY_PROVIDER_PREFERENCE",
    "DifficultyClass",
    "META_LLM_ROUTING_ENV",
    "TaskDifficultyEstimate",
    "estimate_difficulty",
    "is_enabled",
    "provider_preference_index",
    "provider_preference_multiplier",
]
