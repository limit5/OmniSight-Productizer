"""Tests for backend/routers/webhooks.py — Gerrit webhook event handling."""

import pytest


class TestWebhookEndpoint:

    @pytest.mark.asyncio
    async def test_gerrit_disabled_returns_503(self, client):
        """Webhook returns 503 when Gerrit is disabled."""
        from backend.config import settings
        original = settings.gerrit_enabled
        try:
            settings.gerrit_enabled = False
            res = await client.post("/api/v1/webhooks/gerrit", json={"type": "test"})
            assert res.status_code == 503
        finally:
            settings.gerrit_enabled = original

    @pytest.mark.asyncio
    async def test_gerrit_enabled_accepts_event(self, client):
        """Webhook returns 200 when Gerrit is enabled."""
        from backend.config import settings
        original = settings.gerrit_enabled
        try:
            settings.gerrit_enabled = True
            res = await client.post("/api/v1/webhooks/gerrit", json={"type": "unknown-event"})
            assert res.status_code == 200
            data = res.json()
            assert data["event"] == "unknown-event"
        finally:
            settings.gerrit_enabled = original

    @pytest.mark.asyncio
    async def test_invalid_json_returns_400(self, client):
        from backend.config import settings
        original = settings.gerrit_enabled
        try:
            settings.gerrit_enabled = True
            res = await client.post(
                "/api/v1/webhooks/gerrit",
                content=b"not json",
                headers={"Content-Type": "application/json"},
            )
            assert res.status_code == 400
        finally:
            settings.gerrit_enabled = original
