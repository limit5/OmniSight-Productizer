"""Phase-3-Runtime-v2 SP-2.2 — alembic 0017 SQLite no-op + FTS5 smoke.

The tsvector migration (0017) is PG-only by design: ``upgrade()`` and
``downgrade()`` both early-return when ``conn.dialect.name !=
"postgresql"``. This test module proves the SQLite dev path is
undisturbed:

  1. Structural: the dialect-guard clauses are present in the source
     file (catches accidental removal).

  2. Functional: calling ``upgrade()`` / ``downgrade()`` with a fake
     SQLite bind runs without issuing any DDL, i.e. it genuinely
     short-circuits.

  3. Smoke: SQLite's FTS5 virtual table + MATCH query (the dev-mode
     search primitive that SP-3.12 will keep using on SQLite) still
     works end-to-end.

Why we do NOT drive the full ``alembic upgrade head`` on fresh SQLite
here: a pre-existing bug in 0016 creates an index referencing
``episodic_memory.last_used_at`` that the column-add step leaves
absent under SQLite (0016 gates the ADD COLUMN behind is_pg, but the
CREATE INDEX runs unconditionally). In production, ``db.py::_migrate()``
adds the column before alembic runs, so this never surfaces; but a
vanilla `alembic upgrade head` against a fresh SQLite file fails
somewhere around 0016. That's a 0016 issue, not a 0017 issue — the
full-upgrade regression for 0017 is covered by
``test_alembic_0017_tsvector.py`` against the test PG (real target
dialect).
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    BACKEND_ROOT / "alembic" / "versions"
    / "0017_episodic_memory_tsvector.py"
)


# ─── Group 1: structural guards on the migration file ───────────────


class TestMigrationFileHasDialectGuard:
    @pytest.fixture(scope="class")
    def source(self) -> str:
        return MIGRATION_PATH.read_text()

    def test_upgrade_has_is_pg_guard(self, source: str) -> None:
        # We look for the canonical shape — the specific text of the
        # guard. If someone refactors it, this test surfaces the change
        # so they can confirm the guard still short-circuits SQLite.
        assert 'dialect.name == "postgresql"' in source, (
            "0017 must check dialect.name before running any DDL — "
            "required so SQLite dev DBs don't hit `tsvector` errors"
        )

    def test_named_is_pg_flag_used(self, source: str) -> None:
        # A named boolean is_pg is more maintainable than a bare
        # comparison peppered through the function. Both upgrade() and
        # downgrade() should name the flag.
        assert "is_pg" in source

    def test_both_upgrade_and_downgrade_have_early_return(
        self, source: str,
    ) -> None:
        # Count dialect-check occurrences — should be at least one per
        # function (upgrade + downgrade). If consolidated into a helper,
        # update this test accordingly but make sure SQLite short-circuit
        # is preserved in BOTH directions.
        hits = source.count('dialect.name == "postgresql"')
        assert hits >= 2, (
            f"Expected is_pg guard in BOTH upgrade() and downgrade(); "
            f"found {hits} occurrence(s) of the dialect check"
        )


# ─── Group 2: functional early-return via direct invocation ─────────


class TestUpgradeEarlyReturnOnSqlite:
    """Import the migration module and call its ``upgrade()`` /
    ``downgrade()`` functions with a fake SQLite bind. Verify no DDL
    is issued, i.e. the SQLite branch truly short-circuits.

    Why not use alembic's test harness: alembic.op.get_bind() requires
    a full MigrationContext + ``op._proxy`` setup. Building that is
    more ceremony than value for this test. We spy on
    ``alembic.op.get_bind`` directly."""

    @pytest.fixture()
    def migration_mod(self) -> Any:
        # Load the migration module dynamically by file path (it's not
        # on sys.path as a regular importable package — alembic does
        # its own discovery).
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_0017_sp22_test", MIGRATION_PATH,
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def _sqlite_bind(self) -> MagicMock:
        """Fake bind whose dialect.name is 'sqlite' — enough for the
        migration's is_pg check. ``exec_driver_sql`` is a MagicMock so
        we can assert it was NEVER called on the SQLite path."""
        bind = MagicMock()
        bind.dialect.name = "sqlite"
        return bind

    def test_upgrade_is_noop_on_sqlite(
        self, migration_mod: Any, monkeypatch,
    ) -> None:
        fake_bind = self._sqlite_bind()
        monkeypatch.setattr(
            migration_mod.op, "get_bind", lambda: fake_bind,
        )
        migration_mod.upgrade()  # must not raise
        # Crucial: no DDL must have been issued against SQLite.
        fake_bind.exec_driver_sql.assert_not_called()

    def test_downgrade_is_noop_on_sqlite(
        self, migration_mod: Any, monkeypatch,
    ) -> None:
        fake_bind = self._sqlite_bind()
        monkeypatch.setattr(
            migration_mod.op, "get_bind", lambda: fake_bind,
        )
        migration_mod.downgrade()  # must not raise
        fake_bind.exec_driver_sql.assert_not_called()

    def test_upgrade_calls_ddl_on_pg(
        self, migration_mod: Any, monkeypatch,
    ) -> None:
        # Positive sibling: when dialect reports postgresql, the
        # upgrade() DOES issue DDL. Without this test, a future
        # change that broke the PG path but kept the SQLite check
        # would silently leave ``0017`` as a no-op everywhere.
        bind = MagicMock()
        bind.dialect.name = "postgresql"
        monkeypatch.setattr(
            migration_mod.op, "get_bind", lambda: bind,
        )
        migration_mod.upgrade()
        # PG path issues at least two exec_driver_sql calls (ALTER
        # TABLE ADD COLUMN + CREATE INDEX). Check the call count rather
        # than exact SQL, so refactoring inside the PG branch doesn't
        # trip this test.
        assert bind.exec_driver_sql.call_count >= 2, (
            f"PG branch of upgrade() should issue at least 2 DDL "
            f"statements (column + index); got "
            f"{bind.exec_driver_sql.call_count}"
        )


# ─── Group 3: SQLite FTS5 dev-path smoke ────────────────────────────


class TestSqliteFts5StillWorks:
    """Smoke test that SP-2.2 preserves: SP-3.12's dev branch will
    use SQLite's FTS5 virtual table + MATCH query. This test creates
    a minimal SQLite DB with just the FTS5 primitive and proves a
    round-trip — independent of the full application schema."""

    def test_fts5_match_round_trip(self, tmp_path: Path) -> None:
        db_path = tmp_path / "fts5-smoke.db"
        conn = sqlite3.connect(db_path)
        try:
            # Minimal FTS5 table — same column set the real
            # episodic_memory_fts uses in db.py::init() (line ~122),
            # but without the external-content plumbing so the test
            # is self-contained.
            try:
                conn.execute(
                    """CREATE VIRTUAL TABLE em_fts USING fts5(
                           error_signature, solution, soc_vendor, tags
                       )"""
                )
            except sqlite3.OperationalError as exc:
                pytest.skip(
                    f"SQLite build lacks FTS5 extension — "
                    f"dev-path skip: {exc}"
                )
            conn.execute(
                """INSERT INTO em_fts
                       (error_signature, solution, soc_vendor, tags)
                   VALUES (?, ?, ?, ?)""",
                ("kernel panic on boot", "apply patch x",
                 "synaptics", "boot,kernel"),
            )
            conn.commit()

            # MATCH query — the hallmark of FTS5.
            rows = conn.execute(
                "SELECT error_signature FROM em_fts WHERE em_fts MATCH ?",
                ("kernel",),
            ).fetchall()
        finally:
            conn.close()

        assert len(rows) == 1, (
            "FTS5 MATCH query should find the row we inserted. If this "
            "fails, either SQLite is built without FTS5 (we skip in "
            "that case) or the FTS5 query language itself drifted — "
            "SP-3.12 relies on this primitive for the SQLite search path."
        )
        assert "kernel panic" in rows[0][0]
