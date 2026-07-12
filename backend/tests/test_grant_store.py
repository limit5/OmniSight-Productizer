"""OP-2634 — dormant challenge and action-grant store contract tests.

Offline tests exercise the async store helpers against the SQLite ``_SCHEMA``
subset. The final tests use the standard PG fixture and skip when
``OMNI_TEST_PG_URL`` is unset.
"""
from __future__ import annotations

import re
import sqlite3
import uuid
from datetime import UTC, datetime

import asyncpg
import pytest

from backend import db
from scripts import migrate_sqlite_to_pg as mig


FUTURE = "2099-01-01 00:00:00"
FUTURE_PG = datetime(2099, 1, 1, tzinfo=UTC)
PAST = "2000-01-01 00:00:00"


class _SQLiteConn:
    """Minimal asyncpg-shaped adapter for exercising helpers on SQLite."""

    def __init__(self) -> None:
        self.raw = sqlite3.connect(":memory:")
        self.raw.row_factory = sqlite3.Row
        self.raw.executescript(db._SCHEMA)

    async def fetchrow(self, sql: str, *params):
        sqlite_sql = re.sub(r"\$\d+", "?", sql)
        return self.raw.execute(sqlite_sql, params).fetchone()

    def close(self) -> None:
        self.raw.close()


@pytest.fixture
def sqlite_conn():
    conn = _SQLiteConn()
    conn.raw.execute(
        "INSERT INTO tenants (id, name, plan) VALUES (?, ?, 'free')",
        ("t-grant", "t-grant"),
    )
    conn.raw.execute(
        "INSERT INTO tenants (id, name, plan) VALUES (?, ?, 'free')",
        ("t-other", "t-other"),
    )
    try:
        yield conn
    finally:
        conn.close()


def _identity(**overrides) -> dict:
    values = {
        "tenant_id": "t-grant",
        "action_instance_id": "act-grant",
        "principal_type": "user",
        "actor_id": "user-1",
        "request_id": "request-1",
        "model_call_id": "model-call-1",
        "adapter_namespace": "workspace",
        "tool_name": "write_file",
        "schema_version": "v1",
        "family": "workspace.write",
        "canonical_target": "/workspace/output.txt",
        "args_hash": "a" * 64,
        "provenance_kind": "model",
        "model_snapshot_id": "psnap-1",
        "no_model_input_source": None,
        "prepared_action_digest": "d" * 64,
    }
    values.update(overrides)
    return values


def _challenge(challenge_id: str = "challenge-1", **overrides) -> dict:
    values = {
        **_identity(),
        "challenge_id": challenge_id,
        "expires_at": FUTURE,
    }
    values.update(overrides)
    return values


def _grant(grant_id: str = "grant-1", **overrides) -> dict:
    values = {
        **_identity(),
        "grant_id": grant_id,
        "challenge_id": "challenge-1",
        "grant_issuer_source": "ui_confirm",
        "idempotency_key": "sink-key-1",
        "recovery_mode": "sink_idempotency_key",
        "expires_at": FUTURE,
    }
    values.update(overrides)
    return values


@pytest.mark.asyncio
async def test_sqlite_challenge_put_get_roundtrip_within_tenant(
    sqlite_conn,
) -> None:
    challenge = _challenge()
    assert await db.put_challenge(sqlite_conn, **challenge) is True

    stored = await db.get_challenge(
        sqlite_conn, challenge["challenge_id"], tenant_id=challenge["tenant_id"]
    )
    assert stored is not None
    assert set(stored) == {
        "challenge_id",
        "tenant_id",
        "action_instance_id",
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
        "confirmer_actor",
        "confirmer_principal_type",
        "confirmed_at",
        "confirm_reason",
        "state",
        "created_at",
        "expires_at",
    }
    for key, value in challenge.items():
        assert stored[key] == value
    assert stored["state"] == "pending"
    assert stored["confirmer_actor"] is None
    assert stored["created_at"]


@pytest.mark.asyncio
async def test_sqlite_action_grant_put_get_roundtrip_within_tenant(
    sqlite_conn,
) -> None:
    grant = _grant()
    assert await db.put_action_grant(sqlite_conn, **grant) is True

    stored = await db.get_action_grant(
        sqlite_conn, grant["grant_id"], tenant_id=grant["tenant_id"]
    )
    assert stored is not None
    assert set(stored) == {
        "grant_id",
        "tenant_id",
        "challenge_id",
        "action_instance_id",
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
        "grant_issuer_source",
        "idempotency_key",
        "recovery_mode",
        "state",
        "created_at",
        "expires_at",
    }
    for key, value in grant.items():
        assert stored[key] == value
    assert stored["state"] == "pending"
    assert stored["created_at"]


