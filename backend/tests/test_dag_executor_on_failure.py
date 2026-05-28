"""OP-1832 (1E) — executor honors ``Task.on_failure`` (abort vs continue).

OP-1831 (1F.P4) added ``Task.on_failure: Literal["abort","continue"]="abort"``
but the executor did not read it: ANY task failure hard-aborted the plan to
``failed`` (OP-1659 terminal wiring). This slice teaches
:func:`backend.dag_executor.record_and_finalize_plan` to branch on it:

  * ``"abort"`` (default): UNCHANGED — stop the walk, plan terminal ``failed``;
  * ``"continue"``: record the failure, skip-cascade the failed task's
    TRANSITIVE dependents (their upstream output is gone), continue the rest;
  * plan terminal ``failed`` iff an ``abort``-failure occurred, else
    ``completed`` — the per-task step records (``failed`` / ``skipped``) carry
    the truth.

Runs LOCALLY with in-memory doubles for ``backend.workflow`` /
``backend.dag_storage`` (no DB / Docker / host state outside ``tmp_path``),
mirroring the real contract used by ``test_dag_executor_terminal.py``: a UNIQUE
``(run_id, idempotency_key)`` step log over the real :class:`StepRecord` (so
``is_done`` is the real property) and the real ``dag_storage._ALLOWED_TRANSITIONS``
state-machine for ``set_status`` — a drift in either fails these too.
"""

from __future__ import annotations

import time
from pathlib import Path

from backend import dag_executor as dx
from backend import dag_storage as ds
from backend.dag_schema import DAG, Task
from backend.workflow import StepRecord


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  In-memory doubles (mirror the real workflow / dag_storage contract)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class FakeWorkflow:
    """Subset of ``backend.workflow`` the finalize path calls, in memory.

    Enforces the real UNIQUE ``(run_id, idempotency_key)`` step invariant and
    reuses the real :class:`StepRecord` so ``is_done`` is the real property.
    """

    def __init__(self) -> None:
        self.steps: dict[tuple[str, str], StepRecord] = {}
        self.runs: dict[str, dict] = {}
        self.finishes: list[tuple[str, str]] = []
        self._n = 0

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
            raise RuntimeError("version conflict")
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
        if existing is not None:
            return existing  # done-skip / UNIQUE collision read-back
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

    def step(self, run_id, task_id) -> StepRecord:
        return self.steps[(run_id, dx.dag_step_key(task_id))]


class _FakePlan:
    def __init__(self, plan_id, status):
        self.id = plan_id
        self.status = status


class FakeStorage:
    """In-memory ``dag_storage`` double; ``set_status`` validated against the
    REAL ``_ALLOWED_TRANSITIONS`` table."""

    def __init__(self, plans: dict[int, str]):
        self.plans = dict(plans)
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

PLAN_ID = 500


def _task(task_id, *, on_failure="abort", depends_on=None,
          toolchain="make", tier="t1") -> Task:
    return Task(
        task_id=task_id, description=f"task {task_id}",
        required_tier=tier, toolchain=toolchain,
        expected_output=f"out/{task_id}",
        inputs=[], depends_on=depends_on or [], on_failure=on_failure,
    )


def _stored_plan(dag: DAG, *, run_id="wf-of", status="executing") -> ds.StoredPlan:
    return ds.StoredPlan(
        id=PLAN_ID, dag_id=dag.dag_id, run_id=run_id, parent_plan_id=None,
        json_body=dag.model_dump_json(), status=status, mutation_round=0,
        validation_errors=None, created_at=0.0, updated_at=0.0,
    )


def _handler(tmp_path, *, runner) -> dx.LocalTaskHandler:
    builder = dx.PlanWorkspaceBuilder(workdir_root=tmp_path / "wd")
    return dx.LocalTaskHandler(workspace_builder=builder, runner=runner)


