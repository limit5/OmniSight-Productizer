"""Phase 56-DAG-B — dag_plans persistence + workflow integration."""

from __future__ import annotations

import os
import tempfile

import pytest

from backend.dag_schema import DAG, Task


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fixtures
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.fixture()
async def fresh_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "t.db")
        monkeypatch.setenv("OMNISIGHT_DATABASE_PATH", path)
        from backend import config as cfg
        cfg.settings.database_path = path
        from backend import db
        db._DB_PATH = db._resolve_db_path()
        await db.init()
        try:
            yield db
        finally:
            await db.close()


def _t(task_id="T1", required_tier="t1", toolchain="cmake",
       expected_output="build/out.bin", **kw):
    return Task(
        task_id=task_id, description=f"t {task_id}",
        required_tier=required_tier, toolchain=toolchain,
        expected_output=expected_output, **kw,
    )


def _good_dag(dag_id="REQ-1") -> DAG:
    return DAG(dag_id=dag_id, tasks=[
        _t(task_id="A", expected_output="build/a.bin"),
        _t(task_id="B", expected_output="logs/b.log",
           required_tier="t3", toolchain="flash_board",
           inputs=["build/a.bin"], depends_on=["A"]),
    ])


def _bad_dag(dag_id="REQ-bad") -> DAG:
    """Cycle + tier violation in one — passes Pydantic but fails validator."""
    return DAG(dag_id=dag_id, tasks=[
        _t(task_id="X", required_tier="t1", toolchain="flash_board",
           expected_output="logs/x.log"),  # tier_violation
    ])


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CRUD round-trip
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_save_and_load_plan(fresh_db):
    from backend import dag_storage as ds
    dag = _good_dag()
    saved = await ds.save_plan(dag)
    assert saved.id > 0
    assert saved.dag_id == "REQ-1"
    assert saved.status == "pending"
    assert saved.mutation_round == 0
    assert saved.run_id is None

    rehydrated = saved.dag()
    assert isinstance(rehydrated, DAG)
    assert {t.task_id for t in rehydrated.tasks} == {"A", "B"}


@pytest.mark.asyncio
async def test_save_with_validation_errors(fresh_db):
    from backend import dag_storage as ds, dag_validator as dv
    dag = _bad_dag()
    result = dv.validate(dag)
    assert not result.ok
    saved = await ds.save_plan(
        dag, status="failed", validation_errors=result.errors,
    )
    assert saved.status == "failed"
    errs = saved.errors()
    assert errs and errs[0].get("rule") == "tier_violation"


@pytest.mark.asyncio
async def test_unknown_status_rejected(fresh_db):
    from backend import dag_storage as ds
    with pytest.raises(ValueError, match="unknown status"):
        await ds.save_plan(_good_dag(), status="bogus")


@pytest.mark.asyncio
async def test_list_plans_orders_by_round(fresh_db):
    from backend import dag_storage as ds
    p0 = await ds.save_plan(_good_dag(), mutation_round=0)
    p2 = await ds.save_plan(_good_dag(), mutation_round=2)
    p1 = await ds.save_plan(_good_dag(), mutation_round=1)
    plans = await ds.list_plans("REQ-1")
    assert [p.id for p in plans] == [p0.id, p1.id, p2.id]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Status state machine
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_legal_transition(fresh_db):
    from backend import dag_storage as ds
    p = await ds.save_plan(_good_dag())
    p2 = await ds.set_status(p.id, "validated")
    assert p2.status == "validated"
    p3 = await ds.set_status(p.id, "executing")
    assert p3.status == "executing"


@pytest.mark.asyncio
async def test_illegal_transition_rejected(fresh_db):
    from backend import dag_storage as ds
    p = await ds.save_plan(_good_dag())
    # pending → executing is NOT allowed (must go through validated).
    with pytest.raises(ValueError, match="illegal transition"):
        await ds.set_status(p.id, "executing")


