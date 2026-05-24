"""OP-1674 — DagExecutor.run() loop seam: claim → execute → finalize.

Covers the acceptance criteria for wiring the EXISTING executor pieces into
the live run() loop (design doc gap B):

  * Code — run() drives ONE claimed + opt-in plan per tick through the
    EXISTING record_and_finalize_plan; lease acquired-before / released-after;
    OMNISIGHT_DAG_PROJECT_ROOT bound (skip if unset); the dual opt-in gate +
    the enable flag are honoured; no-op when disabled.
  * Integration (in-harness double) — an armed loop claims a seeded opt-in
    executing plan and drives it to 'completed'; a plan missing EITHER the
    env-allowlist OR the metadata opt-in is left untouched; unset project_root
    → skip; disabled → no-op.
  * Exercised (local, end-to-end) — a submitted opt-in DAG reaches 'completed'
    through the loop driving the REAL LocalTaskHandler (no injected runner).

These run LOCALLY with in-memory doubles for ``backend.workflow`` /
``backend.dag_storage`` (no DB, no Docker, no host state outside ``tmp_path``),
mirroring ``test_dag_executor_terminal.py``. The doubles reuse the real
``StepRecord`` / ``PlanLease`` and validate ``set_status`` against the REAL
``dag_storage._ALLOWED_TRANSITIONS`` so a drift in the production state-machine
fails these too.
"""

from __future__ import annotations

import time
from pathlib import Path

from backend import dag_executor as dx
from backend import dag_storage as ds
from backend.dag_schema import DAG, Task
from backend.workflow import StepRecord


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  In-memory doubles (mirror the real contract)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class FakeWorkflow:
    """The subset of ``backend.workflow`` the loop + finalize touch:
    ``get_run`` (run-metadata read for the opt-in gate + the OP-1661 marker),
    ``update_run_metadata`` / ``get_step`` / ``record_dag_step`` /
    ``list_steps`` / ``finish``. The step log enforces the real UNIQUE
    ``(run_id, idempotency_key)`` invariant and reuses the real
    :class:`StepRecord`."""

    def __init__(self) -> None:
        self.steps: dict[tuple[str, str], StepRecord] = {}
        self.runs: dict[str, dict] = {}
        self.finishes: list[tuple[str, str]] = []
        self._n = 0

    def seed_run(self, run_id, *, metadata=None, version=0):
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
            return existing
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


class FakeStorage:
    """In-memory ``dag_storage`` double for the loop seam: discovery
    (``list_plans``), the lease CAS (``claim_plan`` / ``release_lease``), and
    the finalize surface (``get_plan`` / ``set_status``). ``set_status`` is
    validated against the REAL ``_ALLOWED_TRANSITIONS`` table so a transition
    production forbids raises here too. Holds real :class:`ds.StoredPlan`
    rows so ``plan.dag()`` re-hydrates a real DAG."""

    def __init__(self, plans: list[ds.StoredPlan]):
        self.by_id: dict[int, ds.StoredPlan] = {p.id: p for p in plans}
        self.leases: dict[int, ds.PlanLease] = {}
        self.transitions: list[tuple[int, str, str]] = []

    async def list_plans(self, dag_id, conn=None):
        return [p for p in self.by_id.values() if p.dag_id == dag_id]

    async def claim_plan(self, plan_id, owner, *,
                         lease_ttl_s=ds.DEFAULT_LEASE_TTL_S, now=None, conn=None):
        ts = time.time() if now is None else now
        cur = self.leases.get(plan_id)
        if cur is not None and cur.owner != owner and cur.claim_expires_at > ts:
            return None  # live lease held by a different owner → no grant
        lease = ds.PlanLease(
            plan_id=plan_id, owner=owner, token=ds._mint_lease_token(owner),
            claim_expires_at=ts + lease_ttl_s, heartbeat_at=ts,
        )
        self.leases[plan_id] = lease
        return lease

    async def release_lease(self, plan_id, owner, token, conn=None):
        cur = self.leases.get(plan_id)
        if cur and cur.owner == owner and cur.token == token:
            del self.leases[plan_id]
            return True
        return False

    async def get_plan(self, plan_id, conn=None):
        return self.by_id[plan_id]

    async def set_status(self, plan_id, new_status, *, run_id=None, conn=None):
        p = self.by_id[plan_id]
        if new_status not in ds._ALLOWED_TRANSITIONS[p.status]:
            raise ValueError(f"illegal transition {p.status!r} -> {new_status!r}")
        self.transitions.append((plan_id, p.status, new_status))
        p.status = new_status
        return p


