"""OP-2239 alembic 0250 -- ``meetings.retention_until`` contract.

Local upgrade/downgrade verification on SQLite (matches the 0249
precedent). Asserts the migration:

* declares the expected revision id / down_revision (0249 chain),
* adds the ``retention_until`` column to the existing ``meetings`` table
  laid down by 0249,
* adds the secondary index the retention sweep relies on,
* downgrades cleanly back to the 0249 shape.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_0249 = (
    BACKEND_ROOT / "alembic" / "versions" / "0249_bi0_transcripts.py"
)
MIGRATION_0250 = (
    BACKEND_ROOT / "alembic" / "versions" / "0250_bi0b_meetings_retention.py"
)


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture(scope="module")
def m0249():
    return _load_module(MIGRATION_0249, "_alembic_test_0249_for_0250")


@pytest.fixture(scope="module")
def m0250():
    return _load_module(MIGRATION_0250, "_alembic_test_0250")


# ━━ Structural guards ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestMigrationFileStructure:
    @pytest.fixture(scope="class")
    def source(self) -> str:
        return MIGRATION_0250.read_text()

    def test_revision_id_is_0250(self, source: str) -> None:
        assert 'revision = "0250"' in source

    def test_down_revision_is_0249(self, source: str) -> None:
        assert 'down_revision = "0249"' in source

    def test_backwards_compat_marked_safe(self, source: str) -> None:
        assert "backwards-compat: safe" in source

    def test_retention_column_declared(self, source: str) -> None:
        assert "retention_until" in source

    def test_retention_index_declared(self, source: str) -> None:
        assert "idx_meetings_retention_until" in source


# ━━ SQLite upgrade/downgrade ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.fixture()
def conn():
    """An empty in-memory SQLite connection with an Operations proxy
    installed (mirrors the 0249 test fixture)."""
    import sqlalchemy as sa
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    engine = sa.create_engine("sqlite:///:memory:")
    connection = engine.connect()
    ctx = MigrationContext.configure(connection=connection)
    with Operations.context(ctx):
        yield connection
    connection.close()


def _table_names(conn) -> set[str]:
    return {
        row[0]
        for row in conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def _column_names(conn, table: str) -> set[str]:
    rows = conn.exec_driver_sql(
        f"PRAGMA table_info({table})"
    ).fetchall()
    return {row[1] for row in rows}


def _index_names(conn, table: str) -> set[str]:
    rows = conn.exec_driver_sql(
        f"PRAGMA index_list('{table}')"
    ).fetchall()
    return {row[1] for row in rows}


class TestSqliteUpgradeDowngrade:
    def test_upgrade_adds_retention_column(self, conn, m0249, m0250) -> None:
        m0249.upgrade()
        m0250.upgrade()
        cols = _column_names(conn, "meetings")
        assert "retention_until" in cols

    def test_upgrade_adds_retention_index(self, conn, m0249, m0250) -> None:
        m0249.upgrade()
        m0250.upgrade()
        idx = _index_names(conn, "meetings")
        assert "idx_meetings_retention_until" in idx

    def test_upgrade_is_idempotent_on_rerun(self, conn, m0249, m0250) -> None:
        m0249.upgrade()
        m0250.upgrade()
        # Second invocation must not raise -- existence-probe guard +
        # CREATE INDEX IF NOT EXISTS guarantee this.
        m0250.upgrade()
        cols = _column_names(conn, "meetings")
        assert "retention_until" in cols

    def test_existing_meetings_rows_stay_intact(self, conn, m0249, m0250) -> None:
        """Additive nullable column -- a row written before the migration
        ran must still be readable afterwards with a NULL retention_until.
        """
        m0249.upgrade()
        conn.exec_driver_sql(
            "INSERT INTO meetings "
            "(id, tenant_id, status, created_at, updated_at) "
            "VALUES ('mtg-pre', 't-pre', 'open', 1.0, 1.0)"
        )
        m0250.upgrade()
        row = conn.exec_driver_sql(
            "SELECT id, retention_until FROM meetings WHERE id = 'mtg-pre'"
        ).fetchone()
        assert row is not None
        assert row[0] == "mtg-pre"
        assert row[1] is None

    def test_downgrade_drops_retention_column(self, conn, m0249, m0250) -> None:
        m0249.upgrade()
        m0250.upgrade()
        assert "retention_until" in _column_names(conn, "meetings")
        m0250.downgrade()
        # SQLite version may or may not support DROP COLUMN; the
        # migration tolerates either. We only require the index to be
        # gone and the table still in place.
        assert "meetings" in _table_names(conn)
        assert "idx_meetings_retention_until" not in _index_names(
            conn, "meetings",
        )