def _runner(fail=()):
    """A fake runner that FAILS (rc=2) for any task whose id is in ``fail``,
    else writes that task's ``out/<id>`` artifact and succeeds. Records the
    task ids it was actually invoked for on ``run.executed`` (a skipped task
    never runs the handler, so it never appears)."""
    fail = set(fail)
    executed: list[str] = []

    def run(argv, cwd, timeout_s):
        # workspace basename is ``{plan_id}-{task_id}`` (PlanWorkspaceBuilder)
        task_id = Path(cwd).name.split("-", 1)[1]
        executed.append(task_id)
        if task_id in fail:
            return 2, "", "boom\n"
        p = Path(cwd) / "out" / task_id
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("artifact")
        return 0, "ok\n", ""

    run.executed = executed
    return run


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Unit: the transitive-dependents helper (the skip-cascade frontier)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_transitive_dependents_follows_edges_forward():
    # A -> B -> D ; C independent. Dependents of A are {B, D}, NOT C, NOT A.
    dag = DAG(dag_id="d", tasks=[
        _task("A"), _task("B", depends_on=["A"]),
        _task("D", depends_on=["B"]), _task("C"),
    ])
    assert dx._transitive_dependents(dag, "A") == {"B", "D"}
    assert dx._transitive_dependents(dag, "B") == {"D"}
    assert dx._transitive_dependents(dag, "C") == set()
    assert dx._transitive_dependents(dag, "D") == set()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  AC: regression — default abort stops the walk + fails the plan (unchanged)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def test_default_abort_failure_stops_walk_and_fails_plan(tmp_path):
    # 2-task DAG: A (on_failure defaults to "abort") fails -> plan 'failed',
    # the walk stops, downstream B never runs. Proves the default is unchanged.
    dag = DAG(dag_id="abort", tasks=[
        _task("A"),  # default on_failure == "abort"
        _task("B", depends_on=["A"]),
    ])
    assert dag.tasks[0].on_failure == "abort"
    plan = _stored_plan(dag, run_id="wf-abort")
    wf = FakeWorkflow()
    storage = FakeStorage({PLAN_ID: "executing"})
    runner = _runner(fail={"A"})
    handler = _handler(tmp_path, runner=runner)

    res = await dx.record_and_finalize_plan(
        plan, handler=handler, workflow=wf, storage=storage,
    )

    assert res.status == "failed"
    assert storage.plans[PLAN_ID] == "failed"
    assert storage.transitions == [(PLAN_ID, "executing", "failed")]
    assert wf.finishes == [("wf-abort", "failed")]
    # walk stopped at the first failure: only A recorded, B never ran/recorded
    steps = await wf.list_steps("wf-abort")
    assert [s.dag_task_id for s in steps] == ["A"]
    assert not wf.step("wf-abort", "A").is_done
    assert runner.executed == ["A"]  # B downstream not executed


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  AC: continue + skip-cascade — A(continue,fails)->B ; C independent
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def test_continue_failure_skips_dependents_and_completes(tmp_path):
    # [A(continue, fails) -> B(depends A) ; C(independent)]:
    # A recorded failed, B recorded skipped (NOT executed), C runs, plan
    # terminal 'completed'.
    dag = DAG(dag_id="cont", tasks=[
        _task("A", on_failure="continue"),
        _task("B", depends_on=["A"]),
        _task("C"),
    ])
    plan = _stored_plan(dag, run_id="wf-cont")
    wf = FakeWorkflow()
    storage = FakeStorage({PLAN_ID: "executing"})
    runner = _runner(fail={"A"})
    handler = _handler(tmp_path, runner=runner)

    res = await dx.record_and_finalize_plan(
        plan, handler=handler, workflow=wf, storage=storage,
    )

    # plan completes despite the continue-failure (no abort-failure occurred)
    assert res.status == "completed"
    assert storage.plans[PLAN_ID] == "completed"
    assert storage.transitions == [(PLAN_ID, "executing", "completed")]
    assert wf.finishes == [("wf-cont", "completed")]

    # B did NOT execute; C DID. A executed (it had to run to fail).
    assert runner.executed == ["A", "C"]
    assert "B" not in runner.executed

    # all three tasks have a step carrying their real status
    assert sorted(res.recorded) == ["A", "B", "C"]
    a, b, c = (wf.step("wf-cont", t) for t in ("A", "B", "C"))
    # A: failed (error set, not done)
    assert not a.is_done and a.error
    # B: skipped (status carried in output, no error → done so re-claim no-ops)
    assert b.error is None and b.output["status"] == "skipped"
    # C: ok
    assert c.is_done and c.output["status"] == "ok"


