"""Tests for Provider Fallback Chain management (Phase 25)."""

import pytest


class TestProviderHealthEndpoint:

    @pytest.mark.asyncio
    async def test_health_returns_chain(self, client):
        resp = await client.get("/api/v1/providers/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "chain" in data
        assert "health" in data
        assert isinstance(data["chain"], list)
        assert len(data["chain"]) > 0

    @pytest.mark.asyncio
    async def test_health_has_status_fields(self, client):
        resp = await client.get("/api/v1/providers/health")
        data = resp.json()
        for h in data["health"]:
            assert "id" in h
            assert "name" in h
            assert "status" in h
            assert h["status"] in ("active", "cooldown", "available", "unconfigured")
            assert "cooldown_remaining" in h
            assert isinstance(h["cooldown_remaining"], int)


class TestFallbackChainUpdate:

    @pytest.mark.asyncio
    async def test_update_valid_chain(self, client):
        resp = await client.put("/api/v1/providers/fallback-chain", json={
            "chain": ["anthropic", "openai", "ollama"]
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "updated"
        assert data["chain"] == ["anthropic", "openai", "ollama"]

    @pytest.mark.asyncio
    async def test_update_invalid_provider(self, client):
        resp = await client.put("/api/v1/providers/fallback-chain", json={
            "chain": ["anthropic", "nonexistent_provider"]
        })
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_update_empty_chain(self, client):
        resp = await client.put("/api/v1/providers/fallback-chain", json={
            "chain": []
        })
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_chain_persists_in_settings(self, client):
        await client.put("/api/v1/providers/fallback-chain", json={
            "chain": ["ollama", "anthropic"]
        })
        resp = await client.get("/api/v1/providers/health")
        data = resp.json()
        assert data["chain"] == ["ollama", "anthropic"]
        # Restore default
        await client.put("/api/v1/providers/fallback-chain", json={
            "chain": ["anthropic", "openai", "google", "groq", "ollama"]
        })


class TestProviderCooldown:

    def test_cooldown_constant(self):
        from backend.agents.llm import PROVIDER_COOLDOWN
        assert PROVIDER_COOLDOWN == 300

    def test_failures_dict_exists(self):
        from backend.agents.llm import _provider_failures
        assert isinstance(_provider_failures, dict)
