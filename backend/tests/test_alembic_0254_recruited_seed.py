"""OP-2514 — alembic 0254 recruited-character + grandfather-baseline seed.

Mirrors the 0253 seed-migration test structure. Pins:

1.  **Structural** — revision wires onto 0253; upgrade is pure DML (no DDL:
    the three target tables exist since 0226/0239/0253).

2.  **Drift guards** — every recruited def constructs a valid live
    ``CharacterDef`` (brain/guild/tier vocabularies); slugs are disjoint
    from the built-ins; the migration's frozen skill-level thresholds match
    ``skill_leveling.compute_level``; card levels match
    ``xp_engine.level_for_xp``; skill_ids are canonical in the matrix.

3.  **Functional (SQLite fresh DB)** — after 0226→0239→0253→0254, the def
    table holds all 8 characters; iris card = Lv12/3280 with uvc/ipcam
    skills; argus/kai cards + skills seeded; vega gets a def row ONLY (no
    card, no skills); no branch_choice is ever seeded.

4.  **Integration** — ``character_registry.load_characters()`` fed the
    seeded rows returns all 8 (4 built-in + 4 recruited), all active.

5.  **Idempotency / no-clobber** — a second upgrade() changes nothing, and
    live progression written between runs (the existing-prod-DB case) is
    preserved (ON CONFLICT DO NOTHING semantics).

6.  **Downgrade** — removes ONLY the 4 recruited def rows; cards/skills and
    the built-in defs survive; upgrade → downgrade → upgrade round-trips.

7.  **PG branch** — a postgresql-dialect stub captures the rendered SQL:
    every upgrade statement is ``INSERT … ON CONFLICT … DO NOTHING``;
    downgrade is a single slug-scoped DELETE on character_def.

Module-global state audit: every fixture re-derives state from a fresh
in-memory SQLite connection; the character_registry snapshot is reset
around the integration test.
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

from backend.agents import character_registry as cr
from backend.agents import xp_engine
from backend.agents.skill_leveling import LEVEL_THRESHOLDS, compute_level
from backend.agents.skill_matrix import canonical_skill_ids


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERSIONS = BACKEND_ROOT / "alembic" / "versions"
MIGRATION_0254 = VERSIONS / "0254_recruited_character_seed.py"

_DEF_FIELDS = ("slug", "display_name", "brain", "guild", "max_tier", "blurb")
_RECRUITED = ("argus", "iris", "kai", "vega")
_BUILT_IN = ("nova", "pixel", "rex", "sage")


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture(scope="module")
def m0254():
    return _load_module(MIGRATION_0254, "_alembic_test_0254")


@pytest.fixture(scope="module")
def prior_migrations():
    """The migrations owning the three tables 0254 seeds into."""
    return tuple(
        _load_module(VERSIONS / fname, f"_alembic_test_0254_dep_{rev}")
        for rev, fname in (
            ("0226", "0226_agent_skill_state.py"),
            ("0239", "0239_agent_character_card.py"),
            ("0253", "0253_character_def.py"),
        )
    )


# ─── Group 1: structural guards ───────────────────────────────────────────


class TestMigrationFileStructure:
    @pytest.fixture(scope="class")
    def source(self) -> str:
        return MIGRATION_0254.read_text()

    def test_revision_id_is_0254(self, source: str) -> None:
        assert 'revision = "0254"' in source

    def test_down_revision_is_0253(self, source: str) -> None:
        assert 'down_revision = "0253"' in source

    def test_pure_dml_no_ddl(self, source: str) -> None:
        assert "CREATE TABLE" not in source
        assert "DROP TABLE" not in source

    def test_dialect_branches_present(self, source: str) -> None:
        assert 'dialect == "postgresql"' in source
        assert "INSERT OR IGNORE" in source
        assert "DO NOTHING" in source


# ─── Group 2: drift guards vs live registries ─────────────────────────────


class TestSeedDriftGuards:
    def test_recruited_defs_construct_valid_characterdefs(self, m0254) -> None:
        # CharacterDef.__post_init__ validates brain / guild / max_tier
        # against the live vocabularies (jira_dispatch, guild_registry).
        for row in m0254.RECRUITED_CHARACTERS:
            assert set(row) == set(_DEF_FIELDS), row["slug"]
            cr.CharacterDef(**row)

    def test_recruited_slugs_expected_and_disjoint_from_builtins(
        self, m0254
    ) -> None:
        assert tuple(sorted(m0254.RECRUITED_SLUGS)) == _RECRUITED
        assert not set(m0254.RECRUITED_SLUGS) & set(cr.CHARACTERS)

    def test_frozen_thresholds_match_skill_leveling(self, m0254) -> None:
        frozen = {level: xp for level, xp in m0254.SKILL_LEVEL_THRESHOLDS}
        frozen[1] = 0
        assert frozen == dict(LEVEL_THRESHOLDS)

    def test_seeded_skill_levels_match_compute_level(self, m0254) -> None:
        for _agent, skill_id, xp in m0254.GRANDFATHER_SKILLS:
            assert m0254.compute_skill_level(xp) == compute_level(xp), skill_id
        # The documented grandfather grants land on the documented levels.
        by_skill = {s: xp for _a, s, xp in m0254.GRANDFATHER_SKILLS}
        assert m0254.compute_skill_level(by_skill["uvc"]) == 4
        assert m0254.compute_skill_level(by_skill["ipcam"]) == 4
        assert m0254.compute_skill_level(by_skill["security_audit"]) == 4
        assert m0254.compute_skill_level(by_skill["mobile_app"]) == 5

    def test_seeded_card_levels_match_xp_engine_curve(self, m0254) -> None:
        for card in m0254.GRANDFATHER_CARDS:
            assert card["level"] == xp_engine.level_for_xp(card["xp"]), card

    def test_skill_ids_are_canonical(self, m0254) -> None:
        canonical = canonical_skill_ids()
        for _agent, skill_id, _xp in m0254.GRANDFATHER_SKILLS:
            assert skill_id in canonical, skill_id

    def test_vega_has_no_grandfather_grant(self, m0254) -> None:
        assert "vega" not in {c["agent_id"] for c in m0254.GRANDFATHER_CARDS}
        assert "vega" not in {a for a, _s, _x in m0254.GRANDFATHER_SKILLS}

    def test_card_identity_uses_slug_suffix(self, m0254) -> None:
        # Mirrors auto-runner-jira._resolve_card_identity (slug, brain, slug)
        # and keeps the unique ("class", instance_suffix) index conflict-free.
        brains = {r["slug"]: r["brain"] for r in m0254.RECRUITED_CHARACTERS}
        for card in m0254.GRANDFATHER_CARDS:
            assert card["instance_suffix"] == card["agent_id"]
            assert card["class"] == brains[card["agent_id"]]


# ─── SQLite harness ───────────────────────────────────────────────────────


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
def fresh_db(monkeypatch, prior_migrations, m0254) -> sqlite3.Connection:
    """A fresh DB at 0253 (0226+0239+0253 applied), then 0254."""
    conn = sqlite3.connect(":memory:")
    _bind(monkeypatch, conn)
    for prior in prior_migrations:
        prior.upgrade()
    m0254.upgrade()
    return conn


# ─── Group 3: functional fresh-DB seed ────────────────────────────────────


class TestFreshDbSeed:
    def test_def_table_has_all_eight(self, fresh_db) -> None:
        cur = fresh_db.execute("SELECT slug FROM character_def ORDER BY slug")
        assert tuple(r[0] for r in cur.fetchall()) == tuple(
            sorted(_BUILT_IN + _RECRUITED)
        )

    def test_recruited_def_values(self, fresh_db, m0254) -> None:
        cur = fresh_db.execute(
            "SELECT slug, display_name, brain, guild, max_tier, blurb, active "
            "FROM character_def WHERE slug IN ('iris','argus','kai','vega') "
            "ORDER BY slug"
        )
        rows = {r[0]: r for r in cur.fetchall()}
        for seed in m0254.RECRUITED_CHARACTERS:
            row = rows[seed["slug"]]
            assert row == (
                seed["slug"], seed["display_name"], seed["brain"],
                seed["guild"], seed["max_tier"], seed["blurb"], 1,
            )

    def test_iris_card_lv12_3280(self, fresh_db) -> None:
        cur = fresh_db.execute(
            'SELECT "class", instance_suffix, guild, level, xp '
            "FROM agent_character_card WHERE agent_id='iris'"
        )
        assert cur.fetchone() == ("subscription-claude", "iris", "isp", 12, 3280)

    def test_argus_and_kai_cards(self, fresh_db) -> None:
        cur = fresh_db.execute(
            'SELECT agent_id, "class", guild, level, xp '
            "FROM agent_character_card ORDER BY agent_id"
        )
        assert cur.fetchall() == [
            ("argus", "subscription-gemini", "auditor", 2, 320),
            ("iris", "subscription-claude", "isp", 12, 3280),
            ("kai", "subscription-codex", "mobile", 4, 710),
        ]

    def test_vega_seeded_def_only(self, fresh_db) -> None:
        assert fresh_db.execute(
            "SELECT COUNT(*) FROM character_def WHERE slug='vega'"
        ).fetchone()[0] == 1
        assert fresh_db.execute(
            "SELECT COUNT(*) FROM agent_character_card WHERE agent_id='vega'"
        ).fetchone()[0] == 0
        assert fresh_db.execute(
            "SELECT COUNT(*) FROM agent_skill_state WHERE agent_id='vega'"
        ).fetchone()[0] == 0

    def test_skill_rows_levels_and_xp(self, fresh_db) -> None:
        cur = fresh_db.execute(
            "SELECT agent_id, skill_id, level, xp FROM agent_skill_state "
            "ORDER BY agent_id, skill_id"
        )
        assert cur.fetchall() == [
            ("argus", "security_audit", 4, 320),
            ("iris", "ipcam", 4, 310),
            ("iris", "uvc", 4, 390),
            ("kai", "mobile_app", 5, 710),
        ]

    def test_no_branch_choice_seeded(self, fresh_db) -> None:
        cur = fresh_db.execute(
            "SELECT COUNT(*) FROM agent_skill_state "
            "WHERE branch_choice IS NOT NULL"
        )
        assert cur.fetchone()[0] == 0


# ─── Group 4: registry integration ────────────────────────────────────────


class TestRegistryLoadsAllEight:
    def test_load_characters_returns_eight(
        self, fresh_db, m0254, monkeypatch
    ) -> None:
        rows = [
            dict(zip(cr._DB_ROW_FIELDS, row))
            for row in fresh_db.execute(
                "SELECT slug, display_name, brain, guild, max_tier, blurb, "
                "active FROM character_def ORDER BY slug"
            ).fetchall()
        ]
        monkeypatch.setattr(cr, "_fetch_character_rows", lambda: rows)
        monkeypatch.setattr(cr, "_SNAPSHOT", None)
        roster = cr.load_characters()
        assert set(roster) == set(_BUILT_IN + _RECRUITED)
        assert all(c.active for c in roster.values())
        assert roster["iris"].guild == "isp"
        assert roster["iris"].max_tier == "L"
        assert roster["vega"].brain == "subscription-claude"
        # leave no cross-test snapshot behind
        monkeypatch.setattr(cr, "_SNAPSHOT", None)


# ─── Group 5: idempotency + prod no-clobber ───────────────────────────────


class TestIdempotentAndNoClobber:
    def _counts(self, conn) -> tuple[int, int, int]:
        return tuple(
            conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("character_def", "agent_character_card", "agent_skill_state")
        )

    def test_second_upgrade_is_noop(self, fresh_db, m0254) -> None:
        before = self._counts(fresh_db)
        m0254.upgrade()
        assert self._counts(fresh_db) == before == (8, 3, 4)

    def test_reupgrade_preserves_live_progression(self, fresh_db, m0254) -> None:
        # The existing-prod-DB case: organic XP earned after the baseline
        # must survive a re-run untouched (ON CONFLICT DO NOTHING).
        fresh_db.execute(
            "UPDATE agent_character_card SET xp=3400, level=13 "
            "WHERE agent_id='iris'"
        )
        fresh_db.execute(
            "UPDATE agent_skill_state SET xp=450 "
            "WHERE agent_id='iris' AND skill_id='uvc'"
        )
        fresh_db.execute(
            "UPDATE character_def SET blurb='operator-edited' WHERE slug='kai'"
        )
        m0254.upgrade()
        assert fresh_db.execute(
            "SELECT level, xp FROM agent_character_card WHERE agent_id='iris'"
        ).fetchone() == (13, 3400)
        assert fresh_db.execute(
            "SELECT xp FROM agent_skill_state "
            "WHERE agent_id='iris' AND skill_id='uvc'"
        ).fetchone() == (450,)
        assert fresh_db.execute(
            "SELECT blurb FROM character_def WHERE slug='kai'"
        ).fetchone() == ("operator-edited",)


# ─── Group 6: downgrade scope ─────────────────────────────────────────────


class TestDowngradeRemovesOnlyRecruitedDefs:
    def test_downgrade_scope(self, fresh_db, m0254) -> None:
        m0254.downgrade()
        cur = fresh_db.execute("SELECT slug FROM character_def ORDER BY slug")
        assert tuple(r[0] for r in cur.fetchall()) == _BUILT_IN
        # cards + skills (progression history) survive the downgrade
        assert fresh_db.execute(
            "SELECT COUNT(*) FROM agent_character_card"
        ).fetchone()[0] == 3
        assert fresh_db.execute(
            "SELECT COUNT(*) FROM agent_skill_state"
        ).fetchone()[0] == 4

    def test_upgrade_downgrade_upgrade_round_trips(self, fresh_db, m0254) -> None:
        m0254.downgrade()
        m0254.upgrade()
        assert fresh_db.execute(
            "SELECT COUNT(*) FROM character_def"
        ).fetchone()[0] == 8
        assert fresh_db.execute(
            "SELECT level, xp FROM agent_character_card WHERE agent_id='iris'"
        ).fetchone() == (12, 3280)


# ─── Group 7: PG dialect branch ───────────────────────────────────────────


class TestPgBranchExecutes:
    @pytest.fixture()
    def captured(self, monkeypatch, m0254) -> list[str]:
        from alembic import op as alembic_op

        statements: list[str] = []

        class _PgBind:
            class _Dialect:
                name = "postgresql"

            dialect = _Dialect()

            def exec_driver_sql(self, sql: str, *args, **kwargs):
                statements.append(sql)

        monkeypatch.setattr(alembic_op, "get_bind", lambda: _PgBind())
        return statements

    def test_upgrade_emits_only_conflict_skipping_inserts(
        self, captured, m0254
    ) -> None:
        m0254.upgrade()
        assert len(captured) == 4 + 3 + 4  # defs + cards + skills
        for sql in captured:
            assert sql.startswith("INSERT INTO ")
            assert "DO NOTHING" in sql
            assert "INSERT OR IGNORE" not in sql
        assert sum("ON CONFLICT (slug)" in s for s in captured) == 4
        assert sum("ON CONFLICT (agent_id)" in s for s in captured) == 3
        assert sum("ON CONFLICT (agent_id, skill_id)" in s for s in captured) == 4

    def test_downgrade_is_single_slug_scoped_delete(
        self, captured, m0254
    ) -> None:
        m0254.downgrade()
        assert len(captured) == 1
        sql = captured[0]
        assert sql.startswith("DELETE FROM character_def")
        for slug in _RECRUITED:
            assert f"'{slug}'" in sql
        assert "agent_character_card" not in sql
        assert "agent_skill_state" not in sql
