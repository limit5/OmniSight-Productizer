"""Phase 56-DAG-D — Mode A endpoint."""

from __future__ import annotations

import pytest

from backend.dag_schema import DAG, Task


def _valid_dag(dag_id: str = "REQ-api") -> dict:
    """Phase 64-C-LOCAL S4 note: the historic fixture here used
    `t3 + flash_board`, which validates under the narrow hardware-
    bridge contract but fails once the repo's hardware_manifest
    declares `target_platform: host_native` (you can't flash a
    board via localhost). Generic "DAG submits cleanly" case is
    now a two-task t1 pipeline; hardware-bridge tests pass
    `target_platform` explicitly."""
    d = DAG(dag_id=dag_id, tasks=[
        Task(task_id="A", description="compile",
             required_tier="t1", toolchain="cmake",
             expected_output="build/a.bin"),
        Task(task_id="B", description="smoke test",
             required_tier="t1", toolchain="pytest",
             expected_output="logs/smoke.log",
             inputs=["build/a.bin"], depends_on=["A"]),
    ])
    return d.model_dump()


def _bad_dag(dag_id: str = "REQ-api-bad") -> dict:
    """Tier violation: flash_board denied on t1."""
    d = DAG(dag_id=dag_id, tasks=[
        Task(task_id="X", description="boom",
             required_tier="t1", toolchain="flash_board",
             expected_output="logs/x.log"),
    ])
    return d.model_dump()


def _malformed_dag() -> dict:
    """Fails Pydantic schema: missing required fields."""
    return {"dag_id": "broken"}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  POST /dag — submit
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_submit_valid_dag_returns_executing(client):
    r = await client.post("/api/v1/dag",
                           json={"dag": _valid_dag("REQ-ok")})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "executing"
    assert body["validation_errors"] == []
    assert body["run_id"].startswith("wf-")
    assert isinstance(body["plan_id"], int)


@pytest.mark.asyncio
async def test_submit_malformed_schema_returns_422_early(client):
    r = await client.post("/api/v1/dag",
                           json={"dag": _malformed_dag()})
    assert r.status_code == 422
    body = r.json()
    assert body["stage"] == "schema"


@pytest.mark.asyncio
async def test_submit_semantic_failure_returns_422_with_errors(client):
    r = await client.post("/api/v1/dag",
                           json={"dag": _bad_dag("REQ-semfail")})
    assert r.status_code == 422
    body = r.json()
    assert body["status"] == "failed"
    rules = {e["rule"] for e in body["validation_errors"]}
    assert "tier_violation" in rules


@pytest.mark.asyncio
async def test_submit_stores_metadata_on_run(client):
    r = await client.post(
        "/api/v1/dag",
        json={"dag": _valid_dag("REQ-meta"),
              "metadata": {"ticket": "OMNI-42"}},
    )
    assert r.status_code == 200
    run_id = r.json()["run_id"]

    from backend import workflow as wf
    run = await wf.get_run(run_id)
    assert run.metadata.get("ticket") == "OMNI-42"
    assert run.metadata.get("source") == "api:dag-submit"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Mutation mode
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_mutate_false_does_not_invoke_orchestrator(client, monkeypatch):
    """Default mutate=false → semantic failure returns 422 immediately
    without calling the orchestrator."""
    from backend.routers import dag as dag_router
    calls = {"n": 0}

    async def fake_ask(s, u):
        calls["n"] += 1
        return ("{}", 0)
    monkeypatch.setattr(dag_router, "_default_ask_fn", fake_ask)

    r = await client.post("/api/v1/dag",
                           json={"dag": _bad_dag("REQ-no-mut")})
    assert r.status_code == 422
    assert calls["n"] == 0


