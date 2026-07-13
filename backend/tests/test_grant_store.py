"""OP-2634/OP-2638 — dormant challenge and grant store contract tests.

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
        param_indexes: list[int] = []

        def replace_placeholder(match: re.Match) -> str:
            param_indexes.append(int(match.group(1)) - 1)
            return "?"

        sqlite_sql = re.sub(
            r"\$(\d+)(?:::jsonb)?", replace_placeholder, sql
        )
        sqlite_params = tuple(params[index] for index in param_indexes)
        return self.raw.execute(sqlite_sql, sqlite_params).fetchone()

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
        "confirmer_actor": None,
        "confirmer_principal_type": None,
        "confirmed_at": None,
        "confirmer_auth_event_id": None,
        "confirm_reason": None,
        "state": "pending",
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


async def _seed_grant_raw(
    conn,
    *,
    grant_id: str,
    tenant_id: str,
    challenge_id: str,
    action_instance_id: str,
    grant_issuer_source: str = "ui_confirm",
    idempotency_key: str | None = None,
    recovery_mode: str = "non_replayable",
    expires_at: str | datetime,
) -> bool:
    row = await conn.fetchrow(
        """INSERT INTO action_grants
           (grant_id, tenant_id, challenge_id, action_instance_id,
            principal_type, actor_id, request_id, model_call_id,
            adapter_namespace, tool_name, schema_version, family,
            canonical_target, args_hash, provenance_kind, model_snapshot_id,
            no_model_input_source, prepared_action_digest, grant_issuer_source,
            idempotency_key, recovery_mode, state, expires_at)
           SELECT $1, p.tenant_id, $3, p.action_instance_id,
                  p.principal_type, p.actor_id, p.request_id, p.model_call_id,
                  p.adapter_namespace, p.tool_name, p.schema_version, p.family,
                  p.canonical_target, p.args_hash, p.provenance_kind,
                  p.model_snapshot_id, p.no_model_input_source,
                  p.prepared_action_digest, $5, $6, $7, 'pending', $8
           FROM prepared_actions p
           WHERE p.tenant_id = $2 AND p.action_instance_id = $4
           RETURNING grant_id""",
        grant_id,
        tenant_id,
        challenge_id,
        action_instance_id,
        grant_issuer_source,
        idempotency_key,
        recovery_mode,
        expires_at,
    )
    return row is not None


@pytest.mark.asyncio
async def test_sqlite_challenge_copies_identity_from_prepared_action(
    sqlite_conn,
) -> None:
    prepared = _prepared_action("act-copy-identity", "t-grant")
    await db.put_prepared_action(sqlite_conn, **prepared)
    assert (
        await db.put_challenge(
            sqlite_conn,
            challenge_id="challenge-copy-identity",
            tenant_id=prepared["tenant_id"],
            action_instance_id=prepared["action_instance_id"],
            expires_at=FUTURE,
        )
        is True
    )

    stored = await db.get_challenge(
        sqlite_conn,
        "challenge-copy-identity",
        tenant_id=prepared["tenant_id"],
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
        "confirmer_auth_event_id",
        "confirm_reason",
        "state",
        "created_at",
        "expires_at",
    }
    for key in _identity():
        assert stored[key] == prepared[key]
    assert stored["state"] == "pending"
    assert stored["expires_at"] == FUTURE
    for key in (
        "confirmer_actor",
        "confirmer_principal_type",
        "confirmed_at",
        "confirmer_auth_event_id",
        "confirm_reason",
    ):
        assert stored[key] is None
    assert stored["created_at"]


@pytest.mark.asyncio
async def test_sqlite_challenge_without_prepared_action_fails_closed(
    sqlite_conn,
) -> None:
    with pytest.raises(ValueError, match="no prepared_action for challenge"):
        await db.put_challenge(
            sqlite_conn,
            challenge_id="challenge-no-prepared",
            tenant_id="t-grant",
            action_instance_id="act-no-prepared",
            expires_at=FUTURE,
        )


@pytest.mark.parametrize(
    ("challenge_id", "tenant_id", "action_instance_id"),
    (
        ("", "t-grant", "act-non-empty"),
        ("challenge-non-empty", "", "act-non-empty"),
        ("challenge-non-empty", "t-grant", ""),
    ),
)
@pytest.mark.asyncio
async def test_sqlite_challenge_rejects_empty_reference_identity(
    sqlite_conn,
    challenge_id: str,
    tenant_id: str,
    action_instance_id: str,
) -> None:
    with pytest.raises(ValueError, match="must be non-empty"):
        await db.put_challenge(
            sqlite_conn,
            challenge_id=challenge_id,
            tenant_id=tenant_id,
            action_instance_id=action_instance_id,
            expires_at=FUTURE,
        )


@pytest.mark.asyncio
async def test_sqlite_cross_tenant_prepared_action_fails_closed(
    sqlite_conn,
) -> None:
    prepared = _prepared_action("act-cross-tenant", "t-grant")
    await db.put_prepared_action(sqlite_conn, **prepared)

    with pytest.raises(ValueError, match="no prepared_action for challenge"):
        await db.put_challenge(
            sqlite_conn,
            challenge_id="challenge-cross-tenant",
            tenant_id="t-other",
            action_instance_id=prepared["action_instance_id"],
            expires_at=FUTURE,
        )
    assert (
        await db.get_challenge(
            sqlite_conn, "challenge-cross-tenant", tenant_id="t-other"
        )
        is None
    )


@pytest.mark.asyncio
async def test_sqlite_action_grant_raw_seed_get_roundtrip_within_tenant(
    sqlite_conn,
) -> None:
    prepared = _prepared_action("act-grant-raw", "t-grant")
    challenge_id = "challenge-grant-raw"
    grant_id = "grant-raw"
    await db.put_prepared_action(sqlite_conn, **prepared)
    await db.put_challenge(
        sqlite_conn,
        challenge_id=challenge_id,
        tenant_id=prepared["tenant_id"],
        action_instance_id=prepared["action_instance_id"],
        expires_at=FUTURE,
    )
    assert await _seed_grant_raw(
        sqlite_conn,
        grant_id=grant_id,
        tenant_id=prepared["tenant_id"],
        challenge_id=challenge_id,
        action_instance_id=prepared["action_instance_id"],
        grant_issuer_source="ui_confirm",
        idempotency_key="sink-key-raw",
        recovery_mode="sink_idempotency_key",
        expires_at=FUTURE,
    )

    stored = await db.get_action_grant(
        sqlite_conn, grant_id, tenant_id=prepared["tenant_id"]
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
    for key in _identity():
        assert stored[key] == prepared[key]
    assert stored["grant_id"] == grant_id
    assert stored["challenge_id"] == challenge_id
    assert stored["grant_issuer_source"] == "ui_confirm"
    assert stored["idempotency_key"] == "sink-key-raw"
    assert stored["recovery_mode"] == "sink_idempotency_key"
    assert stored["expires_at"] == FUTURE
    assert stored["state"] == "pending"
    assert stored["created_at"]


@pytest.mark.asyncio
async def test_sqlite_cross_tenant_get_returns_none_for_both_tables(
    sqlite_conn,
) -> None:
    challenge = _challenge("challenge-cross")
    grant_id = "grant-cross"
    await db.put_prepared_action(
        sqlite_conn,
        **_prepared_action(
            challenge["action_instance_id"], challenge["tenant_id"]
        ),
    )
    await db.put_challenge(
        sqlite_conn,
        challenge_id=challenge["challenge_id"],
        tenant_id=challenge["tenant_id"],
        action_instance_id=challenge["action_instance_id"],
        expires_at=challenge["expires_at"],
    )
    await _seed_grant_raw(
        sqlite_conn,
        grant_id=grant_id,
        tenant_id=challenge["tenant_id"],
        challenge_id=challenge["challenge_id"],
        action_instance_id=challenge["action_instance_id"],
        expires_at=FUTURE,
    )

    assert (
        await db.get_challenge(
            sqlite_conn, challenge["challenge_id"], tenant_id="t-other"
        )
        is None
    )
    assert (
        await db.get_action_grant(
            sqlite_conn, grant_id, tenant_id="t-other"
        )
        is None
    )


@pytest.mark.asyncio
async def test_sqlite_challenge_replay_is_idempotent_and_unchanged(
    sqlite_conn,
) -> None:
    challenge = _challenge("challenge-replay")
    await db.put_prepared_action(
        sqlite_conn,
        **_prepared_action(
            challenge["action_instance_id"], challenge["tenant_id"]
        ),
    )
    assert (
        await db.put_challenge(
            sqlite_conn,
            challenge_id=challenge["challenge_id"],
            tenant_id=challenge["tenant_id"],
            action_instance_id=challenge["action_instance_id"],
            expires_at=challenge["expires_at"],
        )
        is True
    )
    stored_before_replay = await db.get_challenge(
        sqlite_conn, challenge["challenge_id"], tenant_id=challenge["tenant_id"]
    )
    assert (
        await db.put_challenge(
            sqlite_conn,
            challenge_id=challenge["challenge_id"],
            tenant_id=challenge["tenant_id"],
            action_instance_id=challenge["action_instance_id"],
            expires_at=challenge["expires_at"],
        )
        is False
    )
    stored_challenge = await db.get_challenge(
        sqlite_conn, challenge["challenge_id"], tenant_id=challenge["tenant_id"]
    )
    assert stored_challenge == stored_before_replay


def test_put_action_grant_bypass_is_removed() -> None:
    assert not hasattr(db, "put_action_grant")


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


@pytest.mark.parametrize("idempotency_key", (None, ""))
@pytest.mark.asyncio
async def test_sqlite_sink_idempotency_grant_requires_non_empty_key(
    sqlite_conn,
    idempotency_key: str | None,
) -> None:
    prepared = _prepared_action("act-sink-check", "t-grant")
    challenge_id = "challenge-sink-check"
    await db.put_prepared_action(sqlite_conn, **prepared)
    await db.put_challenge(
        sqlite_conn,
        challenge_id=challenge_id,
        tenant_id=prepared["tenant_id"],
        action_instance_id=prepared["action_instance_id"],
        expires_at=FUTURE,
    )

    with pytest.raises(
        sqlite3.IntegrityError,
        match="ck_grants_sink_idempotency",
    ):
        await _seed_grant_raw(
            sqlite_conn,
            grant_id="grant-sink-invalid",
            tenant_id=prepared["tenant_id"],
            challenge_id=challenge_id,
            action_instance_id=prepared["action_instance_id"],
            idempotency_key=idempotency_key,
            recovery_mode="sink_idempotency_key",
            expires_at=FUTURE,
        )

    assert await _seed_grant_raw(
        sqlite_conn,
        grant_id="grant-sink-valid",
        tenant_id=prepared["tenant_id"],
        challenge_id=challenge_id,
        action_instance_id=prepared["action_instance_id"],
        idempotency_key="sink-key-present",
        recovery_mode="sink_idempotency_key",
        expires_at=FUTURE,
    )


def _insert_sqlite_challenge(conn: sqlite3.Connection, **overrides) -> None:
    challenge = _challenge(**overrides)
    conn.execute(
        """INSERT INTO challenges
           (challenge_id, tenant_id, action_instance_id, principal_type,
            actor_id, request_id, model_call_id, adapter_namespace, tool_name,
            schema_version, family, canonical_target, args_hash,
            provenance_kind, model_snapshot_id, no_model_input_source,
            prepared_action_digest, confirmer_actor,
            confirmer_principal_type, confirmed_at, confirmer_auth_event_id,
            confirm_reason, state, expires_at)
           VALUES (:challenge_id, :tenant_id, :action_instance_id,
                   :principal_type, :actor_id, :request_id, :model_call_id,
                   :adapter_namespace, :tool_name, :schema_version, :family,
                   :canonical_target, :args_hash, :provenance_kind,
                   :model_snapshot_id, :no_model_input_source,
                   :prepared_action_digest, :confirmer_actor,
                   :confirmer_principal_type, :confirmed_at,
                   :confirmer_auth_event_id, :confirm_reason, :state,
                   :expires_at)""",
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


def test_sqlite_confirmed_challenge_requires_full_confirmer_evidence(
    sqlite_conn,
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        _insert_sqlite_challenge(
            sqlite_conn.raw,
            challenge_id="challenge-confirmed-missing-confirmer",
            state="confirmed",
            confirmer_actor=None,
        )

    _insert_sqlite_challenge(
        sqlite_conn.raw,
        challenge_id="challenge-confirmed-with-confirmer",
        state="confirmed",
        confirmer_actor="user-1",
        confirmer_principal_type="user",
        confirmed_at="2098-01-01 00:00:00",
        confirmer_auth_event_id="auth-event-1",
        confirm_reason="approved in authenticated session",
    )
    stored = sqlite_conn.raw.execute(
        "SELECT state, confirmer_auth_event_id FROM challenges "
        "WHERE challenge_id = ?",
        ("challenge-confirmed-with-confirmer",),
    ).fetchone()
    assert tuple(stored) == ("confirmed", "auth-event-1")


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
async def test_pg_grant_challenge_instance_fk_rejects_mismatched_action(
    pg_test_conn,
) -> None:
    suffix = uuid.uuid4().hex
    tenant_id = f"t-grant-challenge-fk-{suffix}"
    await _seed_tenants(pg_test_conn, tenant_id)
    challenge_prepared = _prepared_action(
        f"act-challenge-{suffix}",
        tenant_id,
    )
    grant_prepared = _prepared_action(f"act-grant-{suffix}", tenant_id)
    await db.put_prepared_action(pg_test_conn, **challenge_prepared)
    await db.put_prepared_action(pg_test_conn, **grant_prepared)
    challenge_id = f"challenge-{suffix}"
    await db.put_challenge(
        pg_test_conn,
        challenge_id=challenge_id,
        tenant_id=tenant_id,
        action_instance_id=challenge_prepared["action_instance_id"],
        expires_at=FUTURE_PG,
    )

    with pytest.raises(asyncpg.ForeignKeyViolationError) as exc_info:
        await _seed_grant_raw(
            pg_test_conn,
            grant_id=f"grant-{suffix}",
            tenant_id=tenant_id,
            challenge_id=challenge_id,
            action_instance_id=grant_prepared["action_instance_id"],
            expires_at=FUTURE_PG,
        )
    assert exc_info.value.constraint_name == "fk_grants_challenge_instance"


@pytest.mark.asyncio
async def test_pg_one_active_challenge_per_tenant_instance_unique(
    pg_test_conn,
) -> None:
    suffix = uuid.uuid4().hex
    tenant_id = f"t-grant-unique-{suffix}"
    prepared = _prepared_action(f"act-{suffix}", tenant_id)
    await _seed_tenants(pg_test_conn, tenant_id)
    await db.put_prepared_action(pg_test_conn, **prepared)
    await db.put_challenge(
        pg_test_conn,
        challenge_id=f"challenge-a-{suffix}",
        tenant_id=tenant_id,
        action_instance_id=prepared["action_instance_id"],
        expires_at=FUTURE_PG,
    )
    with pytest.raises(asyncpg.UniqueViolationError) as exc_info:
        await db.put_challenge(
            pg_test_conn,
            challenge_id=f"challenge-b-{suffix}",
            tenant_id=tenant_id,
            action_instance_id=prepared["action_instance_id"],
            expires_at=FUTURE_PG,
        )
    assert exc_info.value.constraint_name == "uq_challenges_tenant_instance"