@pytest.mark.asyncio
async def test_terminal_status_is_terminal(fresh_db):
    from backend import dag_storage as ds
    p = await ds.save_plan(_good_dag())
    p2 = await ds.set_status(p.id, "validated")
    p3 = await ds.set_status(p2.id, "executing")
    p4 = await ds.set_status(p3.id, "completed")
    assert p4.status == "completed"
    with pytest.raises(ValueError, match="illegal transition"):
        await ds.set_status(p.id, "mutated")  # completed is terminal


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  workflow.start integration
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_workflow_start_without_dag_unchanged(fresh_db):
    """Existing callers (no `dag=`) must work exactly as before."""
    from backend import workflow as wf
    run = await wf.start("invoke", metadata={"caller": "legacy"})
    assert run.id.startswith("wf-")
    assert run.status == "running"


@pytest.mark.asyncio
async def test_workflow_start_with_valid_dag_links_plan(fresh_db):
    from backend import workflow as wf, dag_storage as ds
    dag = _good_dag(dag_id="REQ-link")
    run = await wf.start("invoke", dag=dag)
    plan = await ds.get_plan_by_run(run.id)
    assert plan is not None
    assert plan.dag_id == "REQ-link"
    # Valid DAG should land at 'executing' after the auto-validate hop.
    assert plan.status == "executing"
    # Reverse linkage too.
    pid = await ds.get_dag_plan_id_for_run(run.id)
    assert pid == plan.id


@pytest.mark.asyncio
async def test_workflow_start_with_invalid_dag_marks_failed(fresh_db):
    from backend import workflow as wf, dag_storage as ds
    dag = _bad_dag(dag_id="REQ-fail")
    run = await wf.start("invoke", dag=dag)
    plan = await ds.get_plan_by_run(run.id)
    assert plan is not None
    assert plan.status == "failed"
    errs = plan.errors()
    assert any(e.get("rule") == "tier_violation" for e in errs)
    # The workflow_run itself still starts — caller decides on mutation.
    fresh = await wf.get_run(run.id)
    assert fresh.status == "running"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  mutate_workflow chain
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_mutate_workflow_chains_runs_and_plans(fresh_db):
    from backend import workflow as wf, dag_storage as ds, db

    dag1 = _bad_dag(dag_id="REQ-mut")
    run1 = await wf.start("invoke", dag=dag1)
    plan1 = await ds.get_plan_by_run(run1.id)
    assert plan1.status == "failed"

    # Mutate to a good DAG (same dag_id is allowed; mutation_round=1).
    dag2 = _good_dag(dag_id="REQ-mut")
    run2 = await wf.mutate_workflow(run1.id, dag2, mutation_round=1)

    # Old plan was marked mutated.
    plan1_fresh = await ds.get_plan(plan1.id)
    assert plan1_fresh.status == "mutated"

    # New plan exists and chains parent.
    plan2 = await ds.get_plan_by_run(run2.id)
    assert plan2.parent_plan_id == plan1.id
    assert plan2.mutation_round == 1
    assert plan2.status == "executing"  # good DAG → validated → executing

    # workflow_runs.successor_run_id link.
    async with db._conn().execute(
        "SELECT successor_run_id FROM workflow_runs WHERE id=?", (run1.id,),
    ) as cur:
        row = await cur.fetchone()
    assert row["successor_run_id"] == run2.id


@pytest.mark.asyncio
async def test_list_plans_traces_full_mutation_chain(fresh_db):
    from backend import workflow as wf, dag_storage as ds
    run1 = await wf.start("invoke", dag=_bad_dag(dag_id="REQ-chain"))
    _run2 = await wf.mutate_workflow(run1.id, _good_dag(dag_id="REQ-chain"),
                                    mutation_round=1)
    chain = await ds.list_plans("REQ-chain")
    assert len(chain) == 2
    assert chain[0].mutation_round == 0
    assert chain[1].mutation_round == 1
    assert chain[1].parent_plan_id == chain[0].id


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Defensive: failure in plan persistence must not break workflow.start
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_workflow_start_survives_storage_blowup(fresh_db, monkeypatch):
    from backend import workflow as wf, dag_storage as ds

    async def boom(*a, **kw):
        raise RuntimeError("storage offline")
    monkeypatch.setattr(ds, "save_plan", boom)

    run = await wf.start("invoke", dag=_good_dag(dag_id="REQ-resilient"))
    assert run.id.startswith("wf-")
    fresh = await wf.get_run(run.id)
    assert fresh.status == "running"
