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

    SP-8.1 (2026-04-21): loop ceiling bumped from 200 → free-tier
    per_ip + 20 because task #81 raised ``free.per_ip`` from 60 to
    300. Fetching the current budget dynamically means the next
    tuning round won't silently regress this test."""
    from backend.quota import quota_for_plan
    ceiling = quota_for_plan("free").per_ip.capacity + 20
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
