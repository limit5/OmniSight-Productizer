"""W11.8 #XXX — L5 Rate limit + PEP HOLD for the website-cloning pipeline.

Layer 5 of the W11 5-layer defense-in-depth pipeline. Runs **after** the
W11.7 L4 traceability layer has pinned the manifest + HTML comment +
audit row and **immediately before** the router returns the cloned
artefacts to the caller. This module is the policy-enforcement point
(PEP) that holds (i.e. refuses) any clone request that would exceed the
per-tenant × per-target budget over a rolling time window.

W11.8 row spec
--------------
"24h same tenant same target max 3 times" — a rolling 24-hour window in
which any one tenant may clone the same target origin at most three
times. This module is the canonical enforcement of that contract: a
``CloneRateLimitedError`` (PEP HOLD) is raised if a fourth attempt
arrives inside the window.

Why a sliding-window log (and not a token bucket)
-------------------------------------------------
The general-purpose :mod:`backend.rate_limit` module ships a token
bucket that is excellent for high-frequency endpoints (login, API
ingest). For a low-rate human-driven action like "clone this URL the
operator typed into the dashboard", the token bucket has two awkward
edges:

* It does not give a precise *next-available* timestamp — only a
  derived ``retry_after`` from the deficit / refill rate.
* Burst behaviour ("3 in the first second of the window, none for the
  next 24h") is fine for our needs but the audit explanation reads
  awkwardly when an operator asks "when can I clone this site again?"

A sliding-window log (a per-(tenant, target) ZSET of attempt
timestamps) gives:

* Exact answer to "how many clones in the last 24h" (= ``ZCARD`` after
  pruning expired entries).
* Exact ``retry_after`` = ``oldest_in_window + window - now``.
* Symmetric semantics across the Redis-backed prod path and the
  in-memory dev path — the prune-then-count-then-conditionally-append
  shape is identical, just locked differently.

PEP HOLD contract
-----------------
The router pattern is::

    decision = await assert_clone_rate_limit(
        tenant_id=tenant.id,
        target_url=spec.source_url,
        actor=actor_email,
    )
    # decision.allowed == True; ``decision.count`` is now the post-
    # commit count (1..limit); the attempt has been recorded.

Or, if the budget is exhausted::

    raise CloneRateLimitedError(
        decision=CloneRateLimitDecision(
            allowed=False,
            count=3,                # already at limit
            limit=3,
            window_seconds=86400.0,
            retry_after_seconds=12345.6,
            oldest_attempt_at=...,
            tenant_id=...,
            target=...,
        )
    )

``CloneRateLimitedError`` is a :class:`SiteClonerError` subclass so the
existing ``except SiteClonerError`` catch-alls (router, W11.12 audit row
categoriser) keep working without explicit special-casing — they pick
the finer bucket via ``isinstance``.

When PEP HOLDs, ``assert_clone_rate_limit`` *also* writes a
``web.clone.rate_limited`` audit log row (separate action namespace from
the W11.7 ``web.clone`` row so operators can filter "denied attempts"
without scanning the success channel). The audit write is best-effort —
it does not roll back the HOLD decision if the audit subsystem is down.

Module-global state audit (SOP §1)
----------------------------------
Module-level state:

* Immutable constants (``CLONE_RATE_KEY_PREFIX`` /
  ``DEFAULT_CLONE_RATE_LIMIT`` / ``DEFAULT_CLONE_RATE_WINDOW_S`` /
  ``CLONE_RATE_AUDIT_ACTION`` / ``CLONE_RATE_AUDIT_ENTITY_KIND`` /
  Lua script literal). These are constants — answer #1.
* Module-level :data:`logger` (the stdlib ``logging`` system owns its
  own thread-safe singleton — answer #1).
* Module-level singleton ``_limiter`` populated lazily by
  :func:`get_clone_rate_limiter`. Cross-worker semantics:

  - When ``OMNISIGHT_REDIS_URL`` is set, a :class:`RedisCloneRateLimiter`
    is constructed; the *state* (the ZSETs of attempts) lives in Redis,
    so cross-worker consistency is **answer #2** (coordinated through
    Redis with the Lua script's atomicity).
  - When ``OMNISIGHT_REDIS_URL`` is *not* set, an
    :class:`InMemoryCloneRateLimiter` is constructed; each worker's
    state is per-process. We log an explicit warning at construction
    time so an operator that wires a multi-worker prod stack without
    Redis sees the degradation in the boot log. Cross-worker semantics
    in that mode are **answer #3** (deliberately per-replica — the
    fallback is documented as "single worker only" and the warning
    tells the operator to flip ``OMNISIGHT_REDIS_URL`` for prod).

Read-after-write timing audit (SOP §2)
--------------------------------------
N/A. Each :meth:`CloneRateLimiter.check` call is one atomic Redis Lua
script (or one ``threading.Lock``-guarded critical section in the
in-memory limiter). The audit log call is a separate best-effort write
that does not feed back into the decision, so there is no parallel-vs-
serial timing dependence inside this module.

Production Readiness Gate §158
------------------------------
No new pip dependencies. ``redis`` is already in
``backend/requirements.in`` (used by :mod:`backend.rate_limit`);
``urllib.parse`` / ``threading`` / ``time`` / ``uuid`` / ``json`` /
``logging`` are stdlib. No image rebuild required.

Inspired by firecrawl/open-lovable (MIT). The full attribution + license
text lands in the W11.13 row.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Protocol, Tuple
from urllib.parse import urlsplit

from backend.web.site_cloner import (
    InvalidCloneURLError,
    SiteClonerError,
    SUPPORTED_URL_SCHEMES,
)

logger = logging.getLogger(__name__)


# ── Constants ───────────────────────────────────────────────────────────

#: Default ceiling on the number of clone attempts permitted per
#: (tenant, target) pair within :data:`DEFAULT_CLONE_RATE_WINDOW_S`.
#: Pinned to **3** by the W11.8 row spec.
DEFAULT_CLONE_RATE_LIMIT: int = 3

#: Default rolling window length, in seconds. Pinned to **24h** by the
#: W11.8 row spec (24 × 3600 = 86400).
DEFAULT_CLONE_RATE_WINDOW_S: float = 86400.0

#: Lower / upper bounds on the env-tunable limit knob. Below 1 makes the
#: limiter useless; above 1000 means a single tenant can scrape an
#: origin 1000 times in a window which is well past sanity.
_MIN_RATE_LIMIT: int = 1
_MAX_RATE_LIMIT: int = 1000

#: Lower / upper bounds on the env-tunable window knob. Below 60s the
#: rolling-window resolution starts to fight wall-clock skew; above 30
#: days the in-memory fallback's deque starts retaining unbounded history
#: (in practice limit×days entries — still bounded but a reminder that
#: the audit-driven semantics of this row aren't designed for week-scale
#: rate limiting).
_MIN_RATE_WINDOW_S: float = 60.0
_MAX_RATE_WINDOW_S: float = 30 * 24 * 3600.0  # 30 days

#: Prefix for Redis keys / in-memory dict keys storing per-(tenant,
#: target) attempt logs. Pinned so the prefix is grep-able and so an
#: operator can ``redis-cli SCAN MATCH '<prefix>*'`` to inspect.
CLONE_RATE_KEY_PREFIX: str = "omnisight:clone:rl:"

#: Default ports for the URL schemes :mod:`backend.web.site_cloner`
#: accepts. Used by :func:`canonical_clone_target` to strip the port
#: when it equals the scheme default — otherwise ``http://x:80/`` and
#: ``http://x/`` would be treated as different targets.
_DEFAULT_PORTS: dict[str, int] = {
    "http": 80,
    "https": 443,
}

#: Action / entity_kind namespace for audit rows written when this layer
#: HOLDs a clone attempt. Distinct from the W11.7
#: ``web.clone`` action so operators can filter "denied" without scanning
#: the "success" channel; W11.12 audit row picks both up via the shared
#: ``web.clone.*`` action prefix.
CLONE_RATE_AUDIT_ACTION: str = "web.clone.rate_limited"
CLONE_RATE_AUDIT_ENTITY_KIND: str = "web_clone_rate_limit"

#: Lua script driving the Redis sliding-window-log limiter. The script
#: prunes expired entries, counts what's left, and conditionally appends
#: a new entry — the whole sequence runs atomically inside Redis. Kept
#: as a triple-quoted constant (rather than loaded from disk) so the
#: script body is part of the audited public surface.
_SLIDING_WINDOW_LOG_LUA: str = """
local key       = KEYS[1]
local now_ms    = tonumber(ARGV[1])
local window_ms = tonumber(ARGV[2])
local limit     = tonumber(ARGV[3])
local entry     = ARGV[4]
local dry_run   = tonumber(ARGV[5])

