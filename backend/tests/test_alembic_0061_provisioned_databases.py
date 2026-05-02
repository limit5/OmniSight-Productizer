"""FS.1.3 — alembic 0061 ``provisioned_databases`` migration contract."""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_0061 = (
    BACKEND_ROOT / "alembic" / "versions" / "0061_provisioned_databases.py"
)


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture(scope="module")
def m0061():
    return _load_module(MIGRATION_0061, "_alembic_test_0061")


# ─── Group 1: structural guards ───────────────────────────────────────────


class TestMigrationFileStructure:
    @pytest.fixture(scope="class")
    def source(self) -> str:
        return MIGRATION_0061.read_text()

    def test_revision_id_is_0061(self, source: str) -> None:
        assert 'revision = "0061"' in source

    def test_down_revision_is_0057(self, source: str) -> None:
        assert 'down_revision = "0057"' in source

    def test_required_columns_present(self, m0061) -> None:
        required = (
            "tenant_id",
            "provider",
            "connection_url_enc",
            "created_at",
            "status",
        )
        for col in required:
            assert col in m0061._PG_CREATE_TABLE, f"PG branch missing {col}"
            assert col in m0061._SQLITE_CREATE_TABLE, f"SQLite branch missing {col}"

    def test_pg_branch_uses_double_precision(self, m0061) -> None:
        assert "DOUBLE PRECISION" in m0061._PG_CREATE_TABLE
        assert " REAL" not in m0061._PG_CREATE_TABLE

    def test_sqlite_branch_uses_real(self, m0061) -> None:
        assert " REAL" in m0061._SQLITE_CREATE_TABLE
        assert "DOUBLE PRECISION" not in m0061._SQLITE_CREATE_TABLE

    def test_create_table_is_idempotent(self, m0061) -> None:
        assert (
            "CREATE TABLE IF NOT EXISTS provisioned_databases"
            in m0061._PG_CREATE_TABLE
        )
        assert (
            "CREATE TABLE IF NOT EXISTS provisioned_databases"
            in m0061._SQLITE_CREATE_TABLE
        )

    def test_provider_check_clause_matches_db_provisioning_catalog(
        self, m0061,
    ) -> None:
        from backend import db_provisioning

        expected_clause = ",".join(
            f"'{p}'" for p in sorted(db_provisioning.list_providers())
        )
        assert expected_clause == m0061._PROVIDERS_SQL

    def test_composite_pk_declared(self, m0061) -> None:
        assert "PRIMARY KEY (tenant_id, provider)" in m0061._PG_CREATE_TABLE
        assert "PRIMARY KEY (tenant_id, provider)" in m0061._SQLITE_CREATE_TABLE

    def test_tenant_fk_cascade_declared(self, m0061) -> None:
        assert "REFERENCES tenants(id) ON DELETE CASCADE" in m0061._PG_CREATE_TABLE
        assert "REFERENCES tenants(id) ON DELETE CASCADE" in m0061._SQLITE_CREATE_TABLE


# ─── Group 2: functional SQLite upgrade ───────────────────────────────────


def _bootstrap_pre_0061_tenants_schema(conn: sqlite3.Connection) -> None:
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
def upgraded_db(monkeypatch, m0061) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    _bootstrap_pre_0061_tenants_schema(conn)
    conn.execute("INSERT INTO tenants (id, name) VALUES ('t-a', 'Tenant A')")
    conn.execute("INSERT INTO tenants (id, name) VALUES ('t-b', 'Tenant B')")
    _bind(monkeypatch, conn)
    m0061.upgrade()
    return conn


