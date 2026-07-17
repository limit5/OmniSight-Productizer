"""U6-0 T9/T10 G5b/G5c/G5d dormant PostgreSQL execution service.

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


async def _execute_and_finalize(
    pool,
    *,
    tenant_id: str,
    grant_id: str,
    stored: StoredAction,
    executor: Executor,
    attempt_id_factory: Callable[[], str],
    result_of: Callable[[Applied], str],
) -> ExecResult:
    """Execute one stored action and atomically finalize its durable outcome."""
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
                "SELECT g.state, g.expires_at, g.recovery_mode, "
                "g.action_instance_id, g.idempotency_key, g.tenant_id, "
                "g.principal_type, "
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

            # Claim expiry and GAP-6's pending-only sweep intentionally overlap
            # and are mutually idempotent; do not deduplicate them.
            expired = await conn.fetchval(
                "UPDATE action_grants SET state = 'expired' "
                "WHERE tenant_id = $1 AND grant_id = $2 "
                "AND state = 'pending' "
                "AND expires_at <= clock_timestamp() RETURNING grant_id",
                tenant_id,
                grant_id,
            )
            if expired is not None:
                return ExecResult("expired", "expired", "failed", None)

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

            claimed = await conn.fetchval(
                "UPDATE action_grants SET state = 'executing' "
                "WHERE tenant_id = $1 AND grant_id = $2 "
                "AND state = 'pending' "
                "AND expires_at > clock_timestamp() RETURNING grant_id",
                tenant_id,
                grant_id,
            )
            if claimed is None:
                expired = await conn.fetchval(
                    "UPDATE action_grants SET state = 'expired' "
                    "WHERE tenant_id = $1 AND grant_id = $2 "
                    "AND state = 'pending' "
                    "AND expires_at <= clock_timestamp() RETURNING grant_id",
                    tenant_id,
                    grant_id,
                )
                if expired is not None:
                    return ExecResult("expired", "expired", "failed", None)
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

    return await _execute_and_finalize(
        pool,
        tenant_id=tenant_id,
        grant_id=grant_id,
        stored=stored,
        executor=executor,
        attempt_id_factory=attempt_id_factory,
        result_of=result_of,
    )


async def recover_executing_grant(
    pool,
    *,
    tenant_id: str,
    grant_id: str,
    executor: Executor,
    authorizer: Authorizer,
    resume_id: str,
    lease_owner: str,
    lease_epoch: int,
    attempt_id_factory: Callable[[], str],
    result_of: Callable[[Applied], str],
) -> ExecResult:
    """Admit recovery under grant and lease locks, then replay post-commit.

    Only sink-idempotent recovery executes. Its durable key bounds the residual
    if the admitted lease expires during the external executor call.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Lock order is grant then resume; no worker takes the reverse.
            row = await conn.fetchrow(
                "SELECT g.state, g.recovery_mode, g.action_instance_id, "
                "g.idempotency_key, g.tenant_id, g.principal_type, "
                "g.actor_id, p.adapter_namespace, p.tool_name, "
                "p.schema_version, p.canonical_target, p.executable_args "
                "FROM action_grants g JOIN prepared_actions p "
                "ON (p.tenant_id = g.tenant_id "
                "AND p.action_instance_id = g.action_instance_id) "
                "WHERE g.tenant_id = $1 AND g.grant_id = $2 "
                "AND g.state = 'executing' FOR UPDATE OF g",
                tenant_id,
                grant_id,
            )
            if row is None:
                return ExecResult("not_claimable", None, None, None)

            # SKIP LOCKED reclaim either skips this admitted row or wins first,
            # making this read observe the bumped epoch after its lock wait.
            lease_ok = await conn.fetchval(
                "SELECT 1 FROM resume_jobs "
                "WHERE resume_id = $1 AND tenant_id = $2 "
                "AND grant_id = $3 AND lease_owner = $4 "
                "AND lease_epoch = $5 AND state = 'claimed' FOR UPDATE",
                resume_id,
                tenant_id,
                grant_id,
                lease_owner,
                lease_epoch,
            )
            if lease_ok is None:
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
            if stored.recovery_mode in {
                "read_after_write",
                "non_replayable",
            }:
                return ExecResult("recovery_manual", "manual", "manual", None)
            if stored.recovery_mode == "sink_idempotency_key":
                allowed = await authorizer({
                    "tenant_id": row["tenant_id"],
                    "principal_type": row["principal_type"],
                    "actor_id": row["actor_id"],
                    "adapter_namespace": row["adapter_namespace"],
                    "tool_name": row["tool_name"],
                })
                if not allowed:
                    # Pause the resume for a human; the grant stays executing.
                    return ExecResult(
                        "recovery_manual",
                        "manual",
                        "manual",
                        None,
                    )
            else:
                raise ValueError(
                    f"unknown recovery mode: {stored.recovery_mode}"
                )

    return await _execute_and_finalize(
        pool,
        tenant_id=tenant_id,
        grant_id=grant_id,
        stored=stored,
        executor=executor,
        attempt_id_factory=attempt_id_factory,
        result_of=result_of,
    )


