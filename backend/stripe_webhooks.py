"""FS.8.2 -- Stripe webhook verification and event scaffold.

Webhook handling verifies the Stripe-Signature HMAC, parses the event
envelope, and applies the narrow FS.8.4 subscription state projection
into ``provisioned_billing`` for subscription lifecycle events.

Module-global state audit (per implement_phase_step.md SOP Step 1)
------------------------------------------------------------------
Only immutable constants, pure helpers, and the module logger are defined
at module scope. Every worker verifies each request from the same
Settings/env secret and writes subscription state through PG
``INSERT ... ON CONFLICT``; cross-worker consistency is coordinated by
the ``provisioned_billing`` primary key in the database.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from dataclasses import dataclass, field
from typing import Any


STRIPE_WEBHOOK_TOLERANCE_S = 300
STRIPE_PROVIDER = "stripe"
STRIPE_SUBSCRIPTION_EVENTS = (
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
)

logger = logging.getLogger(__name__)


class StripeWebhookConfigError(ValueError):
    """Raised when Stripe webhook settings are incomplete."""


class StripeWebhookSignatureError(ValueError):
    """Raised when a Stripe webhook signature is malformed or invalid."""


@dataclass(frozen=True)
class StripeWebhookEvent:
    """Normalized Stripe event envelope."""

    event_id: str
    event_type: str
    object_type: str = ""
    data_object: dict[str, Any] = field(default_factory=dict, repr=False)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.event_id,
            "type": self.event_type,
            "object": self.object_type,
        }


@dataclass(frozen=True)
class StripeSubscriptionState:
    """Local projection of a Stripe subscription lifecycle event."""

    tenant_id: str
    stripe_customer_id: str
    stripe_subscription_id: str
    stripe_price_id: str
    status: str
    current_period_end: float | None
    cancel_at_period_end: bool


def verify_stripe_webhook_signature(
    raw_body: bytes,
    signature_header: str,
    secret: str,
    *,
    tolerance_s: int = STRIPE_WEBHOOK_TOLERANCE_S,
    now_s: int | None = None,
) -> None:
    """Verify Stripe's ``Stripe-Signature`` header for a raw request body."""
    if not secret:
        raise StripeWebhookConfigError("missing Stripe webhook secret")

    timestamp, signatures = _parse_signature_header(signature_header)
    now = int(time.time() if now_s is None else now_s)
    if tolerance_s >= 0 and abs(now - timestamp) > tolerance_s:
        raise StripeWebhookSignatureError("Stripe webhook timestamp outside tolerance")

    signed_payload = str(timestamp).encode() + b"." + raw_body
    expected = hmac.new(
        secret.encode(),
        signed_payload,
        hashlib.sha256,
    ).hexdigest()
    if not any(hmac.compare_digest(sig, expected) for sig in signatures):
        raise StripeWebhookSignatureError("Invalid Stripe webhook signature")


def parse_stripe_webhook_event(payload: dict[str, Any]) -> StripeWebhookEvent:
    """Parse a Stripe event envelope without applying subscription side effects."""
    event_id = str(payload.get("id") or "").strip()
    event_type = str(payload.get("type") or "").strip()
    if not event_id:
        raise ValueError("Stripe webhook event id is required")
    if not event_type:
        raise ValueError("Stripe webhook event type is required")

    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    data_object = data.get("object") if isinstance(data.get("object"), dict) else {}
    object_type = str(data_object.get("object") or "")
    return StripeWebhookEvent(
        event_id=event_id,
        event_type=event_type,
        object_type=object_type,
        data_object=data_object,
        raw=payload,
    )


async def sync_stripe_subscription_state(
    event: StripeWebhookEvent,
    *,
    conn: Any | None = None,
) -> bool:
    """Persist subscription lifecycle webhooks into ``provisioned_billing``.

    The write is a single PG upsert keyed by ``(tenant_id, provider)``.
    Concurrent webhooks from multiple uvicorn workers serialize through
    that primary key; the latest verified Stripe event wins for the
    tenant's current subscription projection.
    """
    if event.event_type not in STRIPE_SUBSCRIPTION_EVENTS:
        return False
    if event.object_type and event.object_type != "subscription":
        return False

    if conn is not None:
        state = await _subscription_state_from_event(event, conn=conn)
        if state is None:
            return False
        await _upsert_subscription_state(conn, state)
        return True

    from backend.db_pool import get_pool

    async with get_pool().acquire() as owned_conn:
        state = await _subscription_state_from_event(event, conn=owned_conn)
        if state is None:
            return False
        await _upsert_subscription_state(owned_conn, state)
    return True


