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

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from backend.db_context import tenant_insert_value, tenant_where_pg
from backend.db_pool import get_pool

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
    version: int = 0


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
#  DB helpers — native asyncpg (SP-5.6a port)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_RUN_COLS = (
    "id, kind, started_at, completed_at, status, "
    "last_step_id, metadata, version"
)
_STEP_COLS = (
    "id, run_id, idempotency_key, started_at, completed_at, "
    "output_json, error"
)


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Public API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def start(kind: str, *, metadata: dict[str, Any] | None = None,
                run_id: str | None = None,
                dag: Any | None = None,
                parent_plan_id: int | None = None,
                mutation_round: int = 0,
                target_profile: dict | None = None) -> WorkflowRun:
    """Open a new workflow run, or attach to an existing one if
    `run_id` is supplied (used by resume paths).

    Phase 56-DAG-B: when `dag` is supplied (a `dag_schema.DAG`), the
    plan is persisted to `dag_plans`, validated by `dag_validator`,
    and bidirectionally linked to the new workflow_run. The run is
    only set to status='executing' if validation passes; failed
    validation leaves the plan at status='failed' and the run still
    starts (caller decides whether to mutate via Phase 56-DAG-C).

    Phase 64-C-LOCAL S4: `target_profile` flows through to the
    validator so t3 tasks with a host==target profile get the
    tier-relaxation treatment (pre-64-C callers pass None and keep
    the narrow hardware-bridge-only behaviour).

    `parent_plan_id` + `mutation_round` chain mutation history.
    """
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
    async with get_pool().acquire() as conn:
        await conn.execute(
            "INSERT INTO workflow_runs "
            "(id, kind, started_at, status, metadata, tenant_id) "
            "VALUES ($1, $2, $3, 'running', $4, $5)",
            run.id, run.kind, run.started_at,
            json.dumps(run.metadata), tenant_insert_value(),
        )

    # Q.3-SUB-1 (#297): cross-device SSE broadcast. Best-effort —
    # the workflow row is already committed, a flaky bus must not
    # fail start().
    try:
        from backend.events import emit_workflow_updated
        emit_workflow_updated(run.id, run.status, run.version, kind=run.kind)
    except Exception as exc:
        logger.debug("emit_workflow_updated on start failed: %s", exc)

    # Phase 56-DAG-B: persist + validate + link the plan.
    if dag is not None:
        try:
            from backend import dag_storage as _ds
            from backend import dag_validator as _dv
            result = _dv.validate(dag, target_profile=target_profile)
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


def _row_to_run(r) -> "WorkflowRun":
    return WorkflowRun(
        id=r["id"], kind=r["kind"], started_at=r["started_at"],
        completed_at=r["completed_at"], status=r["status"],
        last_step_id=r["last_step_id"],
        metadata=json.loads(r["metadata"] or "{}"),
        version=r["version"],
    )


async def get_run(run_id: str) -> Optional[WorkflowRun]:
    params: list = [run_id]
    conditions = ["id = $1"]
    tenant_where_pg(conditions, params)
    sql = (
        f"SELECT {_RUN_COLS} FROM workflow_runs "
        f"WHERE {' AND '.join(conditions)}"
    )
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(sql, *params)
    return _row_to_run(row) if row else None


async def list_runs(
    status: str | None = None, limit: int = 50,
) -> list[WorkflowRun]:
    conditions: list[str] = []
    p: list = []
    tenant_where_pg(conditions, p)
    if status:
        p.append(status)
        conditions.append(f"status = ${len(p)}")
    sql = f"SELECT {_RUN_COLS} FROM workflow_runs"
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    p.append(limit)
    sql += f" ORDER BY started_at DESC LIMIT ${len(p)}"
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(sql, *p)
    return [_row_to_run(r) for r in rows]


def _row_to_step(r) -> "StepRecord":
    return StepRecord(
        id=r["id"], run_id=r["run_id"],
        idempotency_key=r["idempotency_key"],
        started_at=r["started_at"], completed_at=r["completed_at"],
        output=json.loads(r["output_json"]) if r["output_json"] else None,
        error=r["error"],
    )


async def list_steps(run_id: str) -> list[StepRecord]:
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            f"SELECT {_STEP_COLS} FROM workflow_steps "
            "WHERE run_id = $1 ORDER BY started_at",
            run_id,
        )
    return [_row_to_step(r) for r in rows]


class VersionConflict(Exception):
    """Raised when an optimistic-lock version check fails (HTTP 409)."""


