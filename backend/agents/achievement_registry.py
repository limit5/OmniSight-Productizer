"""RPG.W16.1 -- achievement milestone registry.

ADR-0008 defines W16 as the progressive achievement / badge layer. This module
keeps the first milestone table deliberately small and immutable: callers can
render or evaluate definitions without taking a dependency on a persistence
layer. Durable unlock state belongs to a later storage ticket; this module is
the canonical *definition* side of that split (see
``backend.agents.achievement_unlock_daemon`` for the scan/persist side).

Public API
----------
- ``ACHIEVEMENT_MILESTONES`` — read-only mapping of milestone id to definition,
  in declaration order. Use ``list_achievement_milestones`` when iteration is
  enough.
- ``AchievementMilestone`` — frozen dataclass describing one achievement.
- ``AchievementCategory`` / ``AchievementMetric`` — string ``Literal`` types
  callers can use for type-checked routing.
- ``get_achievement_milestone(achievement_id)`` — lookup one definition or
  raise ``UnknownAchievementMilestoneError``.
- ``list_achievement_milestones()`` — all definitions in stable registry order.
- ``achievement_milestone_reached(achievement_id, metric_value)`` — pure
  threshold check; no clock, no I/O.

Module-global state audit (per project SOP)
-------------------------------------------
This module defines immutable constants, frozen dataclasses, and read-only
mapping views only. It performs no database access, no clock reads, and no
runtime mutation. The ``_assert_registry_keys_match_ids`` invariant check runs
once at import time and raises ``RuntimeError`` if the registry is malformed,
so any import-side breakage surfaces at startup rather than at first lookup.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Mapping

AchievementMetric = Literal[
    "merged_pr_count",
    "zero_regression_streak",
    "agents_taught_count",
    "tier_l_plus_success_count",
    "first_time_skill_success_count",
]

AchievementCategory = Literal[
    "delivery",
    "quality",
    "mentorship",
    "challenge",
    "learning",
]


@dataclass(frozen=True)
class AchievementMilestone:
    """One operator-visible RPG achievement definition.

    Attributes
    ----------
    achievement_id:
        Stable, machine-readable id used as the registry key (e.g.
        ``"merged_pr_100"``). Must equal the corresponding key in
        ``ACHIEVEMENT_MILESTONES``; the import-time invariant check enforces
        this.
    display_name:
        Human-readable label rendered in operator UI / character cards.
    category:
        High-level grouping for display and filtering. One of the
        ``AchievementCategory`` literals (``delivery``, ``quality``,
        ``mentorship``, ``challenge``, ``learning``).
    metric:
        Name of the per-agent metric the scanner compares against
        ``threshold``. One of the ``AchievementMetric`` literals.
    threshold:
        Inclusive minimum metric value at which the achievement unlocks. Must
        be ``>= 1``.
    summary:
        Short prose description shown alongside ``display_name`` in operator
        UI.

    Notes
    -----
    The dataclass is ``frozen=True``; fields are validated in
    ``__post_init__``. String fields must be non-empty after stripping;
    ``threshold`` must be a positive integer.
    """

    achievement_id: str
    display_name: str
    category: AchievementCategory
    metric: AchievementMetric
    threshold: int
    summary: str

    def __post_init__(self) -> None:
        """Validate string fields are non-empty and ``threshold`` is positive.

        Raises ``ValueError`` if any string field is empty/whitespace or if
        ``threshold < 1``. Whitespace-only strings are rejected so a stray
        ``" "`` cannot silently become an achievement id.
        """
        _required("achievement_id", self.achievement_id)
        _required("display_name", self.display_name)
        _required("category", self.category)
        _required("metric", self.metric)
        _required("summary", self.summary)
        if self.threshold < 1:
            raise ValueError("threshold must be >= 1")


def _required(name: str, value: str) -> str:
    """Return ``value`` stripped, or raise ``ValueError`` if empty.

    Internal helper for ``AchievementMilestone.__post_init__``. ``name`` is
    the originating field name and is interpolated into the error message so
    the caller can identify which field failed validation.
    """
    clean = value.strip()
    if not clean:
        raise ValueError(f"{name} must be non-empty")
    return clean


ACHIEVEMENT_MILESTONES: Mapping[str, AchievementMilestone] = MappingProxyType(
    {
        "merged_pr_100": AchievementMilestone(
            achievement_id="merged_pr_100",
            display_name="100 PR Merged",
            category="delivery",
            metric="merged_pr_count",
            threshold=100,
            summary="Awarded after an agent has 100 accepted and merged PRs.",
        ),
        "zero_regression_streak_30": AchievementMilestone(
            achievement_id="zero_regression_streak_30",
            display_name="0 Regression Streak x30",
            category="quality",
            metric="zero_regression_streak",
            threshold=30,
            summary="Awarded after 30 consecutive completed tasks with no regression.",
        ),
        "taught_agents_5": AchievementMilestone(
            achievement_id="taught_agents_5",
            display_name="Taught 5 Agents",
            category="mentorship",
            metric="agents_taught_count",
            threshold=5,
            summary="Awarded after successful same-Guild skill teaching for five agents.",
        ),
        "tier_l_plus_success_10": AchievementMilestone(
            achievement_id="tier_l_plus_success_10",
            display_name="Tier L+ Veteran",
            category="challenge",
            metric="tier_l_plus_success_count",
            threshold=10,
            summary="Awarded after 10 successful Tier L+ assignments.",
        ),
        "first_time_skill_success_25": AchievementMilestone(
            achievement_id="first_time_skill_success_25",
            display_name="Fast Learner",
            category="learning",
            metric="first_time_skill_success_count",
            threshold=25,
            summary="Awarded after 25 successful first-time skill uses.",
        ),
    }
)


class UnknownAchievementMilestoneError(KeyError):
    """Raised when an ``achievement_id`` is absent from the registry.

    Subclasses ``KeyError`` so existing callers that catch ``KeyError`` on
    registry lookups keep working, while specific handlers can target this
    type. The message includes the looked-up id (after stripping) for
    diagnostics.
    """


def get_achievement_milestone(achievement_id: str) -> AchievementMilestone:
    """Return the achievement definition for ``achievement_id``.

    Parameters
    ----------
    achievement_id:
        Registry key to look up. Surrounding whitespace is stripped so callers
        can pass values from operator-facing surfaces without trimming first.

    Returns
    -------
    AchievementMilestone
        The frozen definition stored under the (stripped) key.

    Raises
    ------
    UnknownAchievementMilestoneError
        If no milestone is registered for ``achievement_id``. The lookup is
        case-sensitive; only whitespace is normalised.
    """

    key = achievement_id.strip()
    try:
        return ACHIEVEMENT_MILESTONES[key]
    except KeyError as exc:
        raise UnknownAchievementMilestoneError(
            f"No RPG achievement milestone for achievement_id {key!r}"
        ) from exc


def list_achievement_milestones() -> tuple[AchievementMilestone, ...]:
    """Return all achievement milestones in stable registry order.

    The order matches the declaration order in ``ACHIEVEMENT_MILESTONES``,
    which dict insertion order guarantees on Python 3.7+. Callers can rely on
    this ordering for deterministic rendering (operator UI, character cards,
    golden-file tests) without sorting themselves.

    Returns
    -------
    tuple[AchievementMilestone, ...]
        Every registered ``AchievementMilestone``, snapshot at call time.
        The registry itself is immutable, so the snapshot is stable for the
        lifetime of the process.
    """

    return tuple(ACHIEVEMENT_MILESTONES[key] for key in ACHIEVEMENT_MILESTONES)


def achievement_milestone_reached(
    achievement_id: str,
    metric_value: int,
) -> bool:
    """Whether ``metric_value`` satisfies the achievement threshold.

    Pure threshold check — performs no I/O and does not consult durable
    unlock state. Callers that need "have we already unlocked this?" semantics
    must consult their own store (see ``AchievementUnlockStore`` in
    ``achievement_unlock_daemon``).

    Parameters
    ----------
    achievement_id:
        Registry key for the achievement to evaluate. Forwarded to
        ``get_achievement_milestone`` (whitespace-stripped, case-sensitive).
    metric_value:
        Caller-supplied current value of the agent's metric. Must be a
        non-negative ``int``; ``bool`` values are rejected because they are
        a subclass of ``int`` in Python and almost always indicate a caller
        bug.

    Returns
    -------
    bool
        ``True`` iff ``metric_value`` is at or above the milestone's
        ``threshold``.

    Raises
    ------
    TypeError
        If ``metric_value`` is not an ``int`` (or is a ``bool``).
    ValueError
        If ``metric_value`` is negative.
    UnknownAchievementMilestoneError
        If ``achievement_id`` is not registered.
    """

    if isinstance(metric_value, bool) or not isinstance(metric_value, int):
        raise TypeError("metric_value must be an int")
    if metric_value < 0:
        raise ValueError("metric_value must be >= 0")
    milestone = get_achievement_milestone(achievement_id)
    return metric_value >= milestone.threshold


def _assert_registry_keys_match_ids() -> None:
    """Raise ``RuntimeError`` if any registry key disagrees with its row id.

    Import-time invariant: every key in ``ACHIEVEMENT_MILESTONES`` must equal
    the ``achievement_id`` of the row it points to. Catching this at import
    means a malformed registry blocks startup instead of producing surprising
    lookup misses at runtime.
    """
    for key, milestone in ACHIEVEMENT_MILESTONES.items():
        if key != milestone.achievement_id:
            raise RuntimeError(
                "RPG achievement registry key does not match achievement_id: "
                f"{key!r} != {milestone.achievement_id!r}"
            )


_assert_registry_keys_match_ids()


__all__ = [
    "ACHIEVEMENT_MILESTONES",
    "AchievementCategory",
    "AchievementMetric",
    "AchievementMilestone",
    "UnknownAchievementMilestoneError",
    "achievement_milestone_reached",
    "get_achievement_milestone",
    "list_achievement_milestones",
]
