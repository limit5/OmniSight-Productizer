"""Phase 56 tests — durable workflow checkpointing semantics."""

from __future__ import annotations

import os
import tempfile

import pytest


@pytest.fixture()
async def _wf_db(monkeypatch):
    """Fresh SQLite + workflow tables per test."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "wf.db")
        monkeypatch.setenv("OMNISIGHT_DATABASE_PATH", path)
        from backend import config as _cfg
        _cfg.settings.database_path = path
        from backend import db
        db._DB_PATH = db._resolve_db_path()
        await db.init()
        from backend import workflow as wf
        await wf._reset_for_tests()
        try:
            yield wf
        finally:
            await db.close()


@pytest.mark.asyncio
async def test_start_and_get(_wf_db):
    wf = _wf_db
    run = await wf.start("invoke", metadata={"trigger": "manual"})
    assert run.id.startswith("wf-")
    assert run.kind == "invoke"
    assert run.status == "running"
    fetched = await wf.get_run(run.id)
    assert fetched is not None
    assert fetched.metadata == {"trigger": "manual"}


@pytest.mark.asyncio
async def test_step_runs_once(_wf_db):
    wf = _wf_db
    run = await wf.start("invoke")
    side_effects = []

    @wf.step(run, "compile")
    async def compile_step():
        side_effects.append("ran")
        return {"sha": "abc123"}

    out1 = await compile_step()
    out2 = await compile_step()
    out3 = await compile_step()

    assert out1 == out2 == out3 == {"sha": "abc123"}
    assert side_effects == ["ran"], "step body must execute exactly once"


@pytest.mark.asyncio
async def test_resume_after_simulated_crash(_wf_db):
    """The headline use case: process dies after step A, restart, step
    A returns cached output and step B runs for the first time."""
    wf = _wf_db
    run = await wf.start("pipeline_phase", metadata={"phase": "build"})

    a_calls = b_calls = 0

    @wf.step(run, "fetch_repo")
    async def fetch_repo():
        nonlocal a_calls
        a_calls += 1
        return {"sha": "deadbeef"}

    sha_first = await fetch_repo()

    # Simulate the backend crashing here. On restart, the caller
    # reconstructs the same code path (using the same run id and step
    # keys) and re-runs the loop. Because we kept the run open, both
    # decorated functions remain valid; we just re-call them.
    @wf.step(run, "compile")
    async def compile_step():
        nonlocal b_calls
        b_calls += 1
        return {"image": "fw.bin", "sha": sha_first["sha"]}

    sha_after = await fetch_repo()
    img = await compile_step()

    assert sha_first == sha_after, "fetch_repo must return cached output"
    assert a_calls == 1, "fetch_repo body must NOT re-execute"
    assert b_calls == 1, "compile must run exactly once"
    assert img["sha"] == "deadbeef"


@pytest.mark.asyncio
async def test_step_failure_recorded(_wf_db):
    wf = _wf_db
    run = await wf.start("invoke")

    @wf.step(run, "broken")
    async def broken():
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        await broken()

    steps = await wf.list_steps(run.id)
    assert len(steps) == 1
    assert steps[0].error and "RuntimeError" in steps[0].error
    assert not steps[0].is_done


@pytest.mark.asyncio
async def test_in_flight_listed_after_finish_completed_dropped(_wf_db):
    wf = _wf_db
    a = await wf.start("invoke")
    b = await wf.start("pipeline_phase")
    await wf.finish(a.id, status="completed")
    in_flight = await wf.list_in_flight_on_startup()
    ids = [r.id for r in in_flight]
    assert b.id in ids and a.id not in ids


@pytest.mark.asyncio
async def test_replay_returns_run_and_steps(_wf_db):
    wf = _wf_db
    run = await wf.start("invoke")

    @wf.step(run, "x")
    async def s():
        return 7

    await s()
    payload = await wf.replay(run.id)
    assert payload is not None
    assert payload["run"]["id"] == run.id
    assert payload["in_flight"] is True
    assert len(payload["steps"]) == 1
    assert payload["steps"][0]["output"] == 7
    assert payload["steps"][0]["is_done"] is True


@pytest.mark.asyncio
async def test_replay_unknown_returns_none(_wf_db):
    wf = _wf_db
    payload = await wf.replay("wf-not-exist")
    assert payload is None