@pytest.mark.asyncio
async def test_sqlite_cross_tenant_get_returns_none_for_both_tables(
    sqlite_conn,
) -> None:
    challenge = _challenge("challenge-cross")
    grant = _grant("grant-cross", challenge_id=challenge["challenge_id"])
    await db.put_challenge(sqlite_conn, **challenge)
    await db.put_action_grant(sqlite_conn, **grant)

    assert (
        await db.get_challenge(
            sqlite_conn, challenge["challenge_id"], tenant_id="t-other"
        )
        is None
    )
    assert (
        await db.get_action_grant(
            sqlite_conn, grant["grant_id"], tenant_id="t-other"
        )
        is None
    )


@pytest.mark.asyncio
async def test_sqlite_same_digest_replays_are_idempotent_and_unchanged(
    sqlite_conn,
) -> None:
    challenge = _challenge("challenge-replay")
    assert await db.put_challenge(sqlite_conn, **challenge) is True
    assert (
        await db.put_challenge(
            sqlite_conn, **{**challenge, "canonical_target": "/changed"}
        )
        is False
    )
    stored_challenge = await db.get_challenge(
        sqlite_conn, challenge["challenge_id"], tenant_id=challenge["tenant_id"]
    )
    assert stored_challenge["canonical_target"] == "/workspace/output.txt"

    grant = _grant("grant-replay", challenge_id=challenge["challenge_id"])
    assert await db.put_action_grant(sqlite_conn, **grant) is True
    assert (
        await db.put_action_grant(
            sqlite_conn, **{**grant, "idempotency_key": "changed"}
        )
        is False
    )
    stored_grant = await db.get_action_grant(
        sqlite_conn, grant["grant_id"], tenant_id=grant["tenant_id"]
    )
    assert stored_grant["idempotency_key"] == "sink-key-1"


@pytest.mark.asyncio
async def test_sqlite_different_digest_replays_raise_without_mutation(
    sqlite_conn,
) -> None:
    challenge = _challenge("challenge-write-once")
    await db.put_challenge(sqlite_conn, **challenge)
    with pytest.raises(ValueError, match="challenge digest mismatch"):
        await db.put_challenge(
            sqlite_conn,
            **{**challenge, "prepared_action_digest": "e" * 64},
        )

    grant = _grant("grant-write-once", challenge_id=challenge["challenge_id"])
    await db.put_action_grant(sqlite_conn, **grant)
    with pytest.raises(ValueError, match="action_grant digest mismatch"):
        await db.put_action_grant(
            sqlite_conn,
            **{**grant, "prepared_action_digest": "e" * 64},
        )

    stored_challenge = await db.get_challenge(
        sqlite_conn, challenge["challenge_id"], tenant_id=challenge["tenant_id"]
    )
    stored_grant = await db.get_action_grant(
        sqlite_conn, grant["grant_id"], tenant_id=grant["tenant_id"]
    )
    assert stored_challenge["prepared_action_digest"] == "d" * 64
    assert stored_grant["prepared_action_digest"] == "d" * 64


def test_sqlite_bootstrap_contains_challenge_and_grant_tables() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(db._SCHEMA)
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert {"challenges", "action_grants"} <= tables
    challenge_indexes = {
        row[1] for row in conn.execute("PRAGMA index_list(challenges)")
    }
    grant_indexes = {
        row[1] for row in conn.execute("PRAGMA index_list(action_grants)")
    }
    assert "idx_challenges_tenant_state" in challenge_indexes
    assert "idx_grants_tenant_state" in grant_indexes
    conn.close()


def _insert_sqlite_challenge(conn: sqlite3.Connection, **overrides) -> None:
    challenge = _challenge(**overrides)
    conn.execute(
        """INSERT INTO challenges
           (challenge_id, tenant_id, action_instance_id, principal_type,
            actor_id, request_id, model_call_id, adapter_namespace, tool_name,
            schema_version, family, canonical_target, args_hash,
            provenance_kind, model_snapshot_id, no_model_input_source,
            prepared_action_digest, expires_at)
           VALUES (:challenge_id, :tenant_id, :action_instance_id,
                   :principal_type, :actor_id, :request_id, :model_call_id,
                   :adapter_namespace, :tool_name, :schema_version, :family,
                   :canonical_target, :args_hash, :provenance_kind,
                   :model_snapshot_id, :no_model_input_source,
                   :prepared_action_digest, :expires_at)""",
        challenge,
    )


