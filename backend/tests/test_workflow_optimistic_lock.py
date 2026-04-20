"""J2 tests — workflow_run optimistic locking (version column + 409 conflict).

SP-5.6a (2026-04-21): same pool-backed fixture migration as
test_workflow.py.
"""

from __future__ import annotations

import asyncio

import pytest


@pytest.fixture()
async def _wf_db(pg_test_pool, pg_test_dsn, monkeypatch):
    monkeypatch.setenv("OMNISIGHT_DATABASE_URL", pg_test_dsn)
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE workflow_steps, workflow_runs, dag_plans "
            "RESTART IDENTITY CASCADE"
        )
    from backend import db, workflow as wf
    if db._db is not None:
        await db.close()
    await db.init()
    try:
        yield wf
    finally:
        await db.close()
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "TRUNCATE workflow_steps, workflow_runs, dag_plans "
                "RESTART IDENTITY CASCADE"
            )


@pytest.mark.asyncio
async def test_version_starts_at_zero(_wf_db):
    wf = _wf_db
    run = await wf.start("invoke")
    assert run.version == 0
    fetched = await wf.get_run(run.id)
    assert fetched is not None
    assert fetched.version == 0


@pytest.mark.asyncio
async def test_finish_increments_version(_wf_db):
    wf = _wf_db
    run = await wf.start("invoke")
    await wf.finish(run.id, status="completed", expected_version=0)
    fetched = await wf.get_run(run.id)
    assert fetched is not None
    assert fetched.version == 1
    assert fetched.status == "completed"


@pytest.mark.asyncio
async def test_finish_wrong_version_raises(_wf_db):
    wf = _wf_db
    run = await wf.start("invoke")
    with pytest.raises(wf.VersionConflict):
        await wf.finish(run.id, status="completed", expected_version=99)
    fetched = await wf.get_run(run.id)
    assert fetched is not None
    assert fetched.version == 0
    assert fetched.status == "running"


@pytest.mark.asyncio
async def test_cancel_with_version(_wf_db):
    wf = _wf_db
    run = await wf.start("invoke")
    new_ver = await wf.cancel_run(run.id, expected_version=0)
    assert new_ver == 1
    fetched = await wf.get_run(run.id)
    assert fetched is not None
    assert fetched.status == "halted"
    assert fetched.version == 1


@pytest.mark.asyncio
async def test_cancel_wrong_version_raises(_wf_db):
    wf = _wf_db
    run = await wf.start("invoke")
    with pytest.raises(wf.VersionConflict):
        await wf.cancel_run(run.id, expected_version=5)


@pytest.mark.asyncio
async def test_retry_with_version(_wf_db):
    wf = _wf_db
    run = await wf.start("invoke")
    await wf.finish(run.id, status="failed", expected_version=0)
    updated = await wf.retry_run(run.id, expected_version=1)
    assert updated.status == "running"
    assert updated.version == 2


@pytest.mark.asyncio
async def test_retry_wrong_version_raises(_wf_db):
    wf = _wf_db
    run = await wf.start("invoke")
    await wf.finish(run.id, status="failed", expected_version=0)
    with pytest.raises(wf.VersionConflict):
        await wf.retry_run(run.id, expected_version=0)


@pytest.mark.asyncio
async def test_update_metadata_with_version(_wf_db):
    wf = _wf_db
    run = await wf.start("invoke", metadata={"a": 1})
    new_ver = await wf.update_run_metadata(run.id, expected_version=0, metadata={"b": 2})
    assert new_ver == 1
    fetched = await wf.get_run(run.id)
    assert fetched is not None
    assert fetched.metadata == {"a": 1, "b": 2}
    assert fetched.version == 1


@pytest.mark.asyncio
async def test_concurrent_retry_only_one_succeeds(_wf_db):
    """Simulate two concurrent retries — only one should succeed, the other gets VersionConflict."""
    wf = _wf_db
    run = await wf.start("invoke")
    await wf.finish(run.id, status="failed", expected_version=0)

    results: list[str] = []

    async def attempt_retry(label: str):
        try:
            await wf.retry_run(run.id, expected_version=1)
            results.append(f"{label}:ok")
        except wf.VersionConflict:
            results.append(f"{label}:conflict")

    await asyncio.gather(attempt_retry("A"), attempt_retry("B"))

    ok_count = sum(1 for r in results if r.endswith(":ok"))
    conflict_count = sum(1 for r in results if r.endswith(":conflict"))
    assert ok_count == 1, f"exactly one retry must succeed, got {results}"
    assert conflict_count == 1, f"exactly one retry must conflict, got {results}"


@pytest.mark.asyncio
async def test_version_in_replay(_wf_db):
    wf = _wf_db
    run = await wf.start("invoke")
    payload = await wf.replay(run.id)
    assert payload is not None
    assert payload["run"]["version"] == 0
    await wf.finish(run.id, status="completed", expected_version=0)
    payload2 = await wf.replay(run.id)
    assert payload2 is not None
    assert payload2["run"]["version"] == 1


@pytest.mark.asyncio
async def test_version_in_list_runs(_wf_db):
    wf = _wf_db
    run = await wf.start("invoke")
    runs = await wf.list_runs()
    assert len(runs) >= 1
    found = next(r for r in runs if r.id == run.id)
    assert found.version == 0
