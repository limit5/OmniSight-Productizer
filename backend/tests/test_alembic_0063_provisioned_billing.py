"""FS.8.3 -- alembic 0063 ``provisioned_billing`` migration contract."""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_0063 = (
    BACKEND_ROOT / "alembic" / "versions" / "0063_provisioned_billing.py"
)


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture(scope="module")
def m0063():
    return _load_module(MIGRATION_0063, "_alembic_test_0063")


# -- Group 1: structural guards --------------------------------------------


class TestMigrationFileStructure:
    @pytest.fixture(scope="class")
    def source(self) -> str:
        return MIGRATION_0063.read_text()

    def test_revision_id_is_0063(self, source: str) -> None:
        assert 'revision = "0063"' in source

    def test_down_revision_is_0062(self, source: str) -> None:
        assert 'down_revision = "0062"' in source

    def test_required_columns_present(self, m0063) -> None:
        required = (
            "tenant_id",
            "provider",
            "stripe_customer_id",
            "stripe_subscription_id",
            "stripe_price_id",
            "status",
            "current_period_end",
            "cancel_at_period_end",
            "created_at",
            "updated_at",
        )
        for col in required:
            assert col in m0063._PG_CREATE_TABLE, f"PG branch missing {col}"
            assert col in m0063._SQLITE_CREATE_TABLE, f"SQLite branch missing {col}"

    def test_pg_branch_uses_double_precision_and_boolean(self, m0063) -> None:
        assert "DOUBLE PRECISION" in m0063._PG_CREATE_TABLE
        assert "BOOLEAN NOT NULL DEFAULT FALSE" in m0063._PG_CREATE_TABLE
        assert " REAL" not in m0063._PG_CREATE_TABLE

    def test_sqlite_branch_uses_real_and_integer_boolean(self, m0063) -> None:
        assert " REAL" in m0063._SQLITE_CREATE_TABLE
        assert "INTEGER NOT NULL DEFAULT 0" in m0063._SQLITE_CREATE_TABLE
        assert "DOUBLE PRECISION" not in m0063._SQLITE_CREATE_TABLE

    def test_create_table_is_idempotent(self, m0063) -> None:
        assert "CREATE TABLE IF NOT EXISTS provisioned_billing" in (
            m0063._PG_CREATE_TABLE
        )
        assert "CREATE TABLE IF NOT EXISTS provisioned_billing" in (
            m0063._SQLITE_CREATE_TABLE
        )

    def test_provider_check_clause_is_stripe_only(self, m0063) -> None:
        assert m0063._PROVIDERS_SQL == "'stripe'"
        assert "CHECK (provider IN ('stripe'))" in m0063._PG_CREATE_TABLE
        assert "CHECK (provider IN ('stripe'))" in m0063._SQLITE_CREATE_TABLE

    def test_composite_pk_declared(self, m0063) -> None:
        assert "PRIMARY KEY (tenant_id, provider)" in m0063._PG_CREATE_TABLE
        assert "PRIMARY KEY (tenant_id, provider)" in m0063._SQLITE_CREATE_TABLE

    def test_tenant_fk_cascade_declared(self, m0063) -> None:
        assert "REFERENCES tenants(id) ON DELETE CASCADE" in m0063._PG_CREATE_TABLE
        assert "REFERENCES tenants(id) ON DELETE CASCADE" in (
            m0063._SQLITE_CREATE_TABLE
        )


# -- Group 2: functional SQLite upgrade ------------------------------------


def _bootstrap_pre_0063_tenants_schema(conn: sqlite3.Connection) -> None:
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
def upgraded_db(monkeypatch, m0063) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    _bootstrap_pre_0063_tenants_schema(conn)
    conn.execute("INSERT INTO tenants (id, name) VALUES ('t-a', 'Tenant A')")
    conn.execute("INSERT INTO tenants (id, name) VALUES ('t-b', 'Tenant B')")
    _bind(monkeypatch, conn)
    m0063.upgrade()
    return conn


def _insert_billing(
    conn: sqlite3.Connection,
    tenant_id: str,
    *,
    provider: str = "stripe",
    customer_id: str = "cus_test",
    subscription_id: str = "sub_test",
    price_id: str = "price_test",
    status: str = "active",
) -> None:
    conn.execute(
        "INSERT INTO provisioned_billing "
        "(tenant_id, provider, stripe_customer_id, stripe_subscription_id, "
        "stripe_price_id, status, current_period_end, cancel_at_period_end, "
        "created_at, updated_at) "
        "VALUES (:tenant_id, :provider, :customer_id, :subscription_id, "
        ":price_id, :status, :current_period_end, :cancel_at_period_end, "
        ":created_at, :updated_at)",
        {
            "tenant_id": tenant_id,
            "provider": provider,
            "customer_id": customer_id,
            "subscription_id": subscription_id,
            "price_id": price_id,
            "status": status,
            "current_period_end": 1_800_000_000.0,
            "cancel_at_period_end": 0,
            "created_at": 1.0,
            "updated_at": 2.0,
        },
    )


