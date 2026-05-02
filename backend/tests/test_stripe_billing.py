"""FS.8.1 -- Stripe checkout / billing portal scaffold tests."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx
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
            "subscription_data[metadata][tenant_id]": "t-acme",
            "subscription_data[metadata][user_id]": "u-1",
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


class TestHttpxStripeBillingClient:
    @respx.mock
    async def test_client_posts_checkout_form_with_stripe_headers(self) -> None:
        config = sb.StripeBillingConfig(
            secret_key="sk_test_123",
            api_base_url="https://stripe.test/v1",
        )
        route = respx.post("https://stripe.test/v1/checkout/sessions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "cs_test_123",
                    "object": "checkout.session",
                    "url": "https://checkout.stripe.test/session",
                },
            ),
        )

        out = await sb.HttpxStripeBillingClient().create_checkout_session(
            config, {"mode": "subscription"},
        )

        assert out["id"] == "cs_test_123"
        request = route.calls[0].request
        assert request.headers["Authorization"] == "Bearer sk_test_123"
        assert request.headers["Stripe-Version"] == "2025-04-30.basil"
        assert request.content.decode() == "mode=subscription"

    @respx.mock
    async def test_client_maps_stripe_error_message(self) -> None:
        config = sb.StripeBillingConfig(
            secret_key="sk_test_123",
            api_base_url="https://stripe.test/v1",
        )
        respx.post("https://stripe.test/v1/billing_portal/sessions").mock(
            return_value=httpx.Response(
                400,
                json={"error": {"message": "No such customer: cus_missing"}},
            ),
        )

        with pytest.raises(sb.StripeBillingError) as exc:
            await sb.HttpxStripeBillingClient().create_billing_portal_session(
                config, {"customer": "cus_missing"},
            )

        assert "No such customer" in str(exc.value)


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
    def _client(self) -> TestClient:
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
        return TestClient(app)

    def test_router_uses_require_admin_dependency(self) -> None:
        deps = {
            getattr(dep, "dependency", None)
            for dep in billing_router.router.dependencies
        }
        assert _auth.require_admin in deps

    def test_checkout_route_maps_missing_config_to_400(self) -> None:
        resp = self._client().post("/billing/stripe/checkout-session", json={})

        assert resp.status_code == 400
        assert "Stripe" in resp.json()["detail"]

    def test_checkout_route_passes_user_and_body_to_billing_helper(
        self, monkeypatch,
    ) -> None:
        captured: dict[str, Any] = {}

        async def _fake_create_checkout_session(config, **kwargs):
            captured["config"] = config
            captured["kwargs"] = kwargs
            return {
                "id": "cs_route_123",
                "object": "checkout.session",
                "url": "https://checkout.stripe.test/route",
                "secret": "hidden",
            }

        monkeypatch.setattr(
            billing_router,
            "create_checkout_session",
            _fake_create_checkout_session,
        )

        resp = self._client().post(
            "/billing/stripe/checkout-session",
            json={
                "price_id": "price_override",
                "success_url": "https://app.example/success",
                "cancel_url": "https://app.example/cancel",
                "customer_id": "cus_123",
            },
        )

        assert resp.status_code == 200
        assert resp.json() == {
            "id": "cs_route_123",
            "object": "checkout.session",
            "url": "https://checkout.stripe.test/route",
        }
        assert captured["kwargs"] == {
            "tenant_id": "t-acme",
            "user_id": "u-1",
            "user_email": "owner@example.com",
            "price_id": "price_override",
            "success_url": "https://app.example/success",
            "cancel_url": "https://app.example/cancel",
            "customer_id": "cus_123",
        }

    def test_portal_route_maps_stripe_error_to_502(self, monkeypatch) -> None:
        async def _fake_create_billing_portal_session(config, **kwargs):
            raise sb.StripeBillingError("stripe unavailable")

        monkeypatch.setattr(
            billing_router,
            "create_billing_portal_session",
            _fake_create_billing_portal_session,
        )

        resp = self._client().post(
            "/billing/stripe/portal-session",
            json={"customer_id": "cus_123"},
        )

        assert resp.status_code == 502
        assert resp.json()["detail"] == "stripe unavailable"
