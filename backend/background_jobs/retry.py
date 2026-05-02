"""FS.5.4 -- Failure retry + dead letter queue for background jobs.

This module mirrors the AB.7 retry/DLQ shape in
``backend.agents.rate_limiter`` but keeps the FS.5 provider-neutral
surface small: callers pass an adapter, a ``BackgroundJobRequest``, and
optionally a retry policy / DLQ implementation.

Module-global state audit (per implement_phase_step.md SOP §1)
--------------------------------------------------------------
This module defines immutable dataclasses, protocols, and pure helper
functions only. ``InMemoryBackgroundJobDeadLetterQueue`` is intentionally
per instance for dev/test inspection; production callers that need
cross-worker persistence must inject a PG/Redis-backed queue.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from backend.background_jobs.base import (
    BackgroundJobAdapter,
    BackgroundJobConflictError,
    BackgroundJobError,
    BackgroundJobRateLimitError,
    BackgroundJobRequest,
    BackgroundJobResult,
    InvalidBackgroundJobTokenError,
    MissingBackgroundJobScopeError,
)


@dataclass(frozen=True)
class BackgroundJobRetryPolicy:
    """Bound retry attempts and exponential backoff for FS.5.4 dispatch."""

    max_attempts: int = 3
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 60.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.base_delay_seconds < 0 or self.max_delay_seconds < 0:
            raise ValueError("delay seconds must be >= 0")
        if self.base_delay_seconds > self.max_delay_seconds:
            raise ValueError("base_delay_seconds cannot exceed max_delay_seconds")


@dataclass(frozen=True)
class BackgroundJobDeadLetterEntry:
    """Failed background job dispatch deposited for operator replay."""

    entry_id: str
    provider: str
    job_name: str
    request: dict[str, Any]
    attempts_made: int
    error_type: str
    error_message: str
    status: int
    retryable: bool
    created_at: datetime
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "provider": self.provider,
            "job_name": self.job_name,
            "request": dict(self.request),
            "attempts_made": self.attempts_made,
            "error_type": self.error_type,
            "error_message": self.error_message,
            "status": self.status,
            "retryable": self.retryable,
            "created_at": self.created_at.isoformat(),
            "metadata": dict(self.metadata),
        }


class BackgroundJobDeadLetterQueue(Protocol):
    async def deposit(self, entry: BackgroundJobDeadLetterEntry) -> None: ...
    async def list_entries(self) -> list[BackgroundJobDeadLetterEntry]: ...
    async def remove(self, entry_id: str) -> bool: ...


class InMemoryBackgroundJobDeadLetterQueue:
    """Dev/test DLQ. Production persistence is caller-injected."""

    def __init__(self) -> None:
        self._entries: dict[str, BackgroundJobDeadLetterEntry] = {}

    async def deposit(self, entry: BackgroundJobDeadLetterEntry) -> None:
        self._entries[entry.entry_id] = entry

    async def list_entries(self) -> list[BackgroundJobDeadLetterEntry]:
        return sorted(self._entries.values(), key=lambda e: e.created_at, reverse=True)

    async def remove(self, entry_id: str) -> bool:
        return self._entries.pop(entry_id, None) is not None

    def __len__(self) -> int:
        return len(self._entries)


def background_job_retry_delay(
    attempt_index: int,
    policy: BackgroundJobRetryPolicy,
    *,
    retry_after: int | None = None,
) -> float:
    """Return capped exponential delay for a zero-based retry attempt."""
    if retry_after is not None:
        return min(float(max(0, retry_after)), policy.max_delay_seconds)
    delay = policy.base_delay_seconds * (2 ** attempt_index)
    return min(delay, policy.max_delay_seconds)


def is_retryable_background_job_error(exc: BackgroundJobError) -> bool:
    """Return whether FS.5.4 should retry this adapter failure."""
    if isinstance(
        exc,
        (
            InvalidBackgroundJobTokenError,
            MissingBackgroundJobScopeError,
            BackgroundJobConflictError,
        ),
    ):
        return False
    return True


async def dispatch_background_job_with_retry(
    adapter: BackgroundJobAdapter,
    request: BackgroundJobRequest,
    *,
    policy: BackgroundJobRetryPolicy | None = None,
    dlq: BackgroundJobDeadLetterQueue | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    metadata: dict[str, Any] | None = None,
) -> BackgroundJobResult:
    """Dispatch a job with bounded retry and DLQ deposit on final failure."""
    retry_policy = policy if policy is not None else BackgroundJobRetryPolicy()
    dead_letter_queue = (
        dlq if dlq is not None else InMemoryBackgroundJobDeadLetterQueue()
    )
    last_exc: BackgroundJobError | None = None
    last_retryable = True

    for attempt_index in range(retry_policy.max_attempts):
        try:
            return await adapter.dispatch_job(request)
        except BackgroundJobError as exc:
            last_exc = exc
            last_retryable = is_retryable_background_job_error(exc)
            attempts_made = attempt_index + 1
            if not last_retryable or attempts_made >= retry_policy.max_attempts:
                await _deposit_background_job_dlq(
                    dead_letter_queue,
                    adapter=adapter,
                    request=request,
                    attempts_made=attempts_made,
                    exc=exc,
                    retryable=last_retryable,
                    metadata=metadata or {},
                )
                raise
            retry_after = (
                exc.retry_after if isinstance(exc, BackgroundJobRateLimitError) else None
            )
            await sleep(
                background_job_retry_delay(
                    attempt_index,
                    retry_policy,
                    retry_after=retry_after,
                )
            )

    if last_exc is not None:
        raise last_exc
    raise RuntimeError("background job retry loop exited without dispatch")


async def _deposit_background_job_dlq(
    dlq: BackgroundJobDeadLetterQueue,
    *,
    adapter: BackgroundJobAdapter,
    request: BackgroundJobRequest,
    attempts_made: int,
    exc: BackgroundJobError,
    retryable: bool,
    metadata: dict[str, Any],
) -> None:
    entry = BackgroundJobDeadLetterEntry(
        entry_id=f"bgdlq_{uuid.uuid4().hex[:16]}",
        provider=adapter.provider,
        job_name=request.name,
        request=request.to_dict(),
        attempts_made=attempts_made,
        error_type=type(exc).__name__,
        error_message=str(exc),
        status=exc.status,
        retryable=retryable,
        created_at=datetime.now(timezone.utc),
        metadata=dict(metadata),
    )
    await dlq.deposit(entry)


__all__ = [
    "BackgroundJobDeadLetterEntry",
    "BackgroundJobDeadLetterQueue",
    "BackgroundJobRetryPolicy",
    "InMemoryBackgroundJobDeadLetterQueue",
    "background_job_retry_delay",
    "dispatch_background_job_with_retry",
    "is_retryable_background_job_error",
]
