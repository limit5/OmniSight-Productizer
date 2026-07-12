"""OP-2629 — durable prepared-action store contract tests.

Offline tests exercise the async store helpers against the SQLite ``_SCHEMA``
subset. The final tests use the standard PG fixture and skip when
``OMNI_TEST_PG_URL`` is unset.
"""
from __future__ import annotations

import json
import re
import sqlite3
import uuid

import asyncpg
import pytest

from backend import db
from scripts import migrate_sqlite_to_pg as mig


class _SQLiteConn:
    """Minimal asyncpg-shaped adapter for exercising helpers on SQLite."""

    def __init__(self) -> None:
        self.raw = sqlite3.connect(":memory:")
        self.raw.row_factory = sqlite3.Row
        self.raw.executescript(db._SCHEMA)

    async def fetchrow(self, sql: str, *params):
        sqlite_sql = re.sub(r"\$\d+(?:::jsonb)?", "?", sql)
        return self.raw.execute(sqlite_sql, params).fetchone()

    def close(self) -> None:
        self.raw.close()


@pytest.fixture
def sqlite_conn():
    conn = _SQLiteConn()
    conn.raw.execute(
        "INSERT INTO tenants (id, name, plan) VALUES (?, ?, 'free')",
        ("t-prepared", "t-prepared"),
    )
    conn.raw.execute(
        "INSERT INTO tenants (id, name, plan) VALUES (?, ?, 'free')",
        ("t-other", "t-other"),
    )
    try:
        yield conn
    finally:
        conn.close()


def _action(action_instance_id: str = "act-prepared", **overrides) -> dict:
    values = {
        "action_instance_id": action_instance_id,
        "tenant_id": "t-prepared",
        "principal_type": "user",
        "actor_id": "user-1",
        "request_id": "request-1",
        "model_call_id": "model-call-1",
        "adapter_namespace": "workspace",
        "tool_name": "write_file",
        "schema_version": "v1",
        "family": "workspace.write",
        "effect": "mutating",
        "canonical_target": "/workspace/output.txt",
        "args_hash": "a" * 64,
        "provenance_kind": "model",
        "model_snapshot_id": "psnap-1",
        "no_model_input_source": None,
        "executable_args_json": '{"content":"done","path":"output.txt"}',
        "human_rendering_json": '{"summary":"Write output.txt"}',
        "prepared_action_digest": "d" * 64,
        "recovery_mode": "read_after_write",
    }
    values.update(overrides)
    return values


@pytest.mark.asyncio
async def test_sqlite_put_get_roundtrips_all_columns_within_tenant(
    sqlite_conn,
) -> None:
    action = _action()
    assert await db.put_prepared_action(sqlite_conn, **action) is True

    stored = await db.get_prepared_action(
        sqlite_conn, action["action_instance_id"], tenant_id=action["tenant_id"]
    )
    assert stored is not None
    assert set(stored) == {
        "action_instance_id",
        "tenant_id",
        "principal_type",
        "actor_id",
        "request_id",
        "model_call_id",
        "adapter_namespace",
        "tool_name",
        "schema_version",
        "family",
        "effect",
        "canonical_target",
        "args_hash",
        "provenance_kind",
        "model_snapshot_id",
        "no_model_input_source",
        "executable_args",
        "human_rendering",
        "prepared_action_digest",
        "recovery_mode",
        "created_at",
    }
    for key, value in action.items():
        if key == "executable_args_json":
            assert json.loads(stored["executable_args"]) == json.loads(value)
        elif key == "human_rendering_json":
            assert json.loads(stored["human_rendering"]) == json.loads(value)
        else:
            assert stored[key] == value
    assert stored["created_at"]


@pytest.mark.asyncio
async def test_sqlite_cross_tenant_get_returns_none(sqlite_conn) -> None:
    action = _action("act-cross-tenant")
    await db.put_prepared_action(sqlite_conn, **action)
    assert (
        await db.get_prepared_action(
            sqlite_conn, action["action_instance_id"], tenant_id="t-other"
        )
        is None
    )


@pytest.mark.asyncio
async def test_sqlite_put_defaults_recovery_mode_to_non_replayable(
    sqlite_conn,
) -> None:
    action = _action("act-default-recovery")
    action.pop("recovery_mode")
    await db.put_prepared_action(sqlite_conn, **action)

    stored = await db.get_prepared_action(
        sqlite_conn, action["action_instance_id"], tenant_id=action["tenant_id"]
    )
    assert stored["recovery_mode"] == "non_replayable"


