"""OP-2508 — alembic 0253 ``character_def`` contract.

Mirrors the 0251 seed-migration test structure. Pins:

1.  **Structural** — revision wires onto 0252 (head at OP-2508 pickup);
    PG branch carries the slug regex CHECK + TIMESTAMPTZ, and both
    dialects share the S/M/L/X ``max_tier`` CHECK.

2.  **Drift guard** — the migration's frozen ``SEED_CHARACTERS`` copy is
    field-for-field identical to the live
    ``backend.agents.character_registry.CHARACTERS`` roster, so the
    registry and the seeded table cannot silently diverge.

3.  **Functional (SQLite)** — upgrade against in-memory SQLite creates
    the table and seeds exactly the 4 built-ins with the registry's
    values; ``active`` defaults TRUE, timestamps self-populate, and the
    ``max_tier`` CHECK rejects out-of-ladder values.

4.  **Idempotency** — re-running upgrade() leaves the row count
    unchanged (``INSERT OR IGNORE`` path).

5.  **Symmetry** — downgrade() drops the table; upgrade → downgrade →
    upgrade round-trips back to the seeded state.

6.  **PG branch** — a postgresql-dialect stub captures the rendered
    SQL: regex CHECK, TIMESTAMPTZ, and ``ON CONFLICT (slug) DO
    NOTHING`` seed inserts.

Module-global state audit: pure DDL+DML migration; every fixture
re-derives state from a fresh in-memory SQLite connection.
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

from backend.agents.character_registry import CHARACTERS


BACKEND_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_0253 = BACKEND_ROOT / "alembic" / "versions" / "0253_character_def.py"

_SEED_FIELDS = ("slug", "display_name", "brain", "guild", "max_tier", "blurb")


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture(scope="module")
def m0253():
    return _load_module(MIGRATION_0253, "_alembic_test_0253")


# ─── Group 1: structural guards ───────────────────────────────────────────


class TestMigrationFileStructure:
    @pytest.fixture(scope="class")
    def source(self) -> str:
        return MIGRATION_0253.read_text()

    def test_revision_id_is_0253(self, source: str) -> None:
        assert 'revision = "0253"' in source

    def test_down_revision_is_0252(self, source: str) -> None:
        assert 'down_revision = "0252"' in source

    def test_pg_branch_has_slug_regex_check(self, source: str) -> None:
        assert "slug ~ '^[a-z][a-z0-9-]*$'" in source
        assert 'dialect == "postgresql"' in source

    def test_pg_branch_uses_timestamptz(self, source: str) -> None:
        assert "TIMESTAMPTZ" in source

    def test_max_tier_check_in_both_dialects(self, m0253) -> None:
        assert "CHECK (max_tier IN ('S','M','L','X'))" in m0253._PG_DDL
        assert "CHECK (max_tier IN ('S','M','L','X'))" in m0253._SQLITE_DDL

    def test_brain_and_guild_not_enum_constrained(self, m0253) -> None:
        # Vocabularies grow; the app validates against the live registries.
        for ddl in (m0253._PG_DDL, m0253._SQLITE_DDL):
            assert "CHECK (brain" not in ddl
            assert "CHECK (guild" not in ddl


# ─── Group 2: drift guard vs character_registry ───────────────────────────


class TestSeedMatchesCharacterRegistry:
    def test_same_slug_set(self, m0253) -> None:
        assert {r["slug"] for r in m0253.SEED_CHARACTERS} == set(CHARACTERS)

    def test_seed_count_is_four(self, m0253) -> None:
        assert len(m0253.SEED_CHARACTERS) == 4

    def test_every_field_matches_registry(self, m0253) -> None:
        for row in m0253.SEED_CHARACTERS:
            char = CHARACTERS[row["slug"]]
            assert set(row) == set(_SEED_FIELDS), row["slug"]
            for field in _SEED_FIELDS:
                assert row[field] == getattr(char, field), (
                    f"character_def seed drift for {row['slug']!r}.{field}: "
                    f"migration={row[field]!r} registry={getattr(char, field)!r}"
                )


# ─── Group 3: functional SQLite upgrade ───────────────────────────────────


class _StubBind:
    def __init__(self, raw: sqlite3.Connection, dialect: str = "sqlite") -> None:
        self._raw = raw

        class _Dialect:
            name = dialect

        self.dialect = _Dialect()

    def exec_driver_sql(self, sql: str, *args, **kwargs):
        return self._raw.execute(sql)


def _bind(monkeypatch, conn: sqlite3.Connection) -> None:
    from alembic import op as alembic_op

    monkeypatch.setattr(alembic_op, "get_bind", lambda: _StubBind(conn))


@pytest.fixture()
def upgraded_db(monkeypatch, m0253) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    _bind(monkeypatch, conn)
    m0253.upgrade()
    return conn


class TestSqliteUpgradeSeedsRows:
    def test_four_rows_seeded(self, upgraded_db) -> None:
        cur = upgraded_db.execute("SELECT COUNT(*) FROM character_def")
        assert cur.fetchone()[0] == 4

    def test_rows_match_registry_exactly(self, upgraded_db) -> None:
        cur = upgraded_db.execute(
            "SELECT slug, display_name, brain, guild, max_tier, blurb "
            "FROM character_def ORDER BY slug"
        )
        rows = {r[0]: r for r in cur.fetchall()}
        assert set(rows) == set(CHARACTERS)
        for slug, char in CHARACTERS.items():
            assert rows[slug] == (
                char.slug, char.display_name, char.brain,
                char.guild, char.max_tier, char.blurb,
            )

    def test_active_defaults_true(self, upgraded_db) -> None:
        cur = upgraded_db.execute(
            "SELECT COUNT(*) FROM character_def WHERE active"
        )
        assert cur.fetchone()[0] == 4

    def test_timestamps_self_populate(self, upgraded_db) -> None:
        cur = upgraded_db.execute(
            "SELECT created_at, updated_at FROM character_def WHERE slug='nova'"
        )
        created_at, updated_at = cur.fetchone()
        assert created_at
        assert updated_at

    def test_max_tier_check_rejects_out_of_ladder(self, upgraded_db) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            upgraded_db.execute(
                "INSERT INTO character_def "
                "(slug, display_name, brain, guild, max_tier) "
                "VALUES ('bogus', 'Bogus', 'subscription-claude', "
                "'backend', 'Z')"
            )


# ─── Group 4: idempotency ─────────────────────────────────────────────────


class TestIdempotentReupgrade:
    def test_running_upgrade_twice_no_dup(self, monkeypatch, m0253) -> None:
        conn = sqlite3.connect(":memory:")
        _bind(monkeypatch, conn)
        m0253.upgrade()
        first = conn.execute("SELECT COUNT(*) FROM character_def").fetchone()[0]
        m0253.upgrade()
        second = conn.execute("SELECT COUNT(*) FROM character_def").fetchone()[0]
        assert first == second == 4

    def test_reupgrade_preserves_operator_edits(self, monkeypatch, m0253) -> None:
        # INSERT OR IGNORE must not clobber an operator-deactivated row.
        conn = sqlite3.connect(":memory:")
        _bind(monkeypatch, conn)
        m0253.upgrade()
        conn.execute("UPDATE character_def SET active = 0 WHERE slug='rex'")
        m0253.upgrade()
        cur = conn.execute("SELECT active FROM character_def WHERE slug='rex'")
        assert cur.fetchone()[0] == 0


# ─── Group 5: downgrade symmetry ──────────────────────────────────────────


class TestDowngradeRoundTrip:
    def test_downgrade_drops_table(self, monkeypatch, m0253) -> None:
        conn = sqlite3.connect(":memory:")
        _bind(monkeypatch, conn)
        m0253.upgrade()
        m0253.downgrade()
        cur = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='character_def'"
        )
        assert cur.fetchone() is None

    def test_upgrade_downgrade_upgrade_round_trips(
        self, monkeypatch, m0253
    ) -> None:
        conn = sqlite3.connect(":memory:")
        _bind(monkeypatch, conn)
        m0253.upgrade()
        m0253.downgrade()
        m0253.upgrade()
        cur = conn.execute("SELECT COUNT(*) FROM character_def")
        assert cur.fetchone()[0] == 4


# ─── Group 6: PG dialect branch ───────────────────────────────────────────


class TestPgBranchExecutes:
    def test_pg_branch_emits_regex_check_and_on_conflict(
        self, monkeypatch, m0253
    ) -> None:
        from alembic import op as alembic_op

        captured: list[str] = []

        class _PgBind:
            class _Dialect:
                name = "postgresql"

            dialect = _Dialect()

            def exec_driver_sql(self, sql: str, *args, **kwargs):
                captured.append(sql)

        monkeypatch.setattr(alembic_op, "get_bind", lambda: _PgBind())
        m0253.upgrade()
        assert len(captured) == 1 + 4  # DDL + 4 seed inserts
        ddl = captured[0]
        assert "slug ~ '^[a-z][a-z0-9-]*$'" in ddl
        assert "TIMESTAMPTZ" in ddl
        for insert in captured[1:]:
            assert "ON CONFLICT (slug) DO NOTHING" in insert
            assert "INSERT OR IGNORE" not in insert
        seeded = {r["slug"] for r in m0253.SEED_CHARACTERS}
        assert seeded == {
            s for s in ("nova", "pixel", "sage", "rex")
            if any(f"'{s}'" in i for i in captured[1:])
        }
