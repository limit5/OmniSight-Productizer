"""FS.8.2 -- Stripe webhook verification and event scaffold.

Webhook handling stays deliberately narrow in this module: verify the
Stripe-Signature HMAC, parse the event envelope, and return a normalized
event shape for the router. Subscription persistence and tenant billing
state sync intentionally live in later FS.8 items.

Module-global state audit (per implement_phase_step.md SOP Step 1)
------------------------------------------------------------------
Only immutable constants and pure helpers are defined at module scope.
Every worker verifies each request from the same Settings/env secret and
the raw request body, so no cross-worker mutable state is shared.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass, field
from typing import Any


STRIPE_WEBHOOK_TOLERANCE_S = 300


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
    "STRIPE_WEBHOOK_TOLERANCE_S",
    "StripeWebhookConfigError",
    "StripeWebhookEvent",
    "StripeWebhookSignatureError",
    "parse_stripe_webhook_event",
    "verify_stripe_webhook_signature",
]
