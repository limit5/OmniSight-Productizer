"""FS.8.1 -- Stripe checkout / billing portal scaffold tests."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import auth as _auth
from backend import stripe_billing as sb
from backend.routers import billing as billing_router


class _FakeStripeClient:
    def __init__(self) -> None:
        self.checkout_payloads: list[dict[str, str]] = []
        self.portal_payloads: list[dict[str, str]] = []

    async def create_checkout_session(
        self, config: sb.StripeBillingConfig, payload: dict[str, str],
    ) -> dict[str, Any]:
        self.checkout_payloads.append(payload)
        return {
            "id": "cs_test_123",
            "object": "checkout.session",
            "url": "https://checkout.stripe.test/session",
            "secret": "not-returned",
        }

    async def create_billing_portal_session(
        self, config: sb.StripeBillingConfig, payload: dict[str, str],
    ) -> dict[str, Any]:
        self.portal_payloads.append(payload)
        return {
            "id": "bps_test_123",
            "object": "billing_portal.session",
            "url": "https://billing.stripe.test/session",
        }


def _config() -> sb.StripeBillingConfig:
    return sb.StripeBillingConfig(
        secret_key="sk_test_123",
        checkout_price_id="price_default",
        checkout_success_url="https://app.example/billing/success",
        checkout_cancel_url="https://app.example/billing/cancel",
        portal_return_url="https://app.example/billing",
    )


class TestCheckoutPayload:
    async def test_checkout_session_uses_config_defaults(self) -> None:
        fake = _FakeStripeClient()

        out = await sb.create_checkout_session(
            _config(),
            tenant_id="t-acme",
            user_id="u-1",
            user_email="owner@example.com",
            client=fake,
        )

        assert out["id"] == "cs_test_123"
        assert fake.checkout_payloads == [{
            "mode": "subscription",
            "line_items[0][price]": "price_default",
            "line_items[0][quantity]": "1",
            "success_url": "https://app.example/billing/success",
            "cancel_url": "https://app.example/billing/cancel",
            "client_reference_id": "t-acme",
            "metadata[tenant_id]": "t-acme",
            "metadata[user_id]": "u-1",
            "customer_email": "owner@example.com",
        }]

    async def test_checkout_session_allows_request_overrides(self) -> None:
        fake = _FakeStripeClient()

        await sb.create_checkout_session(
            _config(),
            tenant_id="t-acme",
            user_id="u-1",
            user_email="owner@example.com",
            price_id="price_override",
            success_url="https://app.example/success?session_id={CHECKOUT_SESSION_ID}",
            cancel_url="https://app.example/cancel",
            customer_id="cus_123",
            client=fake,
        )

        payload = fake.checkout_payloads[0]
        assert payload["line_items[0][price]"] == "price_override"
        assert payload["success_url"] == (
            "https://app.example/success?session_id={CHECKOUT_SESSION_ID}"
        )
        assert payload["cancel_url"] == "https://app.example/cancel"
        assert payload["customer"] == "cus_123"
        assert "customer_email" not in payload

    async def test_checkout_requires_secret_and_price_and_urls(self) -> None:
        with pytest.raises(sb.StripeBillingConfigError) as exc:
            await sb.create_checkout_session(
                sb.StripeBillingConfig(secret_key=""),
                tenant_id="t-acme",
                user_id="u-1",
                user_email="owner@example.com",
                client=_FakeStripeClient(),
            )
        assert "missing Stripe secret key" in str(exc.value)

        with pytest.raises(sb.StripeBillingConfigError) as exc:
            await sb.create_checkout_session(
                sb.StripeBillingConfig(secret_key="sk_test_123"),
                tenant_id="t-acme",
                user_id="u-1",
                user_email="owner@example.com",
                client=_FakeStripeClient(),
            )
        assert "price_id" in str(exc.value)
        assert "success_url" in str(exc.value)
        assert "cancel_url" in str(exc.value)


class TestPortalPayload:
    async def test_portal_session_uses_customer_and_return_url(self) -> None:
        fake = _FakeStripeClient()

        out = await sb.create_billing_portal_session(
            _config(), customer_id="cus_123", client=fake,
        )

        assert out["id"] == "bps_test_123"
        assert fake.portal_payloads == [{
            "customer": "cus_123",
            "return_url": "https://app.example/billing",
        }]

    async def test_portal_session_allows_return_url_override(self) -> None:
        fake = _FakeStripeClient()

        await sb.create_billing_portal_session(
            _config(),
            customer_id="cus_123",
            return_url="https://app.example/account",
            client=fake,
        )

        assert fake.portal_payloads[0]["return_url"] == "https://app.example/account"

    async def test_portal_requires_customer_and_return_url(self) -> None:
        with pytest.raises(sb.StripeBillingConfigError) as exc:
            await sb.create_billing_portal_session(
                _config(), customer_id="", client=_FakeStripeClient(),
            )
        assert "customer_id" in str(exc.value)

        with pytest.raises(sb.StripeBillingConfigError) as exc:
            await sb.create_billing_portal_session(
                sb.StripeBillingConfig(secret_key="sk_test_123"),
                customer_id="cus_123",
                client=_FakeStripeClient(),
            )
        assert "return_url" in str(exc.value)


class TestResponseShape:
    async def test_session_response_returns_public_subset(self) -> None:
        assert sb.session_response({
            "id": "cs_test_123",
            "object": "checkout.session",
            "url": "https://checkout.stripe.test/session",
            "client_secret": "hidden",
        }) == {
            "id": "cs_test_123",
            "object": "checkout.session",
            "url": "https://checkout.stripe.test/session",
        }


class TestRouter:
    def test_router_uses_require_admin_dependency(self) -> None:
        deps = {
            getattr(dep, "dependency", None)
            for dep in billing_router.router.dependencies
        }
        assert _auth.require_admin in deps

    def test_checkout_route_maps_missing_config_to_400(self) -> None:
        app = FastAPI()
        app.include_router(billing_router.router)
        app.dependency_overrides[_auth.require_admin] = lambda: _auth.User(
            id="u-1", email="owner@example.com", name="Owner", role="admin",
            tenant_id="t-acme",
        )
        app.dependency_overrides[_auth.current_user] = lambda: _auth.User(
            id="u-1", email="owner@example.com", name="Owner", role="admin",
            tenant_id="t-acme",
        )

        client = TestClient(app)
        resp = client.post("/billing/stripe/checkout-session", json={})

        assert resp.status_code == 400
        assert "Stripe" in resp.json()["detail"]