def _insert_sqlite_grant(conn: sqlite3.Connection, **overrides) -> None:
    grant = _grant(**overrides)
    conn.execute(
        """INSERT INTO action_grants
           (grant_id, tenant_id, challenge_id, action_instance_id,
            principal_type, actor_id, request_id, model_call_id,
            adapter_namespace, tool_name, schema_version, family,
            canonical_target, args_hash, provenance_kind, model_snapshot_id,
            no_model_input_source, prepared_action_digest, grant_issuer_source,
            idempotency_key, recovery_mode, expires_at)
           VALUES (:grant_id, :tenant_id, :challenge_id, :action_instance_id,
                   :principal_type, :actor_id, :request_id, :model_call_id,
                   :adapter_namespace, :tool_name, :schema_version, :family,
                   :canonical_target, :args_hash, :provenance_kind,
                   :model_snapshot_id, :no_model_input_source,
                   :prepared_action_digest, :grant_issuer_source,
                   :idempotency_key, :recovery_mode, :expires_at)""",
        grant,
    )


def test_sqlite_provenance_xor_rejects_bad_challenge_and_grant_rows(
    sqlite_conn,
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        _insert_sqlite_challenge(
            sqlite_conn.raw,
            challenge_id="challenge-bad-xor",
            model_call_id="",
        )
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        _insert_sqlite_grant(
            sqlite_conn.raw,
            grant_id="grant-bad-xor",
            model_call_id="forbidden",
            provenance_kind="no_model_input",
            model_snapshot_id=None,
            no_model_input_source="slash_command",
        )


def test_sqlite_expiry_check_rejects_past_challenge_and_grant_rows(
    sqlite_conn,
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        _insert_sqlite_challenge(
            sqlite_conn.raw,
            challenge_id="challenge-past",
            expires_at=PAST,
        )
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        _insert_sqlite_grant(
            sqlite_conn.raw,
            grant_id="grant-past",
            expires_at=PAST,
        )


def test_migrator_catalog_contains_grant_tables_in_fk_order_as_text_pks() -> None:
    prepared_index = mig.TABLES_IN_ORDER.index("prepared_actions")
    challenge_index = mig.TABLES_IN_ORDER.index("challenges")
    grant_index = mig.TABLES_IN_ORDER.index("action_grants")
    assert prepared_index < challenge_index < grant_index
    assert "challenges" not in mig.TABLES_WITH_IDENTITY_ID
    assert "action_grants" not in mig.TABLES_WITH_IDENTITY_ID


async def _seed_tenants(conn, *tenant_ids: str) -> None:
    for tenant_id in tenant_ids:
        await conn.execute(
            "INSERT INTO tenants (id, name, plan) VALUES ($1, $2, 'free') "
            "ON CONFLICT (id) DO NOTHING",
            tenant_id,
            tenant_id,
        )


def _prepared_action(action_instance_id: str, tenant_id: str, **overrides) -> dict:
    values = {
        **_identity(
            action_instance_id=action_instance_id,
            tenant_id=tenant_id,
            model_call_id="",
            provenance_kind="no_model_input",
            model_snapshot_id=None,
            no_model_input_source="slash_command",
        ),
        "effect": "mutating",
        "executable_args_json": '{"content":"done","path":"output.txt"}',
        "human_rendering_json": '{"summary":"Write output.txt"}',
    }
    values.update(overrides)
    return values


@pytest.mark.asyncio
async def test_pg_grant_composite_fk_rejects_missing_prepared_action(
    pg_test_conn,
) -> None:
    suffix = uuid.uuid4().hex
    tenant_id = f"t-grant-fk-{suffix}"
    await _seed_tenants(pg_test_conn, tenant_id)
    prepared = _prepared_action(f"act-present-{suffix}", tenant_id)
    await db.put_prepared_action(pg_test_conn, **prepared)
    challenge = _challenge(
        f"challenge-{suffix}",
        **{key: prepared[key] for key in _identity()},
        expires_at=FUTURE_PG,
    )
    await db.put_challenge(pg_test_conn, **challenge)

    grant = _grant(
        f"grant-{suffix}",
        **{
            **{key: challenge[key] for key in _identity()},
            "action_instance_id": f"act-missing-{suffix}",
        },
        challenge_id=challenge["challenge_id"],
        grant_issuer_source="slash_command",
        idempotency_key=None,
        recovery_mode="non_replayable",
        expires_at=FUTURE_PG,
    )
    with pytest.raises(asyncpg.ForeignKeyViolationError) as exc_info:
        await db.put_action_grant(pg_test_conn, **grant)
    assert exc_info.value.constraint_name == "fk_grants_prepared_action"


@pytest.mark.asyncio
async def test_pg_model_grant_composite_fk_rejects_missing_snapshot(
    pg_test_conn,
) -> None:
    suffix = uuid.uuid4().hex
    tenant_id = f"t-grant-snapshot-{suffix}"
    action_instance_id = f"act-{suffix}"
    await _seed_tenants(pg_test_conn, tenant_id)
    prepared = _prepared_action(
        action_instance_id,
        tenant_id,
        model_call_id=f"model-{suffix}",
        provenance_kind="model",
        model_snapshot_id=f"snapshot-missing-{suffix}",
        no_model_input_source=None,
    )
    await db.put_prepared_action(pg_test_conn, **prepared)
    challenge = _challenge(
        f"challenge-{suffix}",
        **{key: prepared[key] for key in _identity()},
        expires_at=FUTURE_PG,
    )
    await db.put_challenge(pg_test_conn, **challenge)
    grant = _grant(
        f"grant-{suffix}",
        **{key: challenge[key] for key in _identity()},
        challenge_id=challenge["challenge_id"],
        expires_at=FUTURE_PG,
    )

    with pytest.raises(asyncpg.ForeignKeyViolationError) as exc_info:
        await db.put_action_grant(pg_test_conn, **grant)
    assert exc_info.value.constraint_name == "fk_grants_snapshot"


@pytest.mark.asyncio
async def test_pg_slash_grant_with_null_snapshot_and_tenant_scope(
    pg_test_conn,
) -> None:
    suffix = uuid.uuid4().hex
    tenant_id = f"t-grant-slash-{suffix}"
    other_tenant_id = f"t-grant-other-{suffix}"
    await _seed_tenants(pg_test_conn, tenant_id, other_tenant_id)
    prepared = _prepared_action(f"act-{suffix}", tenant_id)
    await db.put_prepared_action(pg_test_conn, **prepared)
    challenge = _challenge(
        f"challenge-{suffix}",
        **{key: prepared[key] for key in _identity()},
        expires_at=FUTURE_PG,
    )
    await db.put_challenge(pg_test_conn, **challenge)
    grant = _grant(
        f"grant-{suffix}",
        **{key: challenge[key] for key in _identity()},
        challenge_id=challenge["challenge_id"],
        grant_issuer_source="slash_command",
        idempotency_key=None,
        recovery_mode="non_replayable",
        expires_at=FUTURE_PG,
    )
    assert await db.put_action_grant(pg_test_conn, **grant) is True
    assert (
        await db.get_challenge(
            pg_test_conn, challenge["challenge_id"], tenant_id=other_tenant_id
        )
        is None
    )
    assert (
        await db.get_action_grant(
            pg_test_conn, grant["grant_id"], tenant_id=other_tenant_id
        )
        is None
    )


@pytest.mark.asyncio
async def test_pg_one_active_challenge_per_tenant_instance_unique(
    pg_test_conn,
) -> None:
    suffix = uuid.uuid4().hex
    tenant_id = f"t-grant-unique-{suffix}"
    prepared = _prepared_action(f"act-{suffix}", tenant_id)
    await _seed_tenants(pg_test_conn, tenant_id)
    await db.put_prepared_action(pg_test_conn, **prepared)
    identity = {key: prepared[key] for key in _identity()}
    await db.put_challenge(
        pg_test_conn,
        **_challenge(
            f"challenge-a-{suffix}", **identity, expires_at=FUTURE_PG
        ),
    )
    with pytest.raises(asyncpg.UniqueViolationError) as exc_info:
        await db.put_challenge(
            pg_test_conn,
            **_challenge(
                f"challenge-b-{suffix}", **identity, expires_at=FUTURE_PG
            ),
        )
    assert exc_info.value.constraint_name == "uq_challenges_tenant_instance"