class _NoopHeartbeat:
    """Heartbeat double so the loop tests need no Redis."""

    def beat(self, *a, **k):  # noqa: D401
        pass

    def clear(self, *a, **k):
        pass


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


def _smoke_dag(dag_id="smoke") -> DAG:
    return DAG(dag_id=dag_id, tasks=[
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


def _producing_runner(*relpaths):
    """A fake runner that 'succeeds' and writes each artifact path so the
    handler's expected_output check passes."""
    def run(argv, cwd, timeout_s):
        for rel in relpaths:
            p = Path(cwd) / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("artifact")
        return 0, "ok\n", ""
    return run


def _arm_env(monkeypatch, tmp_path, *, allow="smoke", project_root="proj"):
    """Set the loop's env contract: enable flag is handled via ``enabled=True``
    on the executor; here we set the allowlist, project root, and a tmp workdir
    so the PlanWorkspaceBuilder never writes under cwd."""
    monkeypatch.setenv(dx.ALLOW_DAG_IDS_ENV, allow)
    proj = tmp_path / project_root
    proj.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(dx.PROJECT_ROOT_ENV, str(proj))
    monkeypatch.setenv("OMNISIGHT_DAG_WORKDIR", str(tmp_path / "wd"))
    return proj


def _make_executor(storage, wf, *, runner=None, enabled=True, max_ticks=1):
    cfg = dx.DagExecutorConfig(
        instance_id="loop", poll_interval_s=0.0,
        heartbeat_interval_s=0.0, max_ticks=max_ticks,
    )
    return dx.DagExecutor(
        cfg, heartbeat=_NoopHeartbeat(), enabled=enabled,
        storage=storage, workflow=wf, task_runner=runner,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pure-unit: the two seam helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_parse_allow_dag_ids_splits_dedupes_and_trims():
    assert dx.parse_allow_dag_ids(None) == ()
    assert dx.parse_allow_dag_ids("") == ()
    assert dx.parse_allow_dag_ids("   ") == ()
    assert dx.parse_allow_dag_ids("smoke") == ("smoke",)
    assert dx.parse_allow_dag_ids(" a , b ,a, ,c") == ("a", "b", "c")


def test_metadata_opt_in_truthiness():
    assert dx.metadata_opt_in({dx.OPT_IN_METADATA_KEY: True}) is True
    assert dx.metadata_opt_in({dx.OPT_IN_METADATA_KEY: "true"}) is True
    assert dx.metadata_opt_in({dx.OPT_IN_METADATA_KEY: "1"}) is True
    # missing / falsey / empty → not opted in
    assert dx.metadata_opt_in({dx.OPT_IN_METADATA_KEY: False}) is False
    assert dx.metadata_opt_in({dx.OPT_IN_METADATA_KEY: "no"}) is False
    assert dx.metadata_opt_in({"other": True}) is False
    assert dx.metadata_opt_in({}) is False
    assert dx.metadata_opt_in(None) is False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  AC: Integration — armed loop drives a seeded opt-in plan to 'completed'
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def test_armed_loop_claims_optin_plan_and_completes(monkeypatch, tmp_path):
    _arm_env(monkeypatch, tmp_path)
    dag = _smoke_dag()
    plan = _stored_plan(dag)
    storage = FakeStorage([plan])
    wf = FakeWorkflow()
    wf.seed_run("wf-smoke", metadata={dx.OPT_IN_METADATA_KEY: True})
    ex = _make_executor(
        storage, wf, runner=_producing_runner("out/compile", "out/test"),
    )

    result = await ex.run()

    assert result.enabled is True and result.ticks == 1
    # the plan was claimed + driven to completed through record_and_finalize
    assert storage.by_id[plan.id].status == "completed"
    assert storage.transitions == [(plan.id, "executing", "completed")]
    assert wf.finishes == [("wf-smoke", "completed")]
    # lease acquired-before / released-after: nothing left held
    assert storage.leases == {}
    # both tasks recorded
    steps = await wf.list_steps("wf-smoke")
    assert {s.dag_task_id for s in steps} == {"compile", "test"}


async def test_lease_released_after_drive(monkeypatch, tmp_path):
    # Lease is held during the drive and released after — assert via a runner
    # that snapshots the lease table mid-flight.
    _arm_env(monkeypatch, tmp_path)
    dag = DAG(dag_id="smoke", tasks=[_task("compile", output="out/compile")])
    plan = _stored_plan(dag)
    storage = FakeStorage([plan])
    wf = FakeWorkflow()
    wf.seed_run("wf-smoke", metadata={dx.OPT_IN_METADATA_KEY: True})

    held: dict[str, object] = {}

    def runner(argv, cwd, timeout_s):
        # the lease must be held while the handler runs
        held["owner"] = storage.leases.get(plan.id)
        p = Path(cwd) / "out" / "compile"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
        return 0, "", ""

    ex = _make_executor(storage, wf, runner=runner)
    await ex.run()

    assert held["owner"] is not None             # held during execution
    assert held["owner"].owner == "dag-exec-loop"
    assert storage.leases == {}                  # released afterwards


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  AC: Integration — dual gate leaves a plan untouched if EITHER half missing
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def test_plan_outside_allowlist_left_untouched(monkeypatch, tmp_path):
    # dag_id not in the allowlist → discovery never finds it → untouched.
    _arm_env(monkeypatch, tmp_path, allow="some-other-dag")
    dag = _smoke_dag()
    plan = _stored_plan(dag)
    storage = FakeStorage([plan])
    wf = FakeWorkflow()
    wf.seed_run("wf-smoke", metadata={dx.OPT_IN_METADATA_KEY: True})
    ex = _make_executor(storage, wf, runner=_producing_runner())

    await ex.run()

    assert storage.by_id[plan.id].status == "executing"  # untouched
    assert storage.transitions == []
    assert storage.leases == {}
    assert wf.finishes == []


async def test_empty_allowlist_is_noop(monkeypatch, tmp_path):
    # No allowlist at all (the default-OFF deployment posture).
    _arm_env(monkeypatch, tmp_path)
    monkeypatch.delenv(dx.ALLOW_DAG_IDS_ENV, raising=False)
    dag = _smoke_dag()
    plan = _stored_plan(dag)
    storage = FakeStorage([plan])
    wf = FakeWorkflow()
    wf.seed_run("wf-smoke", metadata={dx.OPT_IN_METADATA_KEY: True})
    ex = _make_executor(storage, wf, runner=_producing_runner())

    await ex.run()

    assert storage.by_id[plan.id].status == "executing"
    assert storage.transitions == [] and storage.leases == {}


async def test_plan_without_metadata_optin_left_untouched(monkeypatch, tmp_path):
    # In the allowlist, executing — but the run never opted in.
    _arm_env(monkeypatch, tmp_path)
    dag = _smoke_dag()
    plan = _stored_plan(dag)
    storage = FakeStorage([plan])
    wf = FakeWorkflow()
    wf.seed_run("wf-smoke", metadata={"source": "bootstrap"})  # no opt-in
    ex = _make_executor(storage, wf, runner=_producing_runner())

    await ex.run()

    assert storage.by_id[plan.id].status == "executing"  # left untouched
    assert storage.transitions == []
    assert storage.leases == {}                          # never claimed
    assert wf.finishes == []


async def test_plan_with_optin_false_left_untouched(monkeypatch, tmp_path):
    _arm_env(monkeypatch, tmp_path)
    dag = _smoke_dag()
    plan = _stored_plan(dag)
    storage = FakeStorage([plan])
    wf = FakeWorkflow()
    wf.seed_run("wf-smoke", metadata={dx.OPT_IN_METADATA_KEY: False})
    ex = _make_executor(storage, wf, runner=_producing_runner())

    await ex.run()

    assert storage.by_id[plan.id].status == "executing"
    assert storage.transitions == [] and storage.leases == {}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  AC: Code — unset project_root → skip; disabled → no-op
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def test_unset_project_root_skips_execution(monkeypatch, tmp_path):
    _arm_env(monkeypatch, tmp_path)
    monkeypatch.delenv(dx.PROJECT_ROOT_ENV, raising=False)
    dag = _smoke_dag()
    plan = _stored_plan(dag)
    storage = FakeStorage([plan])
    wf = FakeWorkflow()
    wf.seed_run("wf-smoke", metadata={dx.OPT_IN_METADATA_KEY: True})
    ex = _make_executor(storage, wf, runner=_producing_runner())

    result = await ex.run()

    assert result.enabled is True  # armed, but skipped because no project root
    assert storage.by_id[plan.id].status == "executing"
    assert storage.transitions == [] and storage.leases == {}


async def test_disabled_executor_is_noop(monkeypatch, tmp_path):
    _arm_env(monkeypatch, tmp_path)
    dag = _smoke_dag()
    plan = _stored_plan(dag)
    storage = FakeStorage([plan])
    wf = FakeWorkflow()
    wf.seed_run("wf-smoke", metadata={dx.OPT_IN_METADATA_KEY: True})
    ex = _make_executor(
        storage, wf, runner=_producing_runner(), enabled=False,
    )

    result = await ex.run()

    assert result.enabled is False and result.ticks == 0
    assert storage.by_id[plan.id].status == "executing"  # never touched
    assert storage.transitions == [] and storage.leases == {}


async def test_claim_lost_to_live_owner_is_skipped(monkeypatch, tmp_path):
    # Another live owner already holds the lease → claim returns None → the
    # plan is left for that owner (not driven, not re-transitioned by us).
    _arm_env(monkeypatch, tmp_path)
    dag = _smoke_dag()
    plan = _stored_plan(dag)
    storage = FakeStorage([plan])
    # pre-seed a live foreign lease
    storage.leases[plan.id] = ds.PlanLease(
        plan_id=plan.id, owner="dag-exec-other",
        token="foreign", claim_expires_at=time.time() + 1000, heartbeat_at=0.0,
    )
    wf = FakeWorkflow()
    wf.seed_run("wf-smoke", metadata={dx.OPT_IN_METADATA_KEY: True})
    ex = _make_executor(storage, wf, runner=_producing_runner())

    await ex.run()

    assert storage.by_id[plan.id].status == "executing"
    assert storage.transitions == []
    # the foreign lease is intact (we never released someone else's lease)
    assert storage.leases[plan.id].owner == "dag-exec-other"


async def test_only_first_executing_plan_per_dag_id_considered(monkeypatch, tmp_path):
    # Two executing plans for one dag_id: discovery takes the FIRST
    # (round/insertion order); only it is driven this tick (one per tick).
    _arm_env(monkeypatch, tmp_path)
    dag = DAG(dag_id="smoke", tasks=[_task("compile", output="out/compile")])
    first = _stored_plan(dag, plan_id=10, run_id="wf-a")
    second = _stored_plan(dag, plan_id=20, run_id="wf-b")
    storage = FakeStorage([first, second])
    wf = FakeWorkflow()
    wf.seed_run("wf-a", metadata={dx.OPT_IN_METADATA_KEY: True})
    wf.seed_run("wf-b", metadata={dx.OPT_IN_METADATA_KEY: True})
    ex = _make_executor(storage, wf, runner=_producing_runner("out/compile"))

    await ex.run()

    assert storage.by_id[10].status == "completed"   # first driven
    assert storage.by_id[20].status == "executing"   # second left for next tick


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  AC: Exercised (local, end-to-end) — a submitted opt-in DAG reaches
#  'completed' through the loop driving the REAL LocalTaskHandler.
#
#  Uses a self-contained python3 fixture (a bare, non-``external:`` .py input)
#  so the loop is exercised end-to-end against the real handler + a real
#  subprocess, WITHOUT tripping the known F4/OP-1676 handler-internal gap that
#  the committed F1 CMake smoke fixture hits on its run-test step (that gap is
#  out of this ticket's scope — MUST NOT touch the handler internal).
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def test_end_to_end_local_python_dag_completes_via_loop(monkeypatch, tmp_path):
    proj = _arm_env(monkeypatch, tmp_path)
    # a real, runnable python3 task: writes its declared expected_output
    (proj / "make_artifact.py").write_text(
        "import os\n"
        "os.makedirs('out', exist_ok=True)\n"
        "open('out/result.txt', 'w').write('PASS\\n')\n"
    )
    dag = DAG(dag_id="smoke", tasks=[_task(
        "build", toolchain="python3",
        inputs=["make_artifact.py"], output="out/result.txt",
    )])
    plan = _stored_plan(dag, run_id="wf-e2e")
    storage = FakeStorage([plan])
    wf = FakeWorkflow()
    wf.seed_run("wf-e2e", metadata={dx.OPT_IN_METADATA_KEY: True})
    # NOTE: runner=None → the REAL _run_local subprocess runner (python3).
    ex = _make_executor(storage, wf, runner=None)

    result = await ex.run()

    assert result.ticks == 1
    assert storage.by_id[plan.id].status == "completed"
    assert wf.finishes == [("wf-e2e", "completed")]
    step = (await wf.list_steps("wf-e2e"))[0]
    assert step.dag_task_id == "build" and step.is_done
    assert storage.leases == {}  # lease released after the real drive