async def run_resume_job(
    pool,
    *,
    worker_id: str,
    lease_ttl_seconds: int,
    executor: Executor,
    authorizer: Authorizer,
    attempt_id_factory: Callable[[], str],
    result_of: Callable[[Applied], str],
) -> str:
    """Lease and drive one resume job through a fenced transition.

    PostgreSQL only and dormant: no loop schedules this driver.  Every resume
    transition uses the lease owner and epoch returned by the durable lease.
    """
    async with pool.acquire() as conn:
        leased = await db.lease_next_resume_job(
            conn,
            worker_id=worker_id,
            lease_ttl_seconds=lease_ttl_seconds,
        )
    if leased is None:
        return "idle"

    async def read_grant_state() -> str | None:
        async with pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT state FROM action_grants "
                "WHERE tenant_id = $1 AND grant_id = $2",
                leased["tenant_id"],
                leased["grant_id"],
            )

    grant_state = await read_grant_state()
    resume_next = None
    if grant_state == "pending":
        result = await claim_and_execute(
            pool,
            tenant_id=leased["tenant_id"],
            grant_id=leased["grant_id"],
            executor=executor,
            authorizer=authorizer,
            attempt_id_factory=attempt_id_factory,
            result_of=result_of,
        )
        resume_next = result.resume_next
        if resume_next is None and result.status == "not_claimable":
            grant_state = await read_grant_state()
        elif resume_next is None:
            raise RuntimeError(
                f"resume grant could not be driven: {result.status}"
            )

    if resume_next is None:
        if grant_state == "consumed":
            resume_next = "done"
        elif grant_state in {"failed", "expired"}:
            resume_next = "failed"
        elif grant_state == "manual":
            resume_next = "manual"
        elif grant_state == "executing":
            recovered = await recover_executing_grant(
                pool,
                tenant_id=leased["tenant_id"],
                grant_id=leased["grant_id"],
                executor=executor,
                authorizer=authorizer,
                resume_id=leased["resume_id"],
                lease_owner=leased["lease_owner"],
                lease_epoch=leased["lease_epoch"],
                attempt_id_factory=attempt_id_factory,
                result_of=result_of,
            )
            if recovered.status == "not_claimable":
                grant_state = await read_grant_state()
                if grant_state == "consumed":
                    resume_next = "done"
                elif grant_state in {"failed", "expired"}:
                    resume_next = "failed"
                else:
                    resume_next = "manual"
            else:
                resume_next = recovered.resume_next or "manual"
        else:
            raise RuntimeError(f"unsupported resume grant state: {grant_state}")

    async with pool.acquire() as conn:
        finalized = await db.finalize_resume(
            conn,
            resume_id=leased["resume_id"],
            tenant_id=leased["tenant_id"],
            lease_owner=leased["lease_owner"],
            lease_epoch=leased["lease_epoch"],
            new_state=resume_next,
        )
    return resume_next if finalized else "lease_lost"
