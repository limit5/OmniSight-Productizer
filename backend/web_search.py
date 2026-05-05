"""BP.N.1 -- Web search client, per-tenant rate limit, and cost tracking.

This module is the first, standalone slice of BP.N. It deliberately stops
at three callable pieces:

* ``TavilyWebSearchClient`` -- direct Tavily Search API client using
  the already-shipped ``httpx`` runtime dependency.
* ``WebSearchRateGate`` -- per-tenant token bucket backed by
  ``backend.rate_limit`` (Redis when configured, in-memory otherwise).
* ``WebSearchCostTracker`` -- per-tenant daily USD reserve / commit
  tracker for search spend.

Out of scope for BP.N.1: provider selection env knobs, sanitizer,
guild loadout wiring, audit_log writes, and the BP.N.6 full test matrix.

Module-global state audit (SOP Step 1, 2026-04-21 rule)
-------------------------------------------------------
Only immutable constants live in this module. Rate limiting delegates to
``backend.rate_limit.get_limiter()``: with ``OMNISIGHT_REDIS_URL`` it is
cross-worker atomic via Redis Lua; without Redis it is intentionally
per-worker in-memory fallback, matching the rate-limit module's documented
single-worker/dev mode. Cost tracking has the same shape: Redis Lua is
cross-worker atomic, while ``InMemoryWebSearchCostStore`` is intentionally
per-worker for tests and local dev.

Read-after-write audit (SOP Step 1, 2026-04-21 rule)
---------------------------------------------------
No PG rows are written here. Redis cost reservations are atomic per tenant
daily key. The in-memory fallback is protected by a process-local lock and
does not claim cross-worker read-after-write visibility.

Tavily API notes
----------------
Tavily Search currently uses ``POST https://api.tavily.com/search`` with
Bearer auth. Basic search costs 1 API credit and advanced search costs 2
credits; pay-as-you-go is documented at USD 0.008 / credit. We keep those
as operator-overridable constants rather than adding BP.N.3 env knobs here.
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Literal, Protocol

import httpx

from backend.rate_limit import get_limiter

logger = logging.getLogger(__name__)


TAVILY_SEARCH_URL = "https://api.tavily.com/search"
DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_MAX_RESULTS = 5
DEFAULT_TENANT_RATE_CAPACITY = 30
DEFAULT_TENANT_RATE_WINDOW_SECONDS = 60.0
DEFAULT_DAILY_BUDGET_USD = 5.00
DEFAULT_TAVILY_CREDIT_USD = 0.008
KNOWN_WEB_SEARCH_PROVIDERS = frozenset({"none", "tavily", "exa", "perplexity"})

SearchDepth = Literal["basic", "advanced"]
SearchTopic = Literal["general", "news", "finance"]
WebSearchProvider = Literal["none", "tavily", "exa", "perplexity"]


class WebSearchConfigError(ValueError):
    """Raised when BP.N.3 web-search env knobs are malformed."""


class UnsupportedWebSearchProviderError(WebSearchConfigError):
    """Raised when a configured provider has no concrete client yet."""


@dataclass(frozen=True)
class WebSearchRuntimeConfig:
    """BP.N.3 runtime knobs for web-search provider and tenant budget.

    Module-global state audit: instances are immutable values derived
    from Settings/env per worker; cross-worker spend consistency stays
    in the Redis-backed ``WebSearchCostTracker`` path, with the existing
    in-memory fallback intentionally per-worker for tests and dev.
    """

    provider: WebSearchProvider = "none"
    daily_budget_usd: float = DEFAULT_DAILY_BUDGET_USD

    @classmethod
    def from_settings(cls, settings: Any | None = None) -> "WebSearchRuntimeConfig":
        if settings is None:
            provider_raw = os.environ.get("OMNISIGHT_WEB_SEARCH_PROVIDER", "")
            budget_raw: Any = os.environ.get("OMNISIGHT_WEB_SEARCH_DAILY_BUDGET_USD", "")
        else:
            provider_raw = getattr(settings, "web_search_provider", "")
            budget_raw = getattr(settings, "web_search_daily_budget_usd", "")
        return cls(
            provider=_parse_web_search_provider(provider_raw),
            daily_budget_usd=_parse_daily_budget_usd(budget_raw),
        )


@dataclass(frozen=True)
class WebSearchResult:
    """One normalized web search result for agent consumption."""

    title: str
    url: str
    content: str = ""
    score: float | None = None
    published_date: str = ""
    raw_content: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class WebSearchResponse:
    """Uniform envelope returned by web-search calls."""

    provider: str
    query: str
    tenant_id: str
    fetched_at: str
    search_depth: SearchDepth
    credits_charged: int
    cost_usd_estimated: float
    results: list[WebSearchResult] = field(default_factory=list)
    answer: str = ""
    error: str = ""
    request_id: str = ""

    @property
    def total_results(self) -> int:
        return len(self.results)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "query": self.query,
            "tenant_id": self.tenant_id,
            "fetched_at": self.fetched_at,
            "search_depth": self.search_depth,
            "credits_charged": self.credits_charged,
            "cost_usd_estimated": self.cost_usd_estimated,
            "total_results": self.total_results,
            "results": [item.to_dict() for item in self.results],
            "answer": self.answer,
            "error": self.error,
            "request_id": self.request_id,
        }


@dataclass(frozen=True)
class WebSearchRateLimitConfig:
    """Per-tenant web-search token bucket settings."""

    capacity: int = DEFAULT_TENANT_RATE_CAPACITY
    window_seconds: float = DEFAULT_TENANT_RATE_WINDOW_SECONDS


@dataclass(frozen=True)
class WebSearchBudgetCheck:
    """Cost reserve result for one tenant daily budget."""

    allowed: bool
    tenant_id: str
    reserved_usd: float
    daily_budget_usd: float
    projected_daily_usd: float
    reason: str = ""


@dataclass(frozen=True)
class WebSearchCostReservation:
    """A successful pre-call cost reservation."""

    reservation_id: str
    tenant_id: str
    amount_usd: float
    day_key: str


class WebSearchRateLimited(Exception):
    """Raised when a tenant exceeds the web-search token bucket."""

    def __init__(self, tenant_id: str, retry_after_seconds: float) -> None:
        self.tenant_id = tenant_id
        self.retry_after_seconds = retry_after_seconds
        super().__init__(
            f"Tenant {tenant_id} web search rate limit exceeded; "
            f"retry in {retry_after_seconds:.2f}s"
        )


class WebSearchBudgetExceeded(Exception):
    """Raised when a tenant would exceed the web-search daily budget."""

    def __init__(self, check: WebSearchBudgetCheck) -> None:
        self.check = check
        super().__init__(check.reason)


class WebSearchCredentialMissing(Exception):
    """Raised when a provider API key is missing."""


class WebSearchCostStore(Protocol):
    """Persistence surface for web-search spend reservations."""

    def reserve_daily(
        self,
        tenant_id: str,
        amount_usd: float,
        daily_budget_usd: float,
        *,
        now: datetime | None = None,
    ) -> WebSearchBudgetCheck: ...

    def refund(
        self,
        tenant_id: str,
        amount_usd: float,
        *,
        now: datetime | None = None,
    ) -> None: ...

    def spend_today(self, tenant_id: str, *, now: datetime | None = None) -> float: ...


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(now: datetime | None = None) -> str:
    return (now or _utcnow()).astimezone(timezone.utc).isoformat()


def _day_key(now: datetime | None = None) -> str:
    return (now or _utcnow()).astimezone(timezone.utc).strftime("%Y-%m-%d")


def _credits_for_depth(search_depth: SearchDepth) -> int:
    return 2 if search_depth == "advanced" else 1


def _parse_web_search_provider(value: Any) -> WebSearchProvider:
    provider = str(value or "none").strip().lower() or "none"
    if provider not in KNOWN_WEB_SEARCH_PROVIDERS:
        raise WebSearchConfigError(
            f"unknown web-search provider {provider!r}; expected one of "
            f"{sorted(KNOWN_WEB_SEARCH_PROVIDERS)}"
        )
    return provider  # type: ignore[return-value]


def _parse_daily_budget_usd(value: Any) -> float:
    if value in (None, ""):
        return DEFAULT_DAILY_BUDGET_USD
    try:
        budget = float(value)
    except (TypeError, ValueError) as exc:
        raise WebSearchConfigError(
            "web-search daily budget must be a non-negative number"
        ) from exc
    if budget < 0:
        raise WebSearchConfigError(
            "web-search daily budget must be a non-negative number"
        )
    return budget


def estimate_tavily_cost_usd(
    *,
    search_depth: SearchDepth = "basic",
    credit_usd: float = DEFAULT_TAVILY_CREDIT_USD,
) -> float:
    """Estimate Tavily Search USD cost from documented API credit costs."""

    return _credits_for_depth(search_depth) * max(0.0, float(credit_usd))


class WebSearchRateGate:
    """Per-tenant web-search rate gate.

    The backing limiter is Redis-backed when ``OMNISIGHT_REDIS_URL`` is
    configured; otherwise it is the project-standard in-memory fallback.
    """

    def __init__(
        self,
        config: WebSearchRateLimitConfig | None = None,
        *,
        key_prefix: str = "web_search:tenant",
    ) -> None:
        self.config = config or WebSearchRateLimitConfig()
        self.key_prefix = key_prefix

    def check(self, tenant_id: str) -> None:
        tid = tenant_id or "t-default"
        allowed, retry_after = get_limiter().allow(
            f"{self.key_prefix}:{tid}",
            self.config.capacity,
            self.config.window_seconds,
        )
        if not allowed:
            raise WebSearchRateLimited(tid, retry_after)


_RESERVE_DAILY_LUA = """
local key = KEYS[1]
local amount = tonumber(ARGV[1])
local budget = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])
local current = tonumber(redis.call('GET', key) or '0')
local projected = current + amount

