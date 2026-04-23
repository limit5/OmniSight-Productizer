"""Q.3-SUB-1 (#297) — workflow_run cross-device SSE sync.

Unit level:
  - ``test_workflow_emit_on_update`` — each call to finish / cancel
    / retry / update_run_metadata emits exactly one
    ``workflow_updated`` SSE event carrying the post-bump
    ``status`` + ``version``. A flaky bus must NOT fail the
    workflow mutation.

Integration level:
  - ``test_workflow_sse_cross_device`` — with two parallel
    :func:`backend.events.bus.subscribe` listeners (simulating
    two user sessions), a ``POST /workflow/runs/{id}/cancel`` on
    session A drives a ``workflow_updated`` payload onto session
    B's queue with the same ``run_id`` / ``status`` / ``version``
    the HTTP handler returned. This is the contract the
    frontend ``useWorkflows()`` hook consumes to patch its list
    state without a follow-up GET.

Audit evidence: ``docs/design/multi-device-state-sync.md`` Path 3.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from backend.events import bus as _bus


@pytest.fixture()
async def _wf_db(pg_test_pool, pg_test_dsn, monkeypatch):
    """Mirror of the fixture in test_workflow.py — keep the TRUNCATE
    list narrow so we don't stomp on other suites' data."""
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
async def test_workflow_emit_on_update(_wf_db, monkeypatch):
    """Every workflow_runs mutation path (finish, cancel_run,
    retry_run, update_run_metadata) must emit exactly one
    ``workflow_updated`` SSE event with the post-bump status + version.
    """
    wf = _wf_db
    from backend import events as _events

    captured: list[dict] = []
    real_emit = _events.emit_workflow_updated

    def _record(*args, **kwargs):
        captured.append({"args": args, "kwargs": kwargs})
        real_emit(*args, **kwargs)

    # The workflow module re-imports emit_workflow_updated inside
    # ``_emit_workflow_updated_safe`` at call time, so patching the
    # events module namespace is sufficient.
    monkeypatch.setattr(_events, "emit_workflow_updated", _record)

    # ── start() fires a "running" emit ──
    run = await wf.start("invoke", metadata={"trigger": "test"})
    assert len(captured) == 1, "start() must emit exactly once"
    assert captured[0]["args"][0] == run.id
    assert captured[0]["args"][1] == "running"
    assert captured[0]["args"][2] == 0  # initial version
    assert captured[0]["kwargs"]["kind"] == "invoke"

    # ── update_run_metadata() emits with unchanged status ──
    captured.clear()
    new_ver = await wf.update_run_metadata(run.id, 0, {"tag": "v1"})
    assert len(captured) == 1
    assert captured[0]["args"][1] == "running", (
        "metadata-only patch must re-emit the existing status so "
        "consumers can still refresh their etag"
    )
    assert captured[0]["args"][2] == new_ver
    assert new_ver == 1

    # ── finish() emits with the terminal status ──
    captured.clear()
    await wf.finish(run.id, status="completed", expected_version=new_ver)
    assert len(captured) == 1
    assert captured[0]["args"][1] == "completed"
    assert captured[0]["args"][2] == new_ver + 1

    # ── cancel_run() on a fresh run emits "halted" ──
    run2 = await wf.start("invoke")
    captured.clear()
    new_ver2 = await wf.cancel_run(run2.id, 0)
    assert len(captured) == 1
    assert captured[0]["args"][0] == run2.id
    assert captured[0]["args"][1] == "halted"
    assert captured[0]["args"][2] == new_ver2

    # ── retry_run() on a failed run emits "running" with kind carried ──
    run3 = await wf.start("invoke")
    await wf.finish(run3.id, status="failed", expected_version=0)
    captured.clear()
    restored = await wf.retry_run(run3.id, expected_version=1)
    assert len(captured) == 1
    assert captured[0]["args"][0] == run3.id
    assert captured[0]["args"][1] == "running"
    assert captured[0]["args"][2] == restored.version
    assert captured[0]["kwargs"]["kind"] == "invoke"


@pytest.mark.asyncio
async def test_workflow_emit_failure_does_not_break_mutation(
    _wf_db, monkeypatch,
):
    """A flaky SSE bus / Redis outage must NEVER fail a workflow
    mutation — the truth is in PG, the emit is latency-optimisation."""
    wf = _wf_db
    from backend import events as _events

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated SSE bus outage")

    monkeypatch.setattr(_events, "emit_workflow_updated", _boom)

    # start() still returns a valid run even though the emit blew up.
    run = await wf.start("invoke")
    assert run.id.startswith("wf-")
    persisted = await wf.get_run(run.id)
    assert persisted is not None, "PG row must exist even if SSE fails"

    # finish() must not propagate the emit failure.
    await wf.finish(run.id, status="completed", expected_version=0)
    again = await wf.get_run(run.id)
    assert again is not None
    assert again.status == "completed"


@pytest.mark.asyncio
async def test_workflow_sse_cross_device(client):
    """Two bus subscribers (simulating two user sessions on the same
    user account); the HTTP POST /workflow/runs/{id}/cancel from
    session A must reach session B's queue with the post-bump payload
    the frontend ``useWorkflows()`` hook patches into state.
    """
    from backend import workflow as wf

    run = await wf.start("invoke")
    start_version = (await wf.get_run(run.id)).version

    # Session A — the one firing the mutation. It still gets the event,
    # but we mainly assert on B to prove cross-device fan-out.
    q_a = _bus.subscribe(tenant_id=None)
    q_b = _bus.subscribe(tenant_id=None)
    try:
        res = await client.post(
            f"/api/v1/workflow/runs/{run.id}/cancel",
            headers={"If-Match": str(start_version)},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["status"] == "halted"
        new_version = body["version"]
        assert new_version == start_version + 1

        # The start() emit fired BEFORE we subscribed, so only the
        # cancel emit must be on the queues. Drain with a timeout
        # and filter for our event type so ambient chatter (e.g. a
        # task_update leak from another fixture) doesn't masquerade
        # as the Q.3-SUB-1 payload.
        async def _drain_for_event(queue, target_run_id, timeout=2.0):
            deadline = asyncio.get_event_loop().time() + timeout
            while asyncio.get_event_loop().time() < deadline:
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=0.3)
                except asyncio.TimeoutError:
                    continue
                if msg.get("event") != "workflow_updated":
                    continue
                data = json.loads(msg["data"])
                if data.get("run_id") != target_run_id:
                    continue
                return data
            return None

        data_b = await _drain_for_event(q_b, run.id)
        assert data_b is not None, (
            "session B must receive the cancel's workflow_updated event"
        )
        assert data_b["status"] == "halted"
        assert data_b["version"] == new_version
        assert data_b["_broadcast_scope"] == "user", (
            "workflow_updated must carry broadcast_scope=user so Q.4 "
            "(#298) can enforce per-user delivery without a payload change"
        )

        data_a = await _drain_for_event(q_a, run.id)
        assert data_a is not None, "originator must also see its own event"
        assert data_a["status"] == "halted"
        assert data_a["version"] == new_version
    finally:
        _bus.unsubscribe(q_a)
        _bus.unsubscribe(q_b)


@pytest.mark.asyncio
async def test_workflow_sse_scope_is_user(_wf_db):
    """Lock the ``broadcast_scope='user'`` payload contract — the
    frontend filter (and the eventual Q.4 #298 server enforcement)
    both rely on this label being present on every emit."""
    wf = _wf_db
    q = _bus.subscribe(tenant_id=None)
    try:
        run = await wf.start("invoke")
        # Drain until we find the start emit.
        msg = None
        for _ in range(10):
            try:
                m = await asyncio.wait_for(q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                break
            if m.get("event") == "workflow_updated":
                data = json.loads(m["data"])
                if data.get("run_id") == run.id:
                    msg = data
                    break
        assert msg is not None, "start() must publish a workflow_updated"
        assert msg["_broadcast_scope"] == "user"
        assert msg["run_id"] == run.id
        assert msg["status"] == "running"
        assert msg["kind"] == "invoke"
        assert msg["version"] == 0
    finally:
        _bus.unsubscribe(q)
