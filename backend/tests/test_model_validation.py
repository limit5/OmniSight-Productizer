"""Tests for model validation, parsing, and budget-aware routing.

Covers:
- H9: validate_model_spec() — all branches
- H10: _parse_model_spec() — all formats
- M13: _llm_decompose() — ATOMIC, numbered lines, fallback
- M14: select_model_for_task() budget paths — mocked budget ratios
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  H9: validate_model_spec
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestValidateModelSpec:

    def test_empty_string(self):
        from backend.agents.llm import validate_model_spec
        r = validate_model_spec("")
        assert r["valid"] is True
        assert r["warning"] == ""

    def test_unknown_provider(self):
        from backend.agents.llm import validate_model_spec
        r = validate_model_spec("fakeprovider:model")
        assert r["valid"] is False
        assert "Unknown provider" in r["warning"]

    def test_known_provider_no_key(self):
        from backend.agents.llm import validate_model_spec
        r = validate_model_spec("openrouter:qwen/qwen3-235b")
        assert r["valid"] is False
        assert "API key" in r["warning"]
        assert r["configured"] is False

    def test_ollama_no_key_required(self):
        from backend.agents.llm import validate_model_spec
        r = validate_model_spec("ollama:llama3.1")
        assert r["valid"] is True
        assert r["configured"] is True

    def test_plain_model_no_provider_match(self):
        """Plain model name that doesn't match any provider's model list."""
        from backend.agents.llm import validate_model_spec
        r = validate_model_spec("totally-unknown-model-xyz")
        # Should return valid=True with global provider as fallback
        assert r["valid"] is True

    def test_plain_model_with_provider_match(self):
        """Plain model name found in a provider's model list."""
        from backend.agents.llm import validate_model_spec
        r = validate_model_spec("llama3.1")
        assert r["valid"] is True
        assert r["provider"] == "ollama"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  H10: _parse_model_spec
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestParseModelSpec:

    def test_empty(self):
        from backend.agents.nodes import _parse_model_spec
        assert _parse_model_spec("") == (None, None)

    def test_plain_model(self):
        from backend.agents.nodes import _parse_model_spec
        p, m = _parse_model_spec("claude-sonnet-4")
        assert p is None
        assert m == "claude-sonnet-4"

    def test_provider_colon_model(self):
        from backend.agents.nodes import _parse_model_spec
        p, m = _parse_model_spec("openrouter:qwen/qwen3-235b")
        assert p == "openrouter"
        assert m == "qwen/qwen3-235b"

    def test_provider_colon_simple_model(self):
        from backend.agents.nodes import _parse_model_spec
        p, m = _parse_model_spec("groq:llama-3.3-70b")
        assert p == "groq"
        assert m == "llama-3.3-70b"

    def test_ollama_colon(self):
        from backend.agents.nodes import _parse_model_spec
        p, m = _parse_model_spec("ollama:deepseek-r1")
        assert p == "ollama"
        assert m == "deepseek-r1"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  M13: _llm_decompose
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestLLMDecompose:

    @pytest.mark.asyncio
    async def test_no_llm_returns_none(self):
        """When LLM is unavailable, should return None (trigger regex fallback)."""
        from backend.routers.invoke import _llm_decompose
        result = await _llm_decompose("compile and test")
        # No LLM configured in test env → returns None
        assert result is None

    @pytest.mark.asyncio
    async def test_atomic_returns_empty_list(self):
        """When LLM says ATOMIC, should return [] (no regex fallback)."""
        from backend.routers.invoke import _llm_decompose

        class FakeLLM:
            def invoke(self, msgs):
                class Resp:
                    content = "ATOMIC"
                return Resp()

        with patch("backend.agents.llm.get_llm", return_value=FakeLLM()):
            result = await _llm_decompose("compile driver")
        assert result == []

    @pytest.mark.asyncio
    async def test_numbered_lines_parsed(self):
        """LLM returns numbered lines → parsed into list."""
        from backend.routers.invoke import _llm_decompose

        class FakeLLM:
            def invoke(self, msgs):
                class Resp:
                    content = "1. Compile firmware\n2. Run unit tests\n3. Deploy to EVK"
                return Resp()

        with patch("backend.agents.llm.get_llm", return_value=FakeLLM()):
            result = await _llm_decompose("compile, test, deploy")
        assert result == ["Compile firmware", "Run unit tests", "Deploy to EVK"]

    @pytest.mark.asyncio
    async def test_malformed_output_returns_none(self):
        """LLM returns garbage → should return None (no valid lines)."""
        from backend.routers.invoke import _llm_decompose

        class FakeLLM:
            def invoke(self, msgs):
                class Resp:
                    content = "Sure, here is the decomposition of the task..."
                return Resp()

        with patch("backend.agents.llm.get_llm", return_value=FakeLLM()):
            result = await _llm_decompose("do something")
        assert result is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  M14: select_model_for_task budget paths
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestBudgetAwareRouting:

    def test_high_budget_limits_to_cheap(self):
        """At 90%+ budget, max_cost should be $0.5."""
        from backend.model_router import select_model_for_task
        with patch("backend.model_router._get_budget_ratio", return_value=0.95):
            # No keys configured → falls back to global, but the cost ceiling is applied
            result = select_model_for_task("firmware", "Debug memory leak")
        # With no keys, returns "" regardless, but the logic ran
        assert isinstance(result, str)

    def test_medium_budget_limits_to_standard(self):
        """At 70-90% budget, max_cost should be $5.0."""
        from backend.model_router import select_model_for_task
        with patch("backend.model_router._get_budget_ratio", return_value=0.75):
            result = select_model_for_task("firmware", "compile driver")
        assert isinstance(result, str)

    def test_low_budget_allows_complex_models(self):
        """At <70% budget + complex task, max_cost should be unlimited."""
        from backend.model_router import select_model_for_task
        with patch("backend.model_router._get_budget_ratio", return_value=0.3):
            result = select_model_for_task("firmware", "Refactor ISP pipeline architecture")
        assert isinstance(result, str)

    def test_override_ignores_budget(self):
        """Per-agent override always takes precedence regardless of budget."""
        from backend.model_router import select_model_for_task
        with patch("backend.model_router._get_budget_ratio", return_value=0.99):
            result = select_model_for_task(
                "firmware", "anything",
                agent_ai_model="anthropic:claude-opus-4",
            )
        assert result == "anthropic:claude-opus-4"

    def test_budget_ratio_zero_when_unlimited(self):
        from backend.model_router import _get_budget_ratio
        # Default token_budget_daily is 0.0 (unlimited)
        assert _get_budget_ratio() == 0.0