if projected > budget then
    return {0, tostring(current), tostring(projected)}
end

redis.call('SET', key, tostring(projected), 'EX', ttl)
return {1, tostring(projected), tostring(projected)}
"""


class RedisWebSearchCostStore:
    """Redis-backed web-search daily spend tracker using Lua atomic reserve."""

    def __init__(self, redis_url: str, *, key_prefix: str = "omnisight:web_search:cost") -> None:
        import redis as _redis

        self._pool = _redis.ConnectionPool.from_url(redis_url, decode_responses=True)
        self._client = _redis.Redis(connection_pool=self._pool)
        self._reserve_script = self._client.register_script(_RESERVE_DAILY_LUA)
        self._prefix = key_prefix

    def _key(self, tenant_id: str, now: datetime | None = None) -> str:
        return f"{self._prefix}:{_day_key(now)}:{tenant_id or 't-default'}"

    def _ttl_seconds(self, now: datetime | None = None) -> int:
        # Add a fixed 25h TTL rather than calendar math; a stale daily
        # counter lingering one extra hour is harmless and avoids month-end
        # edge cases in this narrow tracker.
        return 25 * 60 * 60

    def reserve_daily(
        self,
        tenant_id: str,
        amount_usd: float,
        daily_budget_usd: float,
        *,
        now: datetime | None = None,
    ) -> WebSearchBudgetCheck:
        tid = tenant_id or "t-default"
        amount = max(0.0, float(amount_usd))
        budget = max(0.0, float(daily_budget_usd))
        result = self._reserve_script(
            keys=[self._key(tid, now)],
            args=[amount, budget, self._ttl_seconds(now)],
        )
        allowed = int(result[0]) == 1
        projected = float(result[2])
        reason = ""
        if not allowed:
            reason = (
                f"Tenant {tid} web search daily budget exceeded: "
                f"${projected:.4f} > ${budget:.4f}"
            )
        return WebSearchBudgetCheck(
            allowed=allowed,
            tenant_id=tid,
            reserved_usd=amount if allowed else 0.0,
            daily_budget_usd=budget,
            projected_daily_usd=projected,
            reason=reason,
        )

    def refund(
        self,
        tenant_id: str,
        amount_usd: float,
        *,
        now: datetime | None = None,
    ) -> None:
        amount = max(0.0, float(amount_usd))
        if amount <= 0:
            return
        key = self._key(tenant_id or "t-default", now)
        pipe = self._client.pipeline()
        pipe.decrbyfloat(key, amount)
        pipe.expire(key, self._ttl_seconds(now))
        pipe.execute()

    def spend_today(self, tenant_id: str, *, now: datetime | None = None) -> float:
        raw = self._client.get(self._key(tenant_id or "t-default", now))
        try:
            return max(0.0, float(raw or 0.0))
        except (TypeError, ValueError):
            return 0.0


class InMemoryWebSearchCostStore:
    """Thread-safe dev / test store. Intentionally per-worker."""

    def __init__(self) -> None:
        self._daily: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()

    def reserve_daily(
        self,
        tenant_id: str,
        amount_usd: float,
        daily_budget_usd: float,
        *,
        now: datetime | None = None,
    ) -> WebSearchBudgetCheck:
        tid = tenant_id or "t-default"
        key = (_day_key(now), tid)
        amount = max(0.0, float(amount_usd))
        budget = max(0.0, float(daily_budget_usd))
        with self._lock:
            current = self._daily.get(key, 0.0)
            projected = current + amount
            if projected > budget:
                return WebSearchBudgetCheck(
                    allowed=False,
                    tenant_id=tid,
                    reserved_usd=0.0,
                    daily_budget_usd=budget,
                    projected_daily_usd=projected,
                    reason=(
                        f"Tenant {tid} web search daily budget exceeded: "
                        f"${projected:.4f} > ${budget:.4f}"
                    ),
                )
            self._daily[key] = projected
        return WebSearchBudgetCheck(
            allowed=True,
            tenant_id=tid,
            reserved_usd=amount,
            daily_budget_usd=budget,
            projected_daily_usd=projected,
        )

    def refund(
        self,
        tenant_id: str,
        amount_usd: float,
        *,
        now: datetime | None = None,
    ) -> None:
        tid = tenant_id or "t-default"
        key = (_day_key(now), tid)
        amount = max(0.0, float(amount_usd))
        with self._lock:
            self._daily[key] = max(0.0, self._daily.get(key, 0.0) - amount)

    def spend_today(self, tenant_id: str, *, now: datetime | None = None) -> float:
        with self._lock:
            return self._daily.get((_day_key(now), tenant_id or "t-default"), 0.0)

    def clear(self) -> None:
        with self._lock:
            self._daily.clear()


class WebSearchCostTracker:
    """Per-tenant daily search spend tracker."""

    def __init__(
        self,
        store: WebSearchCostStore | None = None,
        *,
        daily_budget_usd: float = DEFAULT_DAILY_BUDGET_USD,
    ) -> None:
        self.store = store if store is not None else _default_cost_store()
        self.daily_budget_usd = max(0.0, float(daily_budget_usd))

    def reserve(
        self,
        tenant_id: str,
        amount_usd: float,
        *,
        now: datetime | None = None,
    ) -> WebSearchCostReservation:
        check = self.store.reserve_daily(
            tenant_id or "t-default",
            amount_usd,
            self.daily_budget_usd,
            now=now,
        )
        if not check.allowed:
            raise WebSearchBudgetExceeded(check)
        return WebSearchCostReservation(
            reservation_id=f"web_search_cost_{uuid.uuid4().hex[:16]}",
            tenant_id=check.tenant_id,
            amount_usd=check.reserved_usd,
            day_key=_day_key(now),
        )

    def refund(
        self,
        reservation: WebSearchCostReservation,
        *,
        now: datetime | None = None,
    ) -> None:
        self.store.refund(reservation.tenant_id, reservation.amount_usd, now=now)

    def spend_today(self, tenant_id: str, *, now: datetime | None = None) -> float:
        return self.store.spend_today(tenant_id or "t-default", now=now)


def _default_cost_store() -> WebSearchCostStore:
    redis_url = (os.environ.get("OMNISIGHT_REDIS_URL") or "").strip()
    if redis_url:
        try:
            return RedisWebSearchCostStore(redis_url)
        except Exception as exc:  # noqa: BLE001 -- degraded mode boundary
            logger.warning(
                "web_search: Redis cost store unavailable (%s), using in-memory",
                exc,
            )
    return InMemoryWebSearchCostStore()


HttpClientFactory = Callable[..., httpx.Client]


class TavilyWebSearchClient:
    """Minimal Tavily Search API client with tenant gates."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        endpoint: str = TAVILY_SEARCH_URL,
        timeout_s: float = DEFAULT_TIMEOUT_SECONDS,
        rate_gate: WebSearchRateGate | None = None,
        cost_tracker: WebSearchCostTracker | None = None,
        client_factory: HttpClientFactory | None = None,
        credit_usd: float = DEFAULT_TAVILY_CREDIT_USD,
    ) -> None:
        self.api_key = (
            api_key
            or os.environ.get("OMNISIGHT_TAVILY_API_KEY")
            or os.environ.get("TAVILY_API_KEY")
            or ""
        ).strip()
        self.endpoint = endpoint
        self.timeout_s = timeout_s
        self.rate_gate = rate_gate or WebSearchRateGate()
        self.cost_tracker = cost_tracker or WebSearchCostTracker()
        self.client_factory = client_factory or httpx.Client
        self.credit_usd = max(0.0, float(credit_usd))

    def search(
        self,
        query: str,
        *,
        tenant_id: str = "t-default",
        max_results: int = DEFAULT_MAX_RESULTS,
        search_depth: SearchDepth = "basic",
        topic: SearchTopic = "general",
        include_answer: bool = False,
        include_raw_content: bool = False,
        now: datetime | None = None,
    ) -> WebSearchResponse:
        """Execute a Tavily search after tenant rate and cost gates."""

        current = now or _utcnow()
        tid = tenant_id or "t-default"
        cleaned_query = query.strip()
        if not cleaned_query:
            return self._error_response(
                query=query,
                tenant_id=tid,
                search_depth=search_depth,
                cost_usd=0.0,
                error="query is empty",
                now=current,
            )
        if not self.api_key:
            raise WebSearchCredentialMissing(
                "OMNISIGHT_TAVILY_API_KEY or TAVILY_API_KEY is required"
            )

        self.rate_gate.check(tid)
        cost_usd = estimate_tavily_cost_usd(
            search_depth=search_depth,
            credit_usd=self.credit_usd,
        )
        reservation = self.cost_tracker.reserve(tid, cost_usd, now=current)

        payload = {
            "query": cleaned_query,
            "topic": topic,
            "search_depth": search_depth,
            "max_results": max(1, min(int(max_results), 20)),
            "include_answer": bool(include_answer),
            "include_raw_content": bool(include_raw_content),
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        try:
            with self.client_factory(timeout=self.timeout_s) as client:
                response = client.post(self.endpoint, json=payload, headers=headers)
                response.raise_for_status()
                data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            self.cost_tracker.refund(reservation, now=current)
            logger.info("tavily search failed tenant=%s error=%s", tid, exc)
            return self._error_response(
                query=cleaned_query,
                tenant_id=tid,
                search_depth=search_depth,
                cost_usd=cost_usd,
                error=f"{type(exc).__name__}: {exc}",
                now=current,
            )

        return WebSearchResponse(
            provider="tavily",
            query=cleaned_query,
            tenant_id=tid,
            fetched_at=_stamp(current),
            search_depth=search_depth,
            credits_charged=_credits_for_depth(search_depth),
            cost_usd_estimated=cost_usd,
            results=_tavily_results(data),
            answer=str(data.get("answer") or ""),
            request_id=str(data.get("request_id") or ""),
        )

    def _error_response(
        self,
        *,
        query: str,
        tenant_id: str,
        search_depth: SearchDepth,
        cost_usd: float,
        error: str,
        now: datetime,
    ) -> WebSearchResponse:
        return WebSearchResponse(
            provider="tavily",
            query=query,
            tenant_id=tenant_id,
            fetched_at=_stamp(now),
            search_depth=search_depth,
            credits_charged=_credits_for_depth(search_depth),
            cost_usd_estimated=cost_usd,
            error=error,
        )


def make_web_search_client(
    provider: str | None = None,
    *,
    settings: Any | None = None,
    cost_store: WebSearchCostStore | None = None,
    **client_kwargs: Any,
) -> TavilyWebSearchClient | None:
    """Construct the configured BP.N web-search client.

    Resolution mirrors the existing clone-source factory pattern:
    explicit ``provider`` argument wins, otherwise the passed Settings
    object / environment provides ``OMNISIGHT_WEB_SEARCH_PROVIDER``.
    ``none`` returns ``None`` so BP.N.4 can leave guilds disabled by
    default. ``exa`` and ``perplexity`` are accepted BP.N.3 knob values
    but raise until their adapters land in a later row.
    """

    config = WebSearchRuntimeConfig.from_settings(settings)
    if provider is not None:
        config = WebSearchRuntimeConfig(
            provider=_parse_web_search_provider(provider),
            daily_budget_usd=config.daily_budget_usd,
        )

    if config.provider == "none":
        return None
    if config.provider != "tavily":
        raise UnsupportedWebSearchProviderError(
            f"web-search provider {config.provider!r} is configured but "
            "no client adapter has shipped yet"
        )

    client_kwargs.setdefault(
        "cost_tracker",
        WebSearchCostTracker(
            store=cost_store,
            daily_budget_usd=config.daily_budget_usd,
        ),
    )
    return TavilyWebSearchClient(**client_kwargs)


def _tavily_results(payload: dict[str, Any]) -> list[WebSearchResult]:
    results: list[WebSearchResult] = []
    for item in payload.get("results") or []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "")
        title = str(item.get("title") or url)
        if not url and not title:
            continue
        score_raw = item.get("score")
        try:
            score = float(score_raw) if score_raw is not None else None
        except (TypeError, ValueError):
            score = None
        results.append(
            WebSearchResult(
                title=title,
                url=url,
                content=str(item.get("content") or ""),
                score=score,
                published_date=str(item.get("published_date") or ""),
                raw_content=str(item.get("raw_content") or ""),
            )
        )
    return results


__all__ = [
    "DEFAULT_DAILY_BUDGET_USD",
    "DEFAULT_MAX_RESULTS",
    "DEFAULT_TAVILY_CREDIT_USD",
    "DEFAULT_TENANT_RATE_CAPACITY",
    "DEFAULT_TENANT_RATE_WINDOW_SECONDS",
    "InMemoryWebSearchCostStore",
    "KNOWN_WEB_SEARCH_PROVIDERS",
    "RedisWebSearchCostStore",
    "TAVILY_SEARCH_URL",
    "TavilyWebSearchClient",
    "WebSearchBudgetCheck",
    "WebSearchBudgetExceeded",
    "WebSearchConfigError",
    "WebSearchCostReservation",
    "WebSearchCostStore",
    "WebSearchCostTracker",
    "WebSearchCredentialMissing",
    "WebSearchProvider",
    "WebSearchRateGate",
    "WebSearchRateLimitConfig",
    "WebSearchRateLimited",
    "WebSearchResponse",
    "WebSearchResult",
    "WebSearchRuntimeConfig",
    "UnsupportedWebSearchProviderError",
    "estimate_tavily_cost_usd",
    "make_web_search_client",
]
