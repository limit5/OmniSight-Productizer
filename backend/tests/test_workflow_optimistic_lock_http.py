"""J2 HTTP-level tests — workflow_run optimistic locking via REST endpoints."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_list_runs_includes_version(client):
    res = await client.post("/api/v1/workflow/runs", json={"kind": "invoke"})
    listing = await client.get("/api/v1/workflow/runs")
    assert listing.status_code == 200
    runs = listing.json()["runs"]
    if runs:
        assert "version" in runs[0]


@pytest.mark.asyncio
async def test_retry_requires_if_match(client):
    from backend import workflow as wf
    run = await wf.start("invoke")
    await wf.finish(run.id, status="failed")
    res = await client.post(f"/api/v1/workflow/runs/{run.id}/retry")
    assert res.status_code == 428


@pytest.mark.asyncio
async def test_retry_with_correct_version(client):
    from backend import workflow as wf
    run = await wf.start("invoke")
    await wf.finish(run.id, status="failed")
    fetched = await wf.get_run(run.id)
    res = await client.post(
        f"/api/v1/workflow/runs/{run.id}/retry",
        headers={"If-Match": str(fetched.version)},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "running"
    assert body["version"] == fetched.version + 1


@pytest.mark.asyncio
async def test_retry_with_wrong_version_returns_409(client):
    from backend import workflow as wf
    run = await wf.start("invoke")
    await wf.finish(run.id, status="failed")
    res = await client.post(
        f"/api/v1/workflow/runs/{run.id}/retry",
        headers={"If-Match": "999"},
    )
    assert res.status_code == 409


@pytest.mark.asyncio
async def test_cancel_with_correct_version(client):
    from backend import workflow as wf
    run = await wf.start("invoke")
    res = await client.post(
        f"/api/v1/workflow/runs/{run.id}/cancel",
        headers={"If-Match": "0"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "halted"
    assert body["version"] == 1


@pytest.mark.asyncio
async def test_cancel_wrong_version_returns_409(client):
    from backend import workflow as wf
    run = await wf.start("invoke")
    res = await client.post(
        f"/api/v1/workflow/runs/{run.id}/cancel",
        headers={"If-Match": "5"},
    )
    assert res.status_code == 409


@pytest.mark.asyncio
async def test_cancel_non_running_returns_400(client):
    from backend import workflow as wf
    run = await wf.start("invoke")
    await wf.finish(run.id, status="completed")
    res = await client.post(
        f"/api/v1/workflow/runs/{run.id}/cancel",
        headers={"If-Match": "1"},
    )
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_patch_metadata_with_version(client):
    from backend import workflow as wf
    run = await wf.start("invoke", metadata={"a": 1})
    res = await client.patch(
        f"/api/v1/workflow/runs/{run.id}",
        json={"metadata": {"b": 2}},
        headers={"If-Match": "0"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["version"] == 1
    fetched = await wf.get_run(run.id)
    assert fetched.metadata == {"a": 1, "b": 2}


@pytest.mark.asyncio
async def test_patch_wrong_version_returns_409(client):
    from backend import workflow as wf
    run = await wf.start("invoke")
    res = await client.patch(
        f"/api/v1/workflow/runs/{run.id}",
        json={"metadata": {"x": 1}},
        headers={"If-Match": "42"},
    )
    assert res.status_code == 409


@pytest.mark.asyncio
async def test_concurrent_retry_http_one_wins(client):
    """Two concurrent retry calls — one succeeds, one gets 409."""
    import asyncio
    from backend import workflow as wf
    run = await wf.start("invoke")
    await wf.finish(run.id, status="failed")

    async def attempt():
        return await client.post(
            f"/api/v1/workflow/runs/{run.id}/retry",
            headers={"If-Match": "1"},
        )

    r1, r2 = await asyncio.gather(attempt(), attempt())
    codes = sorted([r1.status_code, r2.status_code])
    assert codes == [200, 409], f"expected one 200 and one 409, got {codes}"
