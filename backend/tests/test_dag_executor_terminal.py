"""OP-1659 — step recording + terminal wiring (completed/failed + finish).

Covers the acceptance criteria for the slice that closes the executor loop
(design doc §7, codex E5/E6):

  * Code — terminal transitions correct (all ok -> 'completed', any failure
    -> 'failed', plan-status BEFORE run-finish) + idempotent re-claim skips
    already-done steps (and is a full no-op once the plan is terminal);
  * Integration (local) — a 2-task smoke run reaches 'completed' with 2
    recorded steps; a forced task failure lands 'failed';
  * Exercised — an in-harness ``prod_smoke_test``-style poll of the workflow
    store sees status='completed' + the recorded steps;
  * the executor OWNS per-plan workspace cleanup, AFTER steps are recorded.

These run LOCALLY with an in-memory double for ``backend.workflow`` /
``backend.dag_storage`` (no DB, no Docker, no host state outside ``tmp_path``).
The double mirrors the real contract: a UNIQUE ``(run_id, idempotency_key)``
step log, ``StepRecord.is_done`` semantics, and the real
``dag_storage._ALLOWED_TRANSITIONS`` state-machine for ``set_status`` — so a
drift in the real transition table fails these too. A separate test asserts
the real ``workflow`` exposes the imperative recorder the executor calls, so
the in-memory double can never mask a real-API drift.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from backend import dag_executor as dx
from backend import dag_storage as ds
from backend.dag_schema import DAG, Task
from backend.workflow import StepRecord


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  In-memory doubles for workflow + dag_storage (mirror the real contract)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class FakeWorkflow:
    """In-memory stand-in for the subset of ``backend.workflow`` the executor
    calls: ``get_step`` / ``record_dag_step`` / ``list_steps`` / ``finish``.

    The step log enforces the real UNIQUE ``(run_id, idempotency_key)``
    invariant and reuses the real :class:`StepRecord` (so ``is_done`` is the
    real property), and ``record_dag_step`` reproduces the real "skip a
    done step" idempotency.
    """

    def __init__(self) -> None:
        self.steps: dict[tuple[str, str], StepRecord] = {}
        self.runs: dict[str, dict] = {}
        self.finishes: list[tuple[str, str]] = []
        self._n = 0

    def seed_run(self, run_id, *, metadata=None, version=0):
        """Pre-create a run row (as workflow.start would) so the executor's
        marker write (OP-1661) has something to read + update."""
        self.runs[run_id] = {
            "status": "running", "completed_at": None,
            "metadata": dict(metadata or {}), "version": version,
        }

    async def get_run(self, run_id):
        r = self.runs.get(run_id)
        if r is None:
            return None
        from types import SimpleNamespace
        return SimpleNamespace(
            id=run_id, status=r.get("status", "running"),
            metadata=dict(r.get("metadata", {})), version=r.get("version", 0),
        )

    async def update_run_metadata(self, run_id, expected_version, metadata):
        r = self.runs.setdefault(
            run_id,
            {"status": "running", "completed_at": None,
             "metadata": {}, "version": 0},
        )
        if r.get("version", 0) != expected_version:
            raise RuntimeError("version conflict")  # mirrors VersionConflict
        r["metadata"] = {**r.get("metadata", {}), **metadata}
        r["version"] = r.get("version", 0) + 1
        return r["version"]

    async def get_step(self, run_id, key):
        return self.steps.get((run_id, key))

    async def list_steps(self, run_id):
        return [s for (r, _), s in self.steps.items() if r == run_id]

    async def record_dag_step(self, run_id, key, *, dag_task_id=None,
                              output=None, error=None):
        existing = self.steps.get((run_id, key))
        if existing and existing.is_done:
            return existing  # skip-done idempotency
        if existing is not None:
            return existing  # UNIQUE collision read-back (no rewrite)
        self._n += 1
        now = time.time()
        rec = StepRecord(
            id=f"step-{self._n}", run_id=run_id, idempotency_key=key,
            started_at=now, completed_at=now, output=output, error=error,
            dag_task_id=dag_task_id,
        )
        self.steps[(run_id, key)] = rec
        return rec

    async def finish(self, run_id, status="completed", expected_version=None):
        self.finishes.append((run_id, status))
        self.runs.setdefault(run_id, {})
        self.runs[run_id]["status"] = status
        self.runs[run_id]["completed_at"] = time.time()


class _FakePlan:
    def __init__(self, plan_id, status):
        self.id = plan_id
        self.status = status


class FakeStorage:
    """In-memory ``dag_storage`` double. ``set_status`` is validated against
    the REAL ``_ALLOWED_TRANSITIONS`` table so a transition the production
    state-machine forbids raises here too."""

    def __init__(self, plans: dict[int, str]):
        self.plans = dict(plans)  # plan_id -> status
        self.transitions: list[tuple[int, str, str]] = []

    async def get_plan(self, plan_id, conn=None):
        return _FakePlan(plan_id, self.plans[plan_id])

    async def set_status(self, plan_id, new_status, *, run_id=None, conn=None):
        cur = self.plans[plan_id]
        if new_status not in ds._ALLOWED_TRANSITIONS[cur]:
            raise ValueError(f"illegal transition {cur!r} -> {new_status!r}")
        self.transitions.append((plan_id, cur, new_status))
        self.plans[plan_id] = new_status
        return _FakePlan(plan_id, new_status)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fixtures / helpers (all under tmp_path — never test_assets/)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _task(task_id, *, toolchain="make", tier="t1", inputs=None,
          output=None, depends_on=None) -> Task:
    return Task(
        task_id=task_id, description=f"task {task_id}",
        required_tier=tier, toolchain=toolchain,
        expected_output=output or f"out/{task_id}",
        inputs=inputs or [], depends_on=depends_on or [],
    )


def _smoke_dag() -> DAG:
    """The 2-task smoke: compile -> test (test depends on compile)."""
    return DAG(dag_id="smoke", tasks=[
        _task("compile", output="out/compile"),
        _task("test", depends_on=["compile"], output="out/test"),
    ])


def _stored_plan(dag: DAG, *, plan_id=500, run_id="wf-smoke",
                 status="executing") -> ds.StoredPlan:
    return ds.StoredPlan(
        id=plan_id, dag_id=dag.dag_id, run_id=run_id, parent_plan_id=None,
        json_body=dag.model_dump_json(), status=status, mutation_round=0,
        validation_errors=None, created_at=0.0, updated_at=0.0,
    )


def _handler(tmp_path, *, runner) -> dx.LocalTaskHandler:
    builder = dx.PlanWorkspaceBuilder(workdir_root=tmp_path / "wd")
    return dx.LocalTaskHandler(workspace_builder=builder, runner=runner)


def _producing_runner(*relpaths):
    """A fake runner that 'succeeds' and writes each given artifact path so
    the handler's expected_output check passes."""
    def run(argv, cwd, timeout_s):
        for rel in relpaths:
            p = Path(cwd) / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("artifact")
        return 0, "ok\n", ""
    return run


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pure-unit (no handler) — key + terminal semantics
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_dag_step_key_is_stable_and_namespaced():
    assert dx.dag_step_key("compile") == "dag-task:compile"
    # stable: same task -> same key (the re-claim lookup hinge)
    assert dx.dag_step_key("compile") == dx.dag_step_key("compile")