@pytest.mark.asyncio
async def test_mutate_true_recovers_when_orchestrator_succeeds(
    client, monkeypatch,
):
    """mutate=true + orchestrator returns a good DAG → endpoint
    returns 200 + supersedes_run_id + recovered plan."""
    from backend.routers import dag as dag_router
    fixed = DAG.model_validate(_valid_dag("REQ-mut"))

    async def fake_ask(s, u):
        return (fixed.model_dump_json(), 100)
    monkeypatch.setattr(dag_router, "_default_ask_fn", fake_ask)

    r = await client.post(
        "/api/v1/dag",
        json={"dag": _bad_dag("REQ-mut"), "mutate": True},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "executing"
    assert body["mutation_rounds"] == 1
    assert body["supersedes_run_id"].startswith("wf-")
    assert body["run_id"] != body["supersedes_run_id"]


@pytest.mark.asyncio
async def test_mutate_true_exhausted_returns_422_with_mutation_status(
    client, monkeypatch,
):
    """mutate=true + orchestrator keeps returning broken → endpoint
    returns 422 with mutation_status exhausted."""
    from backend.routers import dag as dag_router
    still_bad = DAG.model_validate(_bad_dag("REQ-exh"))

    async def fake_ask(s, u):
        return (still_bad.model_dump_json(), 50)
    monkeypatch.setattr(dag_router, "_default_ask_fn", fake_ask)

    # Reset decision_engine so the DE proposal from the loop doesn't
    # carry over state between tests.
    from backend import decision_engine as de
    de._reset_for_tests()

    r = await client.post(
        "/api/v1/dag",
        json={"dag": _bad_dag("REQ-exh"), "mutate": True},
    )
    assert r.status_code == 422
    body = r.json()
    assert body["stage"] == "mutation_exhausted"
    assert body["mutation_status"] == "exhausted"
    assert body["mutation_rounds"] == 3  # locked in dag_planner


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  GET endpoints
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_get_plan_by_id(client):
    r1 = await client.post("/api/v1/dag",
                            json={"dag": _valid_dag("REQ-lookup")})
    plan_id = r1.json()["plan_id"]
    r2 = await client.get(f"/api/v1/dag/plans/{plan_id}")
    assert r2.status_code == 200
    body = r2.json()
    assert body["id"] == plan_id
    assert body["dag_id"] == "REQ-lookup"
    assert body["status"] == "executing"


@pytest.mark.asyncio
async def test_get_plan_404_for_unknown(client):
    r = await client.get("/api/v1/dag/plans/999999")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_get_plan_for_run(client):
    r1 = await client.post("/api/v1/dag",
                            json={"dag": _valid_dag("REQ-byrun")})
    run_id = r1.json()["run_id"]
    r2 = await client.get(f"/api/v1/dag/runs/{run_id}/plan")
    assert r2.status_code == 200
    assert r2.json()["run_id"] == run_id


@pytest.mark.asyncio
async def test_get_plan_for_unknown_run_404(client):
    r = await client.get("/api/v1/dag/runs/wf-missing/plan")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_list_plan_chain(client, monkeypatch):
    """Submit bad → mutate → good; chain lookup shows both plans."""
    from backend.routers import dag as dag_router
    good = DAG.model_validate(_valid_dag("REQ-chain-api"))

    async def fake_ask(s, u):
        return (good.model_dump_json(), 100)
    monkeypatch.setattr(dag_router, "_default_ask_fn", fake_ask)

    await client.post(
        "/api/v1/dag",
        json={"dag": _bad_dag("REQ-chain-api"), "mutate": True},
    )
    r = await client.get("/api/v1/dag/plans/by-dag/REQ-chain-api")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    assert body["plans"][0]["mutation_round"] == 0
    assert body["plans"][1]["mutation_round"] == 1
    assert body["plans"][1]["parent_plan_id"] == body["plans"][0]["id"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  POST /dag/validate — Phase 56-DAG-E (dry-run for UI)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_validate_valid_dag_returns_ok(client):
    r = await client.post("/api/v1/dag/validate",
                          json={"dag": _valid_dag("REQ-v-ok")})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["stage"] == "semantic"
    assert body["errors"] == []
    assert body["task_count"] == 2


@pytest.mark.asyncio
async def test_validate_malformed_schema_stage(client):
    r = await client.post("/api/v1/dag/validate",
                          json={"dag": _malformed_dag()})
    assert r.status_code == 200  # dry-run always 200; payload carries ok=false
    body = r.json()
    assert body["ok"] is False
    assert body["stage"] == "schema"
    assert body["errors"][0]["rule"] == "schema"


@pytest.mark.asyncio
async def test_validate_semantic_failure_surfaces_all_rules(client):
    r = await client.post("/api/v1/dag/validate",
                          json={"dag": _bad_dag("REQ-v-bad")})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["stage"] == "semantic"
    rules = {e["rule"] for e in body["errors"]}
    assert "tier_violation" in rules


@pytest.mark.asyncio
async def test_validate_does_not_persist(client):
    """Dry-run must not create a workflow_run or plan row."""
    from backend import dag_storage as _ds
    before = await _ds.list_plans("REQ-v-nostore")
    r = await client.post("/api/v1/dag/validate",
                          json={"dag": _valid_dag("REQ-v-nostore")})
    assert r.status_code == 200
    after = await _ds.list_plans("REQ-v-nostore")
    assert len(after) == len(before)  # no new plan
