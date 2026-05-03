"""FS.5.4 -- Tests for background job retry + DLQ behavior."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from backend.background_jobs import (
    BackgroundJobAdapter,
    BackgroundJobDeadLetterEntry,
    BackgroundJobError,
    BackgroundJobRateLimitError,
    BackgroundJobRequest,
    BackgroundJobResult,
    BackgroundJobRetryPolicy,
    CronDescriptor,
    InMemoryBackgroundJobDeadLetterQueue,
    InvalidBackgroundJobTokenError,
    MissingBackgroundJobScopeError,
    background_job_retry_delay,
    dispatch_background_job_with_retry,
    is_retryable_background_job_error,
)


class _ScriptedAdapter(BackgroundJobAdapter):
    provider = "scripted"

    def _configure(self, **kwargs: Any) -> None:
        self.events = list(kwargs.get("events", ()))
        self.calls = 0

    async def dispatch_job(self, request: BackgroundJobRequest, **kwargs: Any):
        self.calls += 1
        event = self.events.pop(0)
        if isinstance(event, BaseException):
            raise event
        return event

    def cron_descriptor(self, request: BackgroundJobRequest) -> CronDescriptor:
        return CronDescriptor(
            provider=self.provider,
            name=request.name,
            schedule=request.cron or "",
            target=request.name,
        )


def _request() -> BackgroundJobRequest:
    return BackgroundJobRequest(
        name="tenant-quota-sweep",
        payload={"tenant_id": "t1"},
        idempotency_key="tenant-quota-t1",
        cron="*/5 * * * *",
    )


class TestBackgroundJobRetryPolicy:

    def test_retry_delay_uses_exponential_backoff(self):
        policy = BackgroundJobRetryPolicy(
            max_attempts=3,
            base_delay_seconds=2.0,
            max_delay_seconds=5.0,
        )

        assert background_job_retry_delay(0, policy) == 2.0
        assert background_job_retry_delay(1, policy) == 4.0
        assert background_job_retry_delay(2, policy) == 5.0

    def test_retry_delay_honors_rate_limit_retry_after(self):
        policy = BackgroundJobRetryPolicy(max_delay_seconds=10.0)

        assert background_job_retry_delay(0, policy, retry_after=7) == 7.0
        assert background_job_retry_delay(0, policy, retry_after=99) == 10.0

    @pytest.mark.parametrize(
        "kwargs,match",
        [
            ({"max_attempts": 0}, "max_attempts"),
            ({"base_delay_seconds": -1}, "delay seconds"),
            (
                {"base_delay_seconds": 5, "max_delay_seconds": 1},
                "base_delay_seconds",
            ),
        ],
    )
    def test_policy_validates_bounds(self, kwargs, match):
        with pytest.raises(ValueError, match=match):
            BackgroundJobRetryPolicy(**kwargs)


class TestBackgroundJobRetryClassification:

    def test_auth_and_scope_errors_are_not_retryable(self):
        assert (
            is_retryable_background_job_error(
                InvalidBackgroundJobTokenError("bad", status=401),
            )
            is False
        )
        assert (
            is_retryable_background_job_error(
                MissingBackgroundJobScopeError("scope", status=403),
            )
            is False
        )

    def test_rate_limit_and_generic_errors_are_retryable(self):
        assert (
            is_retryable_background_job_error(
                BackgroundJobRateLimitError("slow", retry_after=3, status=429),
            )
            is True
        )
        assert (
            is_retryable_background_job_error(BackgroundJobError("boom", status=500))
            is True
        )


class TestBackgroundJobDispatchWithRetry:

    async def test_retries_retryable_failure_then_returns_result(self):
        result = BackgroundJobResult(
            provider="scripted",
            job_id="job_123",
            status="queued",
        )
        adapter = _ScriptedAdapter.from_plaintext_token(
            "token",
            events=[BackgroundJobError("temporary", status=503), result],
        )
        sleeps: list[float] = []

        async def _sleep(delay: float) -> None:
            sleeps.append(delay)

        observed = await dispatch_background_job_with_retry(
            adapter,
            _request(),
            policy=BackgroundJobRetryPolicy(
                max_attempts=3,
                base_delay_seconds=2.0,
                max_delay_seconds=10.0,
            ),
            sleep=_sleep,
        )

        assert observed is result
        assert adapter.calls == 2
        assert sleeps == [2.0]

    async def test_rate_limit_retry_uses_retry_after_delay(self):
        result = BackgroundJobResult(provider="scripted", job_id="job_456")
        adapter = _ScriptedAdapter.from_plaintext_token(
            "token",
            events=[
                BackgroundJobRateLimitError(
                    "slow",
                    retry_after=7,
                    status=429,
                    provider="scripted",
                ),
                result,
            ],
        )
        sleeps: list[float] = []

        async def _sleep(delay: float) -> None:
            sleeps.append(delay)

        await dispatch_background_job_with_retry(
            adapter,
            _request(),
            policy=BackgroundJobRetryPolicy(max_attempts=2),
            sleep=_sleep,
        )

        assert sleeps == [7.0]

    async def test_exhausted_retryable_failure_goes_to_dlq(self):
        dlq = InMemoryBackgroundJobDeadLetterQueue()
        adapter = _ScriptedAdapter.from_plaintext_token(
            "token",
            events=[
                BackgroundJobError("still down", status=503, provider="scripted"),
                BackgroundJobError("still down", status=503, provider="scripted"),
            ],
        )

        with pytest.raises(BackgroundJobError, match="still down"):
            await dispatch_background_job_with_retry(
                adapter,
                _request(),
                policy=BackgroundJobRetryPolicy(max_attempts=2, base_delay_seconds=0),
                dlq=dlq,
                sleep=lambda _: _noop_sleep(),
                metadata={"source": "cron"},
            )

        entries = await dlq.list_entries()
        assert len(entries) == 1
        entry = entries[0]
        assert isinstance(entry, BackgroundJobDeadLetterEntry)
        assert entry.provider == "scripted"
        assert entry.job_name == "tenant-quota-sweep"
        assert entry.attempts_made == 2
        assert entry.status == 503
        assert entry.retryable is True
        assert entry.request["idempotency_key"] == "tenant-quota-t1"
        assert entry.metadata == {"source": "cron"}

    async def test_non_retryable_failure_goes_to_dlq_without_retry(self):
        dlq = InMemoryBackgroundJobDeadLetterQueue()
        adapter = _ScriptedAdapter.from_plaintext_token(
            "token",
            events=[InvalidBackgroundJobTokenError("bad token", status=401)],
        )

        with pytest.raises(InvalidBackgroundJobTokenError):
            await dispatch_background_job_with_retry(
                adapter,
                _request(),
                dlq=dlq,
            )

        entries = await dlq.list_entries()
        assert adapter.calls == 1
        assert len(entries) == 1
        assert entries[0].error_type == "InvalidBackgroundJobTokenError"
        assert entries[0].retryable is False

    async def test_in_memory_dlq_remove(self):
        dlq = InMemoryBackgroundJobDeadLetterQueue()
        entry = BackgroundJobDeadLetterEntry(
            entry_id="bgdlq_test",
            provider="scripted",
            job_name="x",
            request={"name": "x"},
            attempts_made=1,
            error_type="BackgroundJobError",
            error_message="boom",
            status=500,
            retryable=True,
            created_at=datetime.now(timezone.utc),
        )
        await dlq.deposit(entry)

        assert len(dlq) == 1
        assert await dlq.remove("bgdlq_test") is True
        assert await dlq.remove("bgdlq_test") is False
        assert len(dlq) == 0


async def _noop_sleep() -> None:
    return None