def test_real_workflow_exposes_imperative_recorder():
    # The in-memory double must not mask a real-API drift: the executor calls
    # these exact names on backend.workflow.
    from backend import workflow as wf
    assert callable(wf.get_step)
    assert callable(wf.record_dag_step)
    assert callable(wf.finish)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  AC: Integration (local) — smoke run reaches 'completed' with 2 steps
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def test_smoke_run_completes_with_two_recorded_steps(tmp_path):
    dag = _smoke_dag()
    plan = _stored_plan(dag)
    wf = FakeWorkflow()
    storage = FakeStorage({plan.id: "executing"})
    handler = _handler(tmp_path, runner=_producing_runner("out/compile", "out/test"))

    res = await dx.record_and_finalize_plan(
        plan, handler=handler, workflow=wf, storage=storage,
    )

    assert res.status == "completed"
    assert res.recorded == ["compile", "test"]
    # plan transitioned executing -> completed; run finished completed
    assert storage.plans[plan.id] == "completed"
    assert storage.transitions == [(plan.id, "executing", "completed")]
    assert wf.finishes == [("wf-smoke", "completed")]
    # exactly 2 steps recorded, each carrying its dag_task_id, all done
    steps = await wf.list_steps("wf-smoke")
    assert len(steps) == 2
    assert {s.dag_task_id for s in steps} == {"compile", "test"}
    assert all(s.is_done for s in steps)