@pytest.mark.asyncio
async def test_sqlite_same_digest_replay_is_idempotent_and_unchanged(
    sqlite_conn,
) -> None:
    action = _action("act-idempotent")
    assert await db.put_prepared_action(sqlite_conn, **action) is True
    replay = {**action, "human_rendering_json": '{"summary":"changed"}'}
    assert await db.put_prepared_action(sqlite_conn, **replay) is False

    stored = await db.get_prepared_action(
        sqlite_conn, action["action_instance_id"], tenant_id=action["tenant_id"]
    )
    assert json.loads(stored["human_rendering"]) == {
        "summary": "Write output.txt"
    }


@pytest.mark.asyncio
async def test_sqlite_different_digest_replay_raises_without_mutation(
    sqlite_conn,
) -> None:
    action = _action("act-write-once")
    await db.put_prepared_action(sqlite_conn, **action)
    changed = {
        **action,
        "human_rendering_json": '{"summary":"changed"}',
        "prepared_action_digest": "e" * 64,
    }
    with pytest.raises(ValueError, match="prepared_action digest mismatch"):
        await db.put_prepared_action(sqlite_conn, **changed)

    stored = await db.get_prepared_action(
        sqlite_conn, action["action_instance_id"], tenant_id=action["tenant_id"]
    )
    assert stored["prepared_action_digest"] == "d" * 64
    assert json.loads(stored["human_rendering"]) == {
        "summary": "Write output.txt"
    }


def test_sqlite_bootstrap_contains_prepared_actions_table_and_index() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(db._SCHEMA)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(prepared_actions)")}
    assert columns == {
        "action_instance_id",
        "tenant_id",
        "principal_type",
        "actor_id",
        "request_id",
        "model_call_id",
        "adapter_namespace",
        "tool_name",
        "schema_version",
        "family",
        "effect",
        "canonical_target",
        "args_hash",
        "provenance_kind",
        "model_snapshot_id",
        "no_model_input_source",
        "executable_args",
        "human_rendering",
        "prepared_action_digest",
        "recovery_mode",
        "created_at",
    }
    indexes = {row[1] for row in conn.execute("PRAGMA index_list(prepared_actions)")}
    assert "idx_prepared_actions_tenant_digest" in indexes
    conn.close()


