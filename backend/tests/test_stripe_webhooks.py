"""FS.8.2 -- Stripe webhook handler scaffold tests."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from contextlib import contextmanager
from unittest.mock import AsyncMock

import pytest

from backend import stripe_webhooks as sw


@contextmanager
def _stripe_webhook_secret(secret: str):
    from backend.config import settings

    original = settings.stripe_webhook_secret
    try:
        settings.stripe_webhook_secret = secret
        yield
    finally:
        settings.stripe_webhook_secret = original


def _sign(secret: str, raw: bytes, timestamp: int | None = None) -> str:
    timestamp = int(time.time() if timestamp is None else timestamp)
    signed = str(timestamp).encode() + b"." + raw
    digest = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


class TestStripeWebhookHelpers:
    def test_valid_signature_accepts_raw_body(self) -> None:
        raw = b'{"id":"evt_123","type":"checkout.session.completed"}'

        sw.verify_stripe_webhook_signature(
            raw,
            _sign("whsec_test", raw, timestamp=1_700_000_000),
            "whsec_test",
            now_s=1_700_000_000,
        )

    def test_invalid_signature_rejected(self) -> None:
        raw = b'{"id":"evt_123"}'

        with pytest.raises(sw.StripeWebhookSignatureError):
            sw.verify_stripe_webhook_signature(
                raw,
                "t=1700000000,v1=bad",
                "whsec_test",
                now_s=1_700_000_000,
            )

    def test_old_timestamp_rejected(self) -> None:
        raw = b'{"id":"evt_123"}'

        with pytest.raises(sw.StripeWebhookSignatureError):
            sw.verify_stripe_webhook_signature(
                raw,
                _sign("whsec_test", raw, timestamp=1_700_000_000),
                "whsec_test",
                now_s=1_700_001_000,
            )

    def test_parse_event_envelope(self) -> None:
        event = sw.parse_stripe_webhook_event({
            "id": "evt_123",
            "type": "customer.subscription.updated",
            "data": {"object": {"id": "sub_123", "object": "subscription"}},
        })

        assert event.to_dict() == {
            "id": "evt_123",
            "type": "customer.subscription.updated",
            "object": "subscription",
        }
        assert event.data_object["id"] == "sub_123"


class TestStripeWebhookEndpoint:
    @pytest.mark.asyncio
    async def test_unconfigured_returns_503(self, client):
        with _stripe_webhook_secret(""):
            resp = await client.post("/api/v1/webhooks/stripe", json={})

        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_invalid_signature_returns_401(self, client):
        with _stripe_webhook_secret("whsec_test"):
            resp = await client.post(
                "/api/v1/webhooks/stripe",
                json={"id": "evt_123", "type": "checkout.session.completed"},
                headers={"Stripe-Signature": "t=1700000000,v1=bad"},
            )

        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_valid_signature_routes_event(self, client, monkeypatch):
        from backend.routers import webhooks

        mock_handler = AsyncMock()
        monkeypatch.setattr(webhooks, "_on_stripe_webhook_event", mock_handler)

        secret = "whsec_test"
        body = {
            "id": "evt_123",
            "type": "checkout.session.completed",
            "data": {"object": {"id": "cs_test_123", "object": "checkout.session"}},
        }
        raw = json.dumps(body).encode()

        with _stripe_webhook_secret(secret):
            resp = await client.post(
                "/api/v1/webhooks/stripe",
                content=raw,
                headers={
                    "Content-Type": "application/json",
                    "Stripe-Signature": _sign(secret, raw),
                },
            )

        assert resp.status_code == 200
        assert resp.json() == {
            "status": "ok",
            "event": {
                "id": "evt_123",
                "type": "checkout.session.completed",
                "object": "checkout.session",
            },
        }
        mock_handler.assert_awaited_once()
