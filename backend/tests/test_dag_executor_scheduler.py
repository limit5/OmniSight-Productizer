"""OP-1657 — serial topological scheduler over ``dag.tasks``.

Covers the acceptance criteria:

  * Code — ready-set ordering correct + cycle-safe (validator blocks
    cycles upstream; the scheduler refuses loudly if one slips through);
  * Integration — a 3-task diamond runs in dependency order;
  * Exercised — the 2-task ``compile`` → ``test`` smoke sequence is
    ordered + dispatched in order;
  * the walk is SERIAL: a task's handler completes before the next starts.

Local harness only — no DB, no Docker, no host state touched. The
``PlanWorkspaceBuilder`` tests use a tmp project root + tmp workdir.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from backend import dag_executor as dx
from backend.dag_schema import DAG, Task


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  DAG fixtures
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _task(task_id: str, *, depends_on=None, inputs=None, output=None) -> Task:
    return Task(
        task_id=task_id,
        description=f"task {task_id}",
        required_tier="t1",
        toolchain="cmake",
        expected_output=output or f"build/{task_id}.out",
        inputs=inputs or [],
        depends_on=depends_on or [],
    )


def _diamond() -> DAG:
    """A → {B, C} → D. The canonical diamond: B and C both depend on A,
    D depends on both B and C."""
    return DAG(dag_id="diamond", tasks=[
        _task("A"),
        _task("B", depends_on=["A"]),
        _task("C", depends_on=["A"]),
        _task("D", depends_on=["B", "C"]),
    ])


def _compile_then_test() -> DAG:
    """The 2-task smoke: compile, then a test that depends on it."""
    return DAG(dag_id="smoke", tasks=[
        _task("compile", output="build/app.bin"),
        _task("test", depends_on=["compile"], inputs=["build/app.bin"]),
    ])


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  AC: ready-set ordering correct
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_topological_order_linear_chain():
    dag = DAG(dag_id="chain", tasks=[
        _task("c", depends_on=["b"]),
        _task("b", depends_on=["a"]),
        _task("a"),
    ])
    order = [t.task_id for t in dx.topological_order(dag)]
    assert order == ["a", "b", "c"]


def test_topological_order_diamond_respects_deps():
    order = [t.task_id for t in dx.topological_order(_diamond())]
    pos = {tid: i for i, tid in enumerate(order)}
    # every edge points forward
    assert pos["A"] < pos["B"] < pos["D"]
    assert pos["A"] < pos["C"] < pos["D"]
    # declaration-order ready set makes the walk deterministic: B before C
    assert order == ["A", "B", "C", "D"]


def test_topological_order_independent_tasks_keep_declaration_order():
    dag = DAG(dag_id="indep", tasks=[_task("z"), _task("y"), _task("x")])
    assert [t.task_id for t in dx.topological_order(dag)] == ["z", "y", "x"]


def test_topological_order_ignores_unknown_deps():
    # A dep on an id not in the graph is the validator's concern, not the
    # scheduler's — it must not wedge the walk.
    dag = DAG(dag_id="unknown-dep", tasks=[
        _task("only", depends_on=["ghost"]),
    ])
    assert [t.task_id for t in dx.topological_order(dag)] == ["only"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  AC: cycle-safe
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_topological_order_raises_on_cycle():
    # Schema allows this (no self-dep, no dup); the semantic cycle check
    # lives in dag_validator. If a cyclic plan ever reaches the scheduler
    # we refuse loudly instead of dropping the unresolved tail.
    dag = DAG(dag_id="cyclic", tasks=[
        _task("A", depends_on=["B"]),
        _task("B", depends_on=["A"]),
    ])
    with pytest.raises(dx.CycleError) as ei:
        dx.topological_order(dag)
    assert "A" in str(ei.value) and "B" in str(ei.value)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  AC: integration — 3-task diamond runs in dependency order
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def test_diamond_runs_in_dependency_order():
    seen: list[str] = []

    async def handler(task, ws):
        seen.append(task.task_id)

    sched = dx.SerialPlanScheduler(task_handler=handler)
    result = await sched.run_plan(plan_id=42, dag=_diamond())

    assert result.plan_id == 42
    assert result.dag_id == "diamond"
    # handler was invoked in dependency order …
    pos = {tid: i for i, tid in enumerate(seen)}
    assert pos["A"] < pos["B"] < pos["D"]
    assert pos["A"] < pos["C"] < pos["D"]
    assert seen == ["A", "B", "C", "D"]
    # … and the recorded ordering state matches the dispatch order
    assert result.order == seen
    assert [r.task_id for r in result.runs] == seen
    assert [r.order_index for r in result.runs] == [0, 1, 2, 3]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  AC: exercised — 2-task compile → test smoke sequence
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def test_compile_then_test_smoke_order():
    seen: list[str] = []

    async def handler(task, ws):
        seen.append(task.task_id)

    sched = dx.SerialPlanScheduler(task_handler=handler)
    result = await sched.run_plan(plan_id=7, dag=_compile_then_test())

    assert seen == ["compile", "test"]
    assert result.order == ["compile", "test"]


async def test_default_handler_is_noop_but_records_order():
    # A default-constructed scheduler (no handler injected) still computes +
    # records the ordering — the inert-merge contract for OP-1657.
    sched = dx.SerialPlanScheduler()
    result = await sched.run_plan(plan_id=1, dag=_compile_then_test())
    assert result.order == ["compile", "test"]
    assert all(r.status == "ran" for r in result.runs)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Serial execution — no concurrency
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def test_execution_is_serial_not_concurrent():
    # If the scheduler ran tasks concurrently, ``in_flight`` would exceed 1.
    in_flight = 0
    max_in_flight = 0
    events: list[str] = []

    async def handler(task, ws):
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        events.append(f"start:{task.task_id}")
        await asyncio.sleep(0.01)  # yield: a concurrent scheduler would interleave
        events.append(f"end:{task.task_id}")
        in_flight -= 1

    sched = dx.SerialPlanScheduler(task_handler=handler)
    await sched.run_plan(plan_id=3, dag=_diamond())

    assert max_in_flight == 1
    # each task fully finishes (start→end) before the next starts
    assert events == [
        "start:A", "end:A", "start:B", "end:B",
        "start:C", "end:C", "start:D", "end:D",
    ]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Per-plan workspace helper (workdir_root/{plan_id}-{task_id})
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_workspace_builder_path_convention(tmp_path):
    builder = dx.PlanWorkspaceBuilder(workdir_root=tmp_path / "wd")
    ws = builder.prepare(99, _task("compile"))
    assert ws.plan_id == 99 and ws.task_id == "compile"
    assert ws.path == (tmp_path / "wd" / "99-compile")
    assert ws.path.is_dir()
    assert ws.copied == []  # no project root bound → nothing copied


def test_workspace_builder_copies_input_globs(tmp_path):
    project = tmp_path / "proj"
    (project / "build").mkdir(parents=True)
    (project / "build" / "app.bin").write_text("ELF")
    builder = dx.PlanWorkspaceBuilder(
        workdir_root=tmp_path / "wd", project_root=project,
    )
    ws = builder.prepare(5, _task("test", inputs=["build/app.bin"]))
    assert ws.copied == ["build/app.bin"]
    assert (ws.path / "build" / "app.bin").read_text() == "ELF"


def test_workspace_builder_glob_jail_rejects_escape(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    builder = dx.PlanWorkspaceBuilder(
        workdir_root=tmp_path / "wd", project_root=project,
    )
    with pytest.raises(ValueError):
        builder.prepare(1, _task("evil", inputs=["../secrets/*"]))


def test_workspace_builder_cleanup_removes_dir(tmp_path):
    builder = dx.PlanWorkspaceBuilder(workdir_root=tmp_path / "wd")
    ws = builder.prepare(2, _task("A"))
    assert ws.path.is_dir()
    builder.cleanup(ws)
    assert not ws.path.exists()


async def test_run_plan_prepares_per_task_workspaces(tmp_path):
    project = tmp_path / "proj"
    (project / "build").mkdir(parents=True)
    (project / "build" / "app.bin").write_text("bin")
    builder = dx.PlanWorkspaceBuilder(
        workdir_root=tmp_path / "wd", project_root=project,
    )
    handed: list[Path] = []

    async def handler(task, ws):
        assert ws is not None
        assert ws.path.is_dir()
        handed.append(ws.path)

    sched = dx.SerialPlanScheduler(workspace_builder=builder, task_handler=handler)
    result = await sched.run_plan(plan_id=8, dag=_compile_then_test())

    # one workspace per task, under the {plan_id}-{task_id} convention
    assert handed == [
        tmp_path / "wd" / "8-compile",
        tmp_path / "wd" / "8-test",
    ]
    assert [r.workspace for r in result.runs] == handed
    # cleanup defaults off → workspaces persist after the run
    assert all(p.is_dir() for p in handed)


async def test_run_plan_cleanup_flag_removes_workspaces(tmp_path):
    builder = dx.PlanWorkspaceBuilder(workdir_root=tmp_path / "wd")
    sched = dx.SerialPlanScheduler(workspace_builder=builder)
    result = await sched.run_plan(plan_id=9, dag=_compile_then_test(), cleanup=True)
    for r in result.runs:
        assert r.workspace is not None and not r.workspace.exists()


async def test_run_stored_plan_rehydrates_and_walks():
    # run_stored_plan is the shape the gated executor seam will call after a
    # lease is granted — build a StoredPlan by hand (no DB) to exercise it.
    from backend import dag_storage

    dag = _compile_then_test()
    plan = dag_storage.StoredPlan(
        id=123, dag_id=dag.dag_id, run_id=None, parent_plan_id=None,
        json_body=dag.model_dump_json(), status="validated", mutation_round=0,
        validation_errors=None, created_at=0.0, updated_at=0.0,
    )
    sched = dx.SerialPlanScheduler()
    result = await sched.run_stored_plan(plan)
    assert result.plan_id == 123
    assert result.order == ["compile", "test"]
