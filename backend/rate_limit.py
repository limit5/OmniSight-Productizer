"""K2 — In-process token-bucket rate limiter.

Two dimensions tracked independently:
  per-IP    default 5 requests / 60s   (login brute-force)
  per-email default 10 requests / 3600s (credential-stuffing)

Future: swap the in-memory store for Redis when running multiple workers.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class _Bucket:
    tokens: float
    last_refill: float


@dataclass
class TokenBucketLimiter:
    """Thread-safe in-memory token bucket.

    Each key (IP or email) gets its own bucket. Tokens refill at a
    constant rate; a request consumes one token. When the bucket is
    empty the request is denied and the caller gets a retry-after hint.
    """
    capacity: int
    refill_seconds: float
    _buckets: dict[str, _Bucket] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _max_keys: int = 8192

    def _refill(self, bucket: _Bucket, now: float) -> None:
        elapsed = now - bucket.last_refill
        rate = self.capacity / self.refill_seconds
        bucket.tokens = min(self.capacity, bucket.tokens + elapsed * rate)
        bucket.last_refill = now

    def allow(self, key: str) -> tuple[bool, float]:
        """Try to consume one token. Returns (allowed, retry_after_s)."""
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
                bucket = _Bucket(tokens=float(self.capacity), last_refill=now)
                self._buckets[key] = bucket

            self._refill(bucket, now)

            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return True, 0.0

            deficit = 1.0 - bucket.tokens
            rate = self.capacity / self.refill_seconds
            wait = deficit / rate if rate > 0 else self.refill_seconds
            return False, wait

    def reset(self, key: str) -> None:
        with self._lock:
            self._buckets.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._buckets.clear()


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


_ip_limiter: TokenBucketLimiter | None = None
_email_limiter: TokenBucketLimiter | None = None


def ip_limiter() -> TokenBucketLimiter:
    global _ip_limiter
    if _ip_limiter is None:
        _ip_limiter = TokenBucketLimiter(
            capacity=_env_int("OMNISIGHT_LOGIN_IP_RATE", 5),
            refill_seconds=_env_float("OMNISIGHT_LOGIN_IP_WINDOW_S", 60.0),
        )
    return _ip_limiter


def email_limiter() -> TokenBucketLimiter:
    global _email_limiter
    if _email_limiter is None:
        _email_limiter = TokenBucketLimiter(
            capacity=_env_int("OMNISIGHT_LOGIN_EMAIL_RATE", 10),
            refill_seconds=_env_float("OMNISIGHT_LOGIN_EMAIL_WINDOW_S", 3600.0),
        )
    return _email_limiter


def reset_limiters() -> None:
    """For tests — wipe all state."""
    global _ip_limiter, _email_limiter
    if _ip_limiter:
        _ip_limiter.clear()
    if _email_limiter:
        _email_limiter.clear()
    _ip_limiter = None
    _email_limiter = None
