"""U6-5b — per-user L3 eval/approval ledger adapter (DORMANT).

Persists the U6-5a memory-safety eval decision + the user's approval for an L3
candidate fact into the NEW per-user ledger (0274) — NOT the U4
``memory_eval_runs`` (design §2.D: L3 is not the U4 global ledger). Every operation
is RLS-scoped: explicit ``tenant_id = $ AND user_id = $`` predicates (primary) +
FORCED RLS (defense-in-depth, effective under a non-superuser app role).

The eval-gate invariant (mirrors the U4 publication gate): an approval can be
recorded ONLY for an eval run whose decision is ``promote`` — a rejected /
insufficient candidate can never be approved. ``reasons`` is content-free.

The publish / promote / revoke path + the ``/memories`` confirm UX is U6-6; this
adapter records the eval + approval only. PG-native; DORMANT (U6-5a produces the
decision, U6-6 consumes the approval). Not gated by a flag — nothing calls it.
"""

from __future__ import annotations

import json
import uuid

from backend.agents.u6_memory_safety_eval import MemorySafetyDecision
from backend.agents.u6_memory_scope import MemoryScope

_VALID_DECISIONS = ("promote", "reject", "insufficient_evidence", "infra_invalid")


class L3EvalError(Exception):
    """An L3 eval-ledger invariant was violated — fail closed."""


async def _enter_scope(conn, scope: MemoryScope) -> None:
    if not isinstance(scope, MemoryScope):
        raise L3EvalError("scope must be a MemoryScope")
    await conn.execute("SELECT set_config('app.tenant_id', $1, true)", scope.tenant_id)
    await conn.execute("SELECT set_config('app.user_id', $1, true)", scope.user_id)


async def record_eval(
    conn,
    scope: MemoryScope,
    *,
    fact_id: str,
    decision: MemorySafetyDecision,
) -> str:
    """Persist a memory-safety eval run for a candidate fact. Returns the run id."""
    if not isinstance(decision, MemorySafetyDecision):
        raise L3EvalError("decision must be a MemorySafetyDecision")
    if decision.decision not in _VALID_DECISIONS:
        raise L3EvalError(f"invalid decision: {decision.decision!r}")
    if not fact_id:
        raise L3EvalError("fact_id must be non-empty")
    eval_id = "l3ev-" + uuid.uuid4().hex
    async with conn.transaction():
        await _enter_scope(conn, scope)
        await conn.execute(
            """INSERT INTO l3_eval_runs
               (id, tenant_id, user_id, fact_id, eval_kind, decision, reasons)
               VALUES ($1, $2, $3, $4, 'memory_safety', $5, $6::jsonb)""",
            eval_id, scope.tenant_id, scope.user_id, fact_id,
            decision.decision, json.dumps(list(decision.reasons)),
        )
    return eval_id


async def record_approval(
    conn,
    scope: MemoryScope,
    *,
    eval_run_id: str,
    fact_id: str,
    approved_by: str,
) -> str:
    """Record a user approval — ONLY for a ``promote`` eval run in scope (the
    eval-gate). Returns the approval id. The human-principal check (approver ==
    the scoped user) is enforced by the U6-6 confirm router that calls this."""
    if not approved_by:
        raise L3EvalError("approved_by must be non-empty")
    approval_id = "l3ap-" + uuid.uuid4().hex
    async with conn.transaction():
        await _enter_scope(conn, scope)
        run = await conn.fetchrow(
            "SELECT decision, fact_id FROM l3_eval_runs "
            "WHERE id = $1 AND tenant_id = $2 AND user_id = $3",
            eval_run_id, scope.tenant_id, scope.user_id,
        )
        if run is None:
            raise L3EvalError("no such eval run in scope")
        if run["decision"] != "promote":
            raise L3EvalError(f"cannot approve a {run['decision']!r} eval (eval-gate)")
        if run["fact_id"] != fact_id:
            raise L3EvalError("approval fact_id does not match the eval run")
        await conn.execute(
            """INSERT INTO l3_approvals
               (id, tenant_id, user_id, eval_run_id, fact_id, approved_by)
               VALUES ($1, $2, $3, $4, $5, $6)""",
            approval_id, scope.tenant_id, scope.user_id, eval_run_id, fact_id, approved_by,
        )
    return approval_id


async def get_eval_run(conn, scope: MemoryScope, eval_run_id: str) -> dict | None:
    async with conn.transaction():
        await _enter_scope(conn, scope)
        row = await conn.fetchrow(
            "SELECT id, fact_id, eval_kind, decision, reasons, ran_at FROM l3_eval_runs "
            "WHERE id = $1 AND tenant_id = $2 AND user_id = $3",
            eval_run_id, scope.tenant_id, scope.user_id,
        )
    return dict(row) if row is not None else None


async def erase_user_evals(conn, scope: MemoryScope) -> tuple[int, int]:
    """Delete a user's entire eval ledger (approvals then eval runs). Returns
    (runs, approvals) deleted.

    DoD(U6-6): the hard-erase path MUST call BOTH ``u6_l3_store.erase_user`` AND
    this — else the eval/approval rows (user_id/fact_id/approved_by) survive a
    user's erasure. This is a HARD U6-6 acceptance criterion, not optional."""
    async with conn.transaction():
        await _enter_scope(conn, scope)
        approvals = await conn.fetch(
            "DELETE FROM l3_approvals WHERE tenant_id = $1 AND user_id = $2 RETURNING id",
            scope.tenant_id, scope.user_id,
        )
        runs = await conn.fetch(
            "DELETE FROM l3_eval_runs WHERE tenant_id = $1 AND user_id = $2 RETURNING id",
            scope.tenant_id, scope.user_id,
        )
    return len(runs), len(approvals)
