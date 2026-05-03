"""Phase 56-DAG-B — DAG plan persistence.

Thin wrapper over the `dag_plans` table introduced in Phase 56-DAG-B.
Validation lives in `dag_validator.py` (Phase 56-DAG-A); this module
only persists / queries / chains plans.

Status state-machine (no skips, no reverse transitions):

       (DAG submitted)
              │
              ▼
          pending ──────► validated ──────► executing ──────► completed
              │                                  │
              ▼                                  ▼
           failed                            mutated  ──► (new pending plan)
                                                 │
                                                 ▼
                                            exhausted

Phase-3-Runtime-v2 SP-5.1 (2026-04-21): ported from aiosqlite compat
wrapper to native asyncpg pool. ``set_status`` and ``attach_to_run``
run inside ``async with conn.transaction()`` so their multi-statement
read-then-write sequences don't interleave with concurrent writers.

Module-global audit (SOP Step 1, 2026-04-21 rule): this module's only
top-level state is ``logger``, the ``_VALID_STATUSES`` set, the
``_ALLOWED_TRANSITIONS`` dict, and the ``StoredPlan`` dataclass — all
stable constants derived at import time. Each worker computes the
same values from the same source, so no cross-worker coordination
concerns beyond what PG row locks already provide.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Optional

from backend.dag_schema import DAG
from backend.dag_validator import ValidationError as DagValidationError
from backend.db_pool import get_pool

logger = logging.getLogger(__name__)


_VALID_STATUSES = {
    "pending", "validated", "failed",
    "executing", "completed", "mutated", "exhausted",
}

# Forward transitions only — reject anything else at write time.
_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "pending":   {"validated", "failed"},
    "validated": {"executing", "mutated", "exhausted"},
    "failed":    {"mutated", "exhausted"},
    "executing": {"completed", "mutated", "exhausted"},
    "completed": set(),
    "mutated":   set(),
    "exhausted": set(),
}


@dataclass
class StoredPlan:
    id: int
    dag_id: str
    run_id: Optional[str]
    parent_plan_id: Optional[int]
    json_body: str
    status: str
    mutation_round: int
    validation_errors: Optional[str]
    created_at: float
    updated_at: float

    def dag(self) -> DAG:
        """Re-hydrate the DAG model from JSON."""
        return DAG.model_validate(json.loads(self.json_body))

    def errors(self) -> list[dict]:
        if not self.validation_errors:
            return []
        try:
            return json.loads(self.validation_errors)
        except Exception:
            return []


def _row_to_plan(row) -> StoredPlan:
    return StoredPlan(
        id=row["id"], dag_id=row["dag_id"], run_id=row["run_id"],
        parent_plan_id=row["parent_plan_id"], json_body=row["json_body"],
        status=row["status"], mutation_round=row["mutation_round"],
        validation_errors=row["validation_errors"],
        created_at=row["created_at"], updated_at=row["updated_at"],
    )


_PLAN_COLS = (
    "id, dag_id, run_id, parent_plan_id, json_body, status, "
    "mutation_round, validation_errors, created_at, updated_at"
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CRUD
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def _save_plan_impl(
    conn, dag: DAG, run_id: Optional[str], parent_plan_id: Optional[int],
    status: str, mutation_round: int,
    validation_errors: Optional[list[DagValidationError]],
) -> int:
    now = time.time()
    err_json = (
        json.dumps([e.to_dict() for e in validation_errors])
        if validation_errors else None
    )
    row = await conn.fetchrow(
        "INSERT INTO dag_plans "
        "(dag_id, run_id, parent_plan_id, json_body, status, "
        "mutation_round, validation_errors, created_at, updated_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) RETURNING id",
        dag.dag_id, run_id, parent_plan_id, dag.model_dump_json(),
        status, mutation_round, err_json, now, now,
    )
    return row["id"]


async def save_plan(
    dag: DAG, *,
    run_id: Optional[str] = None,
    parent_plan_id: Optional[int] = None,
    status: str = "pending",
    mutation_round: int = 0,
    validation_errors: Optional[list[DagValidationError]] = None,
    conn=None,
) -> StoredPlan:
    """Insert a fresh plan row. Use `set_status` for transitions."""
    if status not in _VALID_STATUSES:
        raise ValueError(f"unknown status {status!r}")
    if conn is None:
        async with get_pool().acquire() as owned:
            new_id = await _save_plan_impl(
                owned, dag, run_id, parent_plan_id,
                status, mutation_round, validation_errors,
            )
    else:
        new_id = await _save_plan_impl(
            conn, dag, run_id, parent_plan_id,
            status, mutation_round, validation_errors,
        )
    logger.info(
        "dag plan saved id=%s dag=%s status=%s round=%d",
        new_id, dag.dag_id, status, mutation_round,
    )
    return await get_plan(new_id, conn=conn)


async def get_plan(plan_id: int, conn=None) -> StoredPlan:
    sql = f"SELECT {_PLAN_COLS} FROM dag_plans WHERE id = $1"
    if conn is None:
        async with get_pool().acquire() as owned:
            row = await owned.fetchrow(sql, plan_id)
    else:
        row = await conn.fetchrow(sql, plan_id)
    if not row:
        raise LookupError(f"no dag_plan id={plan_id}")
    return _row_to_plan(row)


async def get_plan_by_run(
    run_id: str, conn=None,
) -> Optional[StoredPlan]:
    """Latest plan attached to a workflow_run (latest = highest id =
    most recent mutation round)."""
    sql = (
        f"SELECT {_PLAN_COLS} FROM dag_plans WHERE run_id = $1 "
        "ORDER BY id DESC LIMIT 1"
    )
    if conn is None:
        async with get_pool().acquire() as owned:
            row = await owned.fetchrow(sql, run_id)
    else:
        row = await conn.fetchrow(sql, run_id)
    return _row_to_plan(row) if row else None


async def list_plans(
    dag_id: str, conn=None,
) -> list[StoredPlan]:
    """All plans for one logical DAG, ordered by mutation round."""
    sql = (
        f"SELECT {_PLAN_COLS} FROM dag_plans WHERE dag_id = $1 "
        "ORDER BY mutation_round, id"
    )
    if conn is None:
        async with get_pool().acquire() as owned:
            rows = await owned.fetch(sql, dag_id)
    else:
        rows = await conn.fetch(sql, dag_id)
    return [_row_to_plan(r) for r in rows]


async def _set_status_impl(
    conn, plan_id: int, new_status: str, run_id: Optional[str],
) -> None:
    # SELECT FOR UPDATE holds a row-level lock for the duration of the
    # tx — concurrent set_status on the same plan_id serialise on the
    # lock. Without it, two callers could both read status=validated,
    # both compute "validated → executing is legal", both UPDATE; the
    # double-transition is technically idempotent but produces
    # surprising ordering in timestamps and logs.
    row = await conn.fetchrow(
        "SELECT status FROM dag_plans WHERE id = $1 FOR UPDATE",
        plan_id,
    )
    if not row:
        raise LookupError(f"no dag_plan id={plan_id}")
    current = row["status"]
    if new_status not in _ALLOWED_TRANSITIONS[current]:
        raise ValueError(
            f"illegal transition {current!r} → {new_status!r} "
            f"(allowed: {sorted(_ALLOWED_TRANSITIONS[current])})"
        )
    now = time.time()
    if run_id is not None:
        await conn.execute(
            "UPDATE dag_plans SET status = $1, run_id = $2, "
            "updated_at = $3 WHERE id = $4",
            new_status, run_id, now, plan_id,
        )
    else:
        await conn.execute(
            "UPDATE dag_plans SET status = $1, updated_at = $2 "
            "WHERE id = $3",
            new_status, now, plan_id,
        )


async def set_status(
    plan_id: int, new_status: str, *,
    run_id: Optional[str] = None, conn=None,
) -> StoredPlan:
    """Transition status; refuses illegal moves. Optionally attach
    `run_id` (used when 'pending' → 'executing' to bind a new run).

    SP-5.1 (2026-04-21): the SELECT-then-UPDATE is now wrapped in a
    transaction with ``SELECT ... FOR UPDATE`` — under SQLite the
    file-lock serialised callers implicitly; under asyncpg pool two
    concurrent set_status on the same plan could both read the same
    status and both issue UPDATEs, making the ordering ambiguous.
    FOR UPDATE makes it deterministic.
    """
    if new_status not in _VALID_STATUSES:
        raise ValueError(f"unknown status {new_status!r}")
    if conn is None:
        async with get_pool().acquire() as owned:
            async with owned.transaction():
                await _set_status_impl(owned, plan_id, new_status, run_id)
    else:
        async with conn.transaction():
            await _set_status_impl(conn, plan_id, new_status, run_id)
    return await get_plan(plan_id, conn=conn)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  workflow_runs ↔ plan glue
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def _attach_to_run_impl(
    conn, plan_id: int, run_id: str,
) -> None:
    now = time.time()
    await conn.execute(
        "UPDATE dag_plans SET run_id = $1, updated_at = $2 WHERE id = $3",
        run_id, now, plan_id,
    )
    await conn.execute(
        "UPDATE workflow_runs SET dag_plan_id = $1 WHERE id = $2",
        plan_id, run_id,
    )


async def attach_to_run(
    plan_id: int, run_id: str, conn=None,
) -> None:
    """Two-way link: dag_plans.run_id ← run_id, workflow_runs.dag_plan_id ← plan_id.

    SP-5.1: the two UPDATEs now land atomically. Previously a crash
    between the two statements left dag_plans pointing at a run but
    workflow_runs' dag_plan_id unset — the reverse lookup
    ``get_dag_plan_id_for_run`` then returned None while the forward
    lookup ``get_plan_by_run`` worked. The tx wrap makes the link
    all-or-nothing.
    """
    if conn is None:
        async with get_pool().acquire() as owned:
            async with owned.transaction():
                await _attach_to_run_impl(owned, plan_id, run_id)
    else:
        async with conn.transaction():
            await _attach_to_run_impl(conn, plan_id, run_id)


async def link_successor(
    old_run_id: str, new_run_id: str, conn=None,
) -> None:
    """Mark the old workflow_run as superseded by `new_run_id`. Used
    when DAG mutation forces a re-plan — Phase 56's append-only invariant
    is preserved (we never edit the old run's steps), and the chain
    stays traceable for audit/replay."""
    sql = "UPDATE workflow_runs SET successor_run_id = $1 WHERE id = $2"
    if conn is None:
        async with get_pool().acquire() as owned:
            await owned.execute(sql, new_run_id, old_run_id)
    else:
        await conn.execute(sql, new_run_id, old_run_id)


async def get_dag_plan_id_for_run(
    run_id: str, conn=None,
) -> Optional[int]:
    """Reverse lookup helper for any consumer that has a run_id and
    needs the plan."""
    sql = "SELECT dag_plan_id FROM workflow_runs WHERE id = $1"
    if conn is None:
        async with get_pool().acquire() as owned:
            row = await owned.fetchrow(sql, run_id)
    else:
        row = await conn.fetchrow(sql, run_id)
    if not row:
        return None
    return row["dag_plan_id"]