async def _subscription_state_from_event(
    event: StripeWebhookEvent,
    *,
    conn: Any | None = None,
) -> StripeSubscriptionState | None:
    obj = event.data_object
    subscription_id = _stripe_id(obj.get("id"))
    customer_id = _stripe_id(obj.get("customer"))
    status = str(obj.get("status") or "").strip()
    if not subscription_id or not customer_id or not status:
        logger.warning(
            "stripe_subscription_sync_skipped event_id=%s reason=missing_required_id",
            event.event_id,
        )
        return None

    tenant_id = _metadata_value(obj, "tenant_id")
    if not tenant_id and conn is not None:
        tenant_id = await _lookup_tenant_for_subscription(
            conn,
            subscription_id=subscription_id,
            customer_id=customer_id,
        )
    if not tenant_id:
        logger.warning(
            "stripe_subscription_sync_skipped event_id=%s subscription=%s "
            "reason=missing_tenant_id",
            event.event_id,
            subscription_id,
        )
        return None

    return StripeSubscriptionState(
        tenant_id=tenant_id,
        stripe_customer_id=customer_id,
        stripe_subscription_id=subscription_id,
        stripe_price_id=_subscription_price_id(obj),
        status=status,
        current_period_end=_number_or_none(obj.get("current_period_end")),
        cancel_at_period_end=bool(obj.get("cancel_at_period_end") or False),
    )


async def _lookup_tenant_for_subscription(
    conn: Any,
    *,
    subscription_id: str,
    customer_id: str,
) -> str:
    row = await conn.fetchrow(
        "SELECT tenant_id FROM provisioned_billing "
        "WHERE provider = $1 "
        "AND (stripe_subscription_id = $2 OR stripe_customer_id = $3) "
        "LIMIT 1",
        STRIPE_PROVIDER,
        subscription_id,
        customer_id,
    )
    if not row:
        return ""
    return str(row["tenant_id"] or "").strip()


async def _upsert_subscription_state(
    conn: Any,
    state: StripeSubscriptionState,
) -> None:
    now = time.time()
    await conn.execute(
        "INSERT INTO provisioned_billing "
        "(tenant_id, provider, stripe_customer_id, stripe_subscription_id, "
        "stripe_price_id, status, current_period_end, cancel_at_period_end, "
        "created_at, updated_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) "
        "ON CONFLICT (tenant_id, provider) DO UPDATE SET "
        "  stripe_customer_id = EXCLUDED.stripe_customer_id, "
        "  stripe_subscription_id = EXCLUDED.stripe_subscription_id, "
        "  stripe_price_id = CASE "
        "    WHEN EXCLUDED.stripe_price_id = '' "
        "    THEN provisioned_billing.stripe_price_id "
        "    ELSE EXCLUDED.stripe_price_id "
        "  END, "
        "  status = EXCLUDED.status, "
        "  current_period_end = EXCLUDED.current_period_end, "
        "  cancel_at_period_end = EXCLUDED.cancel_at_period_end, "
        "  updated_at = EXCLUDED.updated_at",
        state.tenant_id,
        STRIPE_PROVIDER,
        state.stripe_customer_id,
        state.stripe_subscription_id,
        state.stripe_price_id,
        state.status,
        state.current_period_end,
        state.cancel_at_period_end,
        now,
        now,
    )


def _metadata_value(obj: dict[str, Any], key: str) -> str:
    metadata = obj.get("metadata")
    if not isinstance(metadata, dict):
        return ""
    return str(metadata.get(key) or "").strip()


def _stripe_id(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("id") or "").strip()
    return str(value or "").strip()


def _subscription_price_id(obj: dict[str, Any]) -> str:
    items = obj.get("items")
    data = items.get("data") if isinstance(items, dict) else None
    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict):
            price = first.get("price")
            if isinstance(price, dict):
                price_id = _stripe_id(price)
                if price_id:
                    return price_id
    plan = obj.get("plan")
    if isinstance(plan, dict):
        return _stripe_id(plan)
    return ""


def _number_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_signature_header(header: str) -> tuple[int, list[str]]:
    values: dict[str, list[str]] = {}
    for item in header.split(","):
        key, sep, value = item.partition("=")
        if not sep:
            continue
        values.setdefault(key.strip(), []).append(value.strip())

    try:
        timestamp = int((values.get("t") or [""])[0])
    except ValueError as exc:
        raise StripeWebhookSignatureError("Invalid Stripe webhook timestamp") from exc
    signatures = [sig for sig in values.get("v1", []) if sig]
    if not timestamp or not signatures:
        raise StripeWebhookSignatureError("Invalid Stripe webhook signature header")
    return timestamp, signatures


__all__ = [
    "STRIPE_PROVIDER",
    "STRIPE_SUBSCRIPTION_EVENTS",
    "StripeSubscriptionState",
    "STRIPE_WEBHOOK_TOLERANCE_S",
    "StripeWebhookConfigError",
    "StripeWebhookEvent",
    "StripeWebhookSignatureError",
    "parse_stripe_webhook_event",
    "sync_stripe_subscription_state",
    "verify_stripe_webhook_signature",
]
