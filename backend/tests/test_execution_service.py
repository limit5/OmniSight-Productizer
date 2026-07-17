"""OP-2643 — PostgreSQL-only dormant execution-service tests.

Every test uses the standard real-PG pool and unique durable identities.  The
module skips cleanly when ``OMNI_TEST_PG_URL`` is unset.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from backend import db
from backend.agents import execution_gate
from backend.agents.execution_contract import (
    Applied,
    DefinitelyNotApplied,
    StoredAction,
    Unknown,
)
from backend.agents.execution_service import (
    claim_and_execute,
    recover_executing_grant,
)


async def _seed_case(
    pool,
    *,
    recovery_mode: str = "non_replayable",
    executable_args: dict | None = None,
    tenant_id: str | None = None,
) -> dict:
    suffix = uuid.uuid4().hex
    create_tenant = tenant_id is None
    tenant_id = tenant_id or f"t-execution-service-{suffix}"
    action_instance_id = f"action-{suffix}"
    challenge_id = f"challenge-{suffix}"
    grant_id = f"grant-{suffix}"
    resume_id = f"resume-{suffix}"
    args = executable_args or {
        "content": "server-stored content",
        "path": f"{suffix}.txt",
    }
    prepared = {
        "tenant_id": tenant_id,
        "action_instance_id": action_instance_id,
        "principal_type": "service",
        "actor_id": f"operation-actor-{suffix}",
        "request_id": f"request-{suffix}",
        "model_call_id": "",
        "adapter_namespace": "workspace",
        "tool_name": "write_file",
        "schema_version": "v1",
        "family": "workspace.write",
        "effect": "mutating",
        "canonical_target": f"/workspace/{suffix}.txt",
        "args_hash": "a" * 64,
        "provenance_kind": "no_model_input",
        "model_snapshot_id": None,
        "no_model_input_source": "slash_command",
        "executable_args_json": json.dumps(args),
        "human_rendering_json": json.dumps({"summary": "Write output"}),
        "prepared_action_digest": "d" * 64,
        "recovery_mode": recovery_mode,
    }
    async with pool.acquire() as conn:
        if create_tenant:
            await conn.execute(
                "INSERT INTO tenants (id, name, plan) "
                "VALUES ($1, $2, 'free')",
                tenant_id,
                tenant_id,
            )
        assert await db.put_prepared_action(conn, **prepared) is True
        assert await db.put_challenge(
            conn,
            tenant_id=tenant_id,
            challenge_id=challenge_id,
            action_instance_id=action_instance_id,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        ) is True
        assert await db.confirm_challenge(
            conn,
            tenant_id=tenant_id,
            challenge_id=challenge_id,
            confirmer_actor="approver-1",
            confirmer_principal_type="user",
            confirmer_auth_event_id=f"auth-{suffix}",
            reason="Approved for this operation",
            grant_id=grant_id,
            resume_id=resume_id,
            grant_expires_at=datetime.now(UTC) + timedelta(minutes=30),
        ) == "confirmed"
    return {
        "tenant_id": tenant_id,
        "grant_id": grant_id,
        "resume_id": resume_id,
        "prepared": prepared,
        "executable_args": args,
    }


async def _allow(_identity) -> bool:
    return True


async def _set_executing_and_claim_lease(
    pool,
    case: dict,
    *,
    worker_id: str = "worker-recovery",
) -> dict:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE action_grants SET state = 'executing' "
            "WHERE tenant_id = $1 AND grant_id = $2 "
            "AND state = 'pending'",
            case["tenant_id"],
            case["grant_id"],
        )
        leased = await conn.fetchrow(
            "UPDATE resume_jobs SET state = 'claimed', lease_owner = $3, "
            "lease_epoch = lease_epoch + 1, "
            "lease_expires_at = clock_timestamp() + interval '1 minute' "
            "WHERE tenant_id = $1 AND resume_id = $2 "
            "RETURNING resume_id, tenant_id, grant_id, lease_owner, "
            "lease_epoch",
            case["tenant_id"],
            case["resume_id"],
            worker_id,
        )
    assert leased is not None
    return dict(leased)


async def _recover(
    pool,
    case: dict,
    lease: dict,
    outcome,
    *,
    authorizer=_allow,
):
    calls: list[StoredAction] = []

    async def executor(stored: StoredAction):
        calls.append(stored)
        return outcome

    result = await recover_executing_grant(
        pool,
        tenant_id=case["tenant_id"],
        grant_id=case["grant_id"],
        executor=executor,
        authorizer=authorizer,
        resume_id=lease["resume_id"],
        lease_owner=lease["lease_owner"],
        lease_epoch=lease["lease_epoch"],
        attempt_id_factory=lambda: uuid.uuid4().hex,
        result_of=lambda applied: json.dumps(applied.result),
    )
    return result, calls


async def _execute(
    pool,
    case: dict,
    outcome,
    *,
    authorizer=_allow,
):
    calls: list[StoredAction] = []

    async def executor(stored: StoredAction):
        calls.append(stored)
        return outcome

    result = await claim_and_execute(
        pool,
        tenant_id=case["tenant_id"],
        grant_id=case["grant_id"],
        executor=executor,
        authorizer=authorizer,
        attempt_id_factory=lambda: uuid.uuid4().hex,
        result_of=lambda applied: json.dumps(applied.result),
    )
    return result, calls


async def _state(pool, case: dict) -> str:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT state FROM action_grants "
            "WHERE tenant_id = $1 AND grant_id = $2",
            case["tenant_id"],
            case["grant_id"],
        )


async def _attempts(pool, case: dict) -> list[dict]:
    async with pool.acquire() as conn:
        return await db.get_execution_attempts(
            conn,
            case["grant_id"],
            tenant_id=case["tenant_id"],
        )


async def _result(pool, case: dict) -> dict | None:
    async with pool.acquire() as conn:
        return await db.get_execution_result(
            conn,
            case["grant_id"],
            tenant_id=case["tenant_id"],
        )


async def _past_date_grant(pool, case: dict) -> None:
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL session_replication_role = 'replica'")
            await conn.execute(
                "UPDATE action_grants "
                "SET created_at = clock_timestamp() - interval '2 hours', "
                "expires_at = clock_timestamp() - interval '1 hour' "
                "WHERE tenant_id = $1 AND grant_id = $2",
                case["tenant_id"],
                case["grant_id"],
            )
            await conn.execute("SET LOCAL session_replication_role = 'origin'")


@pytest.mark.asyncio
async def test_live_grant_executes_and_finalizes_unchanged(pg_test_pool) -> None:
    case = await _seed_case(pg_test_pool)
    outcome = Applied(result={"receipt": "ok"}, evidence="sink receipt")

    result, calls = await _execute(pg_test_pool, case, outcome)

    assert result.status == "finalized"
    assert result.grant_next == "consumed"
    assert result.resume_next == "done"
    assert result.outcome == outcome
    assert len(calls) == 1
    assert await _state(pg_test_pool, case) == "consumed"
    stored_result = await _result(pg_test_pool, case)
    assert json.loads(stored_result["result"]) == {"receipt": "ok"}


@pytest.mark.asyncio
async def test_already_expired_grant_terminalizes_without_execution(
    pg_test_pool,
) -> None:
    case = await _seed_case(pg_test_pool)
    await _past_date_grant(pg_test_pool, case)
    authorizer_calls: list[dict] = []

    async def authorizer(identity) -> bool:
        authorizer_calls.append(dict(identity))
        return True

    result, calls = await _execute(
        pg_test_pool,
        case,
        Applied(result={"unexpected": True}, evidence="unexpected"),
        authorizer=authorizer,
    )

    assert result.status == "expired"
    assert result.grant_next == "expired"
    assert result.resume_next == "failed"
    assert result.outcome is None
    assert authorizer_calls == []
    assert calls == []
    assert await _state(pg_test_pool, case) == "expired"
    assert await _attempts(pg_test_pool, case) == []
    assert await _result(pg_test_pool, case) is None


@pytest.mark.asyncio
async def test_grant_expiring_during_authorization_is_not_executed(
    pg_test_pool,
) -> None:
    case = await _seed_case(pg_test_pool)
    async with pg_test_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL session_replication_role = 'replica'")
            expires_at = await conn.fetchval(
                "UPDATE action_grants "
                "SET expires_at = clock_timestamp() + interval '2 seconds' "
                "WHERE tenant_id = $1 AND grant_id = $2 "
                "RETURNING expires_at",
                case["tenant_id"],
                case["grant_id"],
            )
            await conn.execute("SET LOCAL session_replication_role = 'origin'")

    async def cross_deadline(_identity) -> bool:
        async with pg_test_pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT clock_timestamp() < $1::timestamptz",
                expires_at,
            )
            await conn.execute(
                "SELECT pg_sleep((GREATEST(0, EXTRACT(EPOCH FROM "
                "($1::timestamptz - clock_timestamp()))) + 0.05)"
                "::double precision)",
                expires_at,
            )
            assert await conn.fetchval(
                "SELECT clock_timestamp() >= $1::timestamptz",
                expires_at,
            )
        return True

    result, calls = await _execute(
        pg_test_pool,
        case,
        Applied(result={"unexpected": True}, evidence="unexpected"),
        authorizer=cross_deadline,
    )

    assert result.status == "expired"
    assert result.grant_next == "expired"
    assert result.resume_next == "failed"
    assert result.outcome is None
    assert calls == []
    assert await _state(pg_test_pool, case) == "expired"
    assert await _attempts(pg_test_pool, case) == []
    assert await _result(pg_test_pool, case) is None


@pytest.mark.asyncio
async def test_claim_expiry_boundary_uses_complementary_db_clock_predicates(
    pg_test_pool,
) -> None:
    past = await _seed_case(pg_test_pool)
    future = await _seed_case(pg_test_pool)
    await _past_date_grant(pg_test_pool, past)

    async with pg_test_pool.acquire() as conn:
        past_boundary = await conn.fetchrow(
            "SELECT expires_at <= clock_timestamp() AS expired, "
            "expires_at > clock_timestamp() AS live "
            "FROM action_grants "
            "WHERE tenant_id = $1 AND grant_id = $2",
            past["tenant_id"],
            past["grant_id"],
        )
        future_boundary = await conn.fetchrow(
            "SELECT expires_at <= clock_timestamp() AS expired, "
            "expires_at > clock_timestamp() AS live "
            "FROM action_grants "
            "WHERE tenant_id = $1 AND grant_id = $2",
            future["tenant_id"],
            future["grant_id"],
        )

    assert tuple(past_boundary.values()) == (True, False)
    assert tuple(future_boundary.values()) == (False, True)


@pytest.mark.asyncio
async def test_concurrent_claim_executes_exactly_once(pg_test_pool) -> None:
    case = await _seed_case(pg_test_pool)
    executor_calls: list[StoredAction] = []

    async def executor(stored: StoredAction):
        executor_calls.append(stored)
        await asyncio.sleep(0.05)
        return Applied(result={"receipt": "once"}, evidence="sink receipt")

    async def run_once():
        return await claim_and_execute(
            pg_test_pool,
            tenant_id=case["tenant_id"],
            grant_id=case["grant_id"],
            executor=executor,
            authorizer=_allow,
            attempt_id_factory=lambda: uuid.uuid4().hex,
            result_of=lambda applied: json.dumps(applied.result),
        )

    results = await asyncio.gather(run_once(), run_once())

    assert len(executor_calls) == 1
    assert sorted(result.status for result in results) == [
        "finalized",
        "not_claimable",
    ]
    assert await _state(pg_test_pool, case) == "consumed"
    async with pg_test_pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM execution_results "
            "WHERE tenant_id = $1 AND grant_id = $2",
            case["tenant_id"],
            case["grant_id"],
        ) == 1


@pytest.mark.asyncio
async def test_policy_denial_fails_grant_without_executor(pg_test_pool) -> None:
    case = await _seed_case(pg_test_pool)
    identities: list[dict] = []

    async def deny(identity) -> bool:
        identities.append(dict(identity))
        return False

    result, calls = await _execute(
        pg_test_pool,
        case,
        Applied(result={"unexpected": True}, evidence="unexpected"),
        authorizer=deny,
    )

    assert result.status == "policy_denied"
    assert result.grant_next == "failed"
    assert result.resume_next == "failed"
    assert result.outcome is None
    assert calls == []
    assert identities == [{
        "tenant_id": case["tenant_id"],
        "principal_type": case["prepared"]["principal_type"],
        "actor_id": case["prepared"]["actor_id"],
        "adapter_namespace": case["prepared"]["adapter_namespace"],
        "tool_name": case["prepared"]["tool_name"],
    }]
    assert await _state(pg_test_pool, case) == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("decided_state", ["executing", "consumed", "expired"])
async def test_already_decided_grant_is_not_claimable(
    pg_test_pool,
    decided_state: str,
) -> None:
    case = await _seed_case(pg_test_pool)
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "UPDATE action_grants SET state = $3 "
            "WHERE tenant_id = $1 AND grant_id = $2 "
            "AND state = 'pending'",
            case["tenant_id"],
            case["grant_id"],
            decided_state,
        )

    result, calls = await _execute(
        pg_test_pool,
        case,
        Applied(result={"unexpected": True}, evidence="unexpected"),
    )

    assert result.status == "not_claimable"
    assert result.grant_next is None
    assert result.resume_next is None
    assert result.outcome is None
    assert calls == []


@pytest.mark.asyncio
async def test_definitely_not_applied_fails_and_records_attempt(
    pg_test_pool,
) -> None:
    case = await _seed_case(pg_test_pool)
    outcome = DefinitelyNotApplied(
        error="rejected before write",
        evidence="adapter validation",
    )

    result, calls = await _execute(pg_test_pool, case, outcome)

    assert result.status == "finalized"
    assert result.grant_next == "failed"
    assert result.resume_next == "failed"
    assert len(calls) == 1
    assert await _state(pg_test_pool, case) == "failed"
    attempts = await _attempts(pg_test_pool, case)
    assert len(attempts) == 1
    assert attempts[0]["outcome"] == "definitely_not_applied"
    assert attempts[0]["evidence"] == "adapter validation"
    assert attempts[0]["error"] == "rejected before write"
    assert await _result(pg_test_pool, case) is None


@pytest.mark.asyncio
async def test_unknown_non_replayable_requires_manual_review(
    pg_test_pool,
) -> None:
    case = await _seed_case(pg_test_pool, recovery_mode="non_replayable")
    outcome = Unknown(error="connection lost")

    result, _calls = await _execute(pg_test_pool, case, outcome)

    assert result.grant_next == "manual"
    assert result.resume_next == "manual"
    assert await _state(pg_test_pool, case) == "manual"
    attempts = await _attempts(pg_test_pool, case)
    assert len(attempts) == 1
    assert attempts[0]["outcome"] == "unknown"
    assert attempts[0]["error"] == "connection lost"
    assert await _result(pg_test_pool, case) is None


@pytest.mark.asyncio
async def test_unknown_replayable_stays_executing_and_records_attempt(
    pg_test_pool,
) -> None:
    case = await _seed_case(
        pg_test_pool,
        recovery_mode="sink_idempotency_key",
    )
    outcome = Unknown(error="response lost")

    result, _calls = await _execute(pg_test_pool, case, outcome)

    assert result.status == "finalized"
    assert result.grant_next == "executing"
    assert result.resume_next == "queued"
    assert await _state(pg_test_pool, case) == "executing"
    attempts = await _attempts(pg_test_pool, case)
    assert len(attempts) == 1
    assert attempts[0]["outcome"] == "unknown"
    assert await _result(pg_test_pool, case) is None


@pytest.mark.asyncio
async def test_applied_result_and_consumed_state_finalize_atomically(
    pg_test_pool,
) -> None:
    case = await _seed_case(pg_test_pool)
    outcome = Applied(result={"receipt": "atomic"}, evidence="sink receipt")

    await _execute(pg_test_pool, case, outcome)

    async with pg_test_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT g.state, r.result FROM action_grants g "
            "JOIN execution_results r "
            "ON (r.tenant_id = g.tenant_id AND r.grant_id = g.grant_id) "
            "WHERE g.tenant_id = $1 AND g.grant_id = $2",
            case["tenant_id"],
            case["grant_id"],
        )
    assert row["state"] == "consumed"
    assert json.loads(row["result"]) == {"receipt": "atomic"}


@pytest.mark.asyncio
async def test_executor_receives_only_server_stored_action_args(
    pg_test_pool,
) -> None:
    expected_args = {
        "content": "trusted prepared content",
        "nested": {"server": True},
        "path": "trusted.txt",
    }
    case = await _seed_case(pg_test_pool, executable_args=expected_args)

    _result_value, calls = await _execute(
        pg_test_pool,
        case,
        Applied(result={"receipt": "stored"}, evidence="sink receipt"),
    )

    assert len(calls) == 1
    assert isinstance(calls[0], StoredAction)
    assert calls[0].executable_args == expected_args


@pytest.mark.asyncio
async def test_recovery_denied_current_lease_routes_manual_without_execution(
    pg_test_pool,
    monkeypatch,
) -> None:
    case = await _seed_case(
        pg_test_pool,
        recovery_mode="sink_idempotency_key",
    )
    lease = await _set_executing_and_claim_lease(pg_test_pool, case)
    monkeypatch.delenv("OMNISIGHT_U6_EXECUTE", raising=False)

    result, calls = await _recover(
        pg_test_pool,
        case,
        lease,
        Applied(result={"unexpected": True}, evidence="unexpected"),
        authorizer=execution_gate.authorizer,
    )

    assert result.status == "recovery_manual"
    assert result.grant_next == "manual"
    assert result.resume_next == "manual"
    assert result.outcome is None
    assert calls == []
    assert await _state(pg_test_pool, case) == "executing"
    assert await _attempts(pg_test_pool, case) == []
    assert await _result(pg_test_pool, case) is None


@pytest.mark.asyncio
async def test_recovery_stale_lease_is_not_claimable_before_authorization(
    pg_test_pool,
) -> None:
    case = await _seed_case(
        pg_test_pool,
        recovery_mode="sink_idempotency_key",
    )
    stale = await _set_executing_and_claim_lease(pg_test_pool, case)
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "UPDATE resume_jobs SET lease_epoch = lease_epoch + 1 "
            "WHERE tenant_id = $1 AND resume_id = $2",
            case["tenant_id"],
            case["resume_id"],
        )
    identities: list[dict] = []

    async def deny(identity) -> bool:
        identities.append(dict(identity))
        return False

    result, calls = await _recover(
        pg_test_pool,
        case,
        stale,
        Applied(result={"unexpected": True}, evidence="unexpected"),
        authorizer=deny,
    )

    assert result.status == "not_claimable"
    assert result.grant_next is None
    assert result.resume_next is None
    assert identities == []
    assert calls == []
    assert await _state(pg_test_pool, case) == "executing"


@pytest.mark.asyncio
async def test_recovery_lease_for_different_grant_is_not_claimable(
    pg_test_pool,
) -> None:
    case = await _seed_case(
        pg_test_pool,
        recovery_mode="sink_idempotency_key",
    )
    other = await _seed_case(
        pg_test_pool,
        recovery_mode="sink_idempotency_key",
        tenant_id=case["tenant_id"],
    )
    await _set_executing_and_claim_lease(pg_test_pool, case)
    other_lease = await _set_executing_and_claim_lease(
        pg_test_pool,
        other,
        worker_id="worker-other-grant",
    )

    result, calls = await _recover(
        pg_test_pool,
        case,
        other_lease,
        Applied(result={"unexpected": True}, evidence="unexpected"),
    )

    assert result.status == "not_claimable"
    assert calls == []
    assert await _state(pg_test_pool, case) == "executing"


@pytest.mark.asyncio
async def test_recovery_concurrent_consumed_grant_is_not_claimable(
    pg_test_pool,
) -> None:
    case = await _seed_case(
        pg_test_pool,
        recovery_mode="sink_idempotency_key",
    )
    lease = await _set_executing_and_claim_lease(pg_test_pool, case)

    async def unexpected_authorizer(_identity) -> bool:
        pytest.fail("consumed recovery must not authorize")

    async with pg_test_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE action_grants SET state = 'consumed' "
                "WHERE tenant_id = $1 AND grant_id = $2 "
                "AND state = 'executing'",
                case["tenant_id"],
                case["grant_id"],
            )
            recovery_task = asyncio.create_task(_recover(
                pg_test_pool,
                case,
                lease,
                Applied(result={"unexpected": True}, evidence="unexpected"),
                authorizer=unexpected_authorizer,
            ))
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(
                    asyncio.shield(recovery_task),
                    timeout=0.1,
                )

    result, calls = await recovery_task

    assert result.status == "not_claimable"
    assert calls == []
    assert await _state(pg_test_pool, case) == "consumed"


@pytest.mark.asyncio
async def test_recovery_allowed_current_lease_replays_and_finalizes(
    pg_test_pool,
) -> None:
    case = await _seed_case(
        pg_test_pool,
        recovery_mode="sink_idempotency_key",
    )
    lease = await _set_executing_and_claim_lease(pg_test_pool, case)
    outcome = Applied(result={"receipt": "recovered"}, evidence="sink receipt")

    result, calls = await _recover(
        pg_test_pool,
        case,
        lease,
        outcome,
    )

    assert result.status == "finalized"
    assert result.grant_next == "consumed"
    assert result.resume_next == "done"
    assert result.outcome == outcome
    assert len(calls) == 1
    assert calls[0].idempotency_key == case["prepared"]["action_instance_id"]
    assert await _state(pg_test_pool, case) == "consumed"
    stored_result = await _result(pg_test_pool, case)
    assert json.loads(stored_result["result"]) == {"receipt": "recovered"}


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery_mode", ["non_replayable", "read_after_write"])
async def test_recovery_manual_modes_keep_executing_without_authorization(
    pg_test_pool,
    recovery_mode: str,
) -> None:
    case = await _seed_case(pg_test_pool, recovery_mode=recovery_mode)
    lease = await _set_executing_and_claim_lease(pg_test_pool, case)

    async def unexpected_authorizer(_identity) -> bool:
        pytest.fail("manual recovery mode must not authorize")

    result, calls = await _recover(
        pg_test_pool,
        case,
        lease,
        Applied(result={"unexpected": True}, evidence="unexpected"),
        authorizer=unexpected_authorizer,
    )

    assert result.status == "recovery_manual"
    assert result.grant_next == "manual"
    assert result.resume_next == "manual"
    assert calls == []
    assert await _state(pg_test_pool, case) == "executing"


@pytest.mark.asyncio
async def test_recovery_passes_durable_identity_to_authorizer(
    pg_test_pool,
) -> None:
    case = await _seed_case(
        pg_test_pool,
        recovery_mode="sink_idempotency_key",
    )
    lease = await _set_executing_and_claim_lease(pg_test_pool, case)
    identities: list[dict] = []

    async def allow(identity) -> bool:
        identities.append(dict(identity))
        return True

    result, calls = await _recover(
        pg_test_pool,
        case,
        lease,
        Applied(result={"receipt": "ok"}, evidence="sink receipt"),
        authorizer=allow,
    )

    assert result.status == "finalized"
    assert len(calls) == 1
    assert identities == [{
        "tenant_id": case["tenant_id"],
        "principal_type": case["prepared"]["principal_type"],
        "actor_id": case["prepared"]["actor_id"],
        "adapter_namespace": case["prepared"]["adapter_namespace"],
        "tool_name": case["prepared"]["tool_name"],
    }]
