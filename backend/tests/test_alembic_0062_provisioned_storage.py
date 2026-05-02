"""FS.3.2 — alembic 0062 ``provisioned_storage`` migration contract."""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_0062 = (
    BACKEND_ROOT / "alembic" / "versions" / "0062_provisioned_storage.py"
)


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture(scope="module")
def m0062():
    return _load_module(MIGRATION_0062, "_alembic_test_0062")


# ─── Group 1: structural guards ───────────────────────────────────────────


class TestMigrationFileStructure:
    @pytest.fixture(scope="class")
    def source(self) -> str:
        return MIGRATION_0062.read_text()

    def test_revision_id_is_0062(self, source: str) -> None:
        assert 'revision = "0062"' in source

    def test_down_revision_is_0061(self, source: str) -> None:
        assert 'down_revision = "0061"' in source

    def test_required_columns_present(self, m0062) -> None:
        required = (
            "tenant_id",
            "provider",
            "bucket_name",
            "created_at",
        )
        for col in required:
            assert col in m0062._PG_CREATE_TABLE, f"PG branch missing {col}"
            assert col in m0062._SQLITE_CREATE_TABLE, f"SQLite branch missing {col}"

    def test_pg_branch_uses_double_precision(self, m0062) -> None:
        assert "DOUBLE PRECISION" in m0062._PG_CREATE_TABLE
        assert " REAL" not in m0062._PG_CREATE_TABLE

    def test_sqlite_branch_uses_real(self, m0062) -> None:
        assert " REAL" in m0062._SQLITE_CREATE_TABLE
        assert "DOUBLE PRECISION" not in m0062._SQLITE_CREATE_TABLE

    def test_create_table_is_idempotent(self, m0062) -> None:
        assert "CREATE TABLE IF NOT EXISTS provisioned_storage" in (
            m0062._PG_CREATE_TABLE
        )
        assert "CREATE TABLE IF NOT EXISTS provisioned_storage" in (
            m0062._SQLITE_CREATE_TABLE
        )

    def test_provider_check_clause_matches_storage_provisioning_catalog(
        self, m0062,
    ) -> None:
        from backend import storage_provisioning

        expected_clause = ",".join(
            f"'{p}'" for p in sorted(storage_provisioning.list_providers())
        )
        assert expected_clause == m0062._PROVIDERS_SQL

    def test_composite_pk_declared(self, m0062) -> None:
        assert "PRIMARY KEY (tenant_id, provider)" in m0062._PG_CREATE_TABLE
        assert "PRIMARY KEY (tenant_id, provider)" in m0062._SQLITE_CREATE_TABLE

    def test_tenant_fk_cascade_declared(self, m0062) -> None:
        assert "REFERENCES tenants(id) ON DELETE CASCADE" in m0062._PG_CREATE_TABLE
        assert "REFERENCES tenants(id) ON DELETE CASCADE" in (
            m0062._SQLITE_CREATE_TABLE
        )


# ─── Group 2: functional SQLite upgrade ───────────────────────────────────


def _bootstrap_pre_0062_tenants_schema(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(
        """
        CREATE TABLE tenants (
            id   TEXT PRIMARY KEY,
            name TEXT NOT NULL DEFAULT ''
        );
        """
    )


class _StubBind:
    """Mimics enough of an alembic context bind for ``conn.exec_driver_sql``."""

    def __init__(self, raw: sqlite3.Connection) -> None:
        self._raw = raw

        class _Dialect:
            name = "sqlite"

        self.dialect = _Dialect()

    def exec_driver_sql(self, sql: str, *args, **kwargs):
        return self._raw.execute(sql)


def _bind(monkeypatch, conn: sqlite3.Connection) -> None:
    from alembic import op as alembic_op

    bind = _StubBind(conn)
    monkeypatch.setattr(alembic_op, "get_bind", lambda: bind)


@pytest.fixture()
def upgraded_db(monkeypatch, m0062) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    _bootstrap_pre_0062_tenants_schema(conn)
    conn.execute("INSERT INTO tenants (id, name) VALUES ('t-a', 'Tenant A')")
    conn.execute("INSERT INTO tenants (id, name) VALUES ('t-b', 'Tenant B')")
    _bind(monkeypatch, conn)
    m0062.upgrade()
    return conn


class TestSqliteUpgradeCreatesTable:
    def test_provisioned_storage_table_exists(self, upgraded_db) -> None:
        row = upgraded_db.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='provisioned_storage'"
        ).fetchone()
        assert row is not None

    def test_all_required_columns_present(self, upgraded_db) -> None:
        cols = {
            row[1]
            for row in upgraded_db.execute(
                "PRAGMA table_info(provisioned_storage)"
            ).fetchall()
        }
        required = {
            "tenant_id",
            "provider",
            "bucket_name",
            "created_at",
        }
        missing = required - cols
        assert not missing, f"provisioned_storage missing columns: {missing}"

    def test_composite_pk_rejects_duplicate_pair(self, upgraded_db) -> None:
        upgraded_db.execute(
            "INSERT INTO provisioned_storage "
            "(tenant_id, provider, bucket_name, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("t-a", "s3", "bucket-a", 1.0),
        )
        with pytest.raises(sqlite3.IntegrityError):
            upgraded_db.execute(
                "INSERT INTO provisioned_storage "
                "(tenant_id, provider, bucket_name, created_at) "
                "VALUES (?, ?, ?, ?)",
                ("t-a", "s3", "bucket-b", 2.0),
            )

    def test_composite_pk_allows_different_providers_per_tenant(
        self, upgraded_db,
    ) -> None:
        upgraded_db.execute(
            "INSERT INTO provisioned_storage "
            "(tenant_id, provider, bucket_name, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("t-a", "s3", "bucket-a", 1.0),
        )
        upgraded_db.execute(
            "INSERT INTO provisioned_storage "
            "(tenant_id, provider, bucket_name, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("t-a", "r2", "bucket-b", 1.0),
        )

    def test_provider_check_rejects_unknown_provider(
        self, upgraded_db,
    ) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            upgraded_db.execute(
                "INSERT INTO provisioned_storage "
                "(tenant_id, provider, bucket_name, created_at) "
                "VALUES (?, ?, ?, ?)",
                ("t-a", "gcs", "bucket-a", 1.0),
            )

    def test_provider_check_accepts_each_supported_provider(
        self, upgraded_db,
    ) -> None:
        from backend import storage_provisioning

        for provider in sorted(storage_provisioning.list_providers()):
            upgraded_db.execute(
                "INSERT INTO provisioned_storage "
                "(tenant_id, provider, bucket_name, created_at) "
                "VALUES (?, ?, ?, ?)",
                ("t-b", provider, f"bucket-{provider}", 1.0),
            )