class TestSqliteUpgradeCreatesTable:
    def test_provisioned_databases_table_exists(self, upgraded_db) -> None:
        row = upgraded_db.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='provisioned_databases'"
        ).fetchone()
        assert row is not None

    def test_all_required_columns_present(self, upgraded_db) -> None:
        cols = {
            row[1]
            for row in upgraded_db.execute(
                "PRAGMA table_info(provisioned_databases)"
            ).fetchall()
        }
        required = {
            "tenant_id",
            "provider",
            "connection_url_enc",
            "created_at",
            "status",
        }
        missing = required - cols
        assert not missing, f"provisioned_databases missing columns: {missing}"

    def test_composite_pk_rejects_duplicate_pair(self, upgraded_db) -> None:
        upgraded_db.execute(
            "INSERT INTO provisioned_databases "
            "(tenant_id, provider, connection_url_enc, created_at, status) "
            "VALUES (?, ?, ?, ?, ?)",
            ("t-a", "neon", "enc-1", 1.0, "ready"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            upgraded_db.execute(
                "INSERT INTO provisioned_databases "
                "(tenant_id, provider, connection_url_enc, created_at, status) "
                "VALUES (?, ?, ?, ?, ?)",
                ("t-a", "neon", "enc-2", 2.0, "ready"),
            )

    def test_composite_pk_allows_different_providers_per_tenant(
        self, upgraded_db,
    ) -> None:
        upgraded_db.execute(
            "INSERT INTO provisioned_databases "
            "(tenant_id, provider, connection_url_enc, created_at, status) "
            "VALUES (?, ?, ?, ?, ?)",
            ("t-a", "neon", "enc-1", 1.0, "ready"),
        )
        upgraded_db.execute(
            "INSERT INTO provisioned_databases "
            "(tenant_id, provider, connection_url_enc, created_at, status) "
            "VALUES (?, ?, ?, ?, ?)",
            ("t-a", "supabase", "enc-2", 1.0, "ACTIVE"),
        )

    def test_provider_check_rejects_unknown_provider(
        self, upgraded_db,
    ) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            upgraded_db.execute(
                "INSERT INTO provisioned_databases "
                "(tenant_id, provider, connection_url_enc, created_at, status) "
                "VALUES (?, ?, ?, ?, ?)",
                ("t-a", "rds", "enc-1", 1.0, "ready"),
            )

    def test_provider_check_accepts_each_supported_provider(
        self, upgraded_db,
    ) -> None:
        from backend import db_provisioning

        for provider in sorted(db_provisioning.list_providers()):
            upgraded_db.execute(
                "INSERT INTO provisioned_databases "
                "(tenant_id, provider, connection_url_enc, created_at, status) "
                "VALUES (?, ?, ?, ?, ?)",
                ("t-b", provider, f"enc-{provider}", 1.0, "ready"),
            )

    def test_tenant_delete_cascades_to_provisioned_databases(
        self, upgraded_db,
    ) -> None:
        upgraded_db.execute(
            "INSERT INTO provisioned_databases "
            "(tenant_id, provider, connection_url_enc, created_at, status) "
            "VALUES (?, ?, ?, ?, ?)",
            ("t-a", "neon", "enc-a", 1.0, "ready"),
        )
        upgraded_db.execute(
            "INSERT INTO provisioned_databases "
            "(tenant_id, provider, connection_url_enc, created_at, status) "
            "VALUES (?, ?, ?, ?, ?)",
            ("t-b", "neon", "enc-b", 1.0, "ready"),
        )
        upgraded_db.execute("DELETE FROM tenants WHERE id = 't-a'")
        rows = upgraded_db.execute(
            "SELECT tenant_id FROM provisioned_databases ORDER BY tenant_id"
        ).fetchall()
        assert rows == [("t-b",)]


# ─── Group 3: idempotency ─────────────────────────────────────────────────


class TestIdempotentReupgrade:
    def test_running_upgrade_twice_no_dup_no_change(
        self, monkeypatch, m0061,
    ) -> None:
        conn = sqlite3.connect(":memory:")
        _bootstrap_pre_0061_tenants_schema(conn)
        _bind(monkeypatch, conn)
        m0061.upgrade()
        m0061.upgrade()
        row = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='provisioned_databases'"
        ).fetchone()
        assert row is not None


# ─── Group 4: PG dialect branch executes ──────────────────────────────────


class TestPgBranchExecutes:
    def test_pg_branch_emits_create_table(
        self, monkeypatch, m0061,
    ) -> None:
        from alembic import op as alembic_op

        captured: list[str] = []

        class _PgBind:
            class _Dialect:
                name = "postgresql"

            dialect = _Dialect()

            def exec_driver_sql(self, sql, *a, **k):
                captured.append(sql)

        monkeypatch.setattr(alembic_op, "get_bind", lambda: _PgBind())
        m0061.upgrade()

        assert len(captured) == 1
        joined = "\n".join(captured)
        assert "CREATE TABLE IF NOT EXISTS provisioned_databases" in joined
        assert "DOUBLE PRECISION" in joined
        assert "REFERENCES tenants(id) ON DELETE CASCADE" in joined
        assert "PRIMARY KEY (tenant_id, provider)" in joined

    def test_pg_downgrade_drops_table(
        self, monkeypatch, m0061,
    ) -> None:
        from alembic import op as alembic_op

        captured: list[str] = []

        class _PgBind:
            class _Dialect:
                name = "postgresql"

            dialect = _Dialect()

            def exec_driver_sql(self, sql, *a, **k):
                captured.append(sql)

        def _exec(sql):
            captured.append(str(sql))

        monkeypatch.setattr(alembic_op, "get_bind", lambda: _PgBind())
        monkeypatch.setattr(alembic_op, "execute", _exec)
        m0061.downgrade()
        joined = "\n".join(captured)
        assert "DROP TABLE IF EXISTS provisioned_databases" in joined


# ─── Group 5: migrator drift guard ───────────────────────────────────────


class TestMigratorListsTable:
    def test_provisioned_databases_in_tables_in_order(self) -> None:
        import importlib.util as _u
        repo_root = Path(__file__).resolve().parents[2]
        spec = _u.spec_from_file_location(
            "migrate_sqlite_to_pg", repo_root / "scripts" / "migrate_sqlite_to_pg.py"
        )
        mig = _u.module_from_spec(spec)
        sys.modules["migrate_sqlite_to_pg"] = mig
        spec.loader.exec_module(mig)
        assert "provisioned_databases" in mig.TABLES_IN_ORDER

    def test_provisioned_databases_not_in_identity_id_set(self) -> None:
        import importlib.util as _u
        repo_root = Path(__file__).resolve().parents[2]
        spec = _u.spec_from_file_location(
            "migrate_sqlite_to_pg", repo_root / "scripts" / "migrate_sqlite_to_pg.py"
        )
        mig = _u.module_from_spec(spec)
        sys.modules["migrate_sqlite_to_pg"] = mig
        spec.loader.exec_module(mig)
        assert "provisioned_databases" not in mig.TABLES_WITH_IDENTITY_ID

    def test_provisioned_databases_replays_after_tenants(self) -> None:
        import importlib.util as _u
        repo_root = Path(__file__).resolve().parents[2]
        spec = _u.spec_from_file_location(
            "migrate_sqlite_to_pg", repo_root / "scripts" / "migrate_sqlite_to_pg.py"
        )
        mig = _u.module_from_spec(spec)
        sys.modules["migrate_sqlite_to_pg"] = mig
        spec.loader.exec_module(mig)
        order = list(mig.TABLES_IN_ORDER)
        assert order.index("tenants") < order.index("provisioned_databases")
