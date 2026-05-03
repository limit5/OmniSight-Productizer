"""SC.10.1 -- alembic 0064 ``dsar_requests`` migration contract."""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_0064 = BACKEND_ROOT / "alembic" / "versions" / "0064_dsar_requests.py"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture(scope="module")
def m0064():
    return _load_module(MIGRATION_0064, "_alembic_test_0064")


# -- Group 1: structural guards --------------------------------------------


class TestMigrationFileStructure:
    @pytest.fixture(scope="class")
    def source(self) -> str:
        return MIGRATION_0064.read_text()

    def test_revision_id_is_0064(self, source: str) -> None:
        assert 'revision = "0064"' in source

    def test_down_revision_is_0063(self, source: str) -> None:
        assert 'down_revision = "0063"' in source

    def test_required_columns_present(self, m0064) -> None:
        required = (
            "id",
            "tenant_id",
            "user_id",
            "request_type",
            "status",
            "requested_at",
            "due_at",
            "completed_at",
            "payload_json",
            "result_json",
            "error",
            "version",
        )
        for col in required:
            assert col in m0064._PG_CREATE_TABLE, f"PG branch missing {col}"
            assert col in m0064._SQLITE_CREATE_TABLE, f"SQLite branch missing {col}"

    def test_pg_branch_uses_jsonb_and_double_precision(self, m0064) -> None:
        assert "JSONB NOT NULL DEFAULT '{}'::jsonb" in m0064._PG_CREATE_TABLE
        assert "DOUBLE PRECISION" in m0064._PG_CREATE_TABLE
        assert " REAL" not in m0064._PG_CREATE_TABLE

    def test_sqlite_branch_uses_text_json_and_real(self, m0064) -> None:
        assert "TEXT NOT NULL DEFAULT '{}'" in m0064._SQLITE_CREATE_TABLE
        assert " REAL" in m0064._SQLITE_CREATE_TABLE
        assert "JSONB" not in m0064._SQLITE_CREATE_TABLE
        assert "DOUBLE PRECISION" not in m0064._SQLITE_CREATE_TABLE

    def test_create_table_is_idempotent(self, m0064) -> None:
        assert "CREATE TABLE IF NOT EXISTS dsar_requests" in (
            m0064._PG_CREATE_TABLE
        )
        assert "CREATE TABLE IF NOT EXISTS dsar_requests" in (
            m0064._SQLITE_CREATE_TABLE
        )

    def test_request_type_check_clause_covers_sc10_endpoint_rows(
        self, m0064,
    ) -> None:
        assert m0064._REQUEST_TYPES_SQL == "'access','erasure','portability'"
        assert "CHECK (request_type IN ('access','erasure','portability'))" in (
            m0064._PG_CREATE_TABLE
        )

    def test_status_check_clause_declared(self, m0064) -> None:
        assert m0064._STATUSES_SQL == (
            "'cancelled','completed','failed','pending','processing'"
        )
        assert "DEFAULT 'pending'" in m0064._PG_CREATE_TABLE
        assert "DEFAULT 'pending'" in m0064._SQLITE_CREATE_TABLE

    def test_text_pk_and_fks_declared(self, m0064) -> None:
        assert "id            TEXT PRIMARY KEY" in m0064._PG_CREATE_TABLE
        assert "REFERENCES tenants(id) ON DELETE CASCADE" in m0064._PG_CREATE_TABLE
        assert "REFERENCES users(id) ON DELETE CASCADE" in m0064._PG_CREATE_TABLE

    def test_indexes_declared(self, m0064) -> None:
        assert "idx_dsar_requests_user_status" in m0064._PG_INDEX_USER_STATUS
        assert "idx_dsar_requests_tenant_due" in m0064._PG_INDEX_TENANT_DUE
        assert "WHERE status IN ('pending', 'processing')" in (
            m0064._PG_INDEX_TENANT_DUE
        )


# -- Group 2: functional SQLite upgrade ------------------------------------


