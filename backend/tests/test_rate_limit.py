"""I9 — Rate limiter unit tests (in-memory + legacy compat)."""

from __future__ import annotations

import pytest

from backend.rate_limit import InMemoryLimiter, get_limiter, reset_limiters


@pytest.fixture(autouse=True)
def _clean():
    reset_limiters()
    yield
    reset_limiters()


def test_allows_up_to_capacity():
    limiter = InMemoryLimiter()
    for _ in range(3):
        ok, _ = limiter.allow("k", capacity=3, window_seconds=60.0)
        assert ok
    ok, wait = limiter.allow("k", capacity=3, window_seconds=60.0)
    assert not ok
    assert wait > 0


def test_refill_restores_tokens():
    limiter = InMemoryLimiter()
    limiter.allow("k", 2, 10.0)
    limiter.allow("k", 2, 10.0)
    ok, _ = limiter.allow("k", 2, 10.0)
    assert not ok

    bucket = limiter._buckets["k"]
    bucket.last_refill -= 10.0

    ok, _ = limiter.allow("k", 2, 10.0)
    assert ok


def test_independent_keys():
    limiter = InMemoryLimiter()
    ok, _ = limiter.allow("a", 1, 60.0)
    assert ok
    ok, _ = limiter.allow("a", 1, 60.0)
    assert not ok
    ok, _ = limiter.allow("b", 1, 60.0)
    assert ok


def test_reset_key():
    limiter = InMemoryLimiter()
    limiter.allow("k", 1, 60.0)
    ok, _ = limiter.allow("k", 1, 60.0)
    assert not ok
    limiter.reset("k")
    ok, _ = limiter.allow("k", 1, 60.0)
    assert ok


def test_max_keys_eviction():
    limiter = InMemoryLimiter(max_keys=4)
    for i in range(10):
        limiter.allow(f"ip-{i}", 5, 60.0)
    assert len(limiter._buckets) <= 4


def test_clear():
    limiter = InMemoryLimiter()
    limiter.allow("a", 5, 60.0)
    limiter.allow("b", 5, 60.0)
    limiter.clear()
    assert len(limiter._buckets) == 0


def test_get_limiter_returns_in_memory_by_default():
    limiter = get_limiter()
    assert isinstance(limiter, InMemoryLimiter)


def test_legacy_ip_limiter():
    from backend.rate_limit import ip_limiter
    lim = ip_limiter()
    ok, _ = lim.allow("127.0.0.1")
    assert ok


def test_legacy_email_limiter():
    from backend.rate_limit import email_limiter
    lim = email_limiter()
    ok, _ = lim.allow("test@example.com")
    assert ok


def test_three_dimension_rate_limiting():
    """I9: Verify per-IP, per-user, per-tenant buckets are independent."""
    limiter = InMemoryLimiter()
    ok, _ = limiter.allow("api:ip:1.2.3.4", capacity=2, window_seconds=60.0)
    assert ok
    ok, _ = limiter.allow("api:user:u-1", capacity=2, window_seconds=60.0)
    assert ok
    ok, _ = limiter.allow("api:tenant:t-1", capacity=2, window_seconds=60.0)
    assert ok

    limiter.allow("api:ip:1.2.3.4", 2, 60.0)
    ok, _ = limiter.allow("api:ip:1.2.3.4", 2, 60.0)
    assert not ok, "IP bucket should be exhausted"

    ok, _ = limiter.allow("api:user:u-1", 2, 60.0)
    assert ok, "User bucket should still have tokens"
    ok, _ = limiter.allow("api:tenant:t-1", 2, 60.0)
    assert ok, "Tenant bucket should still have tokens"