def _emit_workflow_updated_safe(run_id: str, status: str, version: int,
                                kind: str | None = None) -> None:
    """Q.3-SUB-1 (#297): fire emit_workflow_updated, swallowing errors.

    Used by every workflow_runs UPDATE path (finish / cancel_run /
    retry_run / update_run_metadata). A flaky SSE bus / Redis
    outage must NEVER fail a committed workflow mutation — the
    emit runs AFTER the version bump returned successfully, so
    by this point the truth is in PG and the SSE push is purely a
    UI-latency optimisation.
    """
    try:
        from backend.events import emit_workflow_updated
        emit_workflow_updated(run_id, status, version, kind=kind)
    except Exception as exc:
        logger.debug("emit_workflow_updated failed for %s: %s", run_id, exc)


async def _bump_version(run_id: str, expected_version: int | None,
                        updates: dict[str, Any]) -> int:
    """Apply column updates to a workflow_run with optimistic locking.

    Returns the new version.  Raises VersionConflict when the row's
    current version doesn't match `expected_version`.  When
    `expected_version` is None the check is skipped (internal callers).

    SP-5.6a: now uses ``UPDATE ... RETURNING version`` so the
    success/miss decision rides the same query (asyncpg doesn't
    expose ``rowcount`` on Pool.execute the way the compat wrapper
    did). RETURNING gives us the post-bump version directly, no
    need to compute it from expected_version.
    """
    set_parts: list[str] = []
    params: list[Any] = []
    for col, val in updates.items():
        params.append(val)
        set_parts.append(f"{col} = ${len(params)}")
    set_parts.append("version = version + 1")
    if expected_version is not None:
        params.append(run_id)
        params.append(expected_version)
        where = f"id = ${len(params) - 1} AND version = ${len(params)}"
    else:
        params.append(run_id)
        where = f"id = ${len(params)}"
    sql = (
        f"UPDATE workflow_runs SET {', '.join(set_parts)} "
        f"WHERE {where} RETURNING version"
    )
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(sql, *params)
    if row is None:
        raise VersionConflict(run_id)
    return int(row["version"])


async def finish(run_id: str, status: RunStatus = "completed",
                 expected_version: int | None = None) -> None:
    new_version = await _bump_version(run_id, expected_version, {
        "status": status,
        "completed_at": time.time(),
    })
    _emit_workflow_updated_safe(run_id, status, new_version)

    # Y9 #285 row 3 — fan one ``workflow_run`` billing event per
    # finish() call into ``billing_usage_events`` so T4 can roll up
    # run-counts + duration per ``(tenant_id, project_id)`` tuple.
    # Cost is 0.0 — the LLM calls inside the run already wrote their
    # own ``llm_call`` rows; this row is the run-count + duration
    # billing signal only and never double-counts spend (Y9 row 3
    # contract). Reads ``workflow_runs.tenant_id`` / ``project_id``
    # directly off the row so we don't depend on the request-scope
    # ContextVar still being set when ``finish`` is called from a
    # background task. Best-effort — billing emit failure must never
    # regress the workflow.finish contract.
    try:
        from backend import billing_usage as _billing
        from backend.db_pool import get_pool as _get_pool
        # Y9 #285 row 4 — LEFT JOIN ``projects`` so the billing fan-out
        # carries ``product_line`` (column lives on projects, not
        # workflow_runs). NULL ``project_id`` (legacy pre-Y1 rows) +
        # missing project row both yield ``product_line = None`` which
        # buckets to ``"unknown"`` in the Prometheus label without
        # crashing the JOIN — LEFT JOIN keeps the workflow_run row even
        # when the project is unknown / archived.
        async with _get_pool().acquire() as _conn:
            row = await _conn.fetchrow(
                "SELECT wr.kind, wr.started_at, wr.completed_at, "
                "wr.tenant_id, wr.project_id, p.product_line "
                "FROM workflow_runs wr "
                "LEFT JOIN projects p ON p.id = wr.project_id "
                "WHERE wr.id = $1",
                run_id,
            )
        if row is not None:
            duration_ms: int | None = None
            try:
                if row["completed_at"] is not None and row["started_at"] is not None:
                    duration_ms = int(
                        (float(row["completed_at"]) - float(row["started_at"])) * 1000
                    )
            except Exception:
                duration_ms = None
            await _billing.record_workflow_run(
                workflow_run_id=run_id,
                workflow_kind=row["kind"] or "",
                workflow_status=status,
                duration_ms=duration_ms,
                tenant_id=row["tenant_id"],
                project_id=row["project_id"],
                product_line=row["product_line"],
            )
    except Exception as exc:
        logger.warning(
            "billing_usage.record_workflow_run failed for run=%s: %s",
            run_id, exc,
        )

    # Phase 62 hook: when a long / hard-fought run completes successfully
    # and OMNISIGHT_SELF_IMPROVE_LEVEL includes L1, distil it into a
    # skill candidate and file a Decision Engine proposal. Failures here
    # must NEVER break the workflow.finish contract — wrap everything.
    if status == "completed":
        run = None
        steps = None
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
        try:
            from backend import skill_distiller as _distiller
            if _distiller.is_enabled():
                if run is None:
                    run = await get_run(run_id)
                if steps is None:
                    steps = await list_steps(run_id)
                if run is not None:
                    await _distiller.architect_guild_hook(run, steps)
        except Exception as exc:
            logger.warning(
                "skill distiller hook failed for run=%s: %s", run_id, exc,
            )