def _bootstrap_pre_0064_parent_schema(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(
        """
        CREATE TABLE tenants (
            id   TEXT PRIMARY KEY,
            name TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE users (
            id        TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            email     TEXT NOT NULL DEFAULT ''
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
def upgraded_db(monkeypatch, m0064) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    _bootstrap_pre_0064_parent_schema(conn)
    conn.execute("INSERT INTO tenants (id, name) VALUES ('t-a', 'Tenant A')")
    conn.execute("INSERT INTO tenants (id, name) VALUES ('t-b', 'Tenant B')")
    conn.execute(
        "INSERT INTO users (id, tenant_id, email) VALUES ('u-a', 't-a', 'a@x')"
    )
    conn.execute(
        "INSERT INTO users (id, tenant_id, email) VALUES ('u-b', 't-b', 'b@x')"
    )
    _bind(monkeypatch, conn)
    m0064.upgrade()
    return conn


def _insert_dsar(
    conn: sqlite3.Connection,
    request_id: str,
    user_id: str = "u-a",
    tenant_id: str = "t-a",
    *,
    request_type: str = "access",
    status: str = "pending",
) -> None:
    conn.execute(
        "INSERT INTO dsar_requests "
        "(id, tenant_id, user_id, request_type, status, requested_at, due_at) "
        "VALUES (:id, :tenant_id, :user_id, :request_type, :status, "
        ":requested_at, :due_at)",
        {
            "id": request_id,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "request_type": request_type,
            "status": status,
            "requested_at": 1_770_000_000.0,
            "due_at": 1_772_592_000.0,
        },
    )


class TestSqliteUpgradeCreatesTable:
    def test_dsar_requests_table_exists(self, upgraded_db) -> None:
        row = upgraded_db.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='dsar_requests'"
        ).fetchone()
        assert row is not None

    def test_all_required_columns_present(self, upgraded_db) -> None:
        cols = {
            row[1]
            for row in upgraded_db.execute("PRAGMA table_info(dsar_requests)")
            .fetchall()
        }
        required = {
            "id",
            "tenant_id",
            "user_id",
            "request_type",
            "status",
            "requested_at",
            "due_at",
            "completed_at",
            "payload_json",
            "result_json",
            "error",
            "version",
        }
        missing = required - cols
        assert not missing, f"dsar_requests missing columns: {missing}"

    def test_request_type_check_rejects_unknown_type(self, upgraded_db) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            _insert_dsar(upgraded_db, "dsar-bad-type", request_type="objection")

    def test_request_type_check_accepts_sc10_endpoint_types(
        self, upgraded_db,
    ) -> None:
        for request_type in ("access", "erasure", "portability"):
            _insert_dsar(upgraded_db, f"dsar-{request_type}", request_type=request_type)

    def test_status_check_rejects_unknown_status(self, upgraded_db) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            _insert_dsar(upgraded_db, "dsar-bad-status", status="done")

    def test_default_json_error_and_version_values(self, upgraded_db) -> None:
        _insert_dsar(upgraded_db, "dsar-defaults")
        row = upgraded_db.execute(
            "SELECT payload_json, result_json, error, version "
            "FROM dsar_requests WHERE id='dsar-defaults'"
        ).fetchone()
        assert row == ("{}", "{}", "", 0)

    def test_user_delete_cascades_to_dsar_requests(self, upgraded_db) -> None:
        _insert_dsar(upgraded_db, "dsar-a", user_id="u-a", tenant_id="t-a")
        _insert_dsar(upgraded_db, "dsar-b", user_id="u-b", tenant_id="t-b")
        upgraded_db.execute("DELETE FROM users WHERE id = 'u-a'")
        rows = upgraded_db.execute(
            "SELECT id FROM dsar_requests ORDER BY id"
        ).fetchall()
        assert rows == [("dsar-b",)]

    def test_indexes_created(self, upgraded_db) -> None:
        rows = upgraded_db.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND name LIKE 'idx_dsar_requests_%' "
            "ORDER BY name"
        ).fetchall()
        assert rows == [
            ("idx_dsar_requests_tenant_due",),
            ("idx_dsar_requests_user_status",),
        ]


# -- Group 3: idempotency ---------------------------------------------------


class TestIdempotentReupgrade:
    def test_running_upgrade_twice_no_dup_no_change(
        self, monkeypatch, m0064,
    ) -> None:
        conn = sqlite3.connect(":memory:")
        _bootstrap_pre_0064_parent_schema(conn)
        _bind(monkeypatch, conn)
        m0064.upgrade()
        m0064.upgrade()
        row = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='dsar_requests'"
        ).fetchone()
        assert row is not None


# -- Group 4: PG dialect branch executes -----------------------------------


class TestPgBranchExecutes:
    def test_pg_branch_emits_create_table_and_indexes(
        self, monkeypatch, m0064,
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
        m0064.upgrade()

        assert len(captured) == 3
        joined = "\n".join(captured)
        assert "CREATE TABLE IF NOT EXISTS dsar_requests" in joined
        assert "JSONB NOT NULL DEFAULT '{}'::jsonb" in joined
        assert "DOUBLE PRECISION" in joined
        assert "REFERENCES tenants(id) ON DELETE CASCADE" in joined
        assert "REFERENCES users(id) ON DELETE CASCADE" in joined
        assert "idx_dsar_requests_user_status" in joined
        assert "idx_dsar_requests_tenant_due" in joined

    def test_pg_downgrade_drops_indexes_and_table(self, monkeypatch, m0064) -> None:
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
        m0064.downgrade()
        joined = "\n".join(captured)
        assert "DROP INDEX IF EXISTS idx_dsar_requests_tenant_due" in joined
        assert "DROP INDEX IF EXISTS idx_dsar_requests_user_status" in joined
        assert "DROP TABLE IF EXISTS dsar_requests" in joined


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

    def test_dsar_requests_in_tables_in_order(self) -> None:
        mig = self._load_migrator()
        assert "dsar_requests" in mig.TABLES_IN_ORDER

    def test_dsar_requests_not_in_identity_id_set(self) -> None:
        mig = self._load_migrator()
        assert "dsar_requests" not in mig.TABLES_WITH_IDENTITY_ID

    def test_dsar_requests_replays_after_tenants_and_users(self) -> None:
        mig = self._load_migrator()
        order = list(mig.TABLES_IN_ORDER)
        assert order.index("tenants") < order.index("dsar_requests")
        assert order.index("users") < order.index("dsar_requests")
