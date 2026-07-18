"""OP-2639 — PostgreSQL-only challenge decision lifecycle tests.

Every test uses the standard real-PG fixture and independently minted row
identities.  The module skips cleanly when ``OMNI_TEST_PG_URL`` is unset.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from backend import db


IDENTITY_COLUMNS = (
    "principal_type",
    "actor_id",
    "request_id",
    "model_call_id",
    "adapter_namespace",
    "tool_name",
    "schema_version",
    "family",
    "canonical_target",
    "args_hash",
    "provenance_kind",
    "model_snapshot_id",
    "no_model_input_source",
    "prepared_action_digest",
    "recovery_mode",
)


async def _seed_case(
    conn,
    *,
    recovery_mode: str = "non_replayable",
) -> dict:
    suffix = uuid.uuid4().hex
    tenant_id = f"t-lifecycle-{suffix}"
    action_instance_id = f"action-{suffix}"
    challenge_id = f"challenge-{suffix}"
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
        "executable_args_json": '{"content":"done"}',
        "human_rendering_json": '{"summary":"Write output"}',
        "prepared_action_digest": "d" * 64,
        "recovery_mode": recovery_mode,
    }
    await conn.execute(
        "INSERT INTO tenants (id, name, plan) VALUES ($1, $2, 'free')",
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
    return {
        "tenant_id": tenant_id,
        "action_instance_id": action_instance_id,
        "challenge_id": challenge_id,
        "grant_id": f"grant-{suffix}",
        "resume_id": f"resume-{suffix}",
        "prepared": prepared,
    }


async def _confirm(conn, case: dict, **overrides) -> str:
    values = {
        "tenant_id": case["tenant_id"],
        "challenge_id": case["challenge_id"],
        "confirmer_actor": "approver-1",
        "confirmer_principal_type": "user",
        "confirmer_auth_event_id": f"auth-{uuid.uuid4().hex}",
        "reason": "Approved for this operation",
        "grant_id": case["grant_id"],
        "resume_id": case["resume_id"],
        "grant_expires_at": datetime.now(UTC) + timedelta(minutes=30),
    }
    values.update(overrides)
    return await db.confirm_challenge(conn, **values)


async def _counts(conn, tenant_id: str) -> tuple[int, int]:
    row = await conn.fetchrow(
        "SELECT (SELECT count(*) FROM action_grants WHERE tenant_id = $1) "
        "AS grants, "
        "(SELECT count(*) FROM resume_jobs WHERE tenant_id = $1) AS resumes",
        tenant_id,
    )
    return row["grants"], row["resumes"]


async def _move_expiry_to_past(
    conn,
    table: str,
    id_column: str,
    row_id: str,
) -> None:
    assert table in {"challenges", "action_grants"}
    assert id_column in {"challenge_id", "grant_id"}
    await conn.execute(
        f"UPDATE {table} "
        "SET created_at = clock_timestamp() - interval '2 hours', "
        "expires_at = clock_timestamp() - interval '1 hour' "
        f"WHERE {id_column} = $1",
        row_id,
    )


@pytest.mark.asyncio
async def test_confirm_pending_issues_one_grant_and_resume(pg_test_conn) -> None:
    case = await _seed_case(pg_test_conn)

    assert await _confirm(pg_test_conn, case) == "confirmed"

    challenge = await db.get_challenge(
        pg_test_conn,
        case["challenge_id"],
        tenant_id=case["tenant_id"],
    )
    assert challenge["state"] == "confirmed"
    assert challenge["confirmer_actor"] == "approver-1"
    assert challenge["confirmer_principal_type"] == "user"
    assert challenge["confirmer_auth_event_id"].startswith("auth-")
    assert challenge["confirm_reason"] == "Approved for this operation"
    assert challenge["confirmed_at"] is not None

    grant = await db.get_action_grant(
        pg_test_conn,
        case["grant_id"],
        tenant_id=case["tenant_id"],
    )
    assert grant["action_instance_id"] == case["action_instance_id"]
    assert grant["grant_issuer_source"] == "ui_confirm"
    assert grant["recovery_mode"] == "non_replayable"
    assert grant["state"] == "pending"

    resume = await db.get_resume_job(
        pg_test_conn,
        case["resume_id"],
        tenant_id=case["tenant_id"],
    )
    assert resume["grant_id"] == case["grant_id"]
    assert resume["action_instance_id"] == case["action_instance_id"]
    assert resume["state"] == "queued"
    assert await _counts(pg_test_conn, case["tenant_id"]) == (1, 1)


@pytest.mark.asyncio
async def test_double_confirm_is_not_confirmable_and_issues_nothing(
    pg_test_conn,
) -> None:
    case = await _seed_case(pg_test_conn)
    assert await _confirm(pg_test_conn, case) == "confirmed"

    result = await _confirm(
        pg_test_conn,
        case,
        grant_id=f"grant-second-{uuid.uuid4().hex}",
        resume_id=f"resume-second-{uuid.uuid4().hex}",
    )

    assert result == "not_confirmable"
    assert await _counts(pg_test_conn, case["tenant_id"]) == (1, 1)


@pytest.mark.asyncio
async def test_expired_challenge_is_not_confirmable(pg_test_conn) -> None:
    case = await _seed_case(pg_test_conn)
    await _move_expiry_to_past(
        pg_test_conn,
        "challenges",
        "challenge_id",
        case["challenge_id"],
    )

    assert await _confirm(pg_test_conn, case) == "not_confirmable"
    challenge = await db.get_challenge(
        pg_test_conn,
        case["challenge_id"],
        tenant_id=case["tenant_id"],
    )
    assert challenge["state"] == "pending"
    assert await _counts(pg_test_conn, case["tenant_id"]) == (0, 0)


@pytest.mark.asyncio
async def test_absent_challenge_is_not_confirmable(pg_test_conn) -> None:
    suffix = uuid.uuid4().hex
    tenant_id = f"t-lifecycle-absent-{suffix}"
    await pg_test_conn.execute(
        "INSERT INTO tenants (id, name, plan) VALUES ($1, $2, 'free')",
        tenant_id,
        tenant_id,
    )

    result = await db.confirm_challenge(
        pg_test_conn,
        tenant_id=tenant_id,
        challenge_id=f"challenge-absent-{suffix}",
        confirmer_actor="approver-1",
        confirmer_principal_type="user",
        confirmer_auth_event_id=f"auth-{suffix}",
        reason="Approved for this operation",
        grant_id=f"grant-{suffix}",
        resume_id=f"resume-{suffix}",
        grant_expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )

    assert result == "not_confirmable"
    assert await _counts(pg_test_conn, tenant_id) == (0, 0)


@pytest.mark.asyncio
async def test_reject_pending_records_rejecter_without_issuing_work(
    pg_test_conn,
) -> None:
    case = await _seed_case(pg_test_conn)

    result = await db.reject_challenge(
        pg_test_conn,
        tenant_id=case["tenant_id"],
        challenge_id=case["challenge_id"],
        confirmer_actor="rejecter-1",
        confirmer_principal_type="user",
        confirmer_auth_event_id="auth-reject-1",
        reason="Operation is not approved",
    )

    assert result == "rejected"
    challenge = await db.get_challenge(
        pg_test_conn,
        case["challenge_id"],
        tenant_id=case["tenant_id"],
    )
    assert challenge["state"] == "rejected"
    assert challenge["confirmer_actor"] == "rejecter-1"
    assert challenge["confirmer_principal_type"] == "user"
    assert challenge["confirmer_auth_event_id"] == "auth-reject-1"
    assert challenge["confirm_reason"] == "Operation is not approved"
    assert challenge["confirmed_at"] is not None
    assert await _counts(pg_test_conn, case["tenant_id"]) == (0, 0)


@pytest.mark.asyncio
async def test_expire_stale_updates_only_own_pending_past_rows(
    pg_test_conn,
) -> None:
    past_challenge = await _seed_case(pg_test_conn)
    future_challenge = await _seed_case(pg_test_conn)
    past_grant = await _seed_case(pg_test_conn)
    future_grant = await _seed_case(pg_test_conn)
    await _move_expiry_to_past(
        pg_test_conn,
        "challenges",
        "challenge_id",
        past_challenge["challenge_id"],
    )
    # past_grant is born already-due: the 0270 identity-freeze trigger forbids UPDATEing a grant's expires_at, so it
    # must be set at CONFIRM time (not mutated after). Confirm it with grant_expires_at = a fresh clock_timestamp(); the
    # fixture's outer transaction makes the grant's created_at (= now() = transaction-start time) precede it, so
    # ck_grants_expiry (expires_at > created_at) holds, while the still-later sweep clock_timestamp() makes it due.
    past_grant_expiry = await pg_test_conn.fetchval("SELECT clock_timestamp()")
    assert await _confirm(pg_test_conn, past_grant, grant_expires_at=past_grant_expiry) == "confirmed"
    assert await _confirm(pg_test_conn, future_grant) == "confirmed"
    due = await pg_test_conn.fetchrow(
        "SELECT created_at, expires_at, clock_timestamp() AS now "
        "FROM action_grants WHERE grant_id = $1",
        past_grant["grant_id"],
    )
    assert due["created_at"] < due["expires_at"]   # ck_grants_expiry holds (created_at = the outer-txn now())
    assert due["expires_at"] <= due["now"]         # already past wall time -> the next sweep will expire it

    counts = await db.expire_stale(pg_test_conn)

    assert set(counts) == {"challenges", "grants"}
    assert all(isinstance(value, int) for value in counts.values())
    assert counts["challenges"] >= 1
    assert counts["grants"] >= 1
    for case, expected_state in (
        (past_challenge, "expired"),
        (future_challenge, "pending"),
        (past_grant, "confirmed"),
        (future_grant, "confirmed"),
    ):
        challenge = await db.get_challenge(
            pg_test_conn,
            case["challenge_id"],
            tenant_id=case["tenant_id"],
        )
        assert challenge["state"] == expected_state
    expired_grant = await db.get_action_grant(
        pg_test_conn,
        past_grant["grant_id"],
        tenant_id=past_grant["tenant_id"],
    )
    live_grant = await db.get_action_grant(
        pg_test_conn,
        future_grant["grant_id"],
        tenant_id=future_grant["tenant_id"],
    )
    assert expired_grant["state"] == "expired"
    assert live_grant["state"] == "pending"


@pytest.mark.asyncio
async def test_confirm_copies_every_identity_column_from_prepared_action(
    pg_test_conn,
) -> None:
    case = await _seed_case(pg_test_conn)
    assert await _confirm(pg_test_conn, case) == "confirmed"

    grant = await db.get_action_grant(
        pg_test_conn,
        case["grant_id"],
        tenant_id=case["tenant_id"],
    )

    for column in IDENTITY_COLUMNS:
        assert grant[column] == case["prepared"][column], column


@pytest.mark.asyncio
async def test_confirm_derives_sink_idempotency_key_from_action_instance(
    pg_test_conn,
) -> None:
    sink_case = await _seed_case(
        pg_test_conn,
        recovery_mode="sink_idempotency_key",
    )
    non_replayable_case = await _seed_case(pg_test_conn)
    assert await _confirm(pg_test_conn, sink_case) == "confirmed"
    assert await _confirm(pg_test_conn, non_replayable_case) == "confirmed"

    sink_grant = await db.get_action_grant(
        pg_test_conn,
        sink_case["grant_id"],
        tenant_id=sink_case["tenant_id"],
    )
    non_replayable_grant = await db.get_action_grant(
        pg_test_conn,
        non_replayable_case["grant_id"],
        tenant_id=non_replayable_case["tenant_id"],
    )
    assert sink_grant["idempotency_key"] == sink_case["action_instance_id"]
    assert non_replayable_grant["idempotency_key"] is None


@pytest.mark.asyncio
async def test_resume_insert_failure_rolls_back_confirm_and_grant(
    pg_test_conn,
) -> None:
    first_case = await _seed_case(pg_test_conn)
    second_case = await _seed_case(pg_test_conn)
    assert await _confirm(pg_test_conn, first_case) == "confirmed"

    with pytest.raises(asyncpg.UniqueViolationError):
        await _confirm(
            pg_test_conn,
            second_case,
            resume_id=first_case["resume_id"],
        )

    second_challenge = await db.get_challenge(
        pg_test_conn,
        second_case["challenge_id"],
        tenant_id=second_case["tenant_id"],
    )
    assert second_challenge["state"] == "pending"
    assert await _counts(pg_test_conn, second_case["tenant_id"]) == (0, 0)
