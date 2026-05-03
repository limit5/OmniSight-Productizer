"""FX.1.13 — alembic 0009 workflow_run_version downgrade contract.

The 0009 migration adds one column to workflow_runs:
  version INTEGER NOT NULL DEFAULT 0

Pre-FX.1.13 the downgrade was ``pass`` — bare-minimum compliance, no
schema rollback. This test locks the post-FX.1.13 contract:

  * downgrade() walks SQLAlchemy schema ops (op.drop_column), not
    f-string DDL — same SQLAlchemy-ops track FX.1.10 / FX.1.11 /
    FX.1.12 pulled 0106 / 0007 / 0008 onto.
  * Drops workflow_runs.version (the single column upgrade added).
  * Functional round-trip on SQLite: upgrade() then downgrade() leaves
    workflow_runs back at its pre-0009 column shape.
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_0009 = (
    BACKEND_ROOT / "alembic" / "versions" / "0009_workflow_run_version.py"
)


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture(scope="module")
def m0009():
    return _load_module(MIGRATION_0009, "_alembic_test_0009")


# ─── Group 1: structural guards on the migration file ──────────────


class TestMigrationFileStructure:
    @pytest.fixture(scope="class")
    def source(self) -> str:
        return MIGRATION_0009.read_text()

    def test_revision_id(self, source: str) -> None:
        assert 'revision = "0009"' in source

    def test_down_revision(self, source: str) -> None:
        assert 'down_revision = "0008"' in source

    def test_downgrade_is_not_pass(self, source: str) -> None:
        # Pre-FX.1.13 the downgrade was a literal `pass`. The whole
        # point of FX.1.13 is to give it a real body.
        assert "def downgrade() -> None:\n    pass\n" not in source

    def test_downgrade_uses_alembic_schema_ops(self, source: str) -> None:
        # downgrade() must go through op.drop_column, not raw f-string
        # DDL (the FX.1.10 / SQLAlchemy-ops track).
        assert "op.drop_column" in source

    def test_downgrade_has_no_fstring_ddl(self, source: str) -> None:
        # Belt-and-suspenders: no f-string-built DDL in downgrade(),
        # same fingerprint FX.1.10 / FX.1.11 / FX.1.12 flagged.
        assert 'f"DROP' not in source
        assert "f'DROP" not in source
        # exec_driver_sql may legitimately appear in upgrade(); we
        # don't want it inside the downgrade() block.
        idx = source.index("def downgrade()")
        downgrade_body = source[idx:]
        assert "exec_driver_sql" not in downgrade_body


# ─── Group 2: downgrade ops contract ───────────────────────────────


class TestDowngradeCallsCorrectOps:
    """Monkey-patch ``alembic.op.drop_column`` and assert downgrade()
    drives it with the right (table, column) pair. Dialect-agnostic —
    runs without a real DB."""

    def test_drops_workflow_runs_version(
        self, monkeypatch, m0009,
    ) -> None:
        from alembic import op as alembic_op

        dropped: list[tuple[str, str]] = []

        def _drop_column(table, name, *args, **kwargs):
            dropped.append((table, name))

        monkeypatch.setattr(alembic_op, "drop_column", _drop_column)

        m0009.downgrade()

        assert dropped == [("workflow_runs", "version")]


# ─── Group 3: SQLite functional round-trip ──────────────────────────


def _bootstrap_pre_0009_schema(conn) -> None:
    """Materialise just enough of the post-0008 workflow_runs table for
    0009 to ALTER. We don't run alembic 0001-0008 because their cross-
    table coupling is overkill here.

    Column set mirrors 0002's `CREATE TABLE workflow_runs` (pre-0009
    baseline). 0003-0008 add no columns to workflow_runs (verified by
    grep workflow_runs in versions/), so this is the exact pre-0009
    column set."""
    conn.exec_driver_sql(
        """CREATE TABLE workflow_runs (
               id              TEXT PRIMARY KEY,
               kind            TEXT NOT NULL,
               started_at      REAL NOT NULL,
               completed_at    REAL,
               status          TEXT NOT NULL DEFAULT 'running',
               last_step_id    TEXT,
               metadata        TEXT NOT NULL DEFAULT '{}'
           )"""
    )


def _columns(conn, table: str) -> set[str]:
    rows = conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


class TestSqliteRoundTrip:
    """Functional round-trip: upgrade() adds the version column,
    downgrade() removes it. Skips when the local SQLite is too old
    for ALTER TABLE DROP COLUMN (< 3.35); in that environment alembic's
    op.drop_column raises and the test is meaningless.

    Uses a real SQLAlchemy engine + alembic MigrationContext so that
    op.drop_column has a valid Operations proxy (it needs one — pure
    get_bind monkey-patch is not enough)."""

    @pytest.fixture()
    def alembic_session(self):
        import sqlalchemy as sa
        from alembic.runtime.migration import MigrationContext

        engine = sa.create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            _bootstrap_pre_0009_schema(conn)
            ctx = MigrationContext.configure(connection=conn)
            yield conn, ctx

    def _require_modern_sqlite(self) -> None:
        major, minor, *_ = sqlite3.sqlite_version_info
        if (major, minor) < (3, 35):
            pytest.skip(
                f"SQLite {sqlite3.sqlite_version} < 3.35 lacks "
                f"ALTER TABLE DROP COLUMN — downgrade() can't run"
            )

    def test_upgrade_adds_version_column(
        self, m0009, alembic_session,
    ) -> None:
        from alembic.operations import Operations

        conn, ctx = alembic_session
        # Operations.context installs the module-level proxy that
        # m0009.upgrade()'s `from alembic import op` refers to.
        with Operations.context(ctx):
            m0009.upgrade()

        wr_cols = _columns(conn, "workflow_runs")
        assert "version" in wr_cols

    def test_round_trip_restores_pre_0009_shape(
        self, m0009, alembic_session,
    ) -> None:
        self._require_modern_sqlite()

        from alembic.operations import Operations

        conn, ctx = alembic_session

        pre_wr_cols = _columns(conn, "workflow_runs")
        assert "version" not in pre_wr_cols  # sanity

        with Operations.context(ctx):
            m0009.upgrade()
            assert _columns(conn, "workflow_runs") != pre_wr_cols  # sanity
            m0009.downgrade()

        # Post-downgrade: column set back to baseline.
        assert _columns(conn, "workflow_runs") == pre_wr_cols
