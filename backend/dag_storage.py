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
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Optional

from backend.dag_schema import DAG
from backend.dag_validator import ValidationError as DagValidationError

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


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CRUD
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def save_plan(dag: DAG, *,
                    run_id: Optional[str] = None,
                    parent_plan_id: Optional[int] = None,
                    status: str = "pending",
                    mutation_round: int = 0,
                    validation_errors: Optional[list[DagValidationError]] = None,
                    ) -> StoredPlan:
    """Insert a fresh plan row. Use `set_status` for transitions."""
    if status not in _VALID_STATUSES:
        raise ValueError(f"unknown status {status!r}")
    from backend import db
    now = time.time()
    err_json = (
        json.dumps([e.to_dict() for e in validation_errors])
        if validation_errors else None
    )
    # Phase-3 PG compat: RETURNING id (dialect-neutral) instead of
    # aiosqlite's cur.lastrowid which asyncpg doesn't surface.
    async with db._conn().execute(
        "INSERT INTO dag_plans "
        "(dag_id, run_id, parent_plan_id, json_body, status, "
        "mutation_round, validation_errors, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?) RETURNING id",
        (dag.dag_id, run_id, parent_plan_id,
         dag.model_dump_json(), status, mutation_round, err_json, now, now),
    ) as cur:
        row = await cur.fetchone()
    new_id = row[0] if row else None
    await db._conn().commit()
    logger.info("dag plan saved id=%s dag=%s status=%s round=%d",
                new_id, dag.dag_id, status, mutation_round)
    return await get_plan(new_id)


async def get_plan(plan_id: int) -> StoredPlan:
    from backend import db
    async with db._conn().execute(
        "SELECT * FROM dag_plans WHERE id=?", (plan_id,),
    ) as cur:
        row = await cur.fetchone()
    if not row:
        raise LookupError(f"no dag_plan id={plan_id}")
    return _row_to_plan(row)


async def get_plan_by_run(run_id: str) -> Optional[StoredPlan]:
    """Latest plan attached to a workflow_run (latest = highest id =
    most recent mutation round)."""
    from backend import db
    async with db._conn().execute(
        "SELECT * FROM dag_plans WHERE run_id=? ORDER BY id DESC LIMIT 1",
        (run_id,),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_plan(row) if row else None


async def list_plans(dag_id: str) -> list[StoredPlan]:
    """All plans for one logical DAG, ordered by mutation round."""
    from backend import db
    async with db._conn().execute(
        "SELECT * FROM dag_plans WHERE dag_id=? ORDER BY mutation_round, id",
        (dag_id,),
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_plan(r) for r in rows]


async def set_status(plan_id: int, new_status: str, *,
                     run_id: Optional[str] = None) -> StoredPlan:
    """Transition status; refuses illegal moves. Optionally attach
    `run_id` (used when 'pending' → 'executing' to bind a new run)."""
    if new_status not in _VALID_STATUSES:
        raise ValueError(f"unknown status {new_status!r}")
    plan = await get_plan(plan_id)
    if new_status not in _ALLOWED_TRANSITIONS[plan.status]:
        raise ValueError(
            f"illegal transition {plan.status!r} → {new_status!r} "
            f"(allowed: {sorted(_ALLOWED_TRANSITIONS[plan.status])})"
        )
    from backend import db
    now = time.time()
    if run_id is not None:
        await db._conn().execute(
            "UPDATE dag_plans SET status=?, run_id=?, updated_at=? WHERE id=?",
            (new_status, run_id, now, plan_id),
        )
    else:
        await db._conn().execute(
            "UPDATE dag_plans SET status=?, updated_at=? WHERE id=?",
            (new_status, now, plan_id),
        )
    await db._conn().commit()
    return await get_plan(plan_id)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  workflow_runs ↔ plan glue
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def attach_to_run(plan_id: int, run_id: str) -> None:
    """Two-way link: dag_plans.run_id ← run_id, workflow_runs.dag_plan_id ← plan_id."""
    from backend import db
    now = time.time()
    await db._conn().execute(
        "UPDATE dag_plans SET run_id=?, updated_at=? WHERE id=?",
        (run_id, now, plan_id),
    )
    await db._conn().execute(
        "UPDATE workflow_runs SET dag_plan_id=? WHERE id=?",
        (plan_id, run_id),
    )
    await db._conn().commit()


async def link_successor(old_run_id: str, new_run_id: str) -> None:
    """Mark the old workflow_run as superseded by `new_run_id`. Used
    when DAG mutation forces a re-plan — Phase 56's append-only invariant
    is preserved (we never edit the old run's steps), and the chain
    stays traceable for audit/replay."""
    from backend import db
    await db._conn().execute(
        "UPDATE workflow_runs SET successor_run_id=? WHERE id=?",
        (new_run_id, old_run_id),
    )
    await db._conn().commit()


async def get_dag_plan_id_for_run(run_id: str) -> Optional[int]:
    """Reverse lookup helper for any consumer that has a run_id and
    needs the plan."""
    from backend import db
    async with db._conn().execute(
        "SELECT dag_plan_id FROM workflow_runs WHERE id=?", (run_id,),
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return None
    return row["dag_plan_id"]
