"""FX.1.11 — alembic 0007 session/audit enhancements downgrade contract.

The 0007 migration adds:
  audit_log.session_id      TEXT
  idx_audit_log_session     index on (session_id)
  sessions.metadata         TEXT NOT NULL DEFAULT '{}'
  sessions.mfa_verified     INTEGER NOT NULL DEFAULT 0
  sessions.rotated_from     TEXT

Pre-FX.1.11 the downgrade was ``pass`` — bare-minimum compliance. This
test locks the post-FX.1.11 contract:

  * downgrade() walks SQLAlchemy schema ops (op.drop_index /
    op.drop_column), not f-string DDL (same SQLAlchemy-ops track FX.1.10
    pulled 0106 onto).
  * Drop order is reverse-of-add: index before its column, sessions
    columns in reverse-of-upgrade order, audit_log.session_id last.
  * IF EXISTS semantics preserved on the index drop, mirroring the
    upgrade's CREATE INDEX IF NOT EXISTS idempotency contract. (Column
    drops don't get if_exists in alembic 1.14 — see DropColumnOp
    signature.)
  * Functional round-trip on SQLite: upgrade() then downgrade() leaves
    audit_log / sessions back at their pre-0007 column shape.
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_0007 = (
    BACKEND_ROOT / "alembic" / "versions" / "0007_session_audit_enhancements.py"
)


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture(scope="module")
def m0007():
    return _load_module(MIGRATION_0007, "_alembic_test_0007")


# ─── Group 1: structural guards on the migration file ──────────────


class TestMigrationFileStructure:
    @pytest.fixture(scope="class")
    def source(self) -> str:
        return MIGRATION_0007.read_text()

    def test_revision_id(self, source: str) -> None:
        assert 'revision = "0007"' in source

    def test_down_revision(self, source: str) -> None:
        assert 'down_revision = "0006"' in source

    def test_downgrade_is_not_pass(self, source: str) -> None:
        # Pre-FX.1.11 the downgrade was a literal `pass`. The whole
        # point of FX.1.11 is to give it a real body.
        assert "def downgrade() -> None:\n    pass\n" not in source

    def test_downgrade_uses_alembic_schema_ops(self, source: str) -> None:
        # downgrade() must go through op.drop_index / op.drop_column,
        # not raw f-string DDL (the FX.1.10 / SQLAlchemy-ops track).
        assert "op.drop_index" in source
        assert "op.drop_column" in source

    def test_downgrade_has_no_fstring_ddl(self, source: str) -> None:
        # Belt-and-suspenders: no f-string-built DDL in downgrade(),
        # same fingerprint FX.1.10's HANDOFF flagged.
        assert 'f"DROP' not in source
        assert "f'DROP" not in source
        assert "exec_driver_sql" in source  # only in upgrade()
        # exec_driver_sql may legitimately appear once for upgrade();
        # we don't want it to show up in downgrade(). Cheap check by
        # locating the downgrade() block and grepping inside it.
        idx = source.index("def downgrade()")
        downgrade_body = source[idx:]
        assert "exec_driver_sql" not in downgrade_body


# ─── Group 2: downgrade ops contract ───────────────────────────────


class TestDowngradeCallsCorrectOps:
    """Monkey-patch ``alembic.op.drop_index`` / ``alembic.op.drop_column``
    and assert downgrade() drives them with the right (name, kwargs) in
    the right order. This is dialect-agnostic — runs without a real DB."""

    def test_drops_in_reverse_of_upgrade_order(self, monkeypatch, m0007) -> None:
        from alembic import op as alembic_op

        dropped_indexes: list[tuple[str, dict]] = []
        dropped_columns: list[tuple[str, str]] = []

        def _drop_index(name, *args, **kwargs):
            dropped_indexes.append((name, kwargs))

        def _drop_column(table, name, *args, **kwargs):
            dropped_columns.append((table, name))

        monkeypatch.setattr(alembic_op, "drop_index", _drop_index)
        monkeypatch.setattr(alembic_op, "drop_column", _drop_column)

        m0007.downgrade()

        # Index is dropped before its underlying column.
        assert dropped_indexes == [
            ("idx_audit_log_session", {"table_name": "audit_log", "if_exists": True}),
        ]

        # Columns reverse-of-add: sessions.{rotated_from, mfa_verified,
        # metadata} then audit_log.session_id.
        assert dropped_columns == [
            ("sessions", "rotated_from"),
            ("sessions", "mfa_verified"),
            ("sessions", "metadata"),
            ("audit_log", "session_id"),
        ]

    def test_index_drop_is_idempotent(self, monkeypatch, m0007) -> None:
        # if_exists=True mirrors upgrade's CREATE INDEX IF NOT EXISTS;
        # partial-rollback / re-run scenarios stay safe.
        from alembic import op as alembic_op

        recorded: list[dict] = []

        monkeypatch.setattr(
            alembic_op, "drop_index",
            lambda name, *a, **kw: recorded.append(kw),
        )
        monkeypatch.setattr(alembic_op, "drop_column", lambda *a, **kw: None)

        m0007.downgrade()
        assert recorded == [{"table_name": "audit_log", "if_exists": True}]


# ─── Group 3: SQLite functional round-trip ──────────────────────────


def _bootstrap_pre_0007_schema(conn) -> None:
    """Materialise just enough of the post-0006 schema for 0007 to ALTER:
    audit_log + sessions parent tables. We don't run alembic 0001-0006
    because their cross-table coupling is overkill here."""
    conn.exec_driver_sql(
        """CREATE TABLE audit_log (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               ts REAL NOT NULL,
               actor TEXT NOT NULL DEFAULT 'system',
               action TEXT NOT NULL,
               entity_kind TEXT NOT NULL,
               entity_id TEXT,
               before_json TEXT NOT NULL DEFAULT '{}',
               after_json TEXT NOT NULL DEFAULT '{}',
               prev_hash TEXT NOT NULL DEFAULT '',
               curr_hash TEXT NOT NULL
           )"""
    )
    conn.exec_driver_sql(
        """CREATE TABLE sessions (
               token TEXT PRIMARY KEY,
               user_id TEXT NOT NULL,
               csrf_token TEXT NOT NULL,
               created_at REAL NOT NULL,
               expires_at REAL NOT NULL,
               last_seen_at REAL NOT NULL,
               ip TEXT NOT NULL DEFAULT '',
               user_agent TEXT NOT NULL DEFAULT ''
           )"""
    )


def _columns(conn, table: str) -> set[str]:
    rows = conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


def _indexes(conn, table: str) -> set[str]:
    rows = conn.exec_driver_sql(f"PRAGMA index_list({table})").fetchall()
    return {row[1] for row in rows}


class TestSqliteRoundTrip:
    """Functional round-trip: upgrade() adds the four columns + index,
    downgrade() removes them. Skips when the local SQLite is too old
    for ALTER TABLE DROP COLUMN (< 3.35); in that environment alembic's
    op.drop_column raises and the test is meaningless.

    Uses a real SQLAlchemy engine + alembic MigrationContext so that
    op.drop_index / op.drop_column have a valid Operations proxy
    (they need one — pure get_bind monkey-patch is not enough)."""

    @pytest.fixture()
    def alembic_session(self):
        import sqlalchemy as sa
        from alembic.runtime.migration import MigrationContext

        engine = sa.create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            _bootstrap_pre_0007_schema(conn)
            ctx = MigrationContext.configure(connection=conn)
            yield conn, ctx

    def _require_modern_sqlite(self) -> None:
        major, minor, *_ = sqlite3.sqlite_version_info
        if (major, minor) < (3, 35):
            pytest.skip(
                f"SQLite {sqlite3.sqlite_version} < 3.35 lacks "
                f"ALTER TABLE DROP COLUMN — downgrade() can't run"
            )

    def test_upgrade_adds_columns_and_index(
        self, m0007, alembic_session,
    ) -> None:
        from alembic.operations import Operations

        conn, ctx = alembic_session
        # Operations.context installs the module-level proxy that
        # m0007.upgrade()'s `from alembic import op` refers to.
        with Operations.context(ctx):
            m0007.upgrade()

        assert "session_id" in _columns(conn, "audit_log")
        assert "idx_audit_log_session" in _indexes(conn, "audit_log")
        sessions_cols = _columns(conn, "sessions")
        assert {"metadata", "mfa_verified", "rotated_from"} <= sessions_cols

    def test_round_trip_restores_pre_0007_shape(
        self, m0007, alembic_session,
    ) -> None:
        self._require_modern_sqlite()

        from alembic.operations import Operations

        conn, ctx = alembic_session

        pre_audit_cols = _columns(conn, "audit_log")
        pre_sessions_cols = _columns(conn, "sessions")
        pre_audit_indexes = _indexes(conn, "audit_log")

        with Operations.context(ctx):
            m0007.upgrade()
            assert _columns(conn, "audit_log") != pre_audit_cols  # sanity
            m0007.downgrade()

        # Post-downgrade: column / index sets back to baseline.
        assert _columns(conn, "audit_log") == pre_audit_cols
        assert _columns(conn, "sessions") == pre_sessions_cols
        assert _indexes(conn, "audit_log") == pre_audit_indexes
