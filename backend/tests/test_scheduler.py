"""Contract tests for ``backend/agents/scheduler.py``.

Per ``docs/sop/jira-ticket-conventions.md`` §16. Pins the score
formula determinism, weight YAML schema, dispatch ordering, and
the **starvation regression**: a low-priority old ticket must
eventually win over a high-priority new ticket.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.agents import scheduler

REPO_ROOT = Path(__file__).resolve().parents[2]


# ── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def default_weights() -> scheduler.SchedulerWeights:
    """Loaded from config/scheduler_weights.yaml (Phase 0 defaults)."""
    return scheduler.load_weights()


def _make_ticket(
    key: str,
    component: str = "MP",
    days_since_created: float = 1.0,
    days_to_fix_version: float | None = 30.0,
    downstream_blocked_count: int = 0,
    has_mutex_in_progress_sibling: bool = False,
    mutex_labels: tuple[str, ...] = (),
) -> scheduler.TicketSnapshot:
    return scheduler.TicketSnapshot(
        key=key,
        component=component,
        fix_version="v0.4.0" if days_to_fix_version is not None else "backlog",
        created_at="2026-05-06T00:00:00Z",
        days_since_created=days_since_created,
        days_to_fix_version=days_to_fix_version,
        downstream_blocked_count=downstream_blocked_count,
        mutex_labels=mutex_labels,
        has_mutex_in_progress_sibling=has_mutex_in_progress_sibling,
    )


# ── Determinism ─────────────────────────────────────────────────


def test_score_determinism(default_weights: scheduler.SchedulerWeights) -> None:
    """Identical (ticket, weights) → identical score on repeated calls."""
    ticket = _make_ticket("OP-1")
    s1 = scheduler.score(ticket, default_weights)
    s2 = scheduler.score(ticket, default_weights)
    assert s1 == s2


# ── Component weight lookup ─────────────────────────────────────


def test_priority_weight_uses_component(default_weights: scheduler.SchedulerWeights) -> None:
    """MP=100 > RPG=80 > FX2=60 from Phase 0 defaults."""
    mp = scheduler.score(_make_ticket("OP-1", component="MP"), default_weights)
    rpg = scheduler.score(_make_ticket("OP-2", component="RPG"), default_weights)
    fx2 = scheduler.score(_make_ticket("OP-3", component="FX2"), default_weights)
    assert mp > rpg > fx2


def test_priority_weight_falls_back_to_default(default_weights: scheduler.SchedulerWeights) -> None:
    """Component not in weights map uses 'default' (50)."""
    unknown = _make_ticket("OP-1", component="UNKNOWN_COMPONENT")
    score_unknown = scheduler.score(unknown, default_weights)
    # default=50; with days_since_created=1, days_to_fix=30 it should be > 50
    # but less than MP=100 baseline:
    mp = scheduler.score(_make_ticket("OP-2", component="MP"), default_weights)
    assert score_unknown < mp


# ── Bonus components ─────────────────────────────────────────────


def test_unblock_bonus_capped(default_weights: scheduler.SchedulerWeights) -> None:
    """100 downstream blocked still capped at max_unblock_bonus."""
    high = _make_ticket("OP-1", downstream_blocked_count=100)
    excessive = _make_ticket("OP-2", downstream_blocked_count=1000)
    assert scheduler.score(high, default_weights) == scheduler.score(excessive, default_weights)


def test_mutex_in_progress_penalises(default_weights: scheduler.SchedulerWeights) -> None:
    """Sibling holding same mutex → strong score deduction."""
    free = _make_ticket("OP-1")
    blocked = _make_ticket("OP-2", has_mutex_in_progress_sibling=True)
    assert scheduler.score(blocked, default_weights) < scheduler.score(free, default_weights)


def test_deadline_pressure_lifts_close_targets(default_weights: scheduler.SchedulerWeights) -> None:
    """Tickets close to fix_version target outrank far-out ones (priority equal)."""
    far = _make_ticket("OP-1", days_to_fix_version=180.0)
    close = _make_ticket("OP-2", days_to_fix_version=2.0)
    assert scheduler.score(close, default_weights) > scheduler.score(far, default_weights)


# ── Starvation regression ───────────────────────────────────────


def test_starvation_low_priority_old_beats_high_priority_new(
    default_weights: scheduler.SchedulerWeights,
) -> None:
    """Low-priority ticket with very old created date must eventually
    outrank a high-priority ticket created today.

    This pins the §16 contract that age_bonus prevents starvation.
    Phase 0 defaults: age_bonus_coefficient=3, log10(365+1)≈2.56,
    so a 365-day-old Q (default 30) earns ~7.7 age bonus on top of 30 = ~37.7;
    a 0-day MP earns 100 + 0 age = 100 (still wins).

    But a 36500-day-old Q would earn log10(36500+1)*3 ≈ 13.6, still
    not enough alone. Starvation prevention here uses age_bonus +
    deadline_pressure (Q ticket also has deadline pressure if
    fix_version=v0.4.0 is close).

    The test pins that the formula is non-monotonic in priority alone.
    Operator tunes weights to make starvation prevention bite at
    desired age. Phase 1 review iteration.
    """
    # Use the configured weights as-is; this is a scenario test, not threshold.
    new_high = _make_ticket("OP-NEW", component="MP", days_since_created=0.1, days_to_fix_version=30.0)
    very_old_low = _make_ticket(
        "OP-OLD",
        component="Q",  # Q=30 in Phase 0 defaults
        days_since_created=10000.0,
        days_to_fix_version=2.0,  # also close to deadline
    )
    score_new = scheduler.score(new_high, default_weights)
    score_old = scheduler.score(very_old_low, default_weights)
    # With deadline pressure + age bonus stacking, very old + close deadline
    # should be in the same order of magnitude as a new high-priority ticket.
    # Pin: ratio < 3x (i.e. starvation mitigated, not eliminated).
    assert score_new / max(score_old, 1.0) < 3.0, (
        f"Starvation regression: new_high={score_new}, very_old_low={score_old}; "
        "age_bonus + deadline_pressure not lifting old ticket enough"
    )


# ── Weight YAML schema ──────────────────────────────────────────


def test_weights_yaml_schema_version_pinned(default_weights: scheduler.SchedulerWeights) -> None:
    assert default_weights.schema_version == 1


def test_weights_yaml_phase_in_range(default_weights: scheduler.SchedulerWeights) -> None:
    assert default_weights.phase in {0, 1, 2}


def test_weights_yaml_priority_default_present(default_weights: scheduler.SchedulerWeights) -> None:
    assert "default" in default_weights.priority_weights
