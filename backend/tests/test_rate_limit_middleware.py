"""I9 — Rate limit middleware integration tests."""

from __future__ import annotations

import pytest

from backend.rate_limit import reset_limiters


@pytest.fixture(autouse=True)
def _clean():
    reset_limiters()
    yield
    reset_limiters()


@pytest.mark.asyncio
async def test_health_exempt_from_rate_limit(client):
    """Health endpoint should never be rate-limited."""
    for _ in range(200):
        r = await client.get("/api/v1/health")
        assert r.status_code != 429


@pytest.mark.asyncio
async def test_ip_rate_limit_triggers(client):
    """After exhausting the per-IP budget, requests get 429.

    SP-8.1 / 8.1c: loop ceiling reads the live per_ip budget and
    scales 2× to absorb in-window refill. Token bucket refills at
    ``capacity/window`` per second; a fast sequential test loop
    drains tokens faster than they refill, but not so much faster
    that a 1× ceiling is guaranteed to hit 429 — once per_ip got
    bumped to 1200 (20/sec refill), a 1220-iteration loop that
    takes ~6s only nets ~1100 drained tokens, leaving the bucket
    non-empty and the test flaky. 2× is the safe absorption factor
    across the tuning range."""
    from backend.quota import quota_for_plan
    ceiling = quota_for_plan("free").per_ip.capacity * 2 + 20
    got_429 = False
    for _ in range(ceiling):
        r = await client.get("/api/v1/agents")
        if r.status_code == 429:
            got_429 = True
            assert "retry-after" in r.headers
            break
    assert got_429, "Expected 429 after exceeding per-IP rate limit"


@pytest.mark.asyncio
async def test_rate_limit_headers_present(client):
    """Successful responses should include plan header."""
    r = await client.get("/api/v1/agents")
    if r.status_code != 429:
        assert "x-ratelimit-plan" in r.headers
