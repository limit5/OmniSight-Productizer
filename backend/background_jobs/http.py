"""FS.5.1 -- Shared HTTP error mapper for background job adapters."""

from __future__ import annotations

import httpx

from backend.background_jobs.base import (
    BackgroundJobConflictError,
    BackgroundJobError,
    BackgroundJobRateLimitError,
    InvalidBackgroundJobTokenError,
    MissingBackgroundJobScopeError,
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


def raise_for_background_job_response(resp: httpx.Response, provider: str) -> None:
    """Map provider HTTP failures into the shared FS.5.1 error hierarchy."""
    if 200 <= resp.status_code < 300:
        return
    message = _message(resp)
    status = resp.status_code
    if status == 401:
        raise InvalidBackgroundJobTokenError(message, status=status, provider=provider)
    if status == 403:
        raise MissingBackgroundJobScopeError(message, status=status, provider=provider)
    if status in (409, 422):
        raise BackgroundJobConflictError(message, status=status, provider=provider)
    if status == 429:
        retry_after = int(resp.headers.get("Retry-After") or 60)
        raise BackgroundJobRateLimitError(
            message,
            retry_after=retry_after,
            status=status,
            provider=provider,
        )
    raise BackgroundJobError(message, status=status, provider=provider)


__all__ = ["raise_for_background_job_response"]
