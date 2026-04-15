"""K2 — Token-bucket rate limiter unit tests."""

from __future__ import annotations

import time

import pytest

from backend.rate_limit import TokenBucketLimiter, reset_limiters


@pytest.fixture(autouse=True)
def _clean():
    reset_limiters()
    yield
    reset_limiters()


def test_allows_up_to_capacity():
    limiter = TokenBucketLimiter(capacity=3, refill_seconds=60.0)
    for _ in range(3):
        ok, _ = limiter.allow("k")
        assert ok
    ok, wait = limiter.allow("k")
    assert not ok
    assert wait > 0


def test_refill_restores_tokens(monkeypatch):
    limiter = TokenBucketLimiter(capacity=2, refill_seconds=10.0)
    limiter.allow("k")
    limiter.allow("k")
    ok, _ = limiter.allow("k")
    assert not ok

    bucket = limiter._buckets["k"]
    bucket.last_refill -= 10.0

    ok, _ = limiter.allow("k")
    assert ok


def test_independent_keys():
    limiter = TokenBucketLimiter(capacity=1, refill_seconds=60.0)
    ok, _ = limiter.allow("a")
    assert ok
    ok, _ = limiter.allow("a")
    assert not ok
    ok, _ = limiter.allow("b")
    assert ok


def test_reset_key():
    limiter = TokenBucketLimiter(capacity=1, refill_seconds=60.0)
    limiter.allow("k")
    ok, _ = limiter.allow("k")
    assert not ok
    limiter.reset("k")
    ok, _ = limiter.allow("k")
    assert ok


def test_max_keys_eviction():
    limiter = TokenBucketLimiter(capacity=5, refill_seconds=60.0, _max_keys=4)
    for i in range(10):
        limiter.allow(f"ip-{i}")
    assert len(limiter._buckets) <= 4


def test_clear():
    limiter = TokenBucketLimiter(capacity=5, refill_seconds=60.0)
    limiter.allow("a")
    limiter.allow("b")
    limiter.clear()
    assert len(limiter._buckets) == 0