@pytest.mark.asyncio
async def test_sqlite_recovery_mode_check_rejects_unknown_value(
    sqlite_conn,
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        await db.put_prepared_action(
            sqlite_conn,
            **_action("act-invalid-recovery", recovery_mode="bogus"),
        )


@pytest.mark.parametrize(
    ("sql", "params"),
    [
        (
            "UPDATE prepared_actions SET executable_args = ? "
            "WHERE action_instance_id = ?",
            ('{"changed":true}', "act-immutable"),
        ),
        (
            "DELETE FROM prepared_actions WHERE action_instance_id = ?",
            ("act-immutable",),
        ),
    ],
    ids=("update", "delete"),
)
@pytest.mark.asyncio
async def test_sqlite_prepared_action_direct_mutation_is_blocked(
    sqlite_conn,
    sql: str,
    params: tuple[str, ...],
) -> None:
    action = _action("act-immutable")
    await db.put_prepared_action(sqlite_conn, **action)

    with pytest.raises(sqlite3.IntegrityError, match="PreparedActionImmutable"):
        sqlite_conn.raw.execute(sql, params)


def _insert_sqlite_action(conn: sqlite3.Connection, **overrides) -> None:
    action = _action(**overrides)
    conn.execute(
        """INSERT INTO prepared_actions
           (action_instance_id, tenant_id, principal_type, actor_id,
            request_id, model_call_id, adapter_namespace, tool_name,
            schema_version, family, effect, canonical_target, args_hash,
            provenance_kind, model_snapshot_id, no_model_input_source,
            executable_args, human_rendering, prepared_action_digest)
           VALUES (:action_instance_id, :tenant_id, :principal_type, :actor_id,
                   :request_id, :model_call_id, :adapter_namespace, :tool_name,
                   :schema_version, :family, :effect, :canonical_target,
                   :args_hash, :provenance_kind, :model_snapshot_id,
                   :no_model_input_source, :executable_args_json,
                   :human_rendering_json, :prepared_action_digest)""",
        action,
    )


def test_sqlite_provenance_xor_accepts_valid_shapes_and_rejects_violation(
    sqlite_conn,
) -> None:
    _insert_sqlite_action(sqlite_conn.raw, action_instance_id="act-model")
    _insert_sqlite_action(
        sqlite_conn.raw,
        action_instance_id="act-no-model",
        model_call_id="",
        provenance_kind="no_model_input",
        model_snapshot_id=None,
        no_model_input_source="slash_command",
    )

    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        _insert_sqlite_action(
            sqlite_conn.raw,
            action_instance_id="act-model-invalid",
            model_call_id="",
        )
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        _insert_sqlite_action(
            sqlite_conn.raw,
            action_instance_id="act-no-model-invalid",
            model_call_id="model-call-forbidden",
            provenance_kind="no_model_input",
            model_snapshot_id=None,
            no_model_input_source="slash_command",
        )


def test_migrator_catalog_contains_both_u6_store_tables_as_text_pks() -> None:
    assert "prepared_actions" in mig.TABLES_IN_ORDER
    assert "provenance_snapshots" in mig.TABLES_IN_ORDER
    assert "prepared_actions" not in mig.TABLES_WITH_IDENTITY_ID
    assert "provenance_snapshots" not in mig.TABLES_WITH_IDENTITY_ID


async def _seed_tenants(conn, *tenant_ids: str) -> None:
    for tenant_id in tenant_ids:
        await conn.execute(
            "INSERT INTO tenants (id, name, plan) VALUES ($1, $2, 'free') "
            "ON CONFLICT (id) DO NOTHING",
            tenant_id,
            tenant_id,
        )


@pytest.mark.asyncio
async def test_pg_put_get_roundtrip_and_tenant_scope(pg_test_conn) -> None:
    suffix = uuid.uuid4().hex
    tenant_a = f"t-prepared-a-{suffix}"
    tenant_b = f"t-prepared-b-{suffix}"
    await _seed_tenants(pg_test_conn, tenant_a, tenant_b)
    action = _action(
        f"act-{suffix}",
        tenant_id=tenant_a,
        model_call_id=f"model-{suffix}",
        model_snapshot_id=f"psnap-{suffix}",
    )

    assert await db.put_prepared_action(pg_test_conn, **action) is True
    stored = await db.get_prepared_action(
        pg_test_conn, action["action_instance_id"], tenant_id=tenant_a
    )
    assert stored is not None
    assert json.loads(stored["executable_args"]) == json.loads(
        action["executable_args_json"]
    )
    assert stored["recovery_mode"] == "read_after_write"
    assert (
        await db.get_prepared_action(
            pg_test_conn, action["action_instance_id"], tenant_id=tenant_b
        )
        is None
    )


@pytest.mark.asyncio
async def test_pg_provenance_xor_rejects_invalid_model_shape(pg_test_conn) -> None:
    suffix = uuid.uuid4().hex
    tenant_id = f"t-prepared-{suffix}"
    await _seed_tenants(pg_test_conn, tenant_id)
    action = _action(
        f"act-{suffix}",
        tenant_id=tenant_id,
        model_call_id="",
        model_snapshot_id=f"psnap-{suffix}",
    )
    with pytest.raises(asyncpg.CheckViolationError):
        await db.put_prepared_action(pg_test_conn, **action)


@pytest.mark.asyncio
async def test_pg_recovery_mode_check_rejects_unknown_value(pg_test_conn) -> None:
    suffix = uuid.uuid4().hex
    tenant_id = f"t-prepared-{suffix}"
    await _seed_tenants(pg_test_conn, tenant_id)
    action = _action(
        f"act-{suffix}",
        tenant_id=tenant_id,
        model_call_id=f"model-{suffix}",
        model_snapshot_id=f"psnap-{suffix}",
        recovery_mode="bogus",
    )
    with pytest.raises(asyncpg.CheckViolationError):
        async with pg_test_conn.transaction():
            await db.put_prepared_action(pg_test_conn, **action)


@pytest.mark.parametrize("operation", ("update", "delete"))
@pytest.mark.asyncio
async def test_pg_prepared_action_direct_mutation_is_blocked(
    pg_test_conn,
    operation: str,
) -> None:
    suffix = uuid.uuid4().hex
    tenant_id = f"t-prepared-{suffix}"
    await _seed_tenants(pg_test_conn, tenant_id)
    action = _action(
        f"act-{suffix}",
        tenant_id=tenant_id,
        model_call_id=f"model-{suffix}",
        model_snapshot_id=f"psnap-{suffix}",
    )
    await db.put_prepared_action(pg_test_conn, **action)

    with pytest.raises(asyncpg.RaiseError, match="PreparedActionImmutable"):
        async with pg_test_conn.transaction():
            if operation == "update":
                await pg_test_conn.execute(
                    "UPDATE prepared_actions SET executable_args = $1::jsonb "
                    "WHERE action_instance_id = $2",
                    '{"changed":true}',
                    action["action_instance_id"],
                )
            else:
                await pg_test_conn.execute(
                    "DELETE FROM prepared_actions WHERE action_instance_id = $1",
                    action["action_instance_id"],
                )
