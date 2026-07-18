"""U6-0 B-autoauth (AA-2) — PostgreSQL-only auto-grant txn tests.

Skips cleanly when ``OMNI_TEST_PG_URL`` is unset (same fixture as
test_grant_lifecycle). Proves ``db.auto_grant_from_prepared`` creates a CONFIRMED
challenge (synthetic server confirmer) + a ``server_auto_grant`` grant + a queued
resume in one txn, copying operation identity from the immutable
``prepared_actions`` row (forge-resistant), and that the widened CHECK
(migration 0271) admits ``server_auto_grant``. The seed inserts ONLY the tenant +
prepared action (NOT a challenge) so auto_grant creates the challenge fresh.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from backend import db
from backend.tests.test_grant_lifecycle import IDENTITY_COLUMNS


async def _seed_prepared(conn, *, recovery_mode: str = "non_replayable") -> dict:
    suffix = uuid.uuid4().hex
    tenant_id = f"t-autogrant-{suffix}"
    action_instance_id = f"action-{suffix}"
    prepared = {
        "tenant_id": tenant_id,
        "action_instance_id": action_instance_id,
        "principal_type": "service",
        "actor_id": f"op-actor-{suffix}",
        "request_id": f"req-{suffix}",
        "model_call_id": "",
        "adapter_namespace": "runner_sdk",
        "tool_name": "write_file",
        "schema_version": "v1",
        "family": "code_write",
        "effect": "mutating",
        "canonical_target": f"backend/gen/{suffix}.py",
        "args_hash": "a" * 64,
        "provenance_kind": "no_model_input",
        "model_snapshot_id": None,
        "no_model_input_source": "slash_command",
        "executable_args_json": '{"content":"x"}',
        "human_rendering_json": '{"summary":"write"}',
        "prepared_action_digest": "d" * 64,
        "recovery_mode": recovery_mode,
    }
    await conn.execute(
        "INSERT INTO tenants (id, name, plan) VALUES ($1, $2, 'free')",
        tenant_id,
        tenant_id,
    )
    assert await db.put_prepared_action(conn, **prepared) is True
    return {
        "tenant_id": tenant_id,
        "action_instance_id": action_instance_id,
        "prepared": prepared,
    }


async def _auto_grant(conn, case: dict, **overrides) -> str:
    now = datetime.now(UTC)
    values = {
        "tenant_id": case["tenant_id"],
        "action_instance_id": case["action_instance_id"],
        "challenge_id": f"chal-{uuid.uuid4().hex}",
        "grant_id": f"grant-{uuid.uuid4().hex}",
        "resume_id": f"resume-{uuid.uuid4().hex}",
        "confirmer_actor": "u6-auto-auth",
        "confirmer_principal_type": "service",
        "confirmer_auth_event_id": f"autoauth-v1:{case['action_instance_id']}",
        "reason": "server_auto_auth: contained code_write",
        "grant_expires_at": now + timedelta(hours=1),
        "challenge_expires_at": now + timedelta(hours=1),
    }
    values.update(overrides)
    return await db.auto_grant_from_prepared(conn, **values)


async def test_auto_grant_creates_confirmed_challenge_grant_and_resume(pg_test_conn) -> None:
    case = await _seed_prepared(pg_test_conn)
    challenge_id = f"chal-{uuid.uuid4().hex}"
    assert await _auto_grant(pg_test_conn, case, challenge_id=challenge_id) == "auto_granted"

    chal = await db.get_challenge(pg_test_conn, challenge_id, tenant_id=case["tenant_id"])
    assert chal is not None
    assert chal["state"] == "confirmed"
    assert chal["confirmer_actor"] == "u6-auto-auth"
    assert chal["confirmer_principal_type"] == "service"
    assert chal["confirm_reason"] == "server_auto_auth: contained code_write"
    assert chal["confirmed_at"] is not None

    grant_row = await pg_test_conn.fetchrow(
        "SELECT grant_id, grant_issuer_source, state, challenge_id "
        "FROM action_grants WHERE tenant_id=$1 AND action_instance_id=$2",
        case["tenant_id"],
        case["action_instance_id"],
    )
    assert grant_row is not None
    assert grant_row["grant_issuer_source"] == "server_auto_grant"  # migration 0271 admits it
    assert grant_row["state"] == "pending"
    assert grant_row["challenge_id"] == challenge_id

    resume_row = await pg_test_conn.fetchrow(
        "SELECT state FROM resume_jobs WHERE tenant_id=$1 AND grant_id=$2",
        case["tenant_id"],
        grant_row["grant_id"],
    )
    assert resume_row is not None
    assert resume_row["state"] == "queued"


async def test_auto_grant_copies_identity_from_prepared_not_caller(pg_test_conn) -> None:
    case = await _seed_prepared(pg_test_conn)
    await _auto_grant(pg_test_conn, case)
    prepared = await pg_test_conn.fetchrow(
        "SELECT * FROM prepared_actions WHERE tenant_id=$1 AND action_instance_id=$2",
        case["tenant_id"],
        case["action_instance_id"],
    )
    grant = await pg_test_conn.fetchrow(
        "SELECT * FROM action_grants WHERE tenant_id=$1 AND action_instance_id=$2",
        case["tenant_id"],
        case["action_instance_id"],
    )
    for col in IDENTITY_COLUMNS:
        assert grant[col] == prepared[col], col


async def test_auto_grant_sink_idempotency_key_derived_from_instance(pg_test_conn) -> None:
    case = await _seed_prepared(pg_test_conn, recovery_mode="sink_idempotency_key")
    await _auto_grant(pg_test_conn, case)
    grant = await pg_test_conn.fetchrow(
        "SELECT idempotency_key FROM action_grants WHERE tenant_id=$1 AND action_instance_id=$2",
        case["tenant_id"],
        case["action_instance_id"],
    )
    assert grant["idempotency_key"] == case["action_instance_id"]


async def test_auto_grant_missing_prepared_raises(pg_test_conn) -> None:
    now = datetime.now(UTC)
    with pytest.raises(RuntimeError):
        await db.auto_grant_from_prepared(
            pg_test_conn,
            tenant_id="t-nope-" + uuid.uuid4().hex,
            action_instance_id="action-nope",
            challenge_id="chal-nope-" + uuid.uuid4().hex,
            grant_id="grant-nope",
            resume_id="resume-nope",
            confirmer_actor="u6-auto-auth",
            confirmer_principal_type="service",
            confirmer_auth_event_id="x",
            reason="x",
            grant_expires_at=now + timedelta(hours=1),
            challenge_expires_at=now + timedelta(hours=1),
        )


async def test_auto_grant_empty_reason_rejected(pg_test_conn) -> None:
    case = await _seed_prepared(pg_test_conn)
    with pytest.raises(ValueError):
        await _auto_grant(pg_test_conn, case, reason="")
