"""FX.1.12 — alembic 0008 account-lockout downgrade contract.

The 0008 migration adds two columns to users:
  failed_login_count INTEGER NOT NULL DEFAULT 0
  locked_until       REAL

Pre-FX.1.12 the downgrade was ``pass`` — bare-minimum compliance, no
schema rollback. This test locks the post-FX.1.12 contract:

  * downgrade() walks SQLAlchemy schema ops (op.drop_column), not
    f-string DDL — same SQLAlchemy-ops track FX.1.10 / FX.1.11 pulled
    0106 / 0007 onto.
  * Drop order is reverse-of-add: locked_until before
    failed_login_count.
  * Functional round-trip on SQLite: upgrade() then downgrade() leaves
    users back at its pre-0008 column shape.
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_0008 = (
    BACKEND_ROOT / "alembic" / "versions" / "0008_account_lockout.py"
)


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture(scope="module")
def m0008():
    return _load_module(MIGRATION_0008, "_alembic_test_0008")


# ─── Group 1: structural guards on the migration file ──────────────


class TestMigrationFileStructure:
    @pytest.fixture(scope="class")
    def source(self) -> str:
        return MIGRATION_0008.read_text()

    def test_revision_id(self, source: str) -> None:
        assert 'revision = "0008"' in source

    def test_down_revision(self, source: str) -> None:
        assert 'down_revision = "0007"' in source

    def test_downgrade_is_not_pass(self, source: str) -> None:
        # Pre-FX.1.12 the downgrade was a literal `pass`. The whole
        # point of FX.1.12 is to give it a real body.
        assert "def downgrade() -> None:\n    pass\n" not in source

    def test_downgrade_uses_alembic_schema_ops(self, source: str) -> None:
        # downgrade() must go through op.drop_column, not raw f-string
        # DDL (the FX.1.10 / SQLAlchemy-ops track).
        assert "op.drop_column" in source

    def test_downgrade_has_no_fstring_ddl(self, source: str) -> None:
        # Belt-and-suspenders: no f-string-built DDL in downgrade(),
        # same fingerprint FX.1.10 / FX.1.11 flagged.
        assert 'f"DROP' not in source
        assert "f'DROP" not in source
        assert "exec_driver_sql" in source  # only in upgrade()
        # exec_driver_sql may legitimately appear in upgrade(); we
        # don't want it inside the downgrade() block.
        idx = source.index("def downgrade()")
        downgrade_body = source[idx:]
        assert "exec_driver_sql" not in downgrade_body


# ─── Group 2: downgrade ops contract ───────────────────────────────


class TestDowngradeCallsCorrectOps:
    """Monkey-patch ``alembic.op.drop_column`` and assert downgrade()
    drives it with the right (table, column) pairs in the right
    order. Dialect-agnostic — runs without a real DB."""

    def test_drops_in_reverse_of_upgrade_order(
        self, monkeypatch, m0008,
    ) -> None:
        from alembic import op as alembic_op

        dropped: list[tuple[str, str]] = []

        def _drop_column(table, name, *args, **kwargs):
            dropped.append((table, name))

        monkeypatch.setattr(alembic_op, "drop_column", _drop_column)

        m0008.downgrade()

        # locked_until was added second by upgrade(), so dropped first.
        # failed_login_count was added first, so dropped last.
        assert dropped == [
            ("users", "locked_until"),
            ("users", "failed_login_count"),
        ]


# ─── Group 3: SQLite functional round-trip ──────────────────────────


def _bootstrap_pre_0008_schema(conn) -> None:
    """Materialise just enough of the post-0007 users table for 0008
    to ALTER. We don't run alembic 0001-0007 because their cross-table
    coupling is overkill here.

    Column set mirrors 0005's `CREATE TABLE users` (pre-0008 baseline);
    `must_change_password` lives on a later migration (0016) and is
    intentionally not pre-seeded here."""
    conn.exec_driver_sql(
        """CREATE TABLE users (
               id              TEXT PRIMARY KEY,
               email           TEXT NOT NULL UNIQUE,
               name            TEXT NOT NULL DEFAULT '',
               role            TEXT NOT NULL DEFAULT 'viewer',
               password_hash   TEXT NOT NULL DEFAULT '',
               oidc_provider   TEXT NOT NULL DEFAULT '',
               oidc_subject    TEXT NOT NULL DEFAULT '',
               enabled         INTEGER NOT NULL DEFAULT 1,
               created_at      TEXT NOT NULL DEFAULT (datetime('now')),
               last_login_at   TEXT
           )"""
    )


def _columns(conn, table: str) -> set[str]:
    rows = conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


class TestSqliteRoundTrip:
    """Functional round-trip: upgrade() adds the two columns,
    downgrade() removes them. Skips when the local SQLite is too old
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
            _bootstrap_pre_0008_schema(conn)
            ctx = MigrationContext.configure(connection=conn)
            yield conn, ctx

    def _require_modern_sqlite(self) -> None:
        major, minor, *_ = sqlite3.sqlite_version_info
        if (major, minor) < (3, 35):
            pytest.skip(
                f"SQLite {sqlite3.sqlite_version} < 3.35 lacks "
                f"ALTER TABLE DROP COLUMN — downgrade() can't run"
            )

    def test_upgrade_adds_columns(
        self, m0008, alembic_session,
    ) -> None:
        from alembic.operations import Operations

        conn, ctx = alembic_session
        # Operations.context installs the module-level proxy that
        # m0008.upgrade()'s `from alembic import op` refers to.
        with Operations.context(ctx):
            m0008.upgrade()

        users_cols = _columns(conn, "users")
        assert {"failed_login_count", "locked_until"} <= users_cols

    def test_round_trip_restores_pre_0008_shape(
        self, m0008, alembic_session,
    ) -> None:
        self._require_modern_sqlite()

        from alembic.operations import Operations

        conn, ctx = alembic_session

        pre_users_cols = _columns(conn, "users")
        assert "failed_login_count" not in pre_users_cols  # sanity
        assert "locked_until" not in pre_users_cols

        with Operations.context(ctx):
            m0008.upgrade()
            assert _columns(conn, "users") != pre_users_cols  # sanity
            m0008.downgrade()

        # Post-downgrade: column set back to baseline.
        assert _columns(conn, "users") == pre_users_cols
