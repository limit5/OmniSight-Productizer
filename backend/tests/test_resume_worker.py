"""OP-2644/OP-2645 — PostgreSQL-only fenced resume-worker tests.

Every test uses the standard real-PG pool and unique durable identities.  The
module skips cleanly when ``OMNI_TEST_PG_URL`` is unset.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from backend import db
from backend.agents.execution_contract import Applied, StoredAction, Unknown
from backend.agents.execution_service import run_resume_job


async def _clear_resume_queue(pool) -> None:
    """Remove committed queue rows left by earlier pool-backed PG tests."""
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM resume_jobs")


async def _seed_case(
    pool,
    *,
    recovery_mode: str = "non_replayable",
) -> dict:
    suffix = uuid.uuid4().hex
    tenant_id = f"t-resume-worker-{suffix}"
    action_instance_id = f"action-{suffix}"
    challenge_id = f"challenge-{suffix}"
    grant_id = f"grant-{suffix}"
    resume_id = f"resume-{suffix}"
    executable_args = {
        "content": "server-stored content",
        "path": f"{suffix}.txt",
    }
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tenants (id, name, plan) VALUES ($1, $2, 'free')",
            tenant_id,
            tenant_id,
        )
        assert await db.put_prepared_action(
            conn,
            tenant_id=tenant_id,
            action_instance_id=action_instance_id,
            principal_type="service",
            actor_id=f"operation-actor-{suffix}",
            request_id=f"request-{suffix}",
            model_call_id="",
            adapter_namespace="workspace",
            tool_name="write_file",
            schema_version="v1",
            family="workspace.write",
            effect="mutating",
            canonical_target=f"/workspace/{suffix}.txt",
            args_hash="a" * 64,
            provenance_kind="no_model_input",
            model_snapshot_id=None,
            no_model_input_source="slash_command",
            executable_args_json=json.dumps(executable_args),
            human_rendering_json=json.dumps({"summary": "Write output"}),
            prepared_action_digest="d" * 64,
            recovery_mode=recovery_mode,
        ) is True
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
        "action_instance_id": action_instance_id,
        "grant_id": grant_id,
        "resume_id": resume_id,
    }


async def _allow(_identity) -> bool:
    return True


async def _resume_state(pool, case: dict) -> str:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT state FROM resume_jobs "
            "WHERE tenant_id = $1 AND resume_id = $2",
            case["tenant_id"],
            case["resume_id"],
        )


async def _grant_state(pool, case: dict) -> str:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT state FROM action_grants "
            "WHERE tenant_id = $1 AND grant_id = $2",
            case["tenant_id"],
            case["grant_id"],
        )


@pytest.mark.asyncio
async def test_lease_queued_job_claims_and_bumps_epoch(pg_test_pool) -> None:
    await _clear_resume_queue(pg_test_pool)
    case = await _seed_case(pg_test_pool)

    async with pg_test_pool.acquire() as conn:
        leased = await db.lease_next_resume_job(
            conn,
            worker_id="worker-a",
            lease_ttl_seconds=60,
        )
        stored = await conn.fetchrow(
            "SELECT state, lease_owner, lease_epoch, lease_expires_at "
            "FROM resume_jobs WHERE tenant_id = $1 AND resume_id = $2",
            case["tenant_id"],
            case["resume_id"],
        )

    assert leased is not None
    assert leased["resume_id"] == case["resume_id"]
    assert leased["lease_owner"] == "worker-a"
    assert leased["lease_epoch"] == 1
    assert stored["state"] == "claimed"
    assert stored["lease_owner"] == "worker-a"
    assert stored["lease_epoch"] == 1
    assert stored["lease_expires_at"] > datetime.now(UTC)


@pytest.mark.asyncio
async def test_lease_skips_live_claim_without_double_lease(
    pg_test_pool,
) -> None:
    await _clear_resume_queue(pg_test_pool)
    await _seed_case(pg_test_pool)

    async with pg_test_pool.acquire() as conn:
        first = await db.lease_next_resume_job(
            conn,
            worker_id="worker-a",
            lease_ttl_seconds=60,
        )
        second = await db.lease_next_resume_job(
            conn,
            worker_id="worker-b",
            lease_ttl_seconds=60,
        )

    assert first is not None
    assert second is None


@pytest.mark.asyncio
async def test_stale_re_lease_bumps_epoch_and_changes_owner(
    pg_test_pool,
) -> None:
    await _clear_resume_queue(pg_test_pool)
    case = await _seed_case(pg_test_pool)

    async with pg_test_pool.acquire() as conn:
        first = await db.lease_next_resume_job(
            conn,
            worker_id="worker-a",
            lease_ttl_seconds=60,
        )
        await conn.execute(
            "UPDATE resume_jobs "
            "SET lease_expires_at = clock_timestamp() - interval '1 second' "
            "WHERE tenant_id = $1 AND resume_id = $2",
            case["tenant_id"],
            case["resume_id"],
        )
        second = await db.lease_next_resume_job(
            conn,
            worker_id="worker-b",
            lease_ttl_seconds=60,
        )

    assert first["lease_epoch"] == 1
    assert second["resume_id"] == case["resume_id"]
    assert second["lease_owner"] == "worker-b"
    assert second["lease_epoch"] == 2


@pytest.mark.asyncio
async def test_fencing_blocks_stale_worker_and_allows_current_owner(
    pg_test_pool,
) -> None:
    await _clear_resume_queue(pg_test_pool)
    case = await _seed_case(pg_test_pool)

    async with pg_test_pool.acquire() as conn:
        stale = await db.lease_next_resume_job(
            conn,
            worker_id="worker-a",
            lease_ttl_seconds=60,
        )
        await conn.execute(
            "UPDATE resume_jobs "
            "SET lease_expires_at = clock_timestamp() - interval '1 second' "
            "WHERE tenant_id = $1 AND resume_id = $2",
            case["tenant_id"],
            case["resume_id"],
        )
        current = await db.lease_next_resume_job(
            conn,
            worker_id="worker-b",
            lease_ttl_seconds=60,
        )
        stale_finalized = await db.finalize_resume(
            conn,
            resume_id=case["resume_id"],
            tenant_id=case["tenant_id"],
            lease_owner=stale["lease_owner"],
            lease_epoch=stale["lease_epoch"],
            new_state="done",
        )
        current_finalized = await db.finalize_resume(
            conn,
            resume_id=case["resume_id"],
            tenant_id=case["tenant_id"],
            lease_owner=current["lease_owner"],
            lease_epoch=current["lease_epoch"],
            new_state="done",
        )

    assert stale["lease_epoch"] == 1
    assert current["lease_epoch"] == 2
    assert stale_finalized is False
    assert current_finalized is True
    assert await _resume_state(pg_test_pool, case) == "done"


@pytest.mark.asyncio
async def test_run_resume_job_applied_happy_path_finishes_done(
    pg_test_pool,
) -> None:
    await _clear_resume_queue(pg_test_pool)
    case = await _seed_case(pg_test_pool)
    calls: list[StoredAction] = []

    async def executor(stored: StoredAction):
        calls.append(stored)
        return Applied(result={"receipt": "ok"}, evidence="sink receipt")

    result = await run_resume_job(
        pg_test_pool,
        worker_id="worker-happy",
        lease_ttl_seconds=60,
        executor=executor,
        authorizer=_allow,
        attempt_id_factory=lambda: uuid.uuid4().hex,
        result_of=lambda applied: json.dumps(applied.result),
    )

    assert result == "done"
    assert len(calls) == 1
    assert await _grant_state(pg_test_pool, case) == "consumed"
    assert await _resume_state(pg_test_pool, case) == "done"
    async with pg_test_pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM execution_results "
            "WHERE tenant_id = $1 AND grant_id = $2",
            case["tenant_id"],
            case["grant_id"],
        ) == 1


@pytest.mark.asyncio
async def test_run_resume_job_consumed_crash_recovery_does_not_execute(
    pg_test_pool,
) -> None:
    await _clear_resume_queue(pg_test_pool)
    case = await _seed_case(pg_test_pool)
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "UPDATE action_grants SET state = 'consumed' "
            "WHERE tenant_id = $1 AND grant_id = $2",
            case["tenant_id"],
            case["grant_id"],
        )

    async def executor(_stored: StoredAction):
        pytest.fail("consumed crash recovery must not call the executor")

    result = await run_resume_job(
        pg_test_pool,
        worker_id="worker-consumed",
        lease_ttl_seconds=60,
        executor=executor,
        authorizer=_allow,
        attempt_id_factory=lambda: uuid.uuid4().hex,
        result_of=lambda applied: json.dumps(applied.result),
    )

    assert result == "done"
    assert await _resume_state(pg_test_pool, case) == "done"


@pytest.mark.asyncio
async def test_run_resume_job_sink_executing_crash_auto_recovers(
    pg_test_pool,
) -> None:
    await _clear_resume_queue(pg_test_pool)
    case = await _seed_case(
        pg_test_pool,
        recovery_mode="sink_idempotency_key",
    )
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "UPDATE action_grants SET state = 'executing' "
            "WHERE tenant_id = $1 AND grant_id = $2",
            case["tenant_id"],
            case["grant_id"],
        )
    calls: list[StoredAction] = []

    async def executor(stored: StoredAction):
        calls.append(stored)
        return Applied(result={"receipt": "recovered"}, evidence="sink receipt")

    result = await run_resume_job(
        pg_test_pool,
        worker_id="worker-sink-recovery",
        lease_ttl_seconds=60,
        executor=executor,
        authorizer=_allow,
        attempt_id_factory=lambda: uuid.uuid4().hex,
        result_of=lambda applied: json.dumps(applied.result),
    )

    assert result == "done"
    assert len(calls) == 1
    assert calls[0].idempotency_key == case["action_instance_id"]
    assert await _grant_state(pg_test_pool, case) == "consumed"
    assert await _resume_state(pg_test_pool, case) == "done"
    async with pg_test_pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM execution_results "
            "WHERE tenant_id = $1 AND grant_id = $2",
            case["tenant_id"],
            case["grant_id"],
        ) == 1


@pytest.mark.asyncio
async def test_run_resume_job_non_replayable_executing_crash_routes_manual(
    pg_test_pool,
) -> None:
    await _clear_resume_queue(pg_test_pool)
    case = await _seed_case(pg_test_pool)
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "UPDATE action_grants SET state = 'executing' "
            "WHERE tenant_id = $1 AND grant_id = $2",
            case["tenant_id"],
            case["grant_id"],
        )

    async def executor(_stored: StoredAction):
        pytest.fail("executing crash recovery must not call the executor")

    result = await run_resume_job(
        pg_test_pool,
        worker_id="worker-executing",
        lease_ttl_seconds=60,
        executor=executor,
        authorizer=_allow,
        attempt_id_factory=lambda: uuid.uuid4().hex,
        result_of=lambda applied: json.dumps(applied.result),
    )

    assert result == "manual"
    assert await _grant_state(pg_test_pool, case) == "executing"
    assert await _resume_state(pg_test_pool, case) == "manual"


@pytest.mark.asyncio
async def test_run_resume_job_read_after_write_executing_crash_routes_manual(
    pg_test_pool,
) -> None:
    await _clear_resume_queue(pg_test_pool)
    case = await _seed_case(
        pg_test_pool,
        recovery_mode="read_after_write",
    )
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "UPDATE action_grants SET state = 'executing' "
            "WHERE tenant_id = $1 AND grant_id = $2",
            case["tenant_id"],
            case["grant_id"],
        )

    async def executor(_stored: StoredAction):
        pytest.fail("read-after-write recovery must not call the executor")

    result = await run_resume_job(
        pg_test_pool,
        worker_id="worker-read-after-write",
        lease_ttl_seconds=60,
        executor=executor,
        authorizer=_allow,
        attempt_id_factory=lambda: uuid.uuid4().hex,
        result_of=lambda applied: json.dumps(applied.result),
    )

    assert result == "manual"
    assert await _grant_state(pg_test_pool, case) == "executing"
    assert await _resume_state(pg_test_pool, case) == "manual"


@pytest.mark.asyncio
async def test_run_resume_job_sink_repeated_unknown_stays_executing(
    pg_test_pool,
) -> None:
    await _clear_resume_queue(pg_test_pool)
    case = await _seed_case(
        pg_test_pool,
        recovery_mode="sink_idempotency_key",
    )
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "UPDATE action_grants SET state = 'executing' "
            "WHERE tenant_id = $1 AND grant_id = $2",
            case["tenant_id"],
            case["grant_id"],
        )
    calls: list[StoredAction] = []

    async def executor(stored: StoredAction):
        calls.append(stored)
        return Unknown(error="response still lost")

    result = await run_resume_job(
        pg_test_pool,
        worker_id="worker-sink-unknown",
        lease_ttl_seconds=60,
        executor=executor,
        authorizer=_allow,
        attempt_id_factory=lambda: uuid.uuid4().hex,
        result_of=lambda applied: json.dumps(applied.result),
    )

    assert result == "queued"
    assert len(calls) == 1
    assert await _grant_state(pg_test_pool, case) == "executing"
    assert await _resume_state(pg_test_pool, case) == "queued"
    async with pg_test_pool.acquire() as conn:
        attempts = await db.get_execution_attempts(
            conn,
            case["grant_id"],
            tenant_id=case["tenant_id"],
        )
    assert len(attempts) == 1
    assert attempts[0]["outcome"] == "unknown"


@pytest.mark.asyncio
async def test_run_resume_job_returns_idle_when_queue_is_empty(
    pg_test_pool,
) -> None:
    await _clear_resume_queue(pg_test_pool)

    async def executor(_stored: StoredAction):
        pytest.fail("idle worker must not call the executor")

    result = await run_resume_job(
        pg_test_pool,
        worker_id="worker-idle",
        lease_ttl_seconds=60,
        executor=executor,
        authorizer=_allow,
        attempt_id_factory=lambda: uuid.uuid4().hex,
        result_of=lambda applied: json.dumps(applied.result),
    )

    assert result == "idle"
