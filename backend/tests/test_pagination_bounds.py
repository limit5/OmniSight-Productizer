"""Fix-B B1+B3 — every list endpoint rejects oversized limit."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [
    "/api/v1/decisions?limit=99999",
    "/api/v1/runtime/logs?limit=99999",
    "/api/v1/runtime/notifications?limit=99999",
    "/api/v1/auto-decisions?limit=99999",
    "/api/v1/audit?limit=99999",
    "/api/v1/runtime/simulations?limit=99999",
    "/api/v1/workflow/runs?limit=99999",
    "/api/v1/artifacts?limit=99999",
    "/api/v1/tasks/handoffs/recent?limit=99999",
])
async def test_oversized_limit_rejected(client, path):
    r = await client.get(path)
    assert r.status_code == 422, f"{path} returned {r.status_code}"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [
    "/api/v1/decisions?limit=0",
    "/api/v1/runtime/logs?limit=-5",
    "/api/v1/audit?limit=0",
])
async def test_non_positive_limit_rejected(client, path):
    r = await client.get(path)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_in_bound_limit_passes(client):
    r = await client.get("/api/v1/decisions?limit=50")
    assert r.status_code == 200
    r = await client.get("/api/v1/runtime/logs?limit=10")
    assert r.status_code == 200
