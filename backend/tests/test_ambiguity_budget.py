"""Tests for Phase 47C: Ambiguity handling + Budget Strategy."""

from __future__ import annotations

import pytest

from backend import ambiguity as amb
from backend import budget_strategy as bs
from backend import decision_engine as de


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Ambiguity
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestAmbiguity:

    def setup_method(self):
        de._reset_for_tests()

    def test_requires_options(self):
        with pytest.raises(ValueError):
            amb.propose_options("x", "title", [])

    def test_rejects_duplicate_ids(self):
        with pytest.raises(ValueError):
            amb.propose_options("x", "t", [
                {"id": "a", "label": "A"},
                {"id": "a", "label": "dup"},
            ])

    def test_rejects_missing_id(self):
        with pytest.raises(ValueError):
            amb.propose_options("x", "t", [{"label": "no id"}])

    def test_safe_default_wins(self):
        de.set_mode("manual")
        d = amb.propose_options(
            "impl_choice", "Which approach?",
            [
                {"id": "fast", "label": "Fast"},
                {"id": "safe", "label": "Safe", "is_safe_default": True},
                {"id": "flex", "label": "Flex"},
            ],
        )
        assert d.status == de.DecisionStatus.pending
        assert d.default_option_id == "safe"

    def test_first_option_is_default_when_no_safe(self):
        de.set_mode("manual")
        d = amb.propose_options(
            "x", "t", [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
        )
        assert d.default_option_id == "a"

    def test_auto_selects_in_supervised(self):
        de.set_mode("supervised")
        d = amb.propose_options(
            "x", "t", [
                {"id": "x", "label": "X"},
                {"id": "safe", "label": "Safe", "is_safe_default": True},
            ],
        )
        # routine severity auto-executes in supervised — and picks safe default
        assert d.status == de.DecisionStatus.auto_executed
        assert d.chosen_option_id == "safe"

    def test_queued_when_risky(self):
        de.set_mode("supervised")
        d = amb.propose_options(
            "x", "t",
            [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
            severity="risky",
        )
        # risky requires full_auto+ to auto-execute
        assert d.status == de.DecisionStatus.pending


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Budget strategy
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestBudgetStrategy:

    def setup_method(self):
        bs._reset_for_tests()

    def test_default_is_balanced(self):
        assert bs.get_strategy() == bs.BudgetStrategy.balanced
        t = bs.get_tuning()
        assert t.model_tier == "default"
        assert t.max_retries == 2

    def test_set_by_string(self):
        bs.set_strategy("quality")
        t = bs.get_tuning()
        assert t.model_tier == "premium"
        assert t.max_retries == 3
        assert t.downgrade_at_usage_pct == 100

    def test_cost_saver_tightens_budget(self):
        bs.set_strategy("cost_saver")
        t = bs.get_tuning()
        assert t.model_tier == "budget"
        assert t.max_retries == 1
        assert t.downgrade_at_usage_pct == 70
        assert t.freeze_at_usage_pct == 95

    def test_sprint_enables_parallel(self):
        bs.set_strategy("sprint")
        assert bs.get_tuning().prefer_parallel is True

    def test_invalid_strategy_rejected(self):
        with pytest.raises(ValueError):
            bs.set_strategy("ludicrous")

    def test_list_strategies(self):
        ids = [t["strategy"] for t in bs.list_strategies()]
        assert set(ids) == {"quality", "balanced", "cost_saver", "sprint"}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTTP
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestBudgetRoutes:

    def setup_method(self):
        bs._reset_for_tests()

    @pytest.mark.asyncio
    async def test_get_budget_strategy(self, client):
        r = await client.get("/api/v1/budget-strategy")
        assert r.status_code == 200
        body = r.json()
        assert body["strategy"] == "balanced"
        assert body["tuning"]["max_retries"] == 2
        assert len(body["available"]) == 4

    @pytest.mark.asyncio
    async def test_put_budget_strategy_valid(self, client):
        r = await client.put("/api/v1/budget-strategy", json={"strategy": "cost_saver"})
        assert r.status_code == 200
        assert r.json()["tuning"]["model_tier"] == "budget"

    @pytest.mark.asyncio
    async def test_put_budget_strategy_invalid(self, client):
        r = await client.put("/api/v1/budget-strategy", json={"strategy": "zzz"})
        assert r.status_code == 422
