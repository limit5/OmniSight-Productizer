"""OP-2238 alembic 0249 -- ``meetings`` + ``transcript_segments`` contract.

Local upgrade/downgrade verification on SQLite (matches the 0248
precedent). Asserts the migration:

* declares the expected revision id / down_revision (0248 chain),
* lays down both tables with the UNIQUE + index shape the router
  relies on for tenant-scoped reads + reconnect-replay dedup,
* downgrades cleanly to a pristine DB.
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


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture(scope="module")
def m0249():
    return _load_module(MIGRATION_0249, "_alembic_test_0249")


# ━━ Structural guards ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestMigrationFileStructure:
    @pytest.fixture(scope="class")
    def source(self) -> str:
        return MIGRATION_0249.read_text()

    def test_revision_id_is_0249(self, source: str) -> None:
        assert 'revision = "0249"' in source

    def test_down_revision_is_0248(self, source: str) -> None:
        assert 'down_revision = "0248"' in source

    def test_backwards_compat_marked_safe(self, source: str) -> None:
        assert "backwards-compat: safe" in source

    def test_unique_segment_key_declared(self, source: str) -> None:
        assert "UNIQUE (tenant_id, meeting_id, session_id, segment_seq)" in source

    def test_tenant_index_declared(self, source: str) -> None:
        assert "idx_transcript_segments_tenant" in source

    def test_ordered_read_index_declared(self, source: str) -> None:
        assert "idx_transcript_segments_tenant_meeting_seq" in source


# ━━ SQLite upgrade/downgrade ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.fixture()
def conn():
    """An empty in-memory SQLite connection with an Operations proxy
    installed (mirrors the 0248 test fixture)."""
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


def _index_names(conn, table: str) -> set[str]:
    rows = conn.exec_driver_sql(
        f"PRAGMA index_list('{table}')"
    ).fetchall()
    return {row[1] for row in rows}


class TestSqliteUpgradeDowngrade:
    def test_upgrade_creates_both_tables(self, conn, m0249) -> None:
        m0249.upgrade()
        tables = _table_names(conn)
        assert "meetings" in tables
        assert "transcript_segments" in tables

    def test_upgrade_adds_unique_segment_key(self, conn, m0249) -> None:
        m0249.upgrade()
        # Insert two distinct rows then attempt a duplicate on the
        # UNIQUE composite -- the second insert must raise.
        conn.exec_driver_sql(
            "INSERT INTO transcript_segments "
            "(id, tenant_id, meeting_id, session_id, segment_seq, "
            " text, is_final, source, created_at, updated_at) "
            "VALUES ('s1', 't-1', 'm-1', 'sess', 1, 'hi', 0, 'asr', 1.0, 1.0)"
        )
        with pytest.raises(Exception):
            conn.exec_driver_sql(
                "INSERT INTO transcript_segments "
                "(id, tenant_id, meeting_id, session_id, segment_seq, "
                " text, is_final, source, created_at, updated_at) "
                "VALUES ('s2', 't-1', 'm-1', 'sess', 1, 'dup', 0, 'asr', 2.0, 2.0)"
            )

    def test_upgrade_adds_expected_indexes(self, conn, m0249) -> None:
        m0249.upgrade()
        idx = _index_names(conn, "transcript_segments")
        assert "idx_transcript_segments_tenant" in idx
        assert "idx_transcript_segments_tenant_meeting_seq" in idx

    def test_downgrade_drops_both_tables(self, conn, m0249) -> None:
        m0249.upgrade()
        assert {"meetings", "transcript_segments"} <= _table_names(conn)
        m0249.downgrade()
        remaining = _table_names(conn)
        assert "meetings" not in remaining
        assert "transcript_segments" not in remaining

    def test_upgrade_is_idempotent_on_rerun(self, conn, m0249) -> None:
        m0249.upgrade()
        # Second invocation must not raise -- CREATE TABLE IF NOT EXISTS
        # plus CREATE INDEX IF NOT EXISTS guarantees this.
        m0249.upgrade()
        tables = _table_names(conn)
        assert "meetings" in tables
        assert "transcript_segments" in tables
