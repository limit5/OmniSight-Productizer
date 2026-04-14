"""Fix-D D6 — BudgetStrategy state-machine smoke."""

from __future__ import annotations

import pytest

from backend import budget_strategy as bs


@pytest.fixture(autouse=True)
def _reset():
    bs._reset_for_tests()
    yield
    bs._reset_for_tests()


def test_default_is_balanced():
    assert bs.get_strategy() is bs.BudgetStrategy.balanced
    assert bs.get_tuning().model_tier == "default"


def test_list_strategies_exposes_all_four_with_keys():
    rows = bs.list_strategies()
    assert {r["strategy"] for r in rows} == {"quality", "balanced", "cost_saver", "sprint"}
    required = {"strategy", "model_tier", "max_retries",
                "downgrade_at_usage_pct", "freeze_at_usage_pct", "prefer_parallel"}
    for r in rows:
        assert required.issubset(r.keys())


@pytest.mark.parametrize("name,expected_tier,expected_retries", [
    ("quality", "premium", 3),
    ("balanced", "default", 2),
    ("cost_saver", "budget", 1),
    ("sprint", "default", 2),
])
def test_set_strategy_from_string_returns_tuning(name, expected_tier, expected_retries):
    t = bs.set_strategy(name)
    assert t.strategy.value == name
    assert t.model_tier == expected_tier
    assert t.max_retries == expected_retries
    # And the singleton reflects the change.
    assert bs.get_strategy().value == name


def test_set_strategy_from_enum_also_works():
    t = bs.set_strategy(bs.BudgetStrategy.sprint)
    assert t.prefer_parallel is True


def test_set_strategy_rejects_unknown_string():
    with pytest.raises(ValueError, match="unknown strategy"):
        bs.set_strategy("ultra-cheap")


def test_quality_never_auto_downgrades():
    """Documents the "never downgrade" invariant of the quality profile —
    if someone lowers this to 95, something went wrong."""
    bs.set_strategy("quality")
    assert bs.get_tuning().downgrade_at_usage_pct == 100


def test_sprint_prefers_parallel_while_others_dont():
    tunings = {s.value: bs._TUNINGS[s] for s in bs.BudgetStrategy}
    assert tunings["sprint"].prefer_parallel is True
    for name in ("quality", "balanced", "cost_saver"):
        assert tunings[name].prefer_parallel is False
