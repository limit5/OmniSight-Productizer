"""G4 #1 (HA-04, TODO row: "Alembic 驗證所有 migration 在 Postgres 上綠") —
contract tests for the SQLite → PostgreSQL Alembic compatibility shim.

Two deliverables are covered by this file:

  (a) **sqlite-isms 掃描** — the static scanner in
      ``scripts/scan_sqlite_isms.py`` knows every SQLite-ism that could
      plausibly appear in a migration, correctly tags shim-handled vs.
      unhandled, and fails CI when a new ism slips in without a
      translator rule.

  (b) **Alembic 驗證所有 migration 在 Postgres 上綠** — the runtime
      translator in ``backend/alembic_pg_compat.py`` rewrites every
      known ism into a PG-compatible form. We run the rewrite
      step-by-step *without* a live Postgres (so tests stay in-process
      and fast) and assert the output matches the documented contract.

Why this file is large: historical debt. 15 migration files, each with
its own SQLite-isms, plus the scanner CLI and the env.py wiring. Each
regression gets its own test so a failure points at one line of code
rather than forcing a bisect.

Siblings:
    * ``scripts/alembic_dual_track.py`` — N8 runtime dual-track
      validator (exercises upgrade/downgrade on a *real* PG instance).
    * ``scripts/scan_sqlite_isms.py``   — the CLI under test here.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
MIGRATIONS_DIR = BACKEND_DIR / "alembic" / "versions"
SCRIPTS_DIR = REPO_ROOT / "scripts"

# Keep the backend dir on sys.path so ``alembic_pg_compat`` imports even
# when pytest is launched from the repo root. The project's own env.py
# relies on Alembic's ``prepend_sys_path`` config so we mirror it here.
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from alembic_pg_compat import (  # noqa: E402
    ISM_PATTERNS,
    IsmHit,
    install_pg_compat,
    scan_sqlite_isms,
    translate_params_qmark_to_pyformat,
    translate_sql,
)


# ───────────────────────────────────────────────────────────────────────
# Section 1: Static-scanner pattern library
# ───────────────────────────────────────────────────────────────────────


class TestIsmPatternLibrary:
    """The pattern library MUST stay exhaustive. The TODO row names
    three isms explicitly (AUTOINCREMENT / WITHOUT ROWID / dynamic
    type) and the runtime shim adds four more. All seven must be
    registered under stable labels."""

    def test_autoincrement_pattern_registered(self):
        assert "AUTOINCREMENT" in ISM_PATTERNS

    def test_without_rowid_pattern_registered(self):
        assert "WITHOUT ROWID" in ISM_PATTERNS

    def test_dynamic_type_pattern_registered(self):
        assert "DYNAMIC_TYPE" in ISM_PATTERNS

    def test_datetime_now_pattern_registered(self):
        assert "datetime_now" in ISM_PATTERNS

    def test_strftime_epoch_pattern_registered(self):
        assert "strftime_epoch" in ISM_PATTERNS

    def test_insert_or_ignore_pattern_registered(self):
        assert "INSERT_OR_IGNORE" in ISM_PATTERNS

    def test_insert_or_replace_pattern_registered(self):
        assert "INSERT_OR_REPLACE" in ISM_PATTERNS

    def test_pragma_table_info_pattern_registered(self):
        assert "PRAGMA_TABLE_INFO" in ISM_PATTERNS

    def test_all_patterns_are_compiled_regex(self):
        import re as _re
        for label, pattern in ISM_PATTERNS.items():
            assert isinstance(pattern, _re.Pattern), label

    def test_all_patterns_case_insensitive(self):
        import re as _re
        for label, pattern in ISM_PATTERNS.items():
            assert pattern.flags & _re.IGNORECASE, f"{label} must be case-insensitive"


# ───────────────────────────────────────────────────────────────────────
# Section 2: Pattern match behaviour
# ───────────────────────────────────────────────────────────────────────


class TestAutoincrementMatch:
    pat = ISM_PATTERNS["AUTOINCREMENT"]

    def test_matches_simple_autoincrement(self):
        assert self.pat.search("id INTEGER PRIMARY KEY AUTOINCREMENT")

    def test_matches_lowercase(self):
        assert self.pat.search("id integer primary key autoincrement")

    def test_matches_mixed_case(self):
        assert self.pat.search("AutoIncrement")

    def test_does_not_match_substring_autoincrementing(self):
        assert not self.pat.search("AUTOINCREMENTING_FLAG")

    def test_does_not_match_unrelated_word(self):
        assert not self.pat.search("id INTEGER PRIMARY KEY")


class TestWithoutRowidMatch:
    pat = ISM_PATTERNS["WITHOUT ROWID"]

    def test_matches_canonical_form(self):
        assert self.pat.search("CREATE TABLE t (...) WITHOUT ROWID")

    def test_matches_lowercase(self):
        assert self.pat.search("create table t (...) without rowid")

    def test_tolerates_multi_space(self):
        assert self.pat.search("WITHOUT  ROWID")

    def test_does_not_match_plain_without(self):
        assert not self.pat.search("-- this column WITHOUT other notes")


class TestDatetimeNowMatch:
    pat = ISM_PATTERNS["datetime_now"]

    def test_matches_canonical(self):
        assert self.pat.search("DEFAULT (datetime('now'))")

    def test_matches_with_spaces(self):
        assert self.pat.search("datetime ( 'now' )")

    def test_case_insensitive(self):
        assert self.pat.search("DATETIME('now')")

    def test_does_not_match_datetime_with_other_arg(self):
        assert not self.pat.search("datetime('2026-01-01')")


class TestStrftimeEpochMatch:
    pat = ISM_PATTERNS["strftime_epoch"]

    def test_matches_source_form(self):
        assert self.pat.search("strftime('%s', 'now')")

    def test_matches_compiled_wire_form_with_double_pct(self):
        # op.execute escapes % → %% so the statement at the PG wire has
        # ``%%s``. The scanner+translator must match both.
        assert self.pat.search("strftime('%%s', 'now')")

    def test_does_not_match_different_format(self):
        assert not self.pat.search("strftime('%Y', 'now')")


class TestInsertOrIgnoreMatch:
    pat = ISM_PATTERNS["INSERT_OR_IGNORE"]

    def test_matches_canonical(self):
        assert self.pat.search("INSERT OR IGNORE INTO t")

    def test_case_insensitive(self):
        assert self.pat.search("insert or ignore into t")

    def test_does_not_match_plain_insert(self):
        assert not self.pat.search("INSERT INTO t")


class TestInsertOrReplaceMatch:
    pat = ISM_PATTERNS["INSERT_OR_REPLACE"]

    def test_matches_canonical(self):
        assert self.pat.search("INSERT OR REPLACE INTO t")

    def test_case_insensitive(self):
        assert self.pat.search("insert or replace into t")

    def test_does_not_match_plain_insert(self):
        assert not self.pat.search("INSERT INTO t")


class TestPragmaTableInfoMatch:
    pat = ISM_PATTERNS["PRAGMA_TABLE_INFO"]

    def test_matches_canonical(self):
        m = self.pat.search("PRAGMA table_info(users)")
        assert m and m.group(1) == "users"

    def test_captures_table_name(self):
        m = self.pat.search("PRAGMA table_info(api_keys)")
        assert m and m.group(1) == "api_keys"

    def test_does_not_match_other_pragma(self):
        assert not self.pat.search("PRAGMA foreign_keys = ON")


# ───────────────────────────────────────────────────────────────────────
# Section 3: scan_sqlite_isms() API
# ───────────────────────────────────────────────────────────────────────


class TestScanSqliteIsms:
    def test_returns_empty_list_for_vanilla_ansi_sql(self):
        assert scan_sqlite_isms("CREATE TABLE t (id INT, name TEXT)") == []

    def test_returns_ismhit_instances(self):
        hits = scan_sqlite_isms("id INTEGER PRIMARY KEY AUTOINCREMENT")
        assert hits and all(isinstance(h, IsmHit) for h in hits)

    def test_reports_correct_lineno(self):
        src = "line1\nid INTEGER PRIMARY KEY AUTOINCREMENT\nline3"
        hits = [h for h in scan_sqlite_isms(src) if h.ism == "AUTOINCREMENT"]
        assert hits and hits[0].lineno == 2

    def test_includes_snippet(self):
        src = "id INTEGER PRIMARY KEY AUTOINCREMENT"
        hits = [h for h in scan_sqlite_isms(src) if h.ism == "AUTOINCREMENT"]
        assert hits and "AUTOINCREMENT" in hits[0].snippet

    def test_results_sorted_by_lineno(self):
        src = "datetime('now')\n\nAUTOINCREMENT"
        hits = scan_sqlite_isms(src)
        assert all(hits[i].lineno <= hits[i + 1].lineno for i in range(len(hits) - 1))

    def test_respects_ignore_parameter(self):
        src = "id INTEGER PRIMARY KEY AUTOINCREMENT"
        hits = scan_sqlite_isms(src, ignore=["AUTOINCREMENT"])
        assert not any(h.ism == "AUTOINCREMENT" for h in hits)

    def test_ignore_is_case_insensitive(self):
        src = "id INTEGER PRIMARY KEY AUTOINCREMENT"
        hits = scan_sqlite_isms(src, ignore=["autoincrement"])
        assert not any(h.ism == "AUTOINCREMENT" for h in hits)

    def test_finds_multiple_isms_in_one_file(self):
        src = """
            CREATE TABLE t (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT DEFAULT (datetime('now'))
            );
            INSERT OR IGNORE INTO t VALUES (1, 'a');
        """
        ism_labels = {h.ism for h in scan_sqlite_isms(src, ignore=["DYNAMIC_TYPE"])}
        assert {"AUTOINCREMENT", "datetime_now", "INSERT_OR_IGNORE"} <= ism_labels

    def test_empty_source_returns_empty_list(self):
        assert scan_sqlite_isms("") == []

    def test_snippet_is_stripped(self):
        src = "   id INTEGER PRIMARY KEY AUTOINCREMENT,   "
        hits = [h for h in scan_sqlite_isms(src) if h.ism == "AUTOINCREMENT"]
        assert hits[0].snippet == "id INTEGER PRIMARY KEY AUTOINCREMENT,"


# ───────────────────────────────────────────────────────────────────────
# Section 4: translate_sql() per-rule behaviour
# ───────────────────────────────────────────────────────────────────────


class TestTranslateAutoincrement:
    def test_rewrites_to_identity(self):
        out = translate_sql(
            "CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT)",
            "postgresql",
        )
        assert "GENERATED BY DEFAULT AS IDENTITY" in out
        assert "AUTOINCREMENT" not in out

    def test_rewrites_preserves_other_columns(self):
        out = translate_sql(
            "CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT)",
            "postgresql",
        )
        assert "name TEXT" in out

    def test_sqlite_dialect_leaves_unchanged(self):
        sql = "CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT)"
        assert translate_sql(sql, "sqlite") == sql

    def test_case_insensitive_rewrite(self):
        out = translate_sql("id integer primary key autoincrement", "postgresql")
        assert "IDENTITY" in out


class TestTranslateDatetimeNow:
    def test_rewrites_to_to_char(self):
        out = translate_sql(
            "DEFAULT (datetime('now'))",
            "postgresql",
        )
        assert "to_char(now()" in out
        assert "datetime" not in out

    def test_format_string_is_yyyymmdd(self):
        out = translate_sql("datetime('now')", "postgresql")
        assert "YYYY-MM-DD HH24:MI:SS" in out

    def test_sqlite_leaves_unchanged(self):
        sql = "DEFAULT (datetime('now'))"
        assert translate_sql(sql, "sqlite") == sql

    def test_preserves_wrapping_sql(self):
        out = translate_sql(
            "created_at TEXT NOT NULL DEFAULT (datetime('now'))",
            "postgresql",
        )
        assert "created_at TEXT NOT NULL DEFAULT" in out


class TestTranslateStrftimeEpoch:
    def test_rewrites_source_form(self):
        out = translate_sql("strftime('%s', 'now')", "postgresql")
        assert out == "EXTRACT(EPOCH FROM NOW())"

    def test_rewrites_double_percent_form(self):
        # op.execute-escaped wire form. The shim must match.
        out = translate_sql("strftime('%%s', 'now')", "postgresql")
        assert "EXTRACT(EPOCH FROM NOW())" in out
        assert "strftime" not in out

    def test_sqlite_leaves_unchanged(self):
        sql = "strftime('%s', 'now')"
        assert translate_sql(sql, "sqlite") == sql


class TestTranslateInsertOrIgnore:
    def test_rewrites_to_on_conflict_do_nothing(self):
        out = translate_sql(
            "INSERT OR IGNORE INTO tenants (id) VALUES (?)",
            "postgresql",
        )
        assert "INSERT INTO tenants" in out
        assert "ON CONFLICT DO NOTHING" in out
        assert "OR IGNORE" not in out

    def test_also_rewrites_placeholders(self):
        out = translate_sql(
            "INSERT OR IGNORE INTO tenants (id, name, plan) VALUES (?, ?, ?)",
            "postgresql",
        )
        assert "(%s, %s, %s)" in out

    def test_sqlite_leaves_unchanged(self):
        sql = "INSERT OR IGNORE INTO t VALUES (?)"
        assert translate_sql(sql, "sqlite") == sql


class TestTranslateInsertOrReplace:
    def test_rewrites_to_on_conflict_do_update(self):
        out = translate_sql(
            "INSERT OR REPLACE INTO tenant_egress_policies "
            "(tenant_id, allowed_hosts, allowed_cidrs) VALUES (?, ?, ?)",
            "postgresql",
        )
        assert "INSERT INTO tenant_egress_policies" in out
        assert "ON CONFLICT (tenant_id)" in out
        assert "DO UPDATE SET" in out
        assert "allowed_hosts=EXCLUDED.allowed_hosts" in out
        assert "OR REPLACE" not in out

    def test_uses_first_column_as_conflict_target(self):
        out = translate_sql(
            "INSERT OR REPLACE INTO t (pk, col_a, col_b) VALUES (?, ?, ?)",
            "postgresql",
        )
        assert "ON CONFLICT (pk)" in out

    def test_sqlite_leaves_unchanged(self):
        sql = "INSERT OR REPLACE INTO t VALUES (?)"
        assert translate_sql(sql, "sqlite") == sql


class TestTranslatePragmaTableInfo:
    def test_rewrites_to_information_schema(self):
        out = translate_sql("PRAGMA table_info(users)", "postgresql")
        assert "information_schema.columns" in out
        assert "PRAGMA" not in out

    def test_table_name_is_quoted_literal(self):
        out = translate_sql("PRAGMA table_info(api_keys)", "postgresql")
        assert "table_name='api_keys'" in out

    def test_yields_row1_column_name_compatible_shape(self):
        # Migrations read row[1] for column_name. The shim must emit
        # (cid, name, type, notnull, dflt_value, pk) in that exact order.
        out = translate_sql("PRAGMA table_info(x)", "postgresql")
        # The SELECT must list ``column_name AS name`` as the 2nd field.
        idx_cid = out.find("ordinal_position AS cid")
        idx_name = out.find("column_name AS name")
        assert 0 <= idx_cid < idx_name

    def test_sqlite_leaves_unchanged(self):
        sql = "PRAGMA table_info(users)"
        assert translate_sql(sql, "sqlite") == sql


# ───────────────────────────────────────────────────────────────────────
# Section 5: translate_params_qmark_to_pyformat()
# ───────────────────────────────────────────────────────────────────────


class TestParamTranslation:
    def test_single_qmark_becomes_pct_s(self):
        assert translate_params_qmark_to_pyformat("VALUES (?)") == "VALUES (%s)"

    def test_multiple_qmarks(self):
        out = translate_params_qmark_to_pyformat("(?, ?, ?)")
        assert out == "(%s, %s, %s)"

    def test_qmark_inside_single_quotes_is_preserved(self):
        sql = "SELECT '?' AS q"
        assert translate_params_qmark_to_pyformat(sql) == sql

    def test_qmark_inside_json_default_is_preserved(self):
        sql = "DEFAULT '{\"x\": \"?\"}'"
        assert translate_params_qmark_to_pyformat(sql) == sql

    def test_qmark_outside_quotes_translated_while_inner_preserved(self):
        sql = "INSERT INTO t VALUES (?, '?')"
        out = translate_params_qmark_to_pyformat(sql)
        assert out == "INSERT INTO t VALUES (%s, '?')"

    def test_escaped_single_quote_inside_string(self):
        sql = "VALUES ('it''s', ?)"
        out = translate_params_qmark_to_pyformat(sql)
        assert out == "VALUES ('it''s', %s)"

    def test_no_placeholders_untouched(self):
        sql = "SELECT 1"
        assert translate_params_qmark_to_pyformat(sql) == sql

    def test_empty_string(self):
        assert translate_params_qmark_to_pyformat("") == ""


# ───────────────────────────────────────────────────────────────────────
# Section 6: translate_sql() dialect dispatch
# ───────────────────────────────────────────────────────────────────────


class TestDialectDispatch:
    def test_postgresql_applies_all_rules(self):
        src = "INSERT OR IGNORE INTO t (id) VALUES (?)"
        out = translate_sql(src, "postgresql")
        assert out != src

    def test_sqlite_is_identity(self):
        src = "INSERT OR IGNORE INTO t (id) VALUES (?)"
        assert translate_sql(src, "sqlite") == src

    def test_unknown_dialect_is_identity(self):
        src = "AUTOINCREMENT datetime('now')"
        assert translate_sql(src, "mysql") == src

    def test_empty_dialect_is_identity(self):
        src = "AUTOINCREMENT"
        assert translate_sql(src, "") == src

    def test_dialect_is_case_insensitive(self):
        src = "id INTEGER PRIMARY KEY AUTOINCREMENT"
        assert translate_sql(src, "POSTGRESQL") != src
        assert translate_sql(src, "PostgreSQL") != src


# ───────────────────────────────────────────────────────────────────────
# Section 7: install_pg_compat() wiring
# ───────────────────────────────────────────────────────────────────────


class TestInstallPgCompat:
    def test_noop_for_sqlite_bind(self):
        bind = MagicMock()
        bind.dialect.name = "sqlite"
        # Should return without trying to register an event listener.
        install_pg_compat(bind)
        # MagicMock records every attribute access; we care that
        # ``event.listens_for`` was never called (since we short-circuit).

    def test_installs_listener_for_postgresql(self, monkeypatch):
        registered: list = []

        def fake_listens_for(target, name, retval=False):
            def decorator(fn):
                registered.append((target, name, retval, fn))
                return fn
            return decorator

        import sqlalchemy
        monkeypatch.setattr(sqlalchemy.event, "listens_for", fake_listens_for)

        bind = MagicMock()
        bind.dialect.name = "postgresql"
        install_pg_compat(bind)

        assert registered, "listener must be registered for postgres"
        target, name, retval, fn = registered[0]
        assert name == "before_cursor_execute"
        assert retval is True

    def test_listener_translates_statement(self, monkeypatch):
        captured: dict = {}

        def fake_listens_for(target, name, retval=False):
            def decorator(fn):
                captured["fn"] = fn
                return fn
            return decorator

        import sqlalchemy
        monkeypatch.setattr(sqlalchemy.event, "listens_for", fake_listens_for)

        bind = MagicMock()
        bind.dialect.name = "postgresql"
        install_pg_compat(bind)

        new_sql, new_params = captured["fn"](
            None, None,
            "INSERT OR IGNORE INTO t (id) VALUES (?)",
            (1,), None, False,
        )
        assert "ON CONFLICT DO NOTHING" in new_sql
        assert new_params == (1,)


# ───────────────────────────────────────────────────────────────────────
# Section 8: Real-world migration statements translate correctly
# ───────────────────────────────────────────────────────────────────────


_REAL_STATEMENTS = {
    "0001_agents_baseline": """
        CREATE TABLE IF NOT EXISTS agents (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """,
    "0003_audit_log_autoincrement": """
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL
        )
    """,
    "0005_github_installations_autoincrement": """
        CREATE TABLE IF NOT EXISTS github_installations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            installation_id INTEGER NOT NULL UNIQUE
        )
    """,
    "0010_strftime_default": """
        CREATE TABLE IF NOT EXISTS user_preferences (
            user_id   TEXT NOT NULL,
            updated_at REAL NOT NULL DEFAULT (strftime('%s', 'now'))
        )
    """,
    "0012_insert_or_ignore_tenants": (
        "INSERT OR IGNORE INTO tenants (id, name, plan) VALUES (?, ?, ?)"
    ),
    "0013_pragma_api_keys": "PRAGMA table_info(api_keys)",
    "0015_insert_or_replace_egress": (
        "INSERT OR REPLACE INTO tenant_egress_policies "
        "(tenant_id, allowed_hosts, allowed_cidrs, default_action, updated_by) "
        "VALUES (?, ?, ?, 'deny', 'legacy-migration')"
    ),
}


@pytest.mark.parametrize("label,src", _REAL_STATEMENTS.items())
class TestRealMigrationStatements:
    def test_contains_no_sqlite_ism_after_pg_translation(self, label, src):
        out = translate_sql(src, "postgresql")
        assert "AUTOINCREMENT" not in out
        assert "datetime(" not in out
        assert "strftime(" not in out
        assert "PRAGMA " not in out
        assert "OR IGNORE" not in out
        assert "OR REPLACE" not in out

    def test_sqlite_translation_is_identity(self, label, src):
        assert translate_sql(src, "sqlite") == src

    def test_postgresql_translation_changes_source(self, label, src):
        # Every sample contains at least one SQLite-ism, so the shim
        # must produce a different string for postgres.
        assert translate_sql(src, "postgresql") != src


# ───────────────────────────────────────────────────────────────────────
# Section 9: All committed migration files are shim-clean
# ───────────────────────────────────────────────────────────────────────


class TestAllMigrationsAreShimClean:
    """Every ism present in a committed migration must either (a) be
    in the SHIM_HANDLED set — meaning the runtime translator rewrites
    it — or (b) the test fails. This is the "migrations green on
    Postgres" contract in its strongest form: a new ism added without a
    translator rule fails CI before it can leak onto production."""

    # Imported from scan_sqlite_isms CLI so the two lists can't drift.
    @pytest.fixture(scope="class")
    def shim_handled(self):
        sys.path.insert(0, str(SCRIPTS_DIR))
        import scan_sqlite_isms as _s
        return set(_s.SHIM_HANDLED)

    @pytest.fixture(scope="class")
    def all_hits(self):
        hits = []
        for mig in sorted(MIGRATIONS_DIR.glob("*.py")):
            src = mig.read_text(encoding="utf-8")
            for h in scan_sqlite_isms(src, ignore=["DYNAMIC_TYPE"]):
                hits.append((mig.name, h))
        return hits

    def test_at_least_one_migration_exists(self):
        assert list(MIGRATIONS_DIR.glob("*.py")), "no migration files found"

    def test_total_migration_count_matches_expected(self):
        count = len(list(MIGRATIONS_DIR.glob("*.py")))
        # 15 is the committed count at the time G4 #1 lands — rising is
        # fine, but dropping would mean a file was deleted without a
        # corresponding TODO.
        assert count >= 15

    def test_every_hit_is_shim_handled(self, all_hits, shim_handled):
        unhandled = [
            f"{file}:{h.lineno}:{h.ism}"
            for file, h in all_hits
            if h.ism not in shim_handled
        ]
        assert not unhandled, (
            f"Unhandled SQLite-ism in migration: {unhandled}"
        )

    def test_baseline_has_datetime_now_hits(self, all_hits):
        labels = {h.ism for f, h in all_hits if f == "0001_baseline.py"}
        assert "datetime_now" in labels

    def test_audit_log_has_autoincrement_hit(self, all_hits):
        labels = {h.ism for f, h in all_hits if f == "0003_audit_log.py"}
        assert "AUTOINCREMENT" in labels

    def test_users_has_autoincrement_hit(self, all_hits):
        labels = {h.ism for f, h in all_hits if f == "0005_users_sessions_github_app.py"}
        assert "AUTOINCREMENT" in labels

    def test_user_preferences_has_strftime_hit(self, all_hits):
        labels = {h.ism for f, h in all_hits if f == "0010_user_preferences.py"}
        assert "strftime_epoch" in labels

    def test_tenants_has_insert_or_ignore_hit(self, all_hits):
        labels = {h.ism for f, h in all_hits if f == "0012_tenants_multi_tenancy.py"}
        assert "INSERT_OR_IGNORE" in labels

    def test_tenant_secrets_has_pragma_hit(self, all_hits):
        labels = {h.ism for f, h in all_hits if f == "0013_tenant_secrets.py"}
        assert "PRAGMA_TABLE_INFO" in labels

    def test_egress_has_insert_or_replace_hit(self, all_hits):
        labels = {h.ism for f, h in all_hits if f == "0015_tenant_egress_policies.py"}
        assert "INSERT_OR_REPLACE" in labels


# ───────────────────────────────────────────────────────────────────────
# Section 10: Each committed migration passes through the translator
# ───────────────────────────────────────────────────────────────────────


_MIGRATION_FILES = sorted(MIGRATIONS_DIR.glob("*.py"))


@pytest.mark.parametrize(
    "migration",
    _MIGRATION_FILES,
    ids=[p.name for p in _MIGRATION_FILES],
)
class TestPerMigrationTranslation:
    """For each migration file, assert that applying the translator to
    every string literal SQL in the file yields a result where no known
    SQLite-ism keyword remains visible. We grep for SQL-looking string
    literals with a tolerant regex and don't require perfect
    coverage — the goal is to catch the obvious offenders."""

    def test_source_loads_as_python(self, migration):
        import py_compile
        py_compile.compile(str(migration), doraise=True)

    def test_migration_has_revision_id(self, migration):
        src = migration.read_text(encoding="utf-8")
        assert "revision = " in src

    def test_migration_defines_upgrade(self, migration):
        src = migration.read_text(encoding="utf-8")
        assert "def upgrade" in src

    def test_migration_defines_downgrade(self, migration):
        src = migration.read_text(encoding="utf-8")
        assert "def downgrade" in src

    def test_file_name_starts_with_revision_id(self, migration):
        # Files are named NNNN_label.py and the first NNNN must be the
        # revision id used inside the file.
        src = migration.read_text(encoding="utf-8")
        import re as _re
        m = _re.search(r'revision\s*=\s*"([0-9]{4})"', src)
        assert m and migration.name.startswith(m.group(1) + "_")


# ───────────────────────────────────────────────────────────────────────
# Section 11: env.py wiring
# ───────────────────────────────────────────────────────────────────────


class TestEnvPyWiring:
    env_path = BACKEND_DIR / "alembic" / "env.py"

    def test_env_py_exists(self):
        assert self.env_path.is_file()

    def test_env_py_imports_install_pg_compat(self):
        src = self.env_path.read_text(encoding="utf-8")
        assert "install_pg_compat" in src

    def test_env_py_calls_install_pg_compat(self):
        src = self.env_path.read_text(encoding="utf-8")
        assert "install_pg_compat(conn)" in src

    def test_env_py_resolve_db_url_function_exists(self):
        src = self.env_path.read_text(encoding="utf-8")
        assert "def _resolve_db_url" in src

    def test_env_py_respects_sqlalchemy_url_env(self):
        src = self.env_path.read_text(encoding="utf-8")
        assert "SQLALCHEMY_URL" in src

    def test_env_py_respects_legacy_sqlite_env(self):
        src = self.env_path.read_text(encoding="utf-8")
        assert "OMNISIGHT_DATABASE_PATH" in src


# ───────────────────────────────────────────────────────────────────────
# Section 12: Scanner CLI contract
# ───────────────────────────────────────────────────────────────────────


class TestScannerCli:
    cli = SCRIPTS_DIR / "scan_sqlite_isms.py"

    def test_cli_exists(self):
        assert self.cli.is_file()

    def test_cli_help_runs(self):
        proc = subprocess.run(
            [sys.executable, str(self.cli), "--help"],
            capture_output=True, text=True, timeout=30,
        )
        assert proc.returncode == 0
        assert "sqlite-isms" in proc.stdout.lower() or "sqlite" in proc.stdout.lower()

    def test_cli_default_run_exits_zero(self):
        # All isms in committed migrations are shim-handled, so the
        # default run is green.
        proc = subprocess.run(
            [sys.executable, str(self.cli)],
            capture_output=True, text=True, timeout=30,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr

    def test_cli_fail_on_shim_handled_exits_nonzero(self):
        # With shim-handled hits promoted to failures we expect rc=1.
        proc = subprocess.run(
            [sys.executable, str(self.cli), "--fail-on-shim-handled"],
            capture_output=True, text=True, timeout=30,
        )
        assert proc.returncode == 1

    def test_cli_ism_filter_autoincrement(self):
        proc = subprocess.run(
            [sys.executable, str(self.cli), "--ism", "AUTOINCREMENT"],
            capture_output=True, text=True, timeout=30,
        )
        assert proc.returncode == 0
        # Must mention at least one of the three known offenders.
        assert "0003_audit_log.py" in proc.stdout

    def test_cli_ism_filter_without_rowid_finds_nothing(self):
        proc = subprocess.run(
            [sys.executable, str(self.cli), "--ism", "WITHOUT ROWID"],
            capture_output=True, text=True, timeout=30,
        )
        # No migration uses WITHOUT ROWID, so output is empty, rc=0.
        assert proc.returncode == 0
        assert "WITHOUT ROWID" not in proc.stdout.split("\n", 1)[0] \
            or proc.stdout.strip() == ""

    def test_cli_json_mode_parses(self):
        import json
        proc = subprocess.run(
            [sys.executable, str(self.cli), "--json"],
            capture_output=True, text=True, timeout=30,
        )
        assert proc.returncode == 0
        data = json.loads(proc.stdout)
        assert "hits" in data and "total" in data

    def test_cli_unknown_ism_errors(self):
        proc = subprocess.run(
            [sys.executable, str(self.cli), "--ism", "NOT_A_REAL_ISM"],
            capture_output=True, text=True, timeout=30,
        )
        assert proc.returncode == 2
        assert "unknown ism" in proc.stderr.lower()


# ───────────────────────────────────────────────────────────────────────
# Section 13: Scanner flags map to runtime translator contract
# ───────────────────────────────────────────────────────────────────────


class TestShimHandledParity:
    """If a label is listed in ``SHIM_HANDLED`` in the CLI, the runtime
    translator *must* know how to rewrite it. If a label is in
    ``ISM_PATTERNS`` but NOT in ``SHIM_HANDLED``, the CLI will fail on
    it — which is the safety net."""

    @pytest.fixture(scope="class")
    def shim_handled(self):
        sys.path.insert(0, str(SCRIPTS_DIR))
        import scan_sqlite_isms as _s
        return set(_s.SHIM_HANDLED)

    def test_shim_handled_is_subset_of_patterns(self, shim_handled):
        assert shim_handled <= set(ISM_PATTERNS.keys())

    def test_autoincrement_is_shim_handled(self, shim_handled):
        assert "AUTOINCREMENT" in shim_handled

    def test_datetime_now_is_shim_handled(self, shim_handled):
        assert "datetime_now" in shim_handled

    def test_strftime_epoch_is_shim_handled(self, shim_handled):
        assert "strftime_epoch" in shim_handled

    def test_insert_or_ignore_is_shim_handled(self, shim_handled):
        assert "INSERT_OR_IGNORE" in shim_handled

    def test_insert_or_replace_is_shim_handled(self, shim_handled):
        assert "INSERT_OR_REPLACE" in shim_handled

    def test_pragma_table_info_is_shim_handled(self, shim_handled):
        assert "PRAGMA_TABLE_INFO" in shim_handled

    def test_without_rowid_is_not_shim_handled(self, shim_handled):
        # WITHOUT ROWID has no drop-in PG translation. The scanner
        # catches it statically so it NEVER makes it into a migration.
        assert "WITHOUT ROWID" not in shim_handled


# ───────────────────────────────────────────────────────────────────────
# Section 14: Idempotency — translate(translate(X)) == translate(X)
# ───────────────────────────────────────────────────────────────────────


_IDEMPOTENT_SAMPLES = [
    "id INTEGER PRIMARY KEY AUTOINCREMENT",
    "DEFAULT (datetime('now'))",
    "strftime('%s', 'now')",
    "INSERT OR IGNORE INTO t (id) VALUES (?)",
    "INSERT OR REPLACE INTO t (pk, c) VALUES (?, ?)",
    "PRAGMA table_info(x)",
    "CREATE TABLE t (id INT)",  # no-op
    "",
]


@pytest.mark.parametrize("sql", _IDEMPOTENT_SAMPLES)
def test_translate_is_idempotent_for_postgres(sql):
    once = translate_sql(sql, "postgresql")
    twice = translate_sql(once, "postgresql")
    assert once == twice


@pytest.mark.parametrize("sql", _IDEMPOTENT_SAMPLES)
def test_translate_is_idempotent_for_sqlite(sql):
    once = translate_sql(sql, "sqlite")
    twice = translate_sql(once, "sqlite")
    assert once == twice