class TestSqliteUpgradeCreatesTable:
    def test_provisioned_billing_table_exists(self, upgraded_db) -> None:
        row = upgraded_db.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='provisioned_billing'"
        ).fetchone()
        assert row is not None

    def test_all_required_columns_present(self, upgraded_db) -> None:
        cols = {
            row[1]
            for row in upgraded_db.execute(
                "PRAGMA table_info(provisioned_billing)"
            ).fetchall()
        }
        required = {
            "tenant_id",
            "provider",
            "stripe_customer_id",
            "stripe_subscription_id",
            "stripe_price_id",
            "status",
            "current_period_end",
            "cancel_at_period_end",
            "created_at",
            "updated_at",
        }
        missing = required - cols
        assert not missing, f"provisioned_billing missing columns: {missing}"

    def test_composite_pk_rejects_duplicate_pair(self, upgraded_db) -> None:
        _insert_billing(upgraded_db, "t-a")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_billing(
                upgraded_db,
                "t-a",
                customer_id="cus_other",
                subscription_id="sub_other",
            )

    def test_composite_pk_allows_different_tenants(self, upgraded_db) -> None:
        _insert_billing(upgraded_db, "t-a", customer_id="cus_a")
        _insert_billing(upgraded_db, "t-b", customer_id="cus_b")

    def test_provider_check_rejects_unknown_provider(self, upgraded_db) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            _insert_billing(upgraded_db, "t-a", provider="paddle")

    def test_provider_check_accepts_stripe(self, upgraded_db) -> None:
        _insert_billing(upgraded_db, "t-a", provider="stripe")

    def test_tenant_delete_cascades_to_provisioned_billing(
        self, upgraded_db,
    ) -> None:
        _insert_billing(upgraded_db, "t-a", customer_id="cus_a")
        _insert_billing(upgraded_db, "t-b", customer_id="cus_b")
        upgraded_db.execute("DELETE FROM tenants WHERE id = 't-a'")
        rows = upgraded_db.execute(
            "SELECT tenant_id FROM provisioned_billing ORDER BY tenant_id"
        ).fetchall()
        assert rows == [("t-b",)]


# -- Group 3: idempotency ---------------------------------------------------


class TestIdempotentReupgrade:
    def test_running_upgrade_twice_no_dup_no_change(
        self, monkeypatch, m0063,
    ) -> None:
        conn = sqlite3.connect(":memory:")
        _bootstrap_pre_0063_tenants_schema(conn)
        _bind(monkeypatch, conn)
        m0063.upgrade()
        m0063.upgrade()
        row = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='provisioned_billing'"
        ).fetchone()
        assert row is not None


# -- Group 4: PG dialect branch executes -----------------------------------


class TestPgBranchExecutes:
    def test_pg_branch_emits_create_table(self, monkeypatch, m0063) -> None:
        from alembic import op as alembic_op

        captured: list[str] = []

        class _PgBind:
            class _Dialect:
                name = "postgresql"

            dialect = _Dialect()

            def exec_driver_sql(self, sql, *a, **k):
                captured.append(sql)

        monkeypatch.setattr(alembic_op, "get_bind", lambda: _PgBind())
        m0063.upgrade()

        assert len(captured) == 1
        joined = "\n".join(captured)
        assert "CREATE TABLE IF NOT EXISTS provisioned_billing" in joined
        assert "DOUBLE PRECISION" in joined
        assert "BOOLEAN NOT NULL DEFAULT FALSE" in joined
        assert "REFERENCES tenants(id) ON DELETE CASCADE" in joined
        assert "PRIMARY KEY (tenant_id, provider)" in joined

    def test_pg_downgrade_drops_table(self, monkeypatch, m0063) -> None:
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
        m0063.downgrade()
        joined = "\n".join(captured)
        assert "DROP TABLE IF EXISTS provisioned_billing" in joined


# -- Group 5: migrator drift guard -----------------------------------------


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

    def test_provisioned_billing_in_tables_in_order(self) -> None:
        mig = self._load_migrator()
        assert "provisioned_billing" in mig.TABLES_IN_ORDER

    def test_provisioned_billing_not_in_identity_id_set(self) -> None:
        mig = self._load_migrator()
        assert "provisioned_billing" not in mig.TABLES_WITH_IDENTITY_ID

    def test_provisioned_billing_replays_after_tenants(self) -> None:
        mig = self._load_migrator()
        order = list(mig.TABLES_IN_ORDER)
        assert order.index("tenants") < order.index("provisioned_billing")
