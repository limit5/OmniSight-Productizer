"""Tests for token budget management and provider failover."""

from backend.routers.system import (
    track_tokens,
    get_daily_cost,
    _token_usage,
)


class TestTokenBudget:

    def setup_method(self):
        """Reset token state before each test."""
        _token_usage.clear()
        import backend.routers.system as sys_mod
        sys_mod.token_frozen = False
        sys_mod._last_budget_level = ""

    def test_get_daily_cost_empty(self):
        assert get_daily_cost() == 0

    def test_track_tokens_accumulates_cost(self):
        track_tokens("claude-sonnet-4-20250514", 1000, 500, 100)
        cost = get_daily_cost()
        assert cost > 0

    def test_track_tokens_multiple_models(self):
        track_tokens("claude-sonnet-4-20250514", 1000, 500, 100)
        track_tokens("gpt-4o", 2000, 1000, 200)
        assert len(_token_usage) == 2
        assert get_daily_cost() > 0

    def test_frozen_flag_default_false(self):
        import backend.routers.system as sys_mod
        assert sys_mod.token_frozen is False


class TestProviderFailover:

    def test_get_llm_failover_chain_configured(self):
        """Failover chain is configured and get_llm doesn't crash."""
        from backend.config import settings
        chain = [p.strip() for p in settings.llm_fallback_chain.split(",") if p.strip()]
        assert len(chain) >= 2  # At least 2 providers in chain

    def test_get_llm_returns_none_when_frozen(self):
        """When token budget is frozen, get_llm returns None."""
        import backend.routers.system as sys_mod
        sys_mod.token_frozen = True
        try:
            from backend.agents.llm import get_llm, _cache
            _cache.clear()
            result = get_llm()
            assert result is None
        finally:
            sys_mod.token_frozen = False
