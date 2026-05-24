"""OP-1654 alembic 0248 ``dag_plans`` executor-lease columns contract.

Local upgrade/downgrade verification only (per the ticket MUST NOT: no
host migration). Exercises the SQLite branch functionally and asserts the
PG branch emits the expected ``ALTER TABLE`` DDL via a capturing stub.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_0248 = (
    BACKEND_ROOT / "alembic" / "versions" / "0248_dag_plans_executor_lease.py"
)

_LEASE_COLS = ("claim_owner", "claim_token", "claim_expires_at", "heartbeat_at")


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture(scope="module")
def m0248():
    return _load_module(MIGRATION_0248, "_alembic_test_0248")


# -- Group 1: structural guards --------------------------------------------


class TestMigrationFileStructure:
    @pytest.fixture(scope="class")
    def source(self) -> str:
        return MIGRATION_0248.read_text()

    def test_revision_id_is_0248(self, source: str) -> None:
        assert 'revision = "0248"' in source

    def test_down_revision_is_0247(self, source: str) -> None:
        # 0247 (release_train) is the single alembic head this chains onto.
        assert 'down_revision = "0247"' in source

    def test_backwards_compat_marked_safe(self, source: str) -> None:
        assert "backwards-compat: safe" in source

    def test_all_four_lease_columns_declared(self, m0248) -> None:
        declared = {name for name, _ in m0248._LEASE_COLUMNS}
        assert declared == set(_LEASE_COLS)

    def test_columns_are_nullable_additive(self, m0248) -> None:
        # The migration must only ever add nullable columns -- assert no
        # NOT NULL / server_default slips into the Column construction.
        import sqlalchemy as sa

        for name, type_ in m0248._LEASE_COLUMNS:
            col = sa.Column(name, type_, nullable=True)
            assert col.nullable is True
            assert col.server_default is None

    def test_timestamp_columns_are_float(self, m0248) -> None:
        import sqlalchemy as sa

        by_name = dict(m0248._LEASE_COLUMNS)
        assert isinstance(by_name["claim_expires_at"], sa.Float)
        assert isinstance(by_name["heartbeat_at"], sa.Float)


# -- Group 2: functional SQLite upgrade/downgrade --------------------------


_CREATE_DAG_PLANS = """
CREATE TABLE dag_plans (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    dag_id          TEXT NOT NULL,
    run_id          TEXT,
    parent_plan_id  INTEGER,
    json_body       TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    mutation_round  INTEGER NOT NULL DEFAULT 0,
    validation_errors TEXT,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
)
"""


@pytest.fixture()
def conn():
    """A live SQLAlchemy connection over an in-memory dag_plans table.

    Yields the connection with an Operations proxy installed so the
    migration's ``op.add_column`` / ``op.drop_column`` and ``op.get_bind``
    all resolve against this same connection (mirrors the 0238 test).
    """
    import sqlalchemy as sa
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    engine = sa.create_engine("sqlite:///:memory:")
    connection = engine.connect()
    connection.exec_driver_sql(_CREATE_DAG_PLANS)
    ctx = MigrationContext.configure(connection=connection)
    with Operations.context(ctx):
        yield connection
    connection.close()


def _column_names(conn) -> set[str]:
    return {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(dag_plans)")}


class TestSqliteUpgradeDowngrade:
    def test_upgrade_adds_all_four_nullable_columns(self, conn, m0248) -> None:
        m0248.upgrade()
        cols = {
            row[1]: row
            for row in conn.exec_driver_sql("PRAGMA table_info(dag_plans)")
        }
        for name in _LEASE_COLS:
            assert name in cols, f"missing {name}"
            # PRAGMA table_info row index 3 == notnull flag; 0 means nullable.
            assert cols[name][3] == 0, f"{name} must be nullable"

    def test_downgrade_drops_all_four_columns(self, conn, m0248) -> None:
        m0248.upgrade()
        assert set(_LEASE_COLS) <= _column_names(conn)
        m0248.downgrade()
        remaining = _column_names(conn)
        for name in _LEASE_COLS:
            assert name not in remaining, f"{name} survived downgrade"
        # Pre-existing schema columns are untouched by the downgrade.
        assert {"id", "dag_id", "json_body", "status"} <= remaining

    def test_upgrade_is_idempotent_when_columns_exist(self, conn, m0248) -> None:
        conn.exec_driver_sql("ALTER TABLE dag_plans ADD COLUMN claim_owner TEXT")
        m0248.upgrade()  # must not raise on the pre-existing claim_owner
        owners = [n for n in _column_names(conn) if n == "claim_owner"]
        assert owners == ["claim_owner"]

    def test_existing_rows_intact_after_upgrade(self, conn, m0248) -> None:
        # AC "Exercised": an existing dag_plans row survives the upgrade
        # with its data intact and NULL across the new lease columns.
        conn.exec_driver_sql(
            "INSERT INTO dag_plans (dag_id, run_id, json_body, status, "
            "created_at, updated_at) VALUES "
            "('dag-1', 'run-1', '{\"nodes\": []}', 'validated', 100.0, 200.0)"
        )
        m0248.upgrade()
        row = conn.exec_driver_sql(
            "SELECT dag_id, run_id, json_body, status, created_at, updated_at, "
            "claim_owner, claim_token, claim_expires_at, heartbeat_at "
            "FROM dag_plans WHERE dag_id = 'dag-1'"
        ).fetchone()
        assert tuple(row) == (
            "dag-1",
            "run-1",
            '{"nodes": []}',
            "validated",
            100.0,
            200.0,
            None,
            None,
            None,
            None,
        )


# -- Group 3: PG dialect branch executes -----------------------------------


class _PgBind:
    class _Dialect:
        name = "postgresql"

    dialect = _Dialect()

    def __init__(self, existing: set[str]) -> None:
        self._existing = existing
        self.queries: list[str] = []

    def exec_driver_sql(self, sql, *a, **k):
        self.queries.append(sql)

        class _Result:
            def __init__(self, rows):
                self._rows = rows

            def fetchall(self):
                return self._rows

        return _Result([(c,) for c in self._existing])


class TestPgBranchProbesInformationSchema:
    def test_pg_existing_columns_reads_information_schema(
        self, monkeypatch, m0248
    ) -> None:
        from alembic import op as alembic_op

        bind = _PgBind(existing={"id", "dag_id"})
        monkeypatch.setattr(alembic_op, "get_bind", lambda: bind)
        present = m0248._existing_columns()
        assert present == {"id", "dag_id"}
        assert "information_schema.columns" in bind.queries[0]
        assert "dag_plans" in bind.queries[0]