async def test_step_carries_dag_task_id_and_output(tmp_path):
    dag = DAG(dag_id="one", tasks=[_task("compile", output="out/compile")])
    plan = _stored_plan(dag, run_id="wf-1")
    wf = FakeWorkflow()
    storage = FakeStorage({plan.id: "executing"})
    handler = _handler(tmp_path, runner=_producing_runner("out/compile"))

    await dx.record_and_finalize_plan(plan, handler=handler,
                                      workflow=wf, storage=storage)

    step = (await wf.list_steps("wf-1"))[0]
    assert step.dag_task_id == "compile"
    assert step.output["toolchain"] == "make"
    assert step.output["status"] == "ok"
    assert step.output["rc"] == 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  AC: Integration (local) — a forced task failure lands 'failed'
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def test_forced_failure_lands_failed(tmp_path):
    dag = _smoke_dag()
    plan = _stored_plan(dag)
    wf = FakeWorkflow()
    storage = FakeStorage({plan.id: "executing"})

    def boom(argv, cwd, timeout_s):
        return 2, "", "boom\n"  # first task fails rc=2

    handler = _handler(tmp_path, runner=boom)
    res = await dx.record_and_finalize_plan(
        plan, handler=handler, workflow=wf, storage=storage,
    )

    assert res.status == "failed"
    assert storage.plans[plan.id] == "failed"
    assert storage.transitions == [(plan.id, "executing", "failed")]
    assert wf.finishes == [("wf-smoke", "failed")]
    # the serial walk stops at the first failure: only 'compile' recorded,
    # 'test' (downstream) never ran
    steps = await wf.list_steps("wf-smoke")
    assert [s.dag_task_id for s in steps] == ["compile"]
    failed = steps[0]
    assert not failed.is_done and "rc=2" in (failed.error or "")


async def test_failed_gate_task_lands_failed_without_workspace(tmp_path):
    # A gate failure (unknown toolchain) fails before any workspace is built —
    # still a clean 'failed' terminal, nothing to clean.
    dag = DAG(dag_id="bad", tasks=[_task("x", toolchain="flash_board")])
    plan = _stored_plan(dag, run_id="wf-bad")
    wf = FakeWorkflow()
    storage = FakeStorage({plan.id: "executing"})
    handler = _handler(tmp_path, runner=_producing_runner())

    res = await dx.record_and_finalize_plan(plan, handler=handler,
                                            workflow=wf, storage=storage)
    assert res.status == "failed"
    assert storage.plans[plan.id] == "failed"
    assert res.results[0].workspace is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  AC: Code — idempotent re-claim skips already-done steps / is a no-op
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def test_reclaim_of_completed_plan_is_noop(tmp_path):
    dag = _smoke_dag()
    plan = _stored_plan(dag)
    wf = FakeWorkflow()
    storage = FakeStorage({plan.id: "executing"})
    runs = {"n": 0}

    def counting(argv, cwd, timeout_s):
        runs["n"] += 1
        for rel in ("out/compile", "out/test"):
            p = Path(cwd) / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("a")
        return 0, "", ""

    handler = _handler(tmp_path, runner=counting)

    first = await dx.record_and_finalize_plan(plan, handler=handler,
                                              workflow=wf, storage=storage)
    assert first.status == "completed" and not first.already_terminal
    invocations_after_first = runs["n"]
    finishes_after_first = list(wf.finishes)

    # Re-claim the now-completed plan: must be a no-op — no extra handler runs,
    # no duplicate steps, no second finish, no illegal re-transition.
    second = await dx.record_and_finalize_plan(
        _stored_plan(dag, status="completed"),  # plan row now reads completed
        handler=handler, workflow=wf, storage=storage,
    )
    assert second.already_terminal is True
    assert second.status == "completed"
    assert sorted(second.recorded) == ["compile", "test"]
    assert runs["n"] == invocations_after_first  # handler not re-invoked
    assert wf.finishes == finishes_after_first    # no second finish
    assert len(await wf.list_steps("wf-smoke")) == 2  # no duplicate steps


