"""BP.A2A.4 -- alembic 0196 ``a2a_invocations`` contract."""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_0196 = BACKEND_ROOT / "alembic" / "versions" / "0196_a2a_invocations.py"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture(scope="module")
def m0196():
    return _load_module(MIGRATION_0196, "_alembic_test_0196")


def test_revision_id_and_parent_are_declared() -> None:
    source = MIGRATION_0196.read_text()
    assert 'revision = "0196"' in source
    assert 'down_revision = "0195"' in source


def test_required_columns_present_and_typed_for_pg(m0196) -> None:
    required = (
        "invocation_id",
        "tenant_id",
        "agent_name",
        "caller_identity",
        "payload_hash",
        "response_hash",
        "latency_ms",
        "status",
        "created_at",
    )

    for col in required:
        assert col in m0196._PG_CREATE_TABLE, f"PG branch missing {col}"

    assert "invocation_id   TEXT PRIMARY KEY" in m0196._PG_CREATE_TABLE
    assert "REFERENCES tenants(id) ON DELETE CASCADE" in m0196._PG_CREATE_TABLE
    assert "latency_ms      INTEGER NOT NULL CHECK (latency_ms >= 0)" in (
        m0196._PG_CREATE_TABLE
    )
    assert "status IN ('completed','failed')" in m0196._PG_CREATE_TABLE
    assert "created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()" in (
        m0196._PG_CREATE_TABLE
    )


def test_payload_and_response_plaintext_columns_are_absent(m0196) -> None:
    joined = "\n".join((m0196._PG_CREATE_TABLE, m0196._SQLITE_CREATE_TABLE))
    assert "payload_hash" in joined
    assert "response_hash" in joined
    assert "payload_json" not in joined
    assert "response_json" not in joined
    assert "command" not in joined
    assert "answer" not in joined


def test_indexes_declared(m0196) -> None:
    assert "idx_a2a_invocations_tenant_agent_time" in (
        m0196._INDEX_TENANT_AGENT_TIME
    )
    assert "ON a2a_invocations(tenant_id, agent_name, created_at DESC)" in (
        m0196._INDEX_TENANT_AGENT_TIME
    )
    assert "idx_a2a_invocations_payload_hash" in m0196._INDEX_PAYLOAD_HASH
    assert "ON a2a_invocations(payload_hash)" in m0196._INDEX_PAYLOAD_HASH


def _bootstrap_parent_schema(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(
        """
        CREATE TABLE tenants (
            id   TEXT PRIMARY KEY,
            name TEXT NOT NULL DEFAULT ''
        );
        """
    )


def test_sqlite_upgrade_creates_dev_parity_table_and_indexes(m0196) -> None:
    conn = sqlite3.connect(":memory:")
    _bootstrap_parent_schema(conn)
    conn.execute("INSERT INTO tenants (id, name) VALUES ('t-a', 'Tenant A')")
    conn.executescript(m0196._SQLITE_CREATE_TABLE)
    conn.execute(m0196._INDEX_TENANT_AGENT_TIME)
    conn.execute(m0196._INDEX_PAYLOAD_HASH)

    columns = {
        row[1]: row[2]
        for row in conn.execute("PRAGMA table_info(a2a_invocations)")
    }
    assert columns == {
        "invocation_id": "TEXT",
        "tenant_id": "TEXT",
        "agent_name": "TEXT",
        "caller_identity": "TEXT",
        "payload_hash": "TEXT",
        "response_hash": "TEXT",
        "latency_ms": "INTEGER",
        "status": "TEXT",
        "created_at": "TEXT",
    }
    indexes = {
        row[1]
        for row in conn.execute("PRAGMA index_list(a2a_invocations)")
    }
    assert "idx_a2a_invocations_tenant_agent_time" in indexes
    assert "idx_a2a_invocations_payload_hash" in indexes


def test_sqlite_constraints_enforce_tenant_status_and_latency(m0196) -> None:
    conn = sqlite3.connect(":memory:")
    _bootstrap_parent_schema(conn)
    conn.execute("INSERT INTO tenants (id, name) VALUES ('t-a', 'Tenant A')")
    conn.executescript(m0196._SQLITE_CREATE_TABLE)

    insert_sql = """
        INSERT INTO a2a_invocations (
            invocation_id, tenant_id, agent_name, caller_identity,
            payload_hash, response_hash, latency_ms, status
        ) VALUES (:invocation_id, :tenant_id, 'orchestrator',
            'operator@example.com', 'payload-hash', 'response-hash',
            :latency_ms, :status)
    """
    conn.execute(
        insert_sql,
        {
            "invocation_id": "a2a-ok",
            "tenant_id": "t-a",
            "latency_ms": 12,
            "status": "completed",
        },
    )

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            insert_sql,
            {
                "invocation_id": "a2a-bad-status",
                "tenant_id": "t-a",
                "latency_ms": 12,
                "status": "running",
            },
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            insert_sql,
            {
                "invocation_id": "a2a-negative",
                "tenant_id": "t-a",
                "latency_ms": -1,
                "status": "failed",
            },
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            insert_sql,
            {
                "invocation_id": "a2a-missing-tenant",
                "tenant_id": "t-missing",
                "latency_ms": 1,
                "status": "failed",
            },
        )


class TestMigratorListsTable:
    def _load_migrator(self):
        import importlib.util as _u

        repo_root = Path(__file__).resolve().parents[2]
        spec = _u.spec_from_file_location(
            "migrate_sqlite_to_pg", repo_root / "scripts" / "migrate_sqlite_to_pg.py"
        )
        mig = _u.module_from_spec(spec)
        sys.modules["migrate_sqlite_to_pg"] = mig
        spec.loader.exec_module(mig)
        return mig

    def test_a2a_invocations_in_tables_in_order(self) -> None:
        mig = self._load_migrator()
        assert "a2a_invocations" in mig.TABLES_IN_ORDER

    def test_a2a_invocations_not_in_identity_id_set(self) -> None:
        mig = self._load_migrator()
        assert "a2a_invocations" not in mig.TABLES_WITH_IDENTITY_ID

    def test_a2a_invocations_replays_after_tenants(self) -> None:
        mig = self._load_migrator()
        order = list(mig.TABLES_IN_ORDER)
        assert order.index("tenants") < order.index("a2a_invocations")