async def test_continue_failure_cascades_transitively(tmp_path):
    # A(continue, fails) -> B -> D ; C independent. BOTH B and D are skipped
    # (transitive), C runs, plan completes.
    dag = DAG(dag_id="trans", tasks=[
        _task("A", on_failure="continue"),
        _task("B", depends_on=["A"]),
        _task("D", depends_on=["B"]),
        _task("C"),
    ])
    plan = _stored_plan(dag, run_id="wf-trans")
    wf = FakeWorkflow()
    storage = FakeStorage({PLAN_ID: "executing"})
    runner = _runner(fail={"A"})
    handler = _handler(tmp_path, runner=runner)

    res = await dx.record_and_finalize_plan(
        plan, handler=handler, workflow=wf, storage=storage,
    )

    assert res.status == "completed"
    assert runner.executed == ["A", "C"]  # B and D both skipped
    assert wf.step("wf-trans", "B").output["status"] == "skipped"
    assert wf.step("wf-trans", "D").output["status"] == "skipped"
    assert wf.step("wf-trans", "C").is_done


async def test_continue_failure_with_no_dependents_completes(tmp_path):
    # A continue-failure with NO dependents is recorded failed but, with nothing
    # to cascade and no abort-failure, the plan still completes.
    dag = DAG(dag_id="leaf", tasks=[
        _task("A", on_failure="continue"),
        _task("C"),
    ])
    plan = _stored_plan(dag, run_id="wf-leaf")
    wf = FakeWorkflow()
    storage = FakeStorage({PLAN_ID: "executing"})
    runner = _runner(fail={"A"})
    handler = _handler(tmp_path, runner=runner)

    res = await dx.record_and_finalize_plan(
        plan, handler=handler, workflow=wf, storage=storage,
    )
    assert res.status == "completed"
    assert runner.executed == ["A", "C"]
    assert not wf.step("wf-leaf", "A").is_done
    assert wf.step("wf-leaf", "C").is_done


async def test_abort_wins_over_a_prior_continue_failure(tmp_path):
    # A(continue, fails) then an independent E(abort, fails): the abort-failure
    # still drives the plan to 'failed' even though a continue-failure preceded
    # it. Declaration order [A, B(dep A), E] → walk A, B(skipped), E(abort→fail).
    dag = DAG(dag_id="mixed", tasks=[
        _task("A", on_failure="continue"),
        _task("B", depends_on=["A"]),
        _task("E"),  # default abort, fails
    ])
    plan = _stored_plan(dag, run_id="wf-mixed")
    wf = FakeWorkflow()
    storage = FakeStorage({PLAN_ID: "executing"})
    runner = _runner(fail={"A", "E"})
    handler = _handler(tmp_path, runner=runner)

    res = await dx.record_and_finalize_plan(
        plan, handler=handler, workflow=wf, storage=storage,
    )
    assert res.status == "failed"
    assert storage.plans[PLAN_ID] == "failed"
    # A ran+failed, B skipped, E ran+failed (abort) — walk stops at E
    assert runner.executed == ["A", "E"]
    assert wf.step("wf-mixed", "B").output["status"] == "skipped"
    assert not wf.step("wf-mixed", "E").is_done


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Re-claim: a mid-flight crash mid-continue resumes without failing the plan
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def test_reclaim_resumes_continue_failure_without_failing_plan(tmp_path):
    # Crash window: A's failed step + B's skipped step were already recorded but
    # set_status('completed') never ran (plan still 'executing'). Re-claim must
    # NOT treat A's prior failed step as an abort — it honors on_failure and the
    # plan completes; C runs for the first time.
    dag = DAG(dag_id="reclaim", tasks=[
        _task("A", on_failure="continue"),
        _task("B", depends_on=["A"]),
        _task("C"),
    ])
    plan = _stored_plan(dag, run_id="wf-reclaim")
    wf = FakeWorkflow()
    # pre-seed prior steps as a crashed first invocation would have left them
    await wf.record_dag_step("wf-reclaim", dx.dag_step_key("A"),
                             dag_task_id="A", error="boom rc=2")
    await wf.record_dag_step("wf-reclaim", dx.dag_step_key("B"),
                             dag_task_id="B",
                             output=dx._skipped_step_output(dag.tasks[1]))
    storage = FakeStorage({PLAN_ID: "executing"})
    runner = _runner()  # nothing fails on the resume
    handler = _handler(tmp_path, runner=runner)

    res = await dx.record_and_finalize_plan(
        plan, handler=handler, workflow=wf, storage=storage,
    )

    assert res.status == "completed"
    assert storage.plans[PLAN_ID] == "completed"
    assert runner.executed == ["C"]  # A/B not re-run; only C runs now
    assert sorted(res.recorded) == ["A", "B", "C"]
