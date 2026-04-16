"""I9 — Redis-backed token-bucket rate limiter with in-memory fallback.

Three dimensions tracked independently:
  per-IP      (login brute-force + general API)
  per-user    (authenticated request budgets)
  per-tenant  (aggregate tenant throughput cap)

Uses Redis when OMNISIGHT_REDIS_URL is set; otherwise falls back to the
in-process token bucket (single-worker only).  The Redis implementation
uses a Lua script for atomic token-bucket logic — safe under concurrency
and across multiple Uvicorn workers.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Protocol

logger = logging.getLogger(__name__)


# ── Abstract interface ───────────────────────────────────────────


class RateLimiter(Protocol):
    def allow(self, key: str, capacity: int, window_seconds: float) -> tuple[bool, float]:
        """Try to consume one token.  Returns (allowed, retry_after_s)."""
        ...

    def reset(self, key: str) -> None: ...
    def clear(self) -> None: ...


# ── In-memory implementation (single-worker fallback) ────────────


@dataclass
class _Bucket:
    tokens: float
    last_refill: float


class InMemoryLimiter:
    """Thread-safe in-memory token bucket.

    Each key gets its own bucket parameterised by the caller-supplied
    capacity and window.  Tokens refill at a constant rate; a request
    consumes one token.
    """

    def __init__(self, max_keys: int = 16384) -> None:
        self._buckets: dict[str, _Bucket] = {}
        self._meta: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()
        self._max_keys = max_keys

    def _refill(self, bucket: _Bucket, capacity: int, window: float, now: float) -> None:
        elapsed = now - bucket.last_refill
        rate = capacity / window if window > 0 else capacity
        bucket.tokens = min(capacity, bucket.tokens + elapsed * rate)
        bucket.last_refill = now

    def allow(self, key: str, capacity: int, window_seconds: float) -> tuple[bool, float]:
        now = time.time()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                if len(self._buckets) >= self._max_keys:
                    oldest_key = min(
                        self._buckets,
                        key=lambda k: self._buckets[k].last_refill,
                    )
                    del self._buckets[oldest_key]
                    self._meta.pop(oldest_key, None)
                bucket = _Bucket(tokens=float(capacity), last_refill=now)
                self._buckets[key] = bucket
                self._meta[key] = (capacity, window_seconds)
            else:
                stored_cap, stored_win = self._meta.get(key, (capacity, window_seconds))
                if stored_cap != capacity or stored_win != window_seconds:
                    self._meta[key] = (capacity, window_seconds)

            self._refill(bucket, capacity, window_seconds, now)

            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return True, 0.0

            deficit = 1.0 - bucket.tokens
            rate = capacity / window_seconds if window_seconds > 0 else capacity
            wait = deficit / rate if rate > 0 else window_seconds
            return False, wait

    def reset(self, key: str) -> None:
        with self._lock:
            self._buckets.pop(key, None)
            self._meta.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._buckets.clear()
            self._meta.clear()


# ── Redis implementation ─────────────────────────────────────────

_TOKEN_BUCKET_LUA = """
local key       = KEYS[1]
local capacity  = tonumber(ARGV[1])
local window    = tonumber(ARGV[2])
local now       = tonumber(ARGV[3])

local data = redis.call('HMGET', key, 'tokens', 'last_refill')
local tokens     = tonumber(data[1])
local last_refill = tonumber(data[2])

if tokens == nil then
    tokens = capacity
    last_refill = now
end

-- refill
local elapsed = now - last_refill
local rate = capacity / window
tokens = math.min(capacity, tokens + elapsed * rate)
last_refill = now

local allowed = 0
local wait = 0

if tokens >= 1 then
    tokens = tokens - 1
    allowed = 1
else
    local deficit = 1 - tokens
    wait = deficit / rate
end

redis.call('HSET', key, 'tokens', tostring(tokens), 'last_refill', tostring(last_refill))
redis.call('EXPIRE', key, math.ceil(window) + 60)

