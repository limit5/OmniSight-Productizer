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
import uuid
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
    "executing": {"completed", "failed", "mutated", "exhausted"},
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
#  Executor lease — compare-and-set claim over a dag_plans row (OP-1656)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# A single-writer lease so at most one executor drives a given plan at a
# time. We copy the OP-977 fencing-token PATTERN — mint a unique token per
# claim so a stale owner whose lease expired can never keep mutating the
# row after a re-claim (its token no longer matches) — but in a dag_plans
# *local* namespace. This deliberately does NOT import or touch
# ``backend.agents.runner_coordination`` / the ``runner_claims`` table
# (MUST-NOT per the ticket): the lease lives entirely in the four nullable
# columns alembic 0248 added to ``dag_plans``
# (``claim_owner`` / ``claim_token`` / ``claim_expires_at`` / ``heartbeat_at``).
#
# Why a conditional ``UPDATE ... RETURNING`` and not SELECT-then-UPDATE:
# the CAS precondition lives in the WHERE clause, so the row lock PG takes
# for the UPDATE also re-evaluates the predicate against the freshly
# committed row (EvalPlanQual under READ COMMITTED). Two racing claimers
# therefore serialise on the row lock; the loser re-checks the now-claimed
# row, its WHERE fails, and it gets zero rows back — no double-grant, with
# no explicit ``SELECT ... FOR UPDATE`` needed. (Contrast ``set_status``,
# whose precondition — the legal-transition check — is computed in Python,
# so it *does* need FOR UPDATE to serialise the read-then-write.)

#: Fencing-token namespace — DISTINCT from runner_coordination's ``claim:``
#: so a dag_plans lease token can never be confused with a runner claim.
LEASE_TOKEN_PREFIX = "dag-plan-lease"

#: Default lease lifetime (seconds). Mirrors the dag-exec heartbeat TTL
#: (3x the 15 s heartbeat cadence → two missed renewals == reclaimable).
DEFAULT_LEASE_TTL_S = 45.0


@dataclass
class PlanLease:
    """Snapshot of the lease columns on a ``dag_plans`` row.

    Returned by :func:`claim_plan` / :func:`renew_lease` on success and by
    :func:`get_lease` for an actively-held row; ``None`` from those callers
    means "not granted / not held".
    """

    plan_id: int
    owner: str
    token: str
    claim_expires_at: float
    heartbeat_at: float


def _mint_lease_token(owner: str) -> str:
    """dag_plans-local fencing token: ``dag-plan-lease:{owner}:{epoch_us}-{uuid}``.

    Distinct namespace from OP-977 / runner_coordination's ``claim:`` prefix
    so the two lease systems are never confused. Unique per attempt
    (``epoch_us`` + uuid) so every (re-)claim mints a strictly fresh token —
    that uniqueness is the fencing guarantee that lets a reclaim invalidate
    a stale owner's in-flight renews.
    """
    epoch_us = int(time.time() * 1_000_000)
    return f"{LEASE_TOKEN_PREFIX}:{owner}:{epoch_us}-{uuid.uuid4()}"


def _row_to_lease(plan_id: int, row) -> PlanLease:
    return PlanLease(
        plan_id=plan_id,
        owner=row["claim_owner"],
        token=row["claim_token"],
        claim_expires_at=row["claim_expires_at"],
        heartbeat_at=row["heartbeat_at"],
    )


async def _claim_plan_impl(
    conn, plan_id: int, owner: str, token: str, ttl_s: float, now: float,
):
    expires = now + ttl_s
    # Grant when the row is unclaimed, already ours (refresh / re-claim), or
    # the prior owner's lease has lapsed (stale reclaim). A live lease held
    # by a *different* owner makes every OR-branch false → 0 rows → no grant.
    return await conn.fetchrow(
        "UPDATE dag_plans SET claim_owner = $1, claim_token = $2, "
        "claim_expires_at = $3, heartbeat_at = $4 "
        "WHERE id = $5 AND ("
        "    claim_owner IS NULL "
        "    OR claim_owner = $1 "
        "    OR claim_expires_at IS NULL "
        "    OR claim_expires_at <= $4"
        ") "
        "RETURNING claim_owner, claim_token, claim_expires_at, heartbeat_at",
        owner, token, expires, now, plan_id,
    )


async def claim_plan(
    plan_id: int, owner: str, *,
    lease_ttl_s: float = DEFAULT_LEASE_TTL_S,
    now: Optional[float] = None,
    conn=None,
) -> Optional[PlanLease]:
    """Atomically claim the lease on ``plan_id`` for ``owner``.

    Returns a :class:`PlanLease` (with a freshly minted fencing token) on
    success, or ``None`` when the lease is held by a *different, live*
    owner — or the plan row does not exist. The single conditional
    ``UPDATE ... RETURNING`` is the compare-and-set: concurrent claimers
    serialise on the PG row lock so exactly one wins (no double-grant), and
    a lease whose ``claim_expires_at`` has passed is reclaimable.

    ``now`` is injectable for deterministic expiry tests; production passes
    ``None`` (wall clock).
    """
    if not owner:
        raise ValueError("claim_plan requires a non-empty owner")
    token = _mint_lease_token(owner)
    ts = time.time() if now is None else now
    if conn is None:
        async with get_pool().acquire() as owned:
            async with owned.transaction():
                row = await _claim_plan_impl(
                    owned, plan_id, owner, token, lease_ttl_s, ts,
                )
    else:
        async with conn.transaction():
            row = await _claim_plan_impl(
                conn, plan_id, owner, token, lease_ttl_s, ts,
            )
    if row is None:
        return None
    logger.info(
        "dag plan lease claimed plan=%s owner=%s expires_at=%.3f",
        plan_id, owner, row["claim_expires_at"],
    )
    return _row_to_lease(plan_id, row)