async def test_reclaim_resumes_skipping_done_steps(tmp_path):
    # Mid-flight crash: 'compile' already recorded done, plan still executing.
    # Re-claim resumes: 'compile' is skipped (handler not called for it),
    # 'test' runs for the first time, plan completes.
    dag = _smoke_dag()
    plan = _stored_plan(dag)
    wf = FakeWorkflow()
    # pre-seed a DONE step for compile (as a prior, crashed invocation would)
    await wf.record_dag_step("wf-smoke", dx.dag_step_key("compile"),
                             dag_task_id="compile", output={"status": "ok"})
    storage = FakeStorage({plan.id: "executing"})

    ran: list[str] = []

    def record(argv, cwd, timeout_s):
        # workspace dir basename encodes plan_id-task_id
        ran.append(Path(cwd).name)
        (Path(cwd) / "out").mkdir(parents=True, exist_ok=True)
        (Path(cwd) / "out" / "test").write_text("a")
        return 0, "", ""

    handler = _handler(tmp_path, runner=record)
    res = await dx.record_and_finalize_plan(plan, handler=handler,
                                            workflow=wf, storage=storage)

    assert res.status == "completed"
    assert res.recorded == ["compile", "test"]  # both accounted for
    # only 'test' actually ran a process this invocation
    assert ran == ["500-test"]
    assert storage.plans[plan.id] == "completed"
    assert len(await wf.list_steps("wf-smoke")) == 2


async def test_reclaim_honours_prior_failed_step_without_retry(tmp_path):
    # A prior failed step (plan still executing — crash before set_status) is
    # honoured as a failure on re-claim; the task is NOT re-run (no retry).
    dag = _smoke_dag()
    plan = _stored_plan(dag)
    wf = FakeWorkflow()
    await wf.record_dag_step("wf-smoke", dx.dag_step_key("compile"),
                             dag_task_id="compile", error="boom")
    storage = FakeStorage({plan.id: "executing"})

    ran = {"n": 0}

    def counting(argv, cwd, timeout_s):
        ran["n"] += 1
        return 0, "", ""

    handler = _handler(tmp_path, runner=counting)
    res = await dx.record_and_finalize_plan(plan, handler=handler,
                                            workflow=wf, storage=storage)
    assert res.status == "failed"
    assert ran["n"] == 0          # the failed task was not re-run
    assert storage.plans[plan.id] == "failed"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  AC: own per-plan workspace cleanup AFTER steps are recorded
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def test_workspaces_cleaned_after_recording(tmp_path):
    dag = _smoke_dag()
    plan = _stored_plan(dag)
    wf = FakeWorkflow()
    storage = FakeStorage({plan.id: "executing"})
    handler = _handler(tmp_path, runner=_producing_runner("out/compile", "out/test"))

    res = await dx.record_and_finalize_plan(plan, handler=handler,
                                            workflow=wf, storage=storage)

    assert res.status == "completed"
    # cleanup default on -> each task's scratch is gone, but its step is durable
    assert all(r.workspace is not None and not r.workspace.exists()
               for r in res.results)
    assert len(await wf.list_steps("wf-smoke")) == 2


async def test_cleanup_can_be_disabled(tmp_path):
    dag = DAG(dag_id="one", tasks=[_task("compile", output="out/compile")])
    plan = _stored_plan(dag, run_id="wf-keep")
    wf = FakeWorkflow()
    storage = FakeStorage({plan.id: "executing"})
    handler = _handler(tmp_path, runner=_producing_runner("out/compile"))

    res = await dx.record_and_finalize_plan(plan, handler=handler, workflow=wf,
                                            storage=storage, cleanup=False)
    assert res.results[0].workspace.is_dir()  # scratch preserved


async def test_missing_run_id_raises(tmp_path):
    dag = _smoke_dag()
    plan = _stored_plan(dag, run_id=None)
    wf = FakeWorkflow()
    storage = FakeStorage({plan.id: "executing"})
    handler = _handler(tmp_path, runner=_producing_runner())
    with pytest.raises(ValueError, match="no run_id"):
        await dx.record_and_finalize_plan(plan, handler=handler,
                                          workflow=wf, storage=storage)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  AC: Exercised — in-harness prod_smoke_test-style poll
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def test_prod_smoke_style_poll_sees_completed_and_steps(tmp_path):
    # Mirror a prod_smoke_test poll: drive the plan, then "poll" the workflow
    # store (the read surface a smoke test would hit) and assert it observes a
    # completed run carrying the recorded steps.
    dag = _smoke_dag()
    plan = _stored_plan(dag, run_id="wf-poll")
    wf = FakeWorkflow()
    storage = FakeStorage({plan.id: "executing"})
    handler = _handler(tmp_path, runner=_producing_runner("out/compile", "out/test"))

    await dx.record_and_finalize_plan(plan, handler=handler,
                                      workflow=wf, storage=storage)

    # poll: run status + step count, as a smoke harness would
    assert wf.runs["wf-poll"]["status"] == "completed"
    polled = await wf.list_steps("wf-poll")
    assert sorted(s.dag_task_id for s in polled) == ["compile", "test"]
    assert all(s.is_done for s in polled)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  OP-1661 — executor runs are tagged so finetune_export excludes them
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def test_finalize_stamps_executor_run_marker(tmp_path):
    dag = _smoke_dag()
    plan = _stored_plan(dag)
    wf = FakeWorkflow()
    wf.seed_run("wf-smoke")  # run exists, as workflow.start would have created it
    storage = FakeStorage({plan.id: "executing"})
    handler = _handler(tmp_path, runner=_producing_runner("out/compile", "out/test"))

    res = await dx.record_and_finalize_plan(
        plan, handler=handler, workflow=wf, storage=storage,
    )

    assert res.status == "completed"
    assert wf.runs["wf-smoke"]["metadata"][dx.EXECUTOR_RUN_METADATA_MARKER] is True