return {allowed, tostring(wait)}
"""


class RedisLimiter:
    """Redis-backed token bucket using a Lua script for atomicity."""

    def __init__(self, redis_url: str) -> None:
        import redis as _redis
        self._pool = _redis.ConnectionPool.from_url(redis_url, decode_responses=True)
        self._client = _redis.Redis(connection_pool=self._pool)
        self._script = self._client.register_script(_TOKEN_BUCKET_LUA)
        self._prefix = "omnisight:rl:"

    def allow(self, key: str, capacity: int, window_seconds: float) -> tuple[bool, float]:
        full_key = self._prefix + key
        now = time.time()
        result = self._script(
            keys=[full_key],
            args=[capacity, window_seconds, now],
        )
        allowed = int(result[0]) == 1
        wait = float(result[1])
        return allowed, wait

    def reset(self, key: str) -> None:
        self._client.delete(self._prefix + key)

    def clear(self) -> None:
        cursor = "0"
        while True:
            cursor, keys = self._client.scan(cursor=cursor, match=self._prefix + "*", count=500)
            if keys:
                self._client.delete(*keys)
            if cursor == 0 or cursor == "0":
                break


# ── Singleton management ─────────────────────────────────────────

_limiter: RateLimiter | None = None

# Legacy K2 singletons (login-specific, kept for backward compat)
_ip_limiter_legacy: _LegacyTokenBucketLimiter | None = None
_email_limiter_legacy: _LegacyTokenBucketLimiter | None = None


def _env_int(name: str, default: int, lo: int = 1, hi: int = 100_000) -> int:
    raw = (os.environ.get(name) or str(default)).strip()
    try:
        return max(lo, min(hi, int(raw)))
    except ValueError:
        return default


def _env_float(name: str, default: float, lo: float = 1.0, hi: float = 86400.0) -> float:
    raw = (os.environ.get(name) or str(default)).strip()
    try:
        return max(lo, min(hi, float(raw)))
    except ValueError:
        return default


def get_limiter() -> RateLimiter:
    """Return the global rate limiter (Redis or in-memory)."""
    global _limiter
    if _limiter is not None:
        return _limiter

    redis_url = (os.environ.get("OMNISIGHT_REDIS_URL") or "").strip()
    if redis_url:
        try:
            _limiter = RedisLimiter(redis_url)
            logger.info("I9: Redis rate limiter connected (%s)", redis_url.split("@")[-1])
            return _limiter
        except Exception as exc:
            logger.warning("I9: Redis unavailable (%s), falling back to in-memory", exc)

    _limiter = InMemoryLimiter()
    logger.info("I9: Using in-memory rate limiter (single-worker only)")
    return _limiter


# ── Legacy K2 compatibility layer ────────────────────────────────
# The login endpoint uses ip_limiter() / email_limiter() with fixed
# capacity.  We keep that API but delegate to the unified limiter.


class _LegacyTokenBucketLimiter:
    """Wraps the unified limiter with fixed capacity/window for K2 compat."""

    def __init__(self, prefix: str, capacity: int, refill_seconds: float) -> None:
        self._prefix = prefix
        self.capacity = capacity
        self.refill_seconds = refill_seconds

    def allow(self, key: str) -> tuple[bool, float]:
        return get_limiter().allow(
            f"{self._prefix}:{key}",
            self.capacity,
            self.refill_seconds,
        )

    def reset(self, key: str) -> None:
        get_limiter().reset(f"{self._prefix}:{key}")

    def clear(self) -> None:
        pass


def ip_limiter() -> _LegacyTokenBucketLimiter:
    global _ip_limiter_legacy
    if _ip_limiter_legacy is None:
        _ip_limiter_legacy = _LegacyTokenBucketLimiter(
            prefix="login:ip",
            capacity=_env_int("OMNISIGHT_LOGIN_IP_RATE", 5),
            refill_seconds=_env_float("OMNISIGHT_LOGIN_IP_WINDOW_S", 60.0),
        )
    return _ip_limiter_legacy


def email_limiter() -> _LegacyTokenBucketLimiter:
    global _email_limiter_legacy
    if _email_limiter_legacy is None:
        _email_limiter_legacy = _LegacyTokenBucketLimiter(
            prefix="login:email",
            capacity=_env_int("OMNISIGHT_LOGIN_EMAIL_RATE", 10),
            refill_seconds=_env_float("OMNISIGHT_LOGIN_EMAIL_WINDOW_S", 3600.0),
        )
    return _email_limiter_legacy


def reset_limiters() -> None:
    """For tests — wipe all state."""
    global _limiter, _ip_limiter_legacy, _email_limiter_legacy
    if _limiter:
        _limiter.clear()
    _limiter = None
    _ip_limiter_legacy = None
    _email_limiter_legacy = None
