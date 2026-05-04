"""AB.7 — Rate-limit, retry, backoff, dead-letter queue + workspace partitioning.

Wraps Anthropic API calls with production-grade retry logic so transient
failures (429 rate-limit, 529 overloaded, 5xx, network blips) don't
surface as user-visible errors:

  * Exponential backoff with full jitter (AWS pattern). Honours
    server-sent ``retry-after`` (seconds or HTTP-date).
  * 4-class error classification:
      - RETRYABLE         — 5xx, network, transient → retry
      - RATE_LIMITED      — 429, 529 → retry with retry-after
      - NON_RETRYABLE     — 4xx client error → DLQ immediately
      - SUCCESS           — pass through
  * Sliding-window rate tracker per ``(workspace, model)`` keeps the
    RPM/TPM under Tier 4 limits before the server has to refuse.
  * Anthropic Workspace partitioning (dev / batch / production)
    isolates spend + quota so a runaway dev experiment can't burn
    production budget.
  * Dead-letter queue (DLQ) for max-retry exhaustion + non-retryable
    errors; operator can inspect + manually replay.

Integration points (downstream):

  * AB.4 dispatcher: wraps every batch submit / poll / retrieve call
  * AB.6 cost guard: rate-limited submits don't double-charge — cost
    is counted only on success (handled by caller)
  * Z spend anomaly: 429 burst rate is signal worth exposing on the
    Provider Observability dashboard

Out of scope:

  * Persistent DLQ — InMemoryDLQ ships, PG-backed waits for first
    cross-restart use case (AB.4 dispatcher consumer).
  * Per-tenant rate slicing — multi-tenant quota arrives with KS.1
    envelope + Priority I; until then the tracker treats all calls
    as belonging to the configured workspace.

ADR: docs/operations/anthropic-api-migration-and-batch-mode.md §6.2 + §7
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Literal, Protocol

logger = logging.getLogger(__name__)


# ─── Type aliases ────────────────────────────────────────────────


WorkspaceKind = Literal["dev", "batch", "production"]
"""AB.7.5 workspace partitioning. Each workspace has its own API key,
spend cap, and quota tracker — a dev experiment can't burn batch
budget, batch retries can't starve production traffic.
"""

RetryClassification = Literal[
    "retryable",        # 5xx, network errors, generic transient
    "rate_limited",     # 429, 529 — honour retry-after
    "non_retryable",    # 4xx client error (auth, bad request, etc.)
    "success",          # 2xx (caller doesn't usually wrap this)
]


# ─── Tier 4 rate limits (per workspace) ──────────────────────────


@dataclass(frozen=True)
class ModelRateLimit:
    """Anthropic Tier 4 per-model RPM + TPM caps."""

    model: str
    rpm: int
    input_tpm: int
    output_tpm: int


TIER_4_LIMITS: dict[str, ModelRateLimit] = {
    "claude-opus-4-7": ModelRateLimit(
        model="claude-opus-4-7", rpm=4_000, input_tpm=8_000_000, output_tpm=1_000_000,
    ),
    "claude-sonnet-4-6": ModelRateLimit(
        model="claude-sonnet-4-6", rpm=5_000, input_tpm=16_000_000, output_tpm=10_000_000,
    ),
    "claude-haiku-4-5-20251001": ModelRateLimit(
        model="claude-haiku-4-5-20251001", rpm=5_000, input_tpm=32_000_000, output_tpm=20_000_000,
    ),
    # Legacy compat — uses Sonnet's tier
    "claude-sonnet-4-20250514": ModelRateLimit(
        model="claude-sonnet-4-20250514", rpm=5_000, input_tpm=16_000_000, output_tpm=10_000_000,
    ),
}


def get_rate_limit(model: str) -> ModelRateLimit | None:
    """Look up Tier 4 quota for a model. Returns None if unknown so the
    tracker degrades to no-op rather than raising."""
    return TIER_4_LIMITS.get(model)


# ─── Workspace config ────────────────────────────────────────────


@dataclass(frozen=True)
class WorkspaceConfig:
    """Per-workspace API key + tracker scope (AB.7.5)."""

    kind: WorkspaceKind
    api_key: str
    """Stored encrypted via AS Token Vault / KS.1 envelope (see KS ADR)."""

    description: str = ""

    def __repr__(self) -> str:
        # Never print the API key.
        return f"WorkspaceConfig(kind={self.kind!r}, api_key=<redacted>)"


# ─── Retry classification ────────────────────────────────────────


@dataclass(frozen=True)
class RetryDecision:
    """Whether to retry, after how long, and why (audit + alerts)."""

    should_retry: bool
    classification: RetryClassification
    delay_seconds: float = 0.0
    reason: str = ""


def classify_error(
    *,
    status_code: int | None = None,
    exception: BaseException | None = None,
) -> RetryClassification:
    """Map an HTTP status / exception to a retry classification.

    Order of precedence:
      1. Explicit status_code (preferred — more specific)
      2. Exception type heuristics (network errors, timeouts)
      3. Default to 'retryable' on ambiguity (safer to retry once
         than to silently DLQ a transient blip)
    """
    if status_code is not None:
        if 200 <= status_code < 300:
            return "success"
        if status_code in (429, 529):
            return "rate_limited"
        if 500 <= status_code < 600:
            return "retryable"
        if 400 <= status_code < 500:
            return "non_retryable"

    if exception is not None:
        # Network / connection / timeout exceptions are retryable.
        cls_name = type(exception).__name__
        if any(k in cls_name for k in ("Timeout", "Connection", "Network", "OSError")):
            return "retryable"
        # asyncio.TimeoutError + concurrent.futures.TimeoutError
        if isinstance(exception, (asyncio.TimeoutError, TimeoutError)):
            return "retryable"

    return "retryable"


def parse_retry_after(value: str | None) -> float | None:
    """Parse Retry-After header (RFC 7231): integer seconds or HTTP-date.

    Returns delay in seconds, or None if unparsable / negative.
    """
    if not value:
        return None
    raw = value.strip()
    # Pure integer / float seconds
    if re.fullmatch(r"\d+(\.\d+)?", raw):
        try:
            seconds = float(raw)
            return max(0.0, seconds)
        except ValueError:
            return None
    # HTTP-date (RFC 7231 §7.1.1.1)
    try:
        target = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if target is None:
        return None
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    delta = (target - datetime.now(timezone.utc)).total_seconds()
    return max(0.0, delta)


# ─── Backoff computation ─────────────────────────────────────────


@dataclass(frozen=True)
class RetryPolicy:
    """Tuneable retry policy. Defaults match AWS exponential-backoff guidance."""

    max_retries: int = 5
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 60.0
    jitter: bool = True

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        if self.base_delay_seconds < 0 or self.max_delay_seconds < 0:
            raise ValueError("delay seconds must be >= 0")
        if self.base_delay_seconds > self.max_delay_seconds:
            raise ValueError("base_delay_seconds cannot exceed max_delay_seconds")


def compute_backoff(
    attempt: int,
    policy: RetryPolicy,
    *,
    retry_after: float | None = None,
    rng: Callable[[], float] = random.random,
) -> float:
    """Compute next backoff delay in seconds.

    Honours `retry_after` if server provided one (capped to max_delay
    so a malicious server can't park us forever). Otherwise:

        delay = min(max_delay, base * 2^attempt)
        with full-jitter: delay = uniform(0, delay) when policy.jitter
    """
    if retry_after is not None:
        # Cap server-suggested wait too — defends against malicious /
        # buggy server returning Retry-After: 86400.
        return min(retry_after, policy.max_delay_seconds)

    exp_backoff = policy.base_delay_seconds * (2 ** attempt)
    capped = min(exp_backoff, policy.max_delay_seconds)
    if policy.jitter:
        return capped * rng()
    return capped


# ─── Rate limit tracker ──────────────────────────────────────────


@dataclass
class _Window:
    """Sliding window of (timestamp, value) pairs over `window_seconds`."""

    window_seconds: float
    events: deque = field(default_factory=deque)

    def add(self, ts: float, value: int) -> None:
        self.events.append((ts, value))
        self._evict(ts)

    def total(self, ts: float) -> int:
        self._evict(ts)
        return sum(v for _, v in self.events)

    def _evict(self, ts: float) -> None:
        cutoff = ts - self.window_seconds
        while self.events and self.events[0][0] < cutoff:
            self.events.popleft()


class RateLimitTracker:
    """Sliding-window tracker of RPM + input TPM + output TPM per
    (workspace, model) tuple.

    Two modes of use:

      * ``record(...)``: caller logs an actual request after success.
        Hot-path: O(1) amortised.
      * ``would_exceed(...)``: caller predicts whether the *next* call
        with given token estimate would breach the cap. Used by AB.4
        dispatcher to throttle BEFORE submitting.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        window_seconds: float = 60.0,
    ) -> None:
        self._clock = clock
        self._window_seconds = window_seconds
        # key = (workspace, model); per-key three windows: rpm, in_tpm, out_tpm
        self._req_windows: dict[tuple[str, str], _Window] = {}
        self._in_token_windows: dict[tuple[str, str], _Window] = {}
        self._out_token_windows: dict[tuple[str, str], _Window] = {}

    def _windows_for(
        self, workspace: str, model: str
    ) -> tuple[_Window, _Window, _Window]:
        key = (workspace, model)
        return (
            self._req_windows.setdefault(key, _Window(self._window_seconds)),
            self._in_token_windows.setdefault(key, _Window(self._window_seconds)),
            self._out_token_windows.setdefault(key, _Window(self._window_seconds)),
        )

    def record(
        self,
        *,
        workspace: str,
        model: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        ts = self._clock()
        req_win, in_win, out_win = self._windows_for(workspace, model)
        req_win.add(ts, 1)
        if input_tokens:
            in_win.add(ts, input_tokens)
        if output_tokens:
            out_win.add(ts, output_tokens)

    def current_usage(
        self, *, workspace: str, model: str
    ) -> tuple[int, int, int]:
        """Return (requests, input_tokens, output_tokens) in current window."""
        ts = self._clock()
        req_win, in_win, out_win = self._windows_for(workspace, model)
        return (req_win.total(ts), in_win.total(ts), out_win.total(ts))

    def would_exceed(
        self,
        *,
        workspace: str,
        model: str,
        input_tokens_estimated: int = 0,
        output_tokens_estimated: int = 0,
    ) -> tuple[bool, str]:
        """Predict if the *next* call would breach Tier 4 caps.

        Returns ``(would_exceed, reason)``. ``reason`` is empty when no
        breach predicted; otherwise names which limit (rpm / input_tpm /
        output_tpm) and the current vs cap numbers.
        """
        limit = get_rate_limit(model)
        if limit is None:
            return (False, "")
        req, in_tok, out_tok = self.current_usage(workspace=workspace, model=model)
        if req + 1 > limit.rpm:
            return (True, f"rpm: {req + 1} > {limit.rpm}")
        if in_tok + input_tokens_estimated > limit.input_tpm:
            return (
                True,
                f"input_tpm: {in_tok + input_tokens_estimated} > {limit.input_tpm}",
            )
        if out_tok + output_tokens_estimated > limit.output_tpm:
            return (
                True,
                f"output_tpm: {out_tok + output_tokens_estimated} > {limit.output_tpm}",
            )
        return (False, "")


# ─── Dead Letter Queue (DLQ) ─────────────────────────────────────


@dataclass(frozen=True)
class DLQEntry:
    """Failed call deposited for operator inspection / manual replay."""

    entry_id: str
    workspace: str
    model: str
    classification: RetryClassification
    attempts_made: int
    last_status_code: int | None
    last_exception_repr: str | None
    last_reason: str
    request_metadata: dict[str, Any]
    created_at: datetime


class DeadLetterQueue(Protocol):
    async def deposit(self, entry: DLQEntry) -> None: ...
    async def list_entries(
        self, *, since: datetime | None = None
    ) -> list[DLQEntry]: ...
    async def remove(self, entry_id: str) -> bool: ...


class InMemoryDeadLetterQueue:
    """Dev / test impl. PG-backed waits for AB.4 dispatcher cross-restart."""

    def __init__(self) -> None:
        self._entries: dict[str, DLQEntry] = {}

    async def deposit(self, entry: DLQEntry) -> None:
        self._entries[entry.entry_id] = entry

    async def list_entries(
        self, *, since: datetime | None = None
    ) -> list[DLQEntry]:
        items = list(self._entries.values())
        if since:
            items = [e for e in items if e.created_at >= since]
        return sorted(items, key=lambda e: e.created_at, reverse=True)

    async def remove(self, entry_id: str) -> bool:
        return self._entries.pop(entry_id, None) is not None

    def __len__(self) -> int:
        return len(self._entries)


# ─── Retryable executor ──────────────────────────────────────────


CallableAsync = Callable[[], Awaitable[Any]]
"""The thing we wrap — a zero-arg async closure that performs the
actual API call. The caller closes over their request data, so the
executor doesn't need to know the shape."""


class RetryableExecutor:
    """Execute an async API call with retries + DLQ.

    Caller provides:
      - ``call_factory``: zero-arg coroutine factory; called once per attempt
      - ``status_extractor``: optional fn that extracts HTTP status from
        an exception (Anthropic SDK puts it on .status_code attribute)
      - ``retry_after_extractor``: optional fn that extracts the
        Retry-After header value from an exception (None → use backoff)

    Behaviour:
      - Attempt 1..max_retries+1 with exponential backoff + jitter
      - Honours `retry-after` from rate-limit responses
      - Records to RateLimitTracker on success
      - Deposits to DLQ on max-retry exhaustion or non_retryable error
    """

    def __init__(
        self,
        *,
        policy: RetryPolicy | None = None,
        tracker: RateLimitTracker | None = None,
        dlq: DeadLetterQueue | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        # Use ``is None`` checks rather than ``or``: an empty
        # InMemoryDeadLetterQueue is falsy via ``__len__`` and would
        # silently get replaced with a fresh one, dropping the caller's
        # reference (and its inspection ability).
        self.policy = policy if policy is not None else RetryPolicy()
        self.tracker = tracker if tracker is not None else RateLimitTracker()
        self.dlq: DeadLetterQueue = (
            dlq if dlq is not None else InMemoryDeadLetterQueue()
        )
        self._sleep = sleep

    async def execute(
        self,
        call_factory: CallableAsync,
        *,
        workspace: str,
        model: str,
        input_tokens_estimated: int = 0,
        output_tokens_estimated: int = 0,
        request_metadata: dict[str, Any] | None = None,
        status_extractor: Callable[[BaseException], int | None] = lambda _: None,
        retry_after_extractor: Callable[[BaseException], str | None] = lambda _: None,
    ) -> Any:
        """Run with retries. Returns the call result on success.

        On final failure: deposits DLQ entry + re-raises the last
        exception so the caller can choose to handle / propagate.
        """
        last_exception: BaseException | None = None
        last_classification: RetryClassification = "retryable"
        last_status_code: int | None = None
        last_reason: str = ""

        # Pre-flight: tracker check.
        breached, reason = self.tracker.would_exceed(
            workspace=workspace, model=model,
            input_tokens_estimated=input_tokens_estimated,
            output_tokens_estimated=output_tokens_estimated,
        )
        if breached:
            # Treat predicted breach as a synthetic 429 — backoff + retry.
            logger.info(
                "RateLimitTracker preflight: workspace=%s model=%s would breach (%s); "
                "delaying before submit",
                workspace, model, reason,
            )
            await self._sleep(
                compute_backoff(0, self.policy, retry_after=None)
            )

        for attempt in range(self.policy.max_retries + 1):
            try:
                result = await call_factory()
            except BaseException as e:  # noqa: BLE001
                last_exception = e
                last_status_code = status_extractor(e)
                last_classification = classify_error(
                    status_code=last_status_code, exception=e
                )
                last_reason = f"{type(e).__name__}: {e}"

                if last_classification == "non_retryable":
                    # No retry — straight to DLQ.
                    await self._deposit_dlq(
                        workspace=workspace,
                        model=model,
                        classification=last_classification,
                        attempts_made=attempt + 1,
                        last_status_code=last_status_code,
                        last_exception_repr=repr(e),
                        last_reason=last_reason,
                        request_metadata=request_metadata or {},
                    )
                    raise

                if attempt >= self.policy.max_retries:
                    # Exhausted retries.
                    await self._deposit_dlq(
                        workspace=workspace,
                        model=model,
                        classification=last_classification,
                        attempts_made=attempt + 1,
                        last_status_code=last_status_code,
                        last_exception_repr=repr(e),
                        last_reason=last_reason,
                        request_metadata=request_metadata or {},
                    )
                    raise

                # Compute backoff + retry.
                retry_after = parse_retry_after(retry_after_extractor(e))
                delay = compute_backoff(attempt, self.policy, retry_after=retry_after)
                logger.info(
                    "Retry attempt=%d/%d classification=%s delay=%.2fs reason=%s",
                    attempt + 1, self.policy.max_retries, last_classification,
                    delay, last_reason,
                )
                await self._sleep(delay)
                continue

            # Success path.
            self.tracker.record(
                workspace=workspace, model=model,
                input_tokens=input_tokens_estimated,
                output_tokens=output_tokens_estimated,
            )
            return result

        # Unreachable — loop returns or raises before this.
        if last_exception:
            raise last_exception
        raise RuntimeError("RetryableExecutor exited without return or raise")

    async def _deposit_dlq(
        self,
        *,
        workspace: str,
        model: str,
        classification: RetryClassification,
        attempts_made: int,
        last_status_code: int | None,
        last_exception_repr: str | None,
        last_reason: str,
        request_metadata: dict[str, Any],
    ) -> None:
        entry = DLQEntry(
            entry_id=f"dlq_{uuid.uuid4().hex[:16]}",
            workspace=workspace,
            model=model,
            classification=classification,
            attempts_made=attempts_made,
            last_status_code=last_status_code,
            last_exception_repr=last_exception_repr,
            last_reason=last_reason,
            request_metadata=dict(request_metadata),
            created_at=datetime.now(timezone.utc),
        )
        await self.dlq.deposit(entry)
