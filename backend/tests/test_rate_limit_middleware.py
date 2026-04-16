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
    """After exhausting the per-IP budget, requests get 429."""
    got_429 = False
    for _ in range(200):
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
