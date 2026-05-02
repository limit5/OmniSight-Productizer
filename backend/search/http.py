"""FS.6.1 -- Shared HTTP error mapper for hosted search adapters."""

from __future__ import annotations

import httpx

from backend.search.base import (
    InvalidSearchTokenError,
    MissingSearchScopeError,
    SearchAdapterConflictError,
    SearchAdapterError,
    SearchAdapterRateLimitError,
    SearchIndexNotFoundError,
)


def _message(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return resp.text or resp.reason_phrase
    for key in ("message", "error", "detail"):
        if data.get(key):
            return str(data[key])
    return resp.reason_phrase


def raise_for_search_response(resp: httpx.Response, provider: str) -> None:
    """Map provider HTTP failures into the shared FS.6.1 error hierarchy."""
    if 200 <= resp.status_code < 300:
        return
    message = _message(resp)
    status = resp.status_code
    if status == 401:
        raise InvalidSearchTokenError(message, status=status, provider=provider)
    if status == 403:
        raise MissingSearchScopeError(message, status=status, provider=provider)
    if status == 404:
        raise SearchIndexNotFoundError(message, status=status, provider=provider)
    if status in (409, 422):
        raise SearchAdapterConflictError(message, status=status, provider=provider)
    if status == 429:
        retry_after = int(resp.headers.get("Retry-After") or 60)
        raise SearchAdapterRateLimitError(
            message,
            retry_after=retry_after,
            status=status,
            provider=provider,
        )
    raise SearchAdapterError(message, status=status, provider=provider)


__all__ = ["raise_for_search_response"]