async def cancel_run(run_id: str, expected_version: int) -> int:
    """Cancel a running workflow. Returns the new version."""
    new_version = await _bump_version(run_id, expected_version, {
        "status": "halted",
        "completed_at": time.time(),
    })
    _emit_workflow_updated_safe(run_id, "halted", new_version)
    return new_version


async def retry_run(run_id: str, expected_version: int) -> WorkflowRun:
    """Reset a failed/halted run back to 'running' for retry.
    Returns the updated WorkflowRun."""
    await _bump_version(run_id, expected_version, {
        "status": "running",
        "completed_at": None,
    })
    run = await get_run(run_id)
    assert run is not None
    _emit_workflow_updated_safe(run.id, run.status, run.version, kind=run.kind)
    return run


async def update_run_metadata(run_id: str, expected_version: int,
                              metadata: dict[str, Any]) -> int:
    """Merge new metadata into the run with version guard."""
    existing = await get_run(run_id)
    if not existing:
        raise ValueError(f"run {run_id} not found")
    merged = {**existing.metadata, **metadata}
    new_version = await _bump_version(run_id, expected_version, {
        "metadata": json.dumps(merged),
    })
    # Status is unchanged for metadata-only patches — emit so other
    # devices still see the version bump (etag refresh).
    _emit_workflow_updated_safe(run_id, existing.status, new_version,
                                kind=existing.kind)
    return new_version


async def _get_step(run_id: str, idempotency_key: str) -> Optional[StepRecord]:
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT {_STEP_COLS} FROM workflow_steps "
            "WHERE run_id = $1 AND idempotency_key = $2",
            run_id, idempotency_key,
        )
    return _row_to_step(row) if row else None


async def _record_step(run_id: str, idempotency_key: str,
                       output: Any, error: str | None) -> StepRecord:
    """Atomically insert a finished step. UNIQUE constraint catches a
    race with another writer — in which case we read back the existing
    record and return that (last writer wins on raw run, but for
    idempotency the cached output is what we want anyway).

    SP-5.6a: INSERT + UPDATE now run inside a single tx so the
    last_step_id bump on workflow_runs is committed with the
    workflow_steps row (previously under compat a crash between
    statements could leave the step recorded but last_step_id
    pointing at a prior step — visible to replay() as an
    inconsistency). On UNIQUE-violation we roll back explicitly via
    the ``async with transaction()`` exception path and fall through
    to the idempotency re-read.
    """
    step = StepRecord(
        id=_uid("step"), run_id=run_id, idempotency_key=idempotency_key,
        started_at=time.time(), completed_at=time.time(),
        output=output, error=error,
    )
    try:
        async with get_pool().acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO workflow_steps "
                    "(id, run_id, idempotency_key, started_at, "
                    " completed_at, output_json, error) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7)",
                    step.id, step.run_id, step.idempotency_key,
                    step.started_at, step.completed_at,
                    json.dumps(output) if output is not None else None,
                    error,
                )
                await conn.execute(
                    "UPDATE workflow_runs SET last_step_id = $1 "
                    "WHERE id = $2",
                    step.id, run_id,
                )
        return step
    except Exception as exc:
        # Likely UNIQUE collision — the tx already rolled back.
        if "unique" not in str(exc).lower():
            logger.warning("workflow._record_step error: %s", exc)
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
            "version": run.version,
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
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM workflow_steps")
            await conn.execute("DELETE FROM workflow_runs")
