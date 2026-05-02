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


class _FakeBillingConn:
    def __init__(self, tenant_id: str = "") -> None:
        self.tenant_id = tenant_id
        self.fetchrow_calls: list[tuple[str, tuple[object, ...]]] = []
        self.execute_calls: list[tuple[str, tuple[object, ...]]] = []

    async def fetchrow(self, sql: str, *args: object):
        self.fetchrow_calls.append((sql, args))
        if not self.tenant_id:
            return None
        return {"tenant_id": self.tenant_id}

    async def execute(self, sql: str, *args: object) -> str:
        self.execute_calls.append((sql, args))
        return "INSERT 0 1"


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

    async def test_subscription_state_sync_upserts_metadata_tenant(self) -> None:
        event = sw.parse_stripe_webhook_event({
            "id": "evt_123",
            "type": "customer.subscription.updated",
            "data": {
                "object": {
                    "id": "sub_123",
                    "object": "subscription",
                    "customer": "cus_123",
                    "status": "active",
                    "current_period_end": 1_700_010_000,
                    "cancel_at_period_end": False,
                    "metadata": {"tenant_id": "t-acme"},
                    "items": {
                        "data": [{
                            "price": {"id": "price_pro"},
                        }],
                    },
                },
            },
        })
        conn = _FakeBillingConn()

        synced = await sw.sync_stripe_subscription_state(event, conn=conn)

        assert synced is True
        assert conn.fetchrow_calls == []
        assert len(conn.execute_calls) == 1
        sql, args = conn.execute_calls[0]
        assert "ON CONFLICT (tenant_id, provider) DO UPDATE" in sql
        assert args[:8] == (
            "t-acme",
            "stripe",
            "cus_123",
            "sub_123",
            "price_pro",
            "active",
            1_700_010_000.0,
            False,
        )

    async def test_subscription_state_sync_falls_back_to_existing_row(self) -> None:
        event = sw.parse_stripe_webhook_event({
            "id": "evt_124",
            "type": "customer.subscription.deleted",
            "data": {
                "object": {
                    "id": "sub_123",
                    "object": "subscription",
                    "customer": "cus_123",
                    "status": "canceled",
                    "cancel_at_period_end": True,
                    "metadata": {},
                },
            },
        })
        conn = _FakeBillingConn(tenant_id="t-acme")

        synced = await sw.sync_stripe_subscription_state(event, conn=conn)

        assert synced is True
        assert len(conn.fetchrow_calls) == 1
        assert conn.fetchrow_calls[0][1] == ("stripe", "sub_123", "cus_123")
        assert conn.execute_calls[0][1][0] == "t-acme"
        assert conn.execute_calls[0][1][5] == "canceled"


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
