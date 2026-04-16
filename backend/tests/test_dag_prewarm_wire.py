"""Phase 67-C S2 — DAG router × pre-warm hooks."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from backend.dag_schema import DAG, Task


def _dag(dag_id: str, tasks: list[Task] | None = None) -> dict:
    d = DAG(dag_id=dag_id, tasks=tasks or [
        Task(task_id="A", description="compile",
             required_tier="t1", toolchain="cmake",
             expected_output="build/a.bin"),
        Task(task_id="B", description="flash",
             required_tier="t3", toolchain="flash_board",
             expected_output="logs/b.log",
             inputs=["build/a.bin"], depends_on=["A"]),
    ])
    return d.model_dump()


def _bad_dag(dag_id: str) -> dict:
    d = DAG(dag_id=dag_id, tasks=[
        Task(task_id="X", description="bad",
             required_tier="t1", toolchain="flash_board",  # tier violation
             expected_output="logs/x.log"),
    ])
    return d.model_dump()


@dataclass
class _FakeInfo:
    agent_id: str
    container_id: str = "cid"


@pytest.fixture(autouse=True)
def _reset_prewarm():
    from backend import sandbox_prewarm as pw
    pw._reset_for_tests()
    yield
    pw._reset_for_tests()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pre-warm is opt-in
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_prewarm_disabled_by_default(client, monkeypatch):
    monkeypatch.delenv("OMNISIGHT_PREWARM_ENABLED", raising=False)
    from backend import sandbox_prewarm as pw

    called = {"n": 0}

    async def fake_prewarm(dag, workspace, **kw):
        called["n"] += 1
        return []
    monkeypatch.setattr(pw, "prewarm_for", fake_prewarm)

    r = await client.post("/api/v1/dag", json={"dag": _dag("REQ-noprewarm")})
    assert r.status_code == 200
    # Give the event loop a tick for any create_task to fire.
    await asyncio.sleep(0.05)
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_prewarm_fires_when_enabled_on_validated_submit(
    client, monkeypatch,
):
    monkeypatch.setenv("OMNISIGHT_PREWARM_ENABLED", "true")

    seen = {"dag_id": None}

    async def fake_prewarm(dag, workspace, **kw):
        seen["dag_id"] = dag.dag_id
        return []

    from backend import sandbox_prewarm as pw
    monkeypatch.setattr(pw, "prewarm_for", fake_prewarm)

    r = await client.post(
        "/api/v1/dag", json={"dag": _dag("REQ-warm-me")},
    )
    assert r.status_code == 200
    # Fire-and-forget → give the background task a tick.
    await asyncio.sleep(0.05)
    assert seen["dag_id"] == "REQ-warm-me"


@pytest.mark.asyncio
async def test_prewarm_skipped_when_validation_fails(client, monkeypatch):
    """Validation-fail path (status=failed) must NOT pre-warm, even
    with the env on."""
    monkeypatch.setenv("OMNISIGHT_PREWARM_ENABLED", "true")
    called = {"n": 0}

    async def fake_prewarm(dag, workspace, **kw):
        called["n"] += 1

    from backend import sandbox_prewarm as pw
    monkeypatch.setattr(pw, "prewarm_for", fake_prewarm)

    r = await client.post(
        "/api/v1/dag", json={"dag": _bad_dag("REQ-bad-no-prewarm")},
    )
    assert r.status_code == 422
    await asyncio.sleep(0.05)
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_prewarm_swallows_exceptions(client, monkeypatch, caplog):
    """Pre-warm failure must not surface to the caller."""
    import logging
    monkeypatch.setenv("OMNISIGHT_PREWARM_ENABLED", "true")

    async def boom(dag, workspace, **kw):
        raise RuntimeError("docker down")

    from backend import sandbox_prewarm as pw
    monkeypatch.setattr(pw, "prewarm_for", boom)

    caplog.set_level(logging.DEBUG, logger="backend.routers.dag")
    r = await client.post("/api/v1/dag", json={"dag": _dag("REQ-swallow")})
    # Submit still succeeded despite the pre-warm explosion.
    assert r.status_code == 200
    await asyncio.sleep(0.05)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Mutation cancels pre-warms
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_mutate_path_cancels_prior_prewarms(client, monkeypatch):
    """When the router enters the mutation loop it must call
    cancel_all BEFORE run_mutation_loop so stale speculation doesn't
    burn the lifetime budget of a now-replanned DAG."""
    from backend import sandbox_prewarm as pw
    from backend.routers import dag as dag_router

    cancel_calls: list[str] = []

    async def fake_cancel(*, stopper=None, reason: str = "dag_mutated",
                          tenant_id=None) -> int:
        cancel_calls.append(reason)
        return 0
    monkeypatch.setattr(pw, "cancel_all", fake_cancel)

    # Make the orchestrator recover successfully so we observe the
    # cancel-before-mutate sequence.
    good = DAG.model_validate(_dag("REQ-mut-cancel"))

    async def fake_ask(s, u):
        return (good.model_dump_json(), 100)
    monkeypatch.setattr(dag_router, "_default_ask_fn", fake_ask)

    r = await client.post(
        "/api/v1/dag",
        json={"dag": _bad_dag("REQ-mut-cancel"), "mutate": True},
    )
    assert r.status_code == 200
    assert cancel_calls == ["dag_mutated"]


@pytest.mark.asyncio
async def test_non_mutate_path_does_not_cancel(client, monkeypatch):
    from backend import sandbox_prewarm as pw

    cancel_calls: list[str] = []

    async def fake_cancel(*, stopper=None, reason: str = "dag_mutated",
                          tenant_id=None) -> int:
        cancel_calls.append(reason)
        return 0
    monkeypatch.setattr(pw, "cancel_all", fake_cancel)

    # Valid DAG, no mutation → mutate path never triggered.
    r = await client.post("/api/v1/dag", json={"dag": _dag("REQ-no-mut")})
    assert r.status_code == 200
    assert cancel_calls == []
