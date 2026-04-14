"""Phase 56 — durable workflow checkpointing.

The smallest useful version of "the agent loop survives a crash":

  - `workflow_runs`  one row per logical execution (invoke /
    pipeline_phase / decision_chain / report-gen).
  - `workflow_steps` append-only checkpoint log; (run_id,
    idempotency_key) UNIQUE so repeating the same step returns the
    cached output instead of re-running side effects.

Usage:

    run = await workflow.start("invoke", metadata={"user": "alice"})

    @workflow.step(run, "fetch_repo")
    async def fetch_repo():
        ...
        return {"sha": "abc"}

    sha = (await fetch_repo())["sha"]

    @workflow.step(run, f"compile/{sha}")
    async def compile():
        ...
        return {"image": "fw.bin"}

If the backend crashes after `fetch_repo` succeeded, on restart the
caller invokes the same code path with the same run id — the
decorator sees the cached step output and returns it without
re-running. `compile` then runs for the first time.

Resume helper: `await workflow.replay(run_id)` — returns the run +
its step list so a higher level loop can decide how to continue.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Dataclasses
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

RunStatus = str  # "running" | "completed" | "failed" | "halted"


@dataclass
class WorkflowRun:
    id: str
    kind: str
    started_at: float
    status: RunStatus = "running"
    completed_at: Optional[float] = None
    last_step_id: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class StepRecord:
    id: str
    run_id: str
    idempotency_key: str
    started_at: float
    completed_at: Optional[float] = None
    output: Any = None
    error: Optional[str] = None

    @property
    def is_done(self) -> bool:
        return self.completed_at is not None and self.error is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  DB helpers (raw aiosqlite via backend.db._conn())
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


async def _conn():
    """Lazy import to avoid circular dependency at module load."""
    from backend import db
    return db._conn()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Public API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def start(kind: str, *, metadata: dict[str, Any] | None = None,
                run_id: str | None = None,
                dag: Any | None = None,
                parent_plan_id: int | None = None,
                mutation_round: int = 0) -> WorkflowRun:
    """Open a new workflow run, or attach to an existing one if
    `run_id` is supplied (used by resume paths).

    Phase 56-DAG-B: when `dag` is supplied (a `dag_schema.DAG`), the
    plan is persisted to `dag_plans`, validated by `dag_validator`,
    and bidirectionally linked to the new workflow_run. The run is
    only set to status='executing' if validation passes; failed
    validation leaves the plan at status='failed' and the run still
    starts (caller decides whether to mutate via Phase 56-DAG-C).

    `parent_plan_id` + `mutation_round` chain mutation history.
    """
    conn = await _conn()
    if run_id:
        existing = await get_run(run_id)
        if existing:
            return existing
    run = WorkflowRun(
        id=run_id or _uid("wf"),
        kind=kind,
        started_at=time.time(),
        metadata=metadata or {},
    )
    await conn.execute(
        "INSERT INTO workflow_runs (id, kind, started_at, status, metadata) "
        "VALUES (?, ?, ?, 'running', ?)",
        (run.id, run.kind, run.started_at, json.dumps(run.metadata)),
    )
    await conn.commit()

    # Phase 56-DAG-B: persist + validate + link the plan.
    if dag is not None:
        try:
            from backend import dag_storage as _ds
            from backend import dag_validator as _dv
            result = _dv.validate(dag)
            plan = await _ds.save_plan(
                dag, run_id=run.id,
                parent_plan_id=parent_plan_id,
                status="failed" if not result.ok else "validated",
                mutation_round=mutation_round,
                validation_errors=result.errors if not result.ok else None,
            )
            await _ds.attach_to_run(plan.id, run.id)
            if result.ok:
                await _ds.set_status(plan.id, "executing")
        except Exception as exc:
            # Plan attach failure must not break workflow.start.
            logger.warning("dag plan attach failed for run=%s: %s", run.id, exc)

    logger.info("workflow.start kind=%s id=%s%s",
                kind, run.id,
                f" dag={dag.dag_id}" if dag is not None else "")
    return run


async def mutate_workflow(old_run_id: str, new_dag: Any, *,
                          mutation_round: int) -> WorkflowRun:
    """Phase 56-DAG-B: open a successor workflow_run for a mutated DAG.

    The old run is preserved (Phase 56's append-only invariant) and
    marked with `successor_run_id` pointing at the new run. The new
    plan inherits the old plan's id as `parent_plan_id` so the
    mutation chain is replayable.
    """
    from backend import dag_storage as _ds
    old_plan = await _ds.get_plan_by_run(old_run_id)
    parent_plan_id = old_plan.id if old_plan else None

    new_run = await start(
        kind="invoke",  # mutated runs reuse the invoke kind by default
        dag=new_dag,
        parent_plan_id=parent_plan_id,
        mutation_round=mutation_round,
        metadata={"mutated_from": old_run_id, "mutation_round": mutation_round},
    )
    await _ds.link_successor(old_run_id, new_run.id)
    if old_plan and old_plan.status in {"validated", "executing", "failed"}:
        try:
            await _ds.set_status(old_plan.id, "mutated")
        except Exception as exc:
            logger.warning("mark mutated failed for plan=%s: %s",
                           old_plan.id, exc)
    return new_run


async def get_run(run_id: str) -> Optional[WorkflowRun]:
    conn = await _conn()
    async with conn.execute(
        "SELECT id, kind, started_at, completed_at, status, last_step_id, metadata "
        "FROM workflow_runs WHERE id = ?", (run_id,),
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return None
    return WorkflowRun(
        id=row["id"], kind=row["kind"], started_at=row["started_at"],
        completed_at=row["completed_at"], status=row["status"],
        last_step_id=row["last_step_id"],
        metadata=json.loads(row["metadata"] or "{}"),
    )


async def list_runs(status: str | None = None, limit: int = 50) -> list[WorkflowRun]:
    conn = await _conn()
    if status:
        sql = ("SELECT id, kind, started_at, completed_at, status, last_step_id, metadata "
               "FROM workflow_runs WHERE status=? ORDER BY started_at DESC LIMIT ?")
        params: tuple = (status, limit)
    else:
        sql = ("SELECT id, kind, started_at, completed_at, status, last_step_id, metadata "
               "FROM workflow_runs ORDER BY started_at DESC LIMIT ?")
        params = (limit,)
    async with conn.execute(sql, params) as cur:
        rows = await cur.fetchall()
    return [
        WorkflowRun(
            id=r["id"], kind=r["kind"], started_at=r["started_at"],
            completed_at=r["completed_at"], status=r["status"],
            last_step_id=r["last_step_id"],
            metadata=json.loads(r["metadata"] or "{}"),
        )
        for r in rows
    ]


async def list_steps(run_id: str) -> list[StepRecord]:
    conn = await _conn()
    async with conn.execute(
        "SELECT id, run_id, idempotency_key, started_at, completed_at, output_json, error "
        "FROM workflow_steps WHERE run_id=? ORDER BY started_at",
        (run_id,),
    ) as cur:
        rows = await cur.fetchall()
    out: list[StepRecord] = []
    for r in rows:
        out.append(StepRecord(
            id=r["id"], run_id=r["run_id"],
            idempotency_key=r["idempotency_key"],
            started_at=r["started_at"], completed_at=r["completed_at"],
            output=json.loads(r["output_json"]) if r["output_json"] else None,
            error=r["error"],
        ))
    return out


async def finish(run_id: str, status: RunStatus = "completed") -> None:
    conn = await _conn()
    await conn.execute(
        "UPDATE workflow_runs SET status=?, completed_at=? WHERE id=?",
        (status, time.time(), run_id),
    )
    await conn.commit()

    # Phase 62 hook: when a long / hard-fought run completes successfully
    # and OMNISIGHT_SELF_IMPROVE_LEVEL includes L1, distil it into a
    # skill candidate and file a Decision Engine proposal. Failures here
    # must NEVER break the workflow.finish contract — wrap everything.
    if status == "completed":
        try:
            from backend import skills_extractor as _ex
            if _ex.is_enabled():
                run = await get_run(run_id)
                steps = await list_steps(run_id)
                if run is not None:
                    result = _ex.extract(run, steps)
                    if result.written:
                        _ex.propose_promotion(result, run)
        except Exception as exc:
            logger.warning(
                "skills extractor hook failed for run=%s: %s", run_id, exc,
            )


async def _get_step(run_id: str, idempotency_key: str) -> Optional[StepRecord]:
    conn = await _conn()
    async with conn.execute(
        "SELECT id, run_id, idempotency_key, started_at, completed_at, output_json, error "
        "FROM workflow_steps WHERE run_id=? AND idempotency_key=?",
        (run_id, idempotency_key),
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return None
    return StepRecord(
        id=row["id"], run_id=row["run_id"],
        idempotency_key=row["idempotency_key"],
        started_at=row["started_at"], completed_at=row["completed_at"],
        output=json.loads(row["output_json"]) if row["output_json"] else None,
        error=row["error"],
    )


async def _record_step(run_id: str, idempotency_key: str,
                       output: Any, error: str | None) -> StepRecord:
    """Atomically insert a finished step. UNIQUE constraint catches a
    race with another writer — in which case we read back the existing
    record and return that (last writer wins on raw run, but for
    idempotency the cached output is what we want anyway)."""
    conn = await _conn()
    step = StepRecord(
        id=_uid("step"), run_id=run_id, idempotency_key=idempotency_key,
        started_at=time.time(), completed_at=time.time(),
        output=output, error=error,
    )
    try:
        await conn.execute(
            "INSERT INTO workflow_steps "
            "(id, run_id, idempotency_key, started_at, completed_at, output_json, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (step.id, step.run_id, step.idempotency_key, step.started_at,
             step.completed_at, json.dumps(output) if output is not None else None,
             error),
        )
        await conn.execute(
            "UPDATE workflow_runs SET last_step_id=? WHERE id=?",
            (step.id, run_id),
        )
        await conn.commit()
        return step
    except Exception as exc:
        # Likely UNIQUE collision — re-read.
        if "UNIQUE" not in str(exc):
            logger.warning("workflow._record_step error: %s", exc)
        await conn.rollback()
        existing = await _get_step(run_id, idempotency_key)
        if existing:
            return existing
        raise


def step(run: WorkflowRun, idempotency_key: str):
    """Decorator: wrap an async callable so it only runs once per
    (run, key). On second call with the same key, returns the cached
    output without invoking the body.

    Use distinct keys for genuinely separate work (`fetch_repo`,
    `compile/<sha>`, `push/<branch>`); reuse the same key when you
    want exactly-once semantics across crash-and-resume.
    """
    def decorator(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            cached = await _get_step(run.id, idempotency_key)
            if cached and cached.is_done:
                logger.info("workflow.step cache-hit run=%s key=%s", run.id, idempotency_key)
                return cached.output
            try:
                result = await fn(*args, **kwargs)
            except Exception as exc:
                # Record the failure (so we can audit / inspect)
                await _record_step(run.id, idempotency_key, output=None,
                                   error=f"{type(exc).__name__}: {exc!s}"[:512])
                raise
            await _record_step(run.id, idempotency_key, output=result, error=None)
            return result
        return wrapper
    return decorator


async def replay(run_id: str) -> Optional[dict[str, Any]]:
    """Read run + steps for a resume / inspection caller. Returns the
    structure the /invoke/resume endpoint exposes."""
    run = await get_run(run_id)
    if not run:
        return None
    steps = await list_steps(run_id)
    return {
        "run": {
            "id": run.id, "kind": run.kind, "status": run.status,
            "started_at": run.started_at, "completed_at": run.completed_at,
            "last_step_id": run.last_step_id, "metadata": run.metadata,
        },
        "steps": [
            {
                "id": s.id, "key": s.idempotency_key,
                "started_at": s.started_at, "completed_at": s.completed_at,
                "is_done": s.is_done, "error": s.error,
                "output": s.output if s.is_done else None,
            }
            for s in steps
        ],
        "in_flight": run.status == "running",
    }


async def list_in_flight_on_startup() -> list[WorkflowRun]:
    """Called from the FastAPI lifespan: any workflow_runs.status =
    'running' on cold start represents a workflow that was alive when
    the previous process died. Surface them so the UI can show a
    "resume?" notification banner."""
    return await list_runs(status="running", limit=200)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Test hooks
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def _reset_for_tests() -> None:
    """Clear both tables. NOT for production use."""
    conn = await _conn()
    await conn.execute("DELETE FROM workflow_steps")
    await conn.execute("DELETE FROM workflow_runs")
    await conn.commit()