async def _renew_lease_impl(
    conn, plan_id: int, owner: str, token: str, ttl_s: float, now: float,
):
    expires = now + ttl_s
    # Renew only while the lease is still live AND held by this exact
    # (owner, token). A lapsed expiry or a token that no longer matches
    # (someone reclaimed) yields 0 rows — the fencing stop signal.
    return await conn.fetchrow(
        "UPDATE dag_plans SET heartbeat_at = $1, claim_expires_at = $2 "
        "WHERE id = $3 AND claim_owner = $4 AND claim_token = $5 "
        "AND claim_expires_at > $1 "
        "RETURNING claim_owner, claim_token, claim_expires_at, heartbeat_at",
        now, expires, plan_id, owner, token,
    )


async def renew_lease(
    plan_id: int, owner: str, token: str, *,
    lease_ttl_s: float = DEFAULT_LEASE_TTL_S,
    now: Optional[float] = None,
    conn=None,
) -> Optional[PlanLease]:
    """Heartbeat-renew a held lease: bump ``heartbeat_at`` + extend
    ``claim_expires_at`` by ``lease_ttl_s``.

    Returns the refreshed :class:`PlanLease`, or ``None`` if the lease was
    lost — i.e. it lapsed (``claim_expires_at`` already passed) or another
    owner reclaimed it (token mismatch). A ``None`` return is the executor's
    cue to stop touching the plan: it no longer holds the lease.
    """
    ts = time.time() if now is None else now
    if conn is None:
        async with get_pool().acquire() as owned:
            async with owned.transaction():
                row = await _renew_lease_impl(
                    owned, plan_id, owner, token, lease_ttl_s, ts,
                )
    else:
        async with conn.transaction():
            row = await _renew_lease_impl(
                conn, plan_id, owner, token, lease_ttl_s, ts,
            )
    return _row_to_lease(plan_id, row) if row is not None else None


async def _release_lease_impl(conn, plan_id: int, owner: str, token: str):
    return await conn.fetchrow(
        "UPDATE dag_plans SET claim_owner = NULL, claim_token = NULL, "
        "claim_expires_at = NULL, heartbeat_at = NULL "
        "WHERE id = $1 AND claim_owner = $2 AND claim_token = $3 "
        "RETURNING id",
        plan_id, owner, token,
    )


async def release_lease(
    plan_id: int, owner: str, token: str, *, conn=None,
) -> bool:
    """Release a held lease, clearing all four lease columns.

    Returns ``True`` if this exact ``(owner, token)`` held the lease and it
    was released, else ``False``. **Idempotent**: releasing a lease that was
    never held, already released, or is owned by someone else (wrong owner /
    stale token) is a no-op returning ``False`` — never steals another
    owner's lease. Supports the "release in ``finally``" pattern.
    """
    if conn is None:
        async with get_pool().acquire() as owned:
            async with owned.transaction():
                row = await _release_lease_impl(owned, plan_id, owner, token)
    else:
        async with conn.transaction():
            row = await _release_lease_impl(conn, plan_id, owner, token)
    released = row is not None
    if released:
        logger.info(
            "dag plan lease released plan=%s owner=%s", plan_id, owner,
        )
    return released


async def expire_stale_leases(
    *, now: Optional[float] = None, conn=None,
) -> int:
    """Proactively clear every lease whose ``claim_expires_at`` has passed.

    Returns the number of stale leases swept. This is the explicit
    expire-stale sweeper (a periodic caller is the trigger); note that
    :func:`claim_plan` *also* reclaims a stale lease lazily on contention, so
    a sweeper is an optimisation, not a correctness requirement. Idempotent:
    a second run finds the just-cleared rows already NULL and is a no-op.
    """
    ts = time.time() if now is None else now
    sql = (
        "UPDATE dag_plans SET claim_owner = NULL, claim_token = NULL, "
        "claim_expires_at = NULL, heartbeat_at = NULL "
        "WHERE claim_owner IS NOT NULL AND claim_expires_at IS NOT NULL "
        "AND claim_expires_at <= $1"
    )
    if conn is None:
        async with get_pool().acquire() as owned:
            tag = await owned.execute(sql, ts)
    else:
        tag = await conn.execute(sql, ts)
    # asyncpg returns a command tag like "UPDATE 3"; trailing token is the count.
    try:
        return int(tag.split()[-1])
    except (ValueError, IndexError, AttributeError):
        return 0


async def get_lease(plan_id: int, conn=None) -> Optional[PlanLease]:
    """Read the current lease on ``plan_id``; ``None`` if unclaimed.

    Operator / test read surface — does not take the lease, just reports it.
    """
    sql = (
        "SELECT claim_owner, claim_token, claim_expires_at, heartbeat_at "
        "FROM dag_plans WHERE id = $1"
    )
    if conn is None:
        async with get_pool().acquire() as owned:
            row = await owned.fetchrow(sql, plan_id)
    else:
        row = await conn.fetchrow(sql, plan_id)
    if not row or row["claim_owner"] is None:
        return None
    return _row_to_lease(plan_id, row)


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
