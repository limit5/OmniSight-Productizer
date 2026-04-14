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
                run_id: str | None = None) -> WorkflowRun:
    """Open a new workflow run, or attach to an existing one if
    `run_id` is supplied (used by resume paths)."""
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
    logger.info("workflow.start kind=%s id=%s", kind, run.id)
    return run


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