local cutoff = now_ms - window_ms
redis.call('ZREMRANGEBYSCORE', key, '-inf', cutoff)

local count = redis.call('ZCARD', key)

local allowed
local oldest_ms

if count < limit then
    allowed = 1
    if dry_run == 0 then
        redis.call('ZADD', key, now_ms, entry)
        count = count + 1
        redis.call('PEXPIRE', key, window_ms + 60000)
    end
    -- Oldest in-window entry (post-add when not dry_run) — if any.
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    if oldest[2] ~= nil then oldest_ms = oldest[2] end
else
    allowed = 0
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    if oldest[2] ~= nil then oldest_ms = oldest[2] end
end

return {allowed, count, tostring(oldest_ms or '')}
"""


# ── Errors ──────────────────────────────────────────────────────────────


class CloneRateLimitError(SiteClonerError):
    """Base class for everything raised by ``clone_rate_limit``.

    Subclass of :class:`backend.web.site_cloner.SiteClonerError` so the
    router's blanket ``except SiteClonerError`` keeps catching the L5
    case without special-casing; the W11.12 audit row picks the finer
    bucket via ``isinstance``.
    """


class CloneRateLimitedError(CloneRateLimitError):
    """PEP HOLD raised when a clone attempt would exceed the per-(tenant,
    target) rolling-window budget.

    Carries the full :class:`CloneRateLimitDecision` so the caller
    (router, W11.12 audit row) can:

    * Read ``decision.retry_after_seconds`` and surface it as an HTTP
      ``Retry-After`` header.
    * Read ``decision.count`` and ``decision.limit`` to format an
      operator-facing "you have used 3/3 attempts" message.
    * Read ``decision.tenant_id`` and ``decision.target`` to scope a
      follow-up audit row.

    Raised by :func:`assert_clone_rate_limit`.
    """

    def __init__(self, decision: "CloneRateLimitDecision", *, url: str | None = None) -> None:
        self.decision = decision
        self.url = url
        super().__init__(
            f"clone rate limit exceeded for tenant {decision.tenant_id!r} "
            f"target {decision.target!r}: {decision.count}/{decision.limit} "
            f"in {decision.window_seconds:.0f}s window; "
            f"retry after {decision.retry_after_seconds:.1f}s"
        )


# ── Dataclass ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CloneRateLimitDecision:
    """The verdict returned by :class:`CloneRateLimiter` on a single
    ``check()`` call.

    Frozen — once the limiter returns a decision, the caller (PEP entry
    point, audit log, HTTP response builder) reads from the same fixed
    snapshot. Pickle-safe so worker pools can pass it across boundaries.
    """

    allowed: bool
    count: int
    limit: int
    window_seconds: float
    retry_after_seconds: float
    oldest_attempt_at: Optional[float]
    tenant_id: str
    target: str

    @property
    def held(self) -> bool:
        """True when this decision represents a PEP HOLD (i.e. the
        attempt was rejected). Mirror of ``not allowed`` named for the
        W11.8 row spec ("PEP HOLD")."""
        return not self.allowed


# ── Helpers ─────────────────────────────────────────────────────────────


def canonical_clone_target(url: str) -> str:
    """Reduce a clone-target URL to its canonical *origin* string.

    Defined as ``<scheme>://<lowercase host>[:<non-default port>]``. We
    deliberately strip ``path`` / ``query`` / ``fragment`` and ``user:pass``
    so a tenant cannot dodge the rate limit by appending ``?cb=1``,
    fragments, or alternative auth strings to the same target. We also
    strip the port when it equals the scheme's default so
    ``http://x:80/`` and ``http://x/`` collapse onto the same key.

    Raises:
        InvalidCloneURLError: ``url`` is empty / non-string / has no
            host / has an unsupported scheme. Typed so the PEP entry
            point can re-raise without losing the typed-error
            invariant.
    """
    if not isinstance(url, str) or not url.strip():
        raise InvalidCloneURLError("clone target URL must be a non-empty string")

    parts = urlsplit(url.strip())
    scheme = (parts.scheme or "").lower()
    if scheme not in SUPPORTED_URL_SCHEMES:
        raise InvalidCloneURLError(
            f"unsupported scheme {scheme!r}; expected one of "
            f"{sorted(SUPPORTED_URL_SCHEMES)}"
        )

    host = (parts.hostname or "").lower()
    if not host:
        raise InvalidCloneURLError(f"clone target URL {url!r} has no host")

    # ``urlsplit`` parses port separately; ``hostname`` already lower-
    # cased and stripped of credentials. We rebuild a minimal origin.
    port = parts.port
    if port is not None and port != _DEFAULT_PORTS.get(scheme):
        return f"{scheme}://{host}:{port}"
    return f"{scheme}://{host}"


def clone_rate_limit_key(tenant_id: str, target: str) -> str:
    """Compose the Redis / in-memory dict key for a (tenant, target)
    pair. Pinned so tests and operator-side ``redis-cli SCAN`` agree on
    the format.
    """
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise ValueError("tenant_id must be a non-empty string")
    if not isinstance(target, str) or not target.strip():
        raise ValueError("target must be a non-empty string")
    # Tenant IDs are UUIDs in our system; target is already canonicalised.
    # Both are control-plane-trusted strings (router resolves tenant from
    # the auth context), but we still avoid embedding ``:`` literally
    # from the target into the key — already excluded by
    # ``canonical_clone_target`` (which only emits scheme://host[:port]).
    return f"{CLONE_RATE_KEY_PREFIX}{tenant_id}:{target}"


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return max(lo, min(hi, int(raw)))
    except ValueError:
        logger.warning(
            "W11.8: invalid integer for %s=%r; falling back to default %d",
            name, raw, default,
        )
        return default


def _env_float(name: str, default: float, lo: float, hi: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return max(lo, min(hi, float(raw)))
    except ValueError:
        logger.warning(
            "W11.8: invalid float for %s=%r; falling back to default %.1f",
            name, raw, default,
        )
        return default


def resolve_clone_rate_limit() -> int:
    """Resolve the runtime per-(tenant, target) limit honouring
    ``OMNISIGHT_CLONE_RATE_LIMIT``. Clamped to ``[1, 1000]``."""
    return _env_int(
        "OMNISIGHT_CLONE_RATE_LIMIT",
        DEFAULT_CLONE_RATE_LIMIT,
        _MIN_RATE_LIMIT,
        _MAX_RATE_LIMIT,
    )


def resolve_clone_rate_window_seconds() -> float:
    """Resolve the runtime rolling-window length honouring
    ``OMNISIGHT_CLONE_RATE_WINDOW_S``. Clamped to ``[60s, 30d]``."""
    return _env_float(
        "OMNISIGHT_CLONE_RATE_WINDOW_S",
        DEFAULT_CLONE_RATE_WINDOW_S,
        _MIN_RATE_WINDOW_S,
        _MAX_RATE_WINDOW_S,
    )


# ── Limiter Protocol ────────────────────────────────────────────────────


class CloneRateLimiter(Protocol):
    """Sliding-window-log limiter contract.

    Implementations:

    * :class:`InMemoryCloneRateLimiter` — single-process,
      ``threading.Lock``-guarded; default in non-Redis deployments.
    * :class:`RedisCloneRateLimiter` — Redis ZSET + Lua-atomic;
      default when ``OMNISIGHT_REDIS_URL`` is set.

    Both implementations agree on every observable: same key namespace,
    same ``check()`` return shape, same ``reset()`` semantics. Tests
    treat the Protocol as the contract and run the in-memory and a
    fake-Redis double through the same parametrised cases.
    """

    def check(
        self,
        tenant_id: str,
        target: str,
        *,
        limit: int,
        window_seconds: float,
        now: float | None = None,
        dry_run: bool = False,
    ) -> CloneRateLimitDecision:
        """Atomically prune expired entries, count, and conditionally
        consume one slot. Returns the resulting decision."""
        ...

    def reset(self, tenant_id: str, target: str | None = None) -> None:
        """Wipe all attempts for a (tenant, target) pair, or for the
        whole tenant when ``target`` is None. Test fixture / takedown
        tooling helper."""
        ...

    def clear(self) -> None:
        """Wipe every key managed by this limiter. Test-only."""
        ...


# ── In-memory implementation ────────────────────────────────────────────


class InMemoryCloneRateLimiter:
    """Per-process sliding-window log.

    Each (tenant, target) gets its own ``deque`` of float timestamps.
    ``check()`` prunes the deque to the current window, counts the
    remainder, and conditionally appends ``now``.

    Thread-safe via a single :class:`threading.Lock`. Cross-worker
    semantics are **deliberately per-replica** when this limiter is
    selected (i.e. ``OMNISIGHT_REDIS_URL`` is unset) — see
    :func:`get_clone_rate_limiter` for the warning-on-construction
    pattern. For prod multi-worker deployments operators flip
    ``OMNISIGHT_REDIS_URL`` and the singleton resolves to
    :class:`RedisCloneRateLimiter` instead.
    """

    def __init__(self, *, max_keys: int = 65536) -> None:
        self._buckets: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        self._max_keys = max_keys

    @staticmethod
    def _prune(bucket: deque[float], cutoff: float) -> None:
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()

    def check(
        self,
        tenant_id: str,
        target: str,
        *,
        limit: int,
        window_seconds: float,
        now: float | None = None,
        dry_run: bool = False,
    ) -> CloneRateLimitDecision:
        if limit <= 0:
            raise ValueError(f"limit must be ≥ 1, got {limit!r}")
        if window_seconds <= 0:
            raise ValueError(f"window_seconds must be > 0, got {window_seconds!r}")

        if now is None:
            now = time.time()
        cutoff = now - window_seconds
        key = clone_rate_limit_key(tenant_id, target)

        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                if len(self._buckets) >= self._max_keys:
                    # Evict the oldest-key by smallest first-element ts.
                    # Empty buckets sort first (∞ → 0), pruned every
                    # check, so this naturally trims abandoned keys.
                    def _oldest(item: tuple[str, deque[float]]) -> float:
                        return item[1][0] if item[1] else 0.0

                    oldest_key = min(self._buckets.items(), key=_oldest)[0]
                    del self._buckets[oldest_key]
                bucket = deque()
                self._buckets[key] = bucket

            self._prune(bucket, cutoff)
            count = len(bucket)
            oldest = bucket[0] if bucket else None

            if count < limit:
                if not dry_run:
                    bucket.append(now)
                    count += 1
                    oldest = bucket[0]
                return CloneRateLimitDecision(
                    allowed=True,
                    count=count,
                    limit=limit,
                    window_seconds=window_seconds,
                    retry_after_seconds=0.0,
                    oldest_attempt_at=oldest,
                    tenant_id=tenant_id,
                    target=target,
                )

            # PEP HOLD path — count == limit. Compute precise retry_after
            # from the oldest attempt's expiry.
            assert oldest is not None  # count == limit ≥ 1 → bucket non-empty
            retry_after = max(0.0, (oldest + window_seconds) - now)
            return CloneRateLimitDecision(
                allowed=False,
                count=count,
                limit=limit,
                window_seconds=window_seconds,
                retry_after_seconds=retry_after,
                oldest_attempt_at=oldest,
                tenant_id=tenant_id,
                target=target,
            )

    def reset(self, tenant_id: str, target: str | None = None) -> None:
        with self._lock:
            if target is not None:
                self._buckets.pop(clone_rate_limit_key(tenant_id, target), None)
                return
            prefix = f"{CLONE_RATE_KEY_PREFIX}{tenant_id}:"
            for key in [k for k in self._buckets if k.startswith(prefix)]:
                del self._buckets[key]

    def clear(self) -> None:
        with self._lock:
            self._buckets.clear()


# ── Redis implementation ────────────────────────────────────────────────


class RedisCloneRateLimiter:
    """Redis-backed sliding-window log.

    Uses :data:`_SLIDING_WINDOW_LOG_LUA` so the prune-then-count-then-
    conditionally-append sequence is a single atomic round-trip — no
    risk of a TOCTOU race between two workers checking the same key.

    Backed by :mod:`redis` synchronously; the existing
    :mod:`backend.rate_limit` module ships an identical pattern (also
    sync redis) and runs in production today.
    """

    def __init__(self, redis_url: str) -> None:
        import redis as _redis  # lazy — not all deployments install redis

        self._pool = _redis.ConnectionPool.from_url(redis_url, decode_responses=True)
        self._client = _redis.Redis(connection_pool=self._pool)
        self._script = self._client.register_script(_SLIDING_WINDOW_LOG_LUA)

    def check(
        self,
        tenant_id: str,
        target: str,
        *,
        limit: int,
        window_seconds: float,
        now: float | None = None,
        dry_run: bool = False,
    ) -> CloneRateLimitDecision:
        if limit <= 0:
            raise ValueError(f"limit must be ≥ 1, got {limit!r}")
        if window_seconds <= 0:
            raise ValueError(f"window_seconds must be > 0, got {window_seconds!r}")

        if now is None:
            now = time.time()

        key = clone_rate_limit_key(tenant_id, target)
        now_ms = int(now * 1000)
        window_ms = int(window_seconds * 1000)

        # The entry value must be unique-per-call so a same-millisecond
        # double-call from two workers writes two distinct entries
        # (ZSETs deduplicate by member, not by score).
        entry = f"{now_ms}-{uuid.uuid4().hex[:12]}"

        result = self._script(
            keys=[key],
            args=[now_ms, window_ms, limit, entry, 1 if dry_run else 0],
        )

        allowed = int(result[0]) == 1
        count = int(result[1])
        oldest_raw = result[2]
        oldest_ms = float(oldest_raw) if oldest_raw not in ("", None) else None
        oldest = (oldest_ms / 1000.0) if oldest_ms is not None else None

        if allowed:
            return CloneRateLimitDecision(
                allowed=True,
                count=count,
                limit=limit,
                window_seconds=window_seconds,
                retry_after_seconds=0.0,
                oldest_attempt_at=oldest,
                tenant_id=tenant_id,
                target=target,
            )

        retry_after = max(0.0, (oldest + window_seconds) - now) if oldest is not None else window_seconds
        return CloneRateLimitDecision(
            allowed=False,
            count=count,
            limit=limit,
            window_seconds=window_seconds,
            retry_after_seconds=retry_after,
            oldest_attempt_at=oldest,
            tenant_id=tenant_id,
            target=target,
        )

    def reset(self, tenant_id: str, target: str | None = None) -> None:
        if target is not None:
            self._client.delete(clone_rate_limit_key(tenant_id, target))
            return
        prefix = f"{CLONE_RATE_KEY_PREFIX}{tenant_id}:"
        cursor = 0
        while True:
            cursor, keys = self._client.scan(cursor=cursor, match=prefix + "*", count=500)
            if keys:
                self._client.delete(*keys)
            if cursor == 0:
                break

    def clear(self) -> None:
        cursor = 0
        prefix = CLONE_RATE_KEY_PREFIX
        while True:
            cursor, keys = self._client.scan(cursor=cursor, match=prefix + "*", count=500)
            if keys:
                self._client.delete(*keys)
            if cursor == 0:
                break


# ── Singleton management ────────────────────────────────────────────────

_limiter: CloneRateLimiter | None = None


def get_clone_rate_limiter() -> CloneRateLimiter:
    """Return the process-wide :class:`CloneRateLimiter`.

    Resolution order:

    1. Redis (``OMNISIGHT_REDIS_URL`` set + ``redis`` package importable
       + connection succeeds).
    2. In-memory fallback (warning logged so an operator running
       multi-worker prod without Redis sees the degradation in the
       boot log).

    Cross-worker consistency: see module docstring §"Module-global
    state audit". Redis path is answer #2; in-memory path is answer #3
    (deliberately per-replica with a documented warning).
    """
    global _limiter
    if _limiter is not None:
        return _limiter

    redis_url = (os.environ.get("OMNISIGHT_REDIS_URL") or "").strip()
    if redis_url:
        try:
            _limiter = RedisCloneRateLimiter(redis_url)
            logger.info(
                "W11.8: Redis clone rate limiter connected (%s)",
                redis_url.split("@")[-1],
            )
            return _limiter
        except Exception as exc:  # noqa: BLE001 — defensive at boot
            logger.warning(
                "W11.8: Redis clone rate limiter unavailable (%s); "
                "falling back to in-memory (per-replica state)",
                exc,
            )

    _limiter = InMemoryCloneRateLimiter()
    logger.info(
        "W11.8: in-memory clone rate limiter active "
        "(per-replica state — set OMNISIGHT_REDIS_URL for cross-worker)"
    )
    return _limiter


def reset_clone_rate_limiter() -> None:
    """Test-only: drop the cached singleton so the next
    :func:`get_clone_rate_limiter` call rebuilds it (e.g. so a test that
    sets ``OMNISIGHT_REDIS_URL`` mid-run picks up the new path)."""
    global _limiter
    if _limiter is not None:
        try:
            _limiter.clear()
        except Exception:  # noqa: BLE001 — best-effort
            pass
    _limiter = None


# ── PEP entry point ─────────────────────────────────────────────────────


#: Type alias for the audit-log hook injected by tests / dependency
#: inversion. Default resolves to :func:`backend.audit.log` when called.
AuditLogHook = Callable[..., Awaitable[Any]]


async def _default_audit_log(*args: Any, **kwargs: Any) -> Any:
    """Lazy bridge to :func:`backend.audit.log`.

    Imported lazily so this module imports cleanly in unit-test
    environments where the audit subsystem isn't initialised. Mirror of
    the lazy import in :mod:`backend.web.clone_manifest.record_clone_audit`.
    """
    from backend import audit as _audit_mod

    return await _audit_mod.log(*args, **kwargs)


async def record_clone_rate_limit_hold(
    decision: CloneRateLimitDecision,
    *,
    actor: str | None = None,
    conn: Any = None,
    session_id: str | None = None,
    audit_log: AuditLogHook | None = None,
    url: str | None = None,
) -> Optional[int]:
    """Append a ``web.clone.rate_limited`` audit row recording a PEP HOLD.

    Best-effort — returns ``None`` and does **not** raise if the audit
    subsystem is unreachable, mirroring :func:`backend.audit.log`'s
    contract. The HOLD decision itself has already been made by
    :meth:`CloneRateLimiter.check`; this function records it.

    The ``before`` slot of the audit row is ``None`` (no prior state);
    the ``after`` slot carries the full decision payload so a downstream
    auditor can replay "tenant X tried to clone target Y, was at N/limit,
    next available at T".
    """
    if not isinstance(decision, CloneRateLimitDecision):
        raise TypeError(
            f"decision must be CloneRateLimitDecision, got {type(decision).__name__}"
        )

    hook = audit_log or _default_audit_log

    after = {
        "tenant_id": decision.tenant_id,
        "target": decision.target,
        "url": url,
        "limit": decision.limit,
        "count": decision.count,
        "window_seconds": decision.window_seconds,
        "retry_after_seconds": decision.retry_after_seconds,
        "oldest_attempt_at": decision.oldest_attempt_at,
        "allowed": decision.allowed,
    }

    try:
        return await hook(
            CLONE_RATE_AUDIT_ACTION,
            CLONE_RATE_AUDIT_ENTITY_KIND,
            decision.tenant_id,
            None,
            after,
            actor or "system",
            session_id,
            conn,
        )
    except TypeError:
        # Some hooks (e.g. fakes) bind by keyword. Retry as kwargs.
        return await hook(
            action=CLONE_RATE_AUDIT_ACTION,
            entity_kind=CLONE_RATE_AUDIT_ENTITY_KIND,
            entity_id=decision.tenant_id,
            before=None,
            after=after,
            actor=actor or "system",
            session_id=session_id,
            conn=conn,
        )
    except Exception as exc:  # noqa: BLE001 — audit best-effort
        logger.warning(
            "W11.8: clone rate-limit audit log failed (%s/%s tenant=%s): %s",
            CLONE_RATE_AUDIT_ACTION, CLONE_RATE_AUDIT_ENTITY_KIND,
            decision.tenant_id, exc,
        )
        return None


async def assert_clone_rate_limit(
    tenant_id: str,
    target_url: str,
    *,
    limit: int | None = None,
    window_seconds: float | None = None,
    now: float | None = None,
    dry_run: bool = False,
    actor: str | None = None,
    conn: Any = None,
    session_id: str | None = None,
    limiter: CloneRateLimiter | None = None,
    audit: bool = True,
    audit_log: AuditLogHook | None = None,
) -> CloneRateLimitDecision:
    """Policy-enforcement-point entry: consume a slot or HOLD.

    The W11 router calls this after the W11.7 manifest is pinned and
    before returning the cloned artefacts to the caller::

        decision = await assert_clone_rate_limit(
            tenant_id=tenant.id,
            target_url=spec.source_url,
            actor=actor_email,
        )

    Behaviour:

    * On **allow** (``decision.allowed == True``): the attempt has been
      logged in the limiter's window and the function returns the
      decision so the caller can surface ``count``/``limit`` headers.
      No audit row is written on the allow path — the W11.7
      ``record_clone_audit`` row already covers successful clones.
    * On **HOLD** (``decision.allowed == False``): a
      ``web.clone.rate_limited`` audit row is best-effort appended,
      then :class:`CloneRateLimitedError` is raised carrying the
      decision.

    Knob resolution (``limit`` / ``window_seconds``):
        Explicit kwarg → ``OMNISIGHT_CLONE_RATE_LIMIT`` /
        ``OMNISIGHT_CLONE_RATE_WINDOW_S`` env → row-spec defaults
        (3 / 86400). The env knobs are clamped to safe bounds — see
        ``_MIN_*`` / ``_MAX_*`` module constants.

    Raises:
        InvalidCloneURLError: ``target_url`` is invalid (delegated to
            :func:`canonical_clone_target`).
        CloneRateLimitedError: Budget exhausted; ``decision.held`` True.
        ValueError: ``tenant_id`` is empty or non-string.
    """
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise ValueError("tenant_id must be a non-empty string")

    target = canonical_clone_target(target_url)

    effective_limit = limit if limit is not None else resolve_clone_rate_limit()
    effective_window = (
        window_seconds if window_seconds is not None else resolve_clone_rate_window_seconds()
    )

    lim = limiter or get_clone_rate_limiter()
    decision = lim.check(
        tenant_id,
        target,
        limit=effective_limit,
        window_seconds=effective_window,
        now=now,
        dry_run=dry_run,
    )

    if decision.held:
        if audit:
            await record_clone_rate_limit_hold(
                decision,
                actor=actor,
                conn=conn,
                session_id=session_id,
                audit_log=audit_log,
                url=target_url,
            )
        raise CloneRateLimitedError(decision, url=target_url)

    return decision


__all__ = [
    "CLONE_RATE_AUDIT_ACTION",
    "CLONE_RATE_AUDIT_ENTITY_KIND",
    "CLONE_RATE_KEY_PREFIX",
    "CloneRateLimitDecision",
    "CloneRateLimitError",
    "CloneRateLimitedError",
    "CloneRateLimiter",
    "DEFAULT_CLONE_RATE_LIMIT",
    "DEFAULT_CLONE_RATE_WINDOW_S",
    "InMemoryCloneRateLimiter",
    "RedisCloneRateLimiter",
    "assert_clone_rate_limit",
    "canonical_clone_target",
    "clone_rate_limit_key",
    "get_clone_rate_limiter",
    "record_clone_rate_limit_hold",
    "reset_clone_rate_limiter",
    "resolve_clone_rate_limit",
    "resolve_clone_rate_window_seconds",
]
