"""AB.7 — Rate limiter, retry policy, DLQ, workspace partitioning tests.

Locks:
  - classify_error: 2xx → success, 429/529 → rate_limited, 4xx →
    non_retryable, 5xx + network exceptions → retryable, default
    safe-to-retry on ambiguity
  - parse_retry_after: integer seconds, HTTP-date, garbage → None,
    negative → 0
  - compute_backoff: exponential cap, jitter range, retry_after
    honoured + capped to max_delay (defends against malicious server)
  - RetryPolicy validation: negative max_retries / negative delays /
    base > max all rejected
  - RateLimitTracker: per-(workspace, model) windows isolated, sliding
    eviction, would_exceed predicts rpm/input_tpm/output_tpm boundaries,
    unknown model degrades to no-op
  - WorkspaceConfig: api_key never appears in repr
  - RetryableExecutor: success on first attempt, retry then succeed,
    max retries → DLQ + raise, non_retryable → DLQ immediately, rate-
    limited honours retry_after, tracker pre-flight delays, DLQ entry
    metadata complete
  - DLQ: deposit, list, remove, since-filter

ADR: docs/operations/anthropic-api-migration-and-batch-mode.md §6.2 + §7
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

from backend.agents.rate_limiter import (
    DLQEntry,
    DeadLetterQueue,
    InMemoryDeadLetterQueue,
    ModelRateLimit,
    RateLimitTracker,
    RetryDecision,
    RetryPolicy,
    RetryableExecutor,
    TIER_4_LIMITS,
    WorkspaceConfig,
    classify_error,
    compute_backoff,
    get_rate_limit,
    parse_retry_after,
)


# ─── classify_error ───────────────────────────────────────────────


def test_classify_2xx_success():
    assert classify_error(status_code=200) == "success"
    assert classify_error(status_code=204) == "success"


def test_classify_429_530_rate_limited():
    assert classify_error(status_code=429) == "rate_limited"
    assert classify_error(status_code=529) == "rate_limited"


def test_classify_4xx_non_retryable():
    assert classify_error(status_code=400) == "non_retryable"
    assert classify_error(status_code=401) == "non_retryable"
    assert classify_error(status_code=403) == "non_retryable"
    assert classify_error(status_code=404) == "non_retryable"
    assert classify_error(status_code=422) == "non_retryable"


def test_classify_5xx_retryable():
    assert classify_error(status_code=500) == "retryable"
    assert classify_error(status_code=502) == "retryable"
    assert classify_error(status_code=503) == "retryable"


def test_classify_network_exception_retryable():
    assert classify_error(exception=ConnectionError("dns fail")) == "retryable"
    assert classify_error(exception=TimeoutError("slow")) == "retryable"
    assert classify_error(exception=asyncio.TimeoutError()) == "retryable"
    assert classify_error(exception=OSError("network")) == "retryable"


def test_classify_unknown_exception_defaults_retryable():
    """Default to retryable on ambiguity — safer than silently DLQing transient blip."""
    class WeirdError(Exception):
        pass

    assert classify_error(exception=WeirdError("???")) == "retryable"


def test_classify_status_code_takes_precedence_over_exception():
    """When both given, status_code wins (more specific)."""
    assert classify_error(status_code=400, exception=ConnectionError()) == "non_retryable"


# ─── parse_retry_after ────────────────────────────────────────────


def test_parse_retry_after_integer_seconds():
    assert parse_retry_after("30") == 30.0
    assert parse_retry_after("0") == 0.0
    assert parse_retry_after("3600") == 3600.0


def test_parse_retry_after_float_seconds():
    assert parse_retry_after("12.5") == 12.5


def test_parse_retry_after_http_date_future():
    future = datetime.now(timezone.utc) + timedelta(seconds=60)
    formatted = format_datetime(future)
    parsed = parse_retry_after(formatted)
    assert parsed is not None
    assert 55 < parsed < 65  # within ±5s tolerance for test runtime


def test_parse_retry_after_http_date_past_returns_zero():
    past = datetime.now(timezone.utc) - timedelta(seconds=60)
    formatted = format_datetime(past)
    parsed = parse_retry_after(formatted)
    assert parsed == 0.0


def test_parse_retry_after_garbage_returns_none():
    assert parse_retry_after("not a date or seconds") is None


def test_parse_retry_after_empty_or_none():
    assert parse_retry_after(None) is None
    assert parse_retry_after("") is None
    assert parse_retry_after("   ") is None


# ─── compute_backoff ──────────────────────────────────────────────


def test_backoff_exponential_no_jitter():
    p = RetryPolicy(max_retries=5, base_delay_seconds=1.0, max_delay_seconds=60.0, jitter=False)
    assert compute_backoff(0, p) == 1.0
    assert compute_backoff(1, p) == 2.0
    assert compute_backoff(2, p) == 4.0
    assert compute_backoff(3, p) == 8.0


def test_backoff_capped_at_max():
    p = RetryPolicy(max_retries=10, base_delay_seconds=1.0, max_delay_seconds=10.0, jitter=False)
    # 2^4 = 16, capped to 10
    assert compute_backoff(4, p) == 10.0
    assert compute_backoff(8, p) == 10.0  # well past, still 10


def test_backoff_with_jitter_within_range():
    p = RetryPolicy(max_retries=5, base_delay_seconds=2.0, max_delay_seconds=60.0, jitter=True)
    rng_calls = [0.0, 0.5, 1.0]
    delays = [
        compute_backoff(2, p, rng=lambda v=v: v) for v in rng_calls
    ]
    # base*2^2 = 8; full-jitter scales by [0,1]
    assert delays == [0.0, 4.0, 8.0]


def test_backoff_retry_after_honoured():
    p = RetryPolicy(max_retries=5, max_delay_seconds=60.0)
    # retry_after takes precedence over exponential math
    assert compute_backoff(0, p, retry_after=30.0) == 30.0
    assert compute_backoff(5, p, retry_after=15.0) == 15.0


def test_backoff_retry_after_capped_to_max():
    """Server returning Retry-After: 86400 must not park us all day."""
    p = RetryPolicy(max_retries=5, max_delay_seconds=60.0)
    assert compute_backoff(0, p, retry_after=86400.0) == 60.0


# ─── RetryPolicy validation ───────────────────────────────────────


def test_retry_policy_negative_max_retries_rejected():
    with pytest.raises(ValueError, match="max_retries must be"):
        RetryPolicy(max_retries=-1)


def test_retry_policy_negative_delay_rejected():
    with pytest.raises(ValueError, match="delay seconds"):
        RetryPolicy(base_delay_seconds=-1)
    with pytest.raises(ValueError, match="delay seconds"):
        RetryPolicy(max_delay_seconds=-5)


def test_retry_policy_base_over_max_rejected():
    with pytest.raises(ValueError, match="base_delay_seconds cannot exceed"):
        RetryPolicy(base_delay_seconds=100.0, max_delay_seconds=10.0)


# ─── Tier 4 limits / get_rate_limit ───────────────────────────────


def test_tier_4_limits_have_4_models():
    assert "claude-opus-4-7" in TIER_4_LIMITS
    assert "claude-sonnet-4-6" in TIER_4_LIMITS
    assert "claude-haiku-4-5-20251001" in TIER_4_LIMITS


def test_tier_4_opus_limits():
    limit = get_rate_limit("claude-opus-4-7")
    assert limit is not None
    assert limit.rpm == 4000
    assert limit.input_tpm == 8_000_000
    assert limit.output_tpm == 1_000_000


def test_tier_4_unknown_model_returns_none():
    assert get_rate_limit("claude-bogus-99") is None


# ─── WorkspaceConfig ──────────────────────────────────────────────


def test_workspace_config_redacts_api_key_in_repr():
    cfg = WorkspaceConfig(kind="production", api_key="sk-ant-VERY-SECRET-KEY")
    repr_str = repr(cfg)
    assert "VERY-SECRET-KEY" not in repr_str
    assert "redacted" in repr_str.lower()


# ─── RateLimitTracker ─────────────────────────────────────────────


def test_tracker_window_sliding_eviction():
    # Fake monotonic clock we can advance.
    fake_t = [1000.0]

    def clock():
        return fake_t[0]

    tracker = RateLimitTracker(clock=clock, window_seconds=60.0)
    tracker.record(workspace="dev", model="claude-sonnet-4-6", input_tokens=100)
    fake_t[0] = 1010.0  # 10s later
    tracker.record(workspace="dev", model="claude-sonnet-4-6", input_tokens=200)

    req, in_tok, out_tok = tracker.current_usage(
        workspace="dev", model="claude-sonnet-4-6"
    )
    assert req == 2
    assert in_tok == 300

    # Advance past the window enough to evict ONLY the first event.
    # First event at t=1000, second at t=1010, window=60s.
    # ts=1065 → cutoff=1005 → evicts t=1000, keeps t=1010.
    fake_t[0] = 1065.0
    req, in_tok, _ = tracker.current_usage(
        workspace="dev", model="claude-sonnet-4-6"
    )
    assert req == 1
    assert in_tok == 200

    # Advance further: at ts=1075, cutoff=1015 → both events evicted.
    fake_t[0] = 1075.0
    req, in_tok, _ = tracker.current_usage(
        workspace="dev", model="claude-sonnet-4-6"
    )
    assert req == 0
    assert in_tok == 0


def test_tracker_isolates_workspaces():
    tracker = RateLimitTracker()
    tracker.record(workspace="dev", model="claude-sonnet-4-6")
    tracker.record(workspace="dev", model="claude-sonnet-4-6")
    tracker.record(workspace="production", model="claude-sonnet-4-6")
    dev_req, _, _ = tracker.current_usage(workspace="dev", model="claude-sonnet-4-6")
    prod_req, _, _ = tracker.current_usage(
        workspace="production", model="claude-sonnet-4-6"
    )
    assert dev_req == 2
    assert prod_req == 1


def test_tracker_isolates_models():
    tracker = RateLimitTracker()
    tracker.record(workspace="dev", model="claude-sonnet-4-6")
    tracker.record(workspace="dev", model="claude-opus-4-7")
    sonnet_req, _, _ = tracker.current_usage(workspace="dev", model="claude-sonnet-4-6")
    opus_req, _, _ = tracker.current_usage(workspace="dev", model="claude-opus-4-7")
    assert sonnet_req == 1
    assert opus_req == 1


def test_tracker_would_exceed_rpm():
    tracker = RateLimitTracker()
    # Opus rpm is 4000. Pump 4000 records.
    for _ in range(4000):
        tracker.record(workspace="dev", model="claude-opus-4-7")
    breached, reason = tracker.would_exceed(
        workspace="dev", model="claude-opus-4-7"
    )
    assert breached
    assert "rpm" in reason


def test_tracker_would_exceed_input_tpm():
    tracker = RateLimitTracker()
    # Opus input TPM = 8M. Single big call.
    tracker.record(workspace="dev", model="claude-opus-4-7", input_tokens=7_999_900)
    breached, reason = tracker.would_exceed(
        workspace="dev", model="claude-opus-4-7", input_tokens_estimated=200,
    )
    assert breached
    assert "input_tpm" in reason


def test_tracker_would_exceed_output_tpm():
    tracker = RateLimitTracker()
    tracker.record(workspace="dev", model="claude-opus-4-7", output_tokens=999_900)
    breached, reason = tracker.would_exceed(
        workspace="dev", model="claude-opus-4-7", output_tokens_estimated=200,
    )
    assert breached
    assert "output_tpm" in reason


def test_tracker_would_exceed_unknown_model_no_breach():
    """Unknown model = no quota tracking = never breach."""
    tracker = RateLimitTracker()
    breached, reason = tracker.would_exceed(workspace="dev", model="unknown-model")
    assert not breached
    assert reason == ""


def test_tracker_within_limits():
    tracker = RateLimitTracker()
    tracker.record(workspace="dev", model="claude-sonnet-4-6", input_tokens=1000)
    breached, _ = tracker.would_exceed(
        workspace="dev", model="claude-sonnet-4-6",
        input_tokens_estimated=1000, output_tokens_estimated=500,
    )
    assert not breached


# ─── DeadLetterQueue ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dlq_deposit_and_list():
    dlq = InMemoryDeadLetterQueue()
    entry = DLQEntry(
        entry_id="dlq_x", workspace="dev", model="claude-sonnet-4-6",
        classification="non_retryable", attempts_made=1,
        last_status_code=400, last_exception_repr="ValueError(...)",
        last_reason="bad request",
        request_metadata={"task_id": "t1"},
        created_at=datetime.now(timezone.utc),
    )
    await dlq.deposit(entry)
    items = await dlq.list_entries()
    assert len(items) == 1
    assert items[0].entry_id == "dlq_x"


@pytest.mark.asyncio
async def test_dlq_remove():
    dlq = InMemoryDeadLetterQueue()
    entry = DLQEntry(
        entry_id="dlq_y", workspace="dev", model="x", classification="retryable",
        attempts_made=5, last_status_code=500, last_exception_repr=None,
        last_reason="server error", request_metadata={},
        created_at=datetime.now(timezone.utc),
    )
    await dlq.deposit(entry)
    assert await dlq.remove("dlq_y") is True
    assert await dlq.remove("dlq_y") is False  # already gone
    assert len(await dlq.list_entries()) == 0


@pytest.mark.asyncio
async def test_dlq_list_since_filter():
    dlq = InMemoryDeadLetterQueue()
    early = DLQEntry(
        entry_id="early", workspace="x", model="x", classification="retryable",
        attempts_made=1, last_status_code=None, last_exception_repr=None,
        last_reason="", request_metadata={},
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    late = DLQEntry(
        entry_id="late", workspace="x", model="x", classification="retryable",
        attempts_made=1, last_status_code=None, last_exception_repr=None,
        last_reason="", request_metadata={},
        created_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    await dlq.deposit(early)
    await dlq.deposit(late)
    cutoff = datetime(2026, 3, 1, tzinfo=timezone.utc)
    items = await dlq.list_entries(since=cutoff)
    assert {e.entry_id for e in items} == {"late"}


# ─── RetryableExecutor ────────────────────────────────────────────


@pytest.fixture
def fake_sleep():
    """Sleep stub: tracks calls without real time."""
    calls: list[float] = []

    async def _sleep(seconds: float) -> None:
        calls.append(seconds)
        await asyncio.sleep(0)

    _sleep.calls = calls  # type: ignore[attr-defined]
    return _sleep


@pytest.mark.asyncio
async def test_executor_success_on_first_attempt(fake_sleep):
    executor = RetryableExecutor(sleep=fake_sleep)

    async def call():
        return "ok"

    result = await executor.execute(
        call, workspace="dev", model="claude-sonnet-4-6"
    )
    assert result == "ok"
    # Tracker recorded the success.
    req, _, _ = executor.tracker.current_usage(
        workspace="dev", model="claude-sonnet-4-6"
    )
    assert req == 1
    # No retries → no sleep calls.
    assert fake_sleep.calls == []


@pytest.mark.asyncio
async def test_executor_retries_then_succeeds(fake_sleep):
    executor = RetryableExecutor(
        policy=RetryPolicy(max_retries=3, base_delay_seconds=0.001,
                           max_delay_seconds=10.0, jitter=False),
        sleep=fake_sleep,
    )
    attempts = {"n": 0}

    class FlakyError(Exception):
        pass

    async def call():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise FlakyError("transient")
        return "ok"

    result = await executor.execute(
        call, workspace="dev", model="claude-sonnet-4-6",
    )
    assert result == "ok"
    assert attempts["n"] == 3
    assert len(fake_sleep.calls) == 2  # two retries


@pytest.mark.asyncio
async def test_executor_max_retry_dlq(fake_sleep):
    dlq = InMemoryDeadLetterQueue()
    executor = RetryableExecutor(
        policy=RetryPolicy(max_retries=2, base_delay_seconds=0.001,
                           max_delay_seconds=1.0, jitter=False),
        dlq=dlq,
        sleep=fake_sleep,
    )

    class StubbornError(Exception):
        pass

    async def call():
        raise StubbornError("never works")

    with pytest.raises(StubbornError):
        await executor.execute(
            call, workspace="dev", model="claude-sonnet-4-6",
            request_metadata={"task_id": "stuck"},
        )

    items = await dlq.list_entries()
    assert len(items) == 1
    entry = items[0]
    assert entry.workspace == "dev"
    assert entry.attempts_made == 3  # max_retries=2 means 3 total attempts
    assert entry.classification == "retryable"
    assert entry.request_metadata["task_id"] == "stuck"


@pytest.mark.asyncio
async def test_executor_non_retryable_immediate_dlq(fake_sleep):
    """4xx error → no retry, straight to DLQ."""
    dlq = InMemoryDeadLetterQueue()
    executor = RetryableExecutor(
        policy=RetryPolicy(max_retries=5, base_delay_seconds=0.001,
                           max_delay_seconds=1.0, jitter=False),
        dlq=dlq,
        sleep=fake_sleep,
    )

    class AuthError(Exception):
        pass

    async def call():
        raise AuthError("invalid api key")

    with pytest.raises(AuthError):
        await executor.execute(
            call, workspace="dev", model="claude-sonnet-4-6",
            status_extractor=lambda _: 401,
            request_metadata={"task_id": "auth_bad"},
        )

    # No retries → no sleep
    assert fake_sleep.calls == []
    items = await dlq.list_entries()
    assert len(items) == 1
    assert items[0].classification == "non_retryable"
    assert items[0].attempts_made == 1


@pytest.mark.asyncio
async def test_executor_rate_limited_honours_retry_after(fake_sleep):
    """429 with Retry-After header: backoff equals header value."""
    executor = RetryableExecutor(
        policy=RetryPolicy(max_retries=5, base_delay_seconds=0.001,
                           max_delay_seconds=120.0, jitter=False),
        sleep=fake_sleep,
    )
    attempts = {"n": 0}

    class RateLimitError(Exception):
        status_code = 429

    async def call():
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise RateLimitError("slow down")
        return "ok"

    await executor.execute(
        call, workspace="dev", model="claude-sonnet-4-6",
        status_extractor=lambda e: getattr(e, "status_code", None),
        retry_after_extractor=lambda _: "5",
    )
    assert fake_sleep.calls == [5.0]


@pytest.mark.asyncio
async def test_executor_tracker_preflight_delays(fake_sleep):
    """When tracker predicts breach, executor sleeps once before submit."""
    tracker = RateLimitTracker()
    # Pre-saturate Opus rpm.
    for _ in range(4000):
        tracker.record(workspace="dev", model="claude-opus-4-7")

    executor = RetryableExecutor(
        policy=RetryPolicy(max_retries=2, base_delay_seconds=0.5,
                           max_delay_seconds=2.0, jitter=False),
        tracker=tracker,
        sleep=fake_sleep,
    )

    async def call():
        return "after-throttle"

    result = await executor.execute(
        call, workspace="dev", model="claude-opus-4-7",
    )
    assert result == "after-throttle"
    # First entry in fake_sleep.calls is the preflight delay.
    assert len(fake_sleep.calls) >= 1
    assert fake_sleep.calls[0] > 0


@pytest.mark.asyncio
async def test_executor_dlq_entry_fields_complete(fake_sleep):
    dlq = InMemoryDeadLetterQueue()
    executor = RetryableExecutor(
        policy=RetryPolicy(max_retries=0, base_delay_seconds=0.001,
                           max_delay_seconds=0.1, jitter=False),
        dlq=dlq,
        sleep=fake_sleep,
    )

    class FailNow(Exception):
        status_code = 503

    async def call():
        raise FailNow("oops")

    with pytest.raises(FailNow):
        await executor.execute(
            call, workspace="batch", model="claude-haiku-4-5-20251001",
            status_extractor=lambda e: getattr(e, "status_code", None),
            request_metadata={"reason": "smoke test"},
        )

    items = await dlq.list_entries()
    assert len(items) == 1
    e = items[0]
    assert e.workspace == "batch"
    assert e.model == "claude-haiku-4-5-20251001"
    assert e.last_status_code == 503
    assert "FailNow" in (e.last_exception_repr or "")
    assert "oops" in e.last_reason
    assert e.request_metadata == {"reason": "smoke test"}
    assert e.classification == "retryable"


@pytest.mark.asyncio
async def test_executor_records_tracker_only_on_success(fake_sleep):
    """Failed attempts must NOT count toward tracker (no double-charge)."""
    tracker = RateLimitTracker()
    executor = RetryableExecutor(
        policy=RetryPolicy(max_retries=1, base_delay_seconds=0.001,
                           max_delay_seconds=0.1, jitter=False),
        tracker=tracker,
        sleep=fake_sleep,
    )

    class Boom(Exception):
        pass

    async def call():
        raise Boom("burst")

    with pytest.raises(Boom):
        await executor.execute(call, workspace="dev", model="claude-sonnet-4-6")

    req, _, _ = tracker.current_usage(workspace="dev", model="claude-sonnet-4-6")
    assert req == 0  # no successes recorded