async def test_finalize_marks_failed_run_too(tmp_path):
    """A failed executor run is tagged as well — it surfaces in workflow_runs
    and the marker is the durable 'executor-produced' signal regardless of
    terminal status."""
    dag = _smoke_dag()
    plan = _stored_plan(dag, run_id="wf-fail")
    wf = FakeWorkflow()
    wf.seed_run("wf-fail")
    storage = FakeStorage({plan.id: "executing"})
    handler = _handler(tmp_path, runner=lambda *a: (2, "", "boom\n"))

    res = await dx.record_and_finalize_plan(
        plan, handler=handler, workflow=wf, storage=storage,
    )
    assert res.status == "failed"
    assert wf.runs["wf-fail"]["metadata"][dx.EXECUTOR_RUN_METADATA_MARKER] is True


async def test_marker_write_is_idempotent(tmp_path):
    """A run already carrying the marker is left untouched (no version bump /
    no duplicate write) — covers the re-claim / retry path."""
    dag = DAG(dag_id="one", tasks=[_task("compile", output="out/compile")])
    plan = _stored_plan(dag, run_id="wf-pre")
    wf = FakeWorkflow()
    wf.seed_run("wf-pre", metadata={dx.EXECUTOR_RUN_METADATA_MARKER: True},
                version=7)
    storage = FakeStorage({plan.id: "executing"})
    handler = _handler(tmp_path, runner=_producing_runner("out/compile"))

    await dx.record_and_finalize_plan(plan, handler=handler,
                                      workflow=wf, storage=storage)
    # marker still set, and version untouched (no needless update)
    assert wf.runs["wf-pre"]["metadata"][dx.EXECUTOR_RUN_METADATA_MARKER] is True
    assert wf.runs["wf-pre"]["version"] == 7


async def test_finalize_survives_missing_run_for_marker(tmp_path):
    """If get_run returns None (run row absent), marker stamping is a no-op and
    finalize still completes — the exporter's dag_task_id backstop covers it."""
    dag = DAG(dag_id="one", tasks=[_task("compile", output="out/compile")])
    plan = _stored_plan(dag, run_id="wf-norun")
    wf = FakeWorkflow()  # NOT seeded → get_run("wf-norun") is None
    storage = FakeStorage({plan.id: "executing"})
    handler = _handler(tmp_path, runner=_producing_runner("out/compile"))

    res = await dx.record_and_finalize_plan(plan, handler=handler,
                                            workflow=wf, storage=storage)
    assert res.status == "completed"  # finalize unaffected by the missing run


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  OP-1674: the run() seam now DRIVES record_and_finalize_plan (was inert in
#  OP-1659). The wiring lives in the _maybe_claim_and_run_one seam helper —
#  run()'s loop body delegates to it; the helper binds the LocalTaskHandler +
#  calls record_and_finalize_plan. Execution stays default-OFF (the dual gate
#  is what holds the loop inert until an operator opts a dag_id in).
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_run_seam_drives_finalize_via_helper():
    import inspect
    # run()'s loop body delegates to the seam helper...
    run_src = inspect.getsource(dx.DagExecutor.run)
    assert "_maybe_claim_and_run_one" in run_src
    # ...and the seam helper is what wires the existing finalize + handler.
    seam_src = inspect.getsource(dx.DagExecutor._maybe_claim_and_run_one)
    assert "record_and_finalize_plan" in seam_src
    handler_src = inspect.getsource(dx.DagExecutor._build_handler)
    assert "LocalTaskHandler" in handler_src
