"""FS.4.1 -- Shared HTTP helpers for email delivery adapters."""

from __future__ import annotations

from typing import Any

import httpx

from backend.email_delivery.base import (
    EmailDeliveryConflictError,
    EmailDeliveryError,
    EmailDeliveryRateLimitError,
    InvalidEmailDeliveryTokenError,
    MissingEmailDeliveryScopeError,
)


def raise_for_email_response(resp: httpx.Response, provider: str) -> None:
    if resp.status_code < 400:
        return
    try:
        body = resp.json()
    except Exception:
        body = {}
    msg = _message_from_body(body) or resp.text or resp.reason_phrase or "unknown error"
    if resp.status_code == 401:
        raise InvalidEmailDeliveryTokenError(msg, status=401, provider=provider)
    if resp.status_code == 403:
        raise MissingEmailDeliveryScopeError(msg, status=403, provider=provider)
    if resp.status_code in (409, 422):
        raise EmailDeliveryConflictError(msg, status=resp.status_code, provider=provider)
    if resp.status_code == 429:
        retry = int(resp.headers.get("Retry-After", "60"))
        raise EmailDeliveryRateLimitError(
            msg,
            retry_after=retry,
            status=429,
            provider=provider,
        )
    raise EmailDeliveryError(msg, status=resp.status_code, provider=provider)


def _message_from_body(body: dict[str, Any]) -> str:
    for key in ("message", "error", "Message", "ErrorCode"):
        value = body.get(key)
        if value:
            return str(value)
    errors = body.get("errors") or body.get("Errors")
    if isinstance(errors, list) and errors:
        first = errors[0]
        if isinstance(first, dict):
            return str(first.get("message") or first.get("Message") or first)
        return str(first)
    return ""


__all__ = ["raise_for_email_response"]
