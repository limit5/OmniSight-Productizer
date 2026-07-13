"""U6-0 T9/T10 G5b dormant PostgreSQL execution service.

The service owns fresh pooled connections so the pending-to-executing claim is
committed before the executor can perform an external side effect.  Durable
PostgreSQL row locks and state CAS updates coordinate workers; this module has
no mutable module-global state.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

from backend import db
from backend.agents.execution_contract import (
    Applied,
    DefinitelyNotApplied,
    ExecOutcome,
    StoredAction,
    Unknown,
    resolve_terminal,
)


Executor = Callable[[StoredAction], Awaitable[ExecOutcome]]
Authorizer = Callable[[Mapping], Awaitable[bool]]


@dataclass(frozen=True)
class ExecResult:
    """Result of one claim-and-execute request."""

    status: str
    grant_next: str | None
    resume_next: str | None
    outcome: ExecOutcome | None


async def claim_and_execute(
    pool,
    *,
    tenant_id: str,
    grant_id: str,
    executor: Executor,
    authorizer: Authorizer,
    attempt_id_factory: Callable[[], str],
    result_of: Callable[[Applied], str],
) -> ExecResult:
    """Claim one pending grant, execute its stored action, and finalize it.

    PostgreSQL only.  The top-level claim transaction commits before executor
    invocation, preventing an outer rollback from making a performed action
    claimable again.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT g.state, g.recovery_mode, g.action_instance_id, "
                "g.idempotency_key, g.tenant_id, g.principal_type, "
                "g.actor_id, p.adapter_namespace, p.tool_name, "
                "p.schema_version, p.canonical_target, p.executable_args "
                "FROM action_grants g JOIN prepared_actions p "
                "ON (p.tenant_id = g.tenant_id "
                "AND p.action_instance_id = g.action_instance_id) "
                "WHERE g.tenant_id = $1 AND g.grant_id = $2 "
                "FOR UPDATE OF g",
                tenant_id,
                grant_id,
            )
            if row is None:
                return ExecResult("not_found", None, None, None)
            if row["state"] != "pending":
                return ExecResult("not_claimable", None, None, None)

            allowed = await authorizer({
                "tenant_id": row["tenant_id"],
                "principal_type": row["principal_type"],
                "actor_id": row["actor_id"],
                "adapter_namespace": row["adapter_namespace"],
                "tool_name": row["tool_name"],
            })
            if not allowed:
                await conn.execute(
                    "UPDATE action_grants SET state = 'failed' "
                    "WHERE tenant_id = $1 AND grant_id = $2 "
                    "AND state = 'pending'",
                    tenant_id,
                    grant_id,
                )
                return ExecResult("policy_denied", "failed", "failed", None)

            claimed = await conn.execute(
                "UPDATE action_grants SET state = 'executing' "
                "WHERE tenant_id = $1 AND grant_id = $2 "
                "AND state = 'pending'",
                tenant_id,
                grant_id,
            )
            if claimed.split()[-1] != "1":
                return ExecResult("not_claimable", None, None, None)

            stored = StoredAction(
                grant_id=grant_id,
                idempotency_key=row["idempotency_key"],
                recovery_mode=row["recovery_mode"],
                adapter_namespace=row["adapter_namespace"],
                tool_name=row["tool_name"],
                schema_version=row["schema_version"],
                canonical_target=row["canonical_target"],
                executable_args=json.loads(row["executable_args"]),
            )

    try:
        outcome = await executor(stored)
    except Exception as exc:
        outcome = Unknown(str(exc))

    plan = resolve_terminal(outcome, stored.recovery_mode)
    async with pool.acquire() as conn:
        async with conn.transaction():
            if plan.record_attempt:
                if isinstance(outcome, DefinitelyNotApplied):
                    attempt_outcome = "definitely_not_applied"
                    evidence = outcome.evidence
                    error = outcome.error
                elif isinstance(outcome, Unknown):
                    attempt_outcome = "unknown"
                    evidence = ""
                    error = outcome.error
                else:
                    attempt_outcome = "applied"
                    evidence = outcome.evidence
                    error = ""
                await db.put_execution_attempt(
                    conn,
                    attempt_id=attempt_id_factory(),
                    tenant_id=tenant_id,
                    grant_id=grant_id,
                    outcome=attempt_outcome,
                    evidence=evidence,
                    error=error,
                )
            if plan.write_result:
                await db.put_execution_result(
                    conn,
                    grant_id=grant_id,
                    tenant_id=tenant_id,
                    result_json=result_of(outcome),
                    ambiguous=False,
                )
            if plan.grant_next != "executing":
                await conn.execute(
                    "UPDATE action_grants SET state = $3 "
                    "WHERE tenant_id = $1 AND grant_id = $2 "
                    "AND state = 'executing'",
                    tenant_id,
                    grant_id,
                    plan.grant_next,
                )

    return ExecResult("finalized", plan.grant_next, plan.resume_next, outcome)
