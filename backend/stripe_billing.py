"""FS.8.1 -- Stripe checkout / billing portal scaffold.

This module owns the narrow Stripe API surface needed to create hosted
checkout and customer-portal sessions. Webhook handling, subscription
state sync, and local billing persistence intentionally live in later
FS.8 items.

Module-global state audit (per implement_phase_step.md SOP Step 1)
------------------------------------------------------------------
Only immutable endpoint constants are defined at module scope. Runtime
configuration is read per request from ``Settings`` and the HTTP client
is constructed per call unless tests inject one, so no cross-worker
state is shared or coordinated here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import httpx


STRIPE_API_BASE_URL = "https://api.stripe.com/v1"


class StripeBillingConfigError(ValueError):
    """Raised when Stripe billing settings are incomplete."""


class StripeBillingError(RuntimeError):
    """Raised when Stripe rejects or cannot serve a session request."""


@dataclass(frozen=True)
class StripeBillingConfig:
    secret_key: str = ""
    checkout_price_id: str = ""
    checkout_success_url: str = ""
    checkout_cancel_url: str = ""
    portal_return_url: str = ""
    api_base_url: str = STRIPE_API_BASE_URL
    timeout_s: float = 10.0

    @classmethod
    def from_settings(cls, settings: Any) -> "StripeBillingConfig":
        return cls(
            secret_key=str(getattr(settings, "stripe_secret_key", "") or "").strip(),
            checkout_price_id=str(
                getattr(settings, "stripe_checkout_price_id", "") or "",
            ).strip(),
            checkout_success_url=str(
                getattr(settings, "stripe_checkout_success_url", "") or "",
            ).strip(),
            checkout_cancel_url=str(
                getattr(settings, "stripe_checkout_cancel_url", "") or "",
            ).strip(),
            portal_return_url=str(
                getattr(settings, "stripe_billing_portal_return_url", "") or "",
            ).strip(),
            api_base_url=str(
                getattr(settings, "stripe_api_base_url", "") or STRIPE_API_BASE_URL,
            ).strip().rstrip("/") or STRIPE_API_BASE_URL,
        )


class StripeBillingClient(Protocol):
    async def create_checkout_session(
        self, config: StripeBillingConfig, payload: dict[str, str],
    ) -> dict[str, Any]:
        """Create a Stripe Checkout Session from form-encoded payload."""

    async def create_billing_portal_session(
        self, config: StripeBillingConfig, payload: dict[str, str],
    ) -> dict[str, Any]:
        """Create a Stripe Billing Portal Session from form-encoded payload."""


class HttpxStripeBillingClient:
    """Async Stripe client using ``httpx`` and Stripe form encoding."""

    async def create_checkout_session(
        self, config: StripeBillingConfig, payload: dict[str, str],
    ) -> dict[str, Any]:
        return await self._post(config, "/checkout/sessions", payload)

    async def create_billing_portal_session(
        self, config: StripeBillingConfig, payload: dict[str, str],
    ) -> dict[str, Any]:
        return await self._post(config, "/billing_portal/sessions", payload)

    async def _post(
        self, config: StripeBillingConfig, path: str, payload: dict[str, str],
    ) -> dict[str, Any]:
        url = f"{config.api_base_url}{path}"
        headers = {
            "Authorization": f"Bearer {config.secret_key}",
            "Stripe-Version": "2025-04-30.basil",
        }
        try:
            async with httpx.AsyncClient(timeout=config.timeout_s) as client:
                response = await client.post(url, data=payload, headers=headers)
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = _stripe_error_message(exc.response)
            raise StripeBillingError(detail) from exc
        except httpx.HTTPError as exc:
            raise StripeBillingError(f"stripe transport error: {exc}") from exc

        try:
            parsed = response.json()
        except ValueError as exc:
            raise StripeBillingError("stripe returned a non-JSON response") from exc
        if not isinstance(parsed, dict):
            raise StripeBillingError("stripe returned an unexpected response shape")
        return parsed


def build_checkout_payload(
    config: StripeBillingConfig,
    *,
    tenant_id: str,
    user_id: str,
    user_email: str,
    price_id: str = "",
    success_url: str = "",
    cancel_url: str = "",
    customer_id: str = "",
) -> dict[str, str]:
    """Build the Stripe Checkout Session form body.

    The payload carries only metadata needed by later FS.8 webhook/state
    tasks to correlate the Stripe session back to OmniSight tenant/user
    records. It does not persist anything locally.
    """
    _require_secret(config)
    resolved_price = (price_id or config.checkout_price_id).strip()
    resolved_success = (success_url or config.checkout_success_url).strip()
    resolved_cancel = (cancel_url or config.checkout_cancel_url).strip()
    missing = [
        name
        for name, value in (
            ("price_id", resolved_price),
            ("success_url", resolved_success),
            ("cancel_url", resolved_cancel),
        )
        if not value
    ]
    if missing:
        raise StripeBillingConfigError(
            "missing Stripe checkout setting(s): " + ", ".join(missing),
        )

    payload = {
        "mode": "subscription",
        "line_items[0][price]": resolved_price,
        "line_items[0][quantity]": "1",
        "success_url": resolved_success,
        "cancel_url": resolved_cancel,
        "client_reference_id": tenant_id,
        "metadata[tenant_id]": tenant_id,
        "metadata[user_id]": user_id,
        "subscription_data[metadata][tenant_id]": tenant_id,
        "subscription_data[metadata][user_id]": user_id,
    }
    if customer_id:
        payload["customer"] = customer_id
    elif user_email:
        payload["customer_email"] = user_email
    return payload


def build_portal_payload(
    config: StripeBillingConfig,
    *,
    customer_id: str,
    return_url: str = "",
) -> dict[str, str]:
    """Build the Stripe Billing Portal Session form body."""
    _require_secret(config)
    resolved_return_url = (return_url or config.portal_return_url).strip()
    if not customer_id.strip():
        raise StripeBillingConfigError("missing Stripe portal customer_id")
    if not resolved_return_url:
        raise StripeBillingConfigError("missing Stripe portal return_url")
    return {
        "customer": customer_id.strip(),
        "return_url": resolved_return_url,
    }


async def create_checkout_session(
    config: StripeBillingConfig,
    *,
    tenant_id: str,
    user_id: str,
    user_email: str,
    price_id: str = "",
    success_url: str = "",
    cancel_url: str = "",
    customer_id: str = "",
    client: StripeBillingClient | None = None,
) -> dict[str, Any]:
    payload = build_checkout_payload(
        config,
        tenant_id=tenant_id,
        user_id=user_id,
        user_email=user_email,
        price_id=price_id,
        success_url=success_url,
        cancel_url=cancel_url,
        customer_id=customer_id,
    )
    billing_client = client or HttpxStripeBillingClient()
    return await billing_client.create_checkout_session(config, payload)


async def create_billing_portal_session(
    config: StripeBillingConfig,
    *,
    customer_id: str,
    return_url: str = "",
    client: StripeBillingClient | None = None,
) -> dict[str, Any]:
    payload = build_portal_payload(
        config, customer_id=customer_id, return_url=return_url,
    )
    billing_client = client or HttpxStripeBillingClient()
    return await billing_client.create_billing_portal_session(config, payload)


def session_response(session: dict[str, Any]) -> dict[str, str]:
    """Return the stable public subset of a Stripe session response."""
    return {
        "id": str(session.get("id") or ""),
        "url": str(session.get("url") or ""),
        "object": str(session.get("object") or ""),
    }


def _require_secret(config: StripeBillingConfig) -> None:
    if not config.secret_key:
        raise StripeBillingConfigError("missing Stripe secret key")


def _stripe_error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return f"stripe request failed with HTTP {response.status_code}"
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
    return f"stripe request failed with HTTP {response.status_code}"


__all__ = [
    "HttpxStripeBillingClient",
    "StripeBillingClient",
    "StripeBillingConfig",
    "StripeBillingConfigError",
    "StripeBillingError",
    "build_checkout_payload",
    "build_portal_payload",
    "create_billing_portal_session",
    "create_checkout_session",
    "session_response",
]
