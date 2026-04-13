"""Tests for OpenRouter provider integration (Phase 37)."""

from __future__ import annotations

import pytest


class TestOpenRouterProvider:

    def test_provider_in_list(self):
        from backend.agents.llm import list_providers
        providers = list_providers()
        ids = [p["id"] for p in providers]
        assert "openrouter" in ids

    def test_provider_metadata(self):
        from backend.agents.llm import list_providers
        providers = list_providers()
        or_provider = next(p for p in providers if p["id"] == "openrouter")
        assert or_provider["name"] == "OpenRouter"
        assert or_provider["requires_key"] is True
        assert or_provider["env_var"] == "OMNISIGHT_OPENROUTER_API_KEY"
        assert or_provider["default_model"] == "anthropic/claude-sonnet-4"

    def test_exclusive_models_present(self):
        """OpenRouter should list exclusive models not available via direct providers."""
        from backend.agents.llm import list_providers
        providers = list_providers()
        or_provider = next(p for p in providers if p["id"] == "openrouter")
        models = or_provider["models"]
        # These are only accessible through OpenRouter
        assert "qwen/qwen3-235b-a22b" in models
        assert "cohere/command-r-plus" in models
        assert "mistralai/mistral-large" in models
        assert "meta-llama/llama-4-maverick" in models
        assert "perplexity/sonar-pro" in models
        assert "mistralai/codestral" in models

    def test_model_count(self):
        from backend.agents.llm import list_providers
        providers = list_providers()
        or_provider = next(p for p in providers if p["id"] == "openrouter")
        assert len(or_provider["models"]) >= 14

    def test_total_providers_is_nine(self):
        from backend.agents.llm import list_providers
        providers = list_providers()
        assert len(providers) == 9

    def test_create_llm_no_key(self):
        """Without API key, _create_llm returns None."""
        from backend.agents.llm import _create_llm
        result = _create_llm("openrouter", None)
        assert result is None

    def test_config_has_key_field(self):
        from backend.config import settings
        assert hasattr(settings, "openrouter_api_key")
        # Default is empty string
        assert settings.openrouter_api_key == ""


class TestFallbackChain:

    def test_openrouter_in_default_chain(self):
        from backend.config import settings
        chain = [p.strip() for p in settings.llm_fallback_chain.split(",")]
        assert "openrouter" in chain

    def test_openrouter_before_ollama(self):
        """OpenRouter should be before Ollama (last resort local) in chain."""
        from backend.config import settings
        chain = [p.strip() for p in settings.llm_fallback_chain.split(",")]
        or_idx = chain.index("openrouter")
        ollama_idx = chain.index("ollama")
        assert or_idx < ollama_idx

    def test_chain_order(self):
        from backend.config import settings
        chain = [p.strip() for p in settings.llm_fallback_chain.split(",")]
        # Direct providers first, then aggregator, then local
        assert chain.index("anthropic") < chain.index("openrouter")
        assert chain.index("openai") < chain.index("openrouter")
        assert chain.index("openrouter") < chain.index("ollama")


class TestProviderEndpoint:

    @pytest.mark.asyncio
    async def test_providers_endpoint_includes_openrouter(self, client):
        resp = await client.get("/api/v1/providers")
        assert resp.status_code == 200
        data = resp.json()
        provider_ids = [p["id"] for p in data["providers"]]
        assert "openrouter" in provider_ids

    @pytest.mark.asyncio
    async def test_openrouter_models_in_response(self, client):
        resp = await client.get("/api/v1/providers")
        data = resp.json()
        or_provider = next(p for p in data["providers"] if p["id"] == "openrouter")
        assert "qwen/qwen3-235b-a22b" in or_provider["models"]
        assert "mistralai/mistral-large" in or_provider["models"]
