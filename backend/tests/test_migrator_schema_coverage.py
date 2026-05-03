"""Phase-3 F3 (2026-04-20) — migrator vs live alembic schema drift gate.

What this test guards against
─────────────────────────────
The Phase-3 Step-1 audit discovered that
``scripts/migrate_sqlite_to_pg.py::TABLES_IN_ORDER`` was seven tables
behind the live alembic schema. Every new alembic migration that
adds a table but forgets to update the migrator silently loses that
table's data on the next cutover. Root cause: nothing cross-
referenced the migrator list against the actual schema.

This test forces the two sides to stay in sync. We spin a fresh
SQLite file in a tmpdir, let the application's own schema
bootstrap (``backend.db.init`` — which runs the same CREATE TABLE
+ migration path production uses), then introspect
``sqlite_master`` for the resulting table list and diff it against
``TABLES_IN_ORDER`` modulo a small explicit exclusion list:

  * ``alembic_version`` — Alembic's own bookkeeping; recreated
    automatically on the PG side by ``alembic upgrade head``
    before any app-layer migration runs. Migrator must NOT copy
    it (would cause ``alembic`` to think it's at a stale rev).

  * ``sqlite_*`` / ``episodic_memory_fts*`` — FTS5 virtual tables
    and their shadow storage; SQLite-only. The PG-side alembic
    migration creates them via its own ``CREATE VIRTUAL TABLE``
    path (or skips them with an engine guard).

Any new table NOT on the exclusion list must appear in
``TABLES_IN_ORDER``. Any new table whose PK is INTEGER must ALSO
appear in ``TABLES_WITH_IDENTITY_ID``.

How to fix a failing run
────────────────────────
  1. Identify the missing table from the assertion message.
  2. Decide FK ordering — what parent table(s) does the new one
     reference? Place the entry in ``TABLES_IN_ORDER`` AFTER its
     FK parents.
  3. If its PK is INTEGER, also append it to
     ``TABLES_WITH_IDENTITY_ID``.
  4. Update the alembic migration's comment in the historical
     note so the reviewer has the "why this table existed".

Do NOT try to silence this test by extending the exclusion list
unless the new table is genuinely SQLite-only (e.g. another FTS5
shadow); that is a deliberate decision with operator-facing
consequences and must be justified in a PR comment.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# Load the migrator script as a module via importlib because scripts/
# isn't on sys.path and isn't a package.
import importlib.util

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MIGRATOR_PATH = _REPO_ROOT / "scripts" / "migrate_sqlite_to_pg.py"

_spec = importlib.util.spec_from_file_location("migrate_sqlite_to_pg", _MIGRATOR_PATH)
assert _spec and _spec.loader
mig = importlib.util.module_from_spec(_spec)
import sys
sys.modules["migrate_sqlite_to_pg"] = mig
_spec.loader.exec_module(mig)


# Tables the migrator MUST NOT copy. Each entry has a rationale —
# new exclusions require the same justification in a PR comment.
_EXCLUDED_FROM_MIGRATOR: frozenset[str] = frozenset({
    # Alembic's own version tracking. ``alembic upgrade head`` on
    # the PG side creates + populates this table before the app
    # sees it. Copying the SQLite version would leave a stale rev.
    "alembic_version",
    # FTS5 virtual table (episodic memory full-text search) + its
    # four shadow storage tables. SQLite-specific; the PG path
    # either re-creates them via CREATE VIRTUAL TABLE (if SQLite
    # FTS5 extension is linked) or skips them. Either way, raw row
    # copy is wrong.
    "episodic_memory_fts",
    "episodic_memory_fts_config",
    "episodic_memory_fts_data",
    "episodic_memory_fts_docsize",
    "episodic_memory_fts_idx",
})


@pytest.fixture
async def _live_schema_tables(tmp_path):
    """Spin a fresh SQLite via ``backend.db.init`` so we see every
    CREATE TABLE the real boot path executes — including the ones
    added by the most recent alembic migrations.

    Function-scoped rather than module-scoped so pytest-asyncio's
    event-loop scoping rules stay straight; the cost of re-init-ing
    a fresh sqlite in /tmp is low (~0.3 s on this hardware) and the
    test file only has three cases total."""
    db_path = tmp_path / "probe.db"
    os.environ["OMNISIGHT_DATABASE_PATH"] = str(db_path)
    from backend import config as _cfg
    _cfg.settings.database_path = str(db_path)
    from backend import db
    db._DB_PATH = db._resolve_db_path()
    await db.init()
    try:
        conn = db._conn()
        async with conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        ) as cur:
            rows = await cur.fetchall()
        names = {r[0] for r in rows}
        yield names
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_migrator_covers_every_live_table_except_exclusions(
    _live_schema_tables,
):
    """Any table present in the live schema (fresh ``backend.db.init``)
    and NOT on the explicit exclusion list MUST be in the migrator's
    TABLES_IN_ORDER. This is the load-bearing assertion that prevents
    silent schema drift."""
    live_tables = _live_schema_tables
    covered = set(mig.TABLES_IN_ORDER)
    missing = live_tables - covered - _EXCLUDED_FROM_MIGRATOR
    if missing:
        pytest.fail(
            "migrator/alembic schema drift detected — the following "
            "tables exist in the live DB but are NOT in "
            "scripts/migrate_sqlite_to_pg.py::TABLES_IN_ORDER: "
            f"{sorted(missing)}\n\n"
            "Fix: append each missing table to TABLES_IN_ORDER (in "
            "FK-safe order — parent tables first) AND, if the PK is "
            "INTEGER, also append to TABLES_WITH_IDENTITY_ID. See "
            "the docstring in this test file for the playbook. "
            "DO NOT silence this test by extending _EXCLUDED_FROM_MIGRATOR "
            "unless the table is genuinely SQLite-only (e.g. another "
            "FTS5 shadow) — that decision has operator-facing "
            "data-loss consequences at cutover time."
        )


@pytest.mark.asyncio
async def test_migrator_does_not_list_non_existent_tables(
    _live_schema_tables,
):
    """Reverse check: anything in ``TABLES_IN_ORDER`` that does NOT
    appear in the live schema is either (a) a typo or (b) a table
    that was dropped by a later migration and the migrator list
    forgot to follow. Fail loudly in either case."""
    live_tables = _live_schema_tables
    phantom = set(mig.TABLES_IN_ORDER) - live_tables
    assert not phantom, (
        f"TABLES_IN_ORDER contains names that don't exist in the live "
        f"schema: {sorted(phantom)}. Either fix the typo or remove the "
        "entry (if the table was dropped by a newer migration)."
    )


def test_identity_subset_only_contains_integer_pk_tables():
    """Introspect the alembic/CREATE-TABLE comment hint rather than
    hitting the DB — this is a pure contract assertion. Locking:
    bootstrap_state (TEXT PK ``step``), user_mfa (TEXT PK
    app-generated), and adaptive_budget_state (TEXT PK singleton
    ``'global'``) must NEVER be in TABLES_WITH_IDENTITY_ID, because
    the sequence-reset logic assumes INTEGER PK and would crash on
    a TEXT id."""
    non_integer_pks = {
        "adaptive_budget_state",
        "bootstrap_state",
        "decryption_audits",
        "kek_rotations",
        "kms_keys",
        "spend_thresholds",
        "tenant_deks",
        "user_mfa",
    }
    for t in non_integer_pks:
        assert t in mig.TABLES_IN_ORDER, (
            f"sanity: {t} should be covered by migrator"
        )
        assert t not in mig.TABLES_WITH_IDENTITY_ID, (
            f"{t} has a TEXT primary key — listing it as an IDENTITY "
            "table will blow up at sequence-reset time on PG. "
            "Remove from TABLES_WITH_IDENTITY_ID."
        )


# ──────────────────────────────────────────────────────────────────────
# Phase-3 P1 (2026-04-20) — column-level parity gate.
#
# The table-level drift guard above caught "table exists in live
# SQLite but migrator doesn't list it". That missed a deeper class:
# "column exists in live SQLite (added by ``backend/db.py::_migrate()``
# ALTER TABLE) but is NOT in any alembic migration". Phase 3 discovered
# PG (fresh ``alembic upgrade head``) was missing 7 columns across
# sessions / users / workflow_runs / workflow_steps / episodic_memory
# — silent until the first cutover attempt crashed the migrator with
# ``UndefinedColumnError``.
#
# This test locks the third leg down by diffing column sets per-table
# between live SQLite and fresh alembic-upgraded PG. Requires
# ``OMNI_TEST_PG_URL`` (same env knob as test_alembic_pg_live_upgrade);
# skipped in the main test run.
# ──────────────────────────────────────────────────────────────────────

_PG_URL = os.environ.get("OMNI_TEST_PG_URL", "").strip()

# Columns that are legitimately SQLite-only — must never appear in PG.
# Empty today; present as a hook for future FTS5 shadow columns etc.
_PG_EXCLUDED_COLUMNS: frozenset[tuple[str, str]] = frozenset()


@pytest.fixture
async def _sqlite_schema_columns(tmp_path):
    """Per-table ``{column_name → type_affinity}`` dict from the live
    SQLite schema (fresh ``backend.db.init``)."""
    db_path = tmp_path / "probe.db"
    os.environ["OMNISIGHT_DATABASE_PATH"] = str(db_path)
    from backend import config as _cfg
    _cfg.settings.database_path = str(db_path)
    from backend import db
    db._DB_PATH = db._resolve_db_path()
    await db.init()
    try:
        conn = db._conn()
        async with conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "AND name NOT LIKE 'episodic_memory_fts%' "
            "AND name != 'alembic_version' "
            "ORDER BY name"
        ) as cur:
            tables = [r[0] for r in await cur.fetchall()]
        out: dict[str, set[str]] = {}
        for t in tables:
            async with conn.execute(f"PRAGMA table_info({t})") as cur:
                out[t] = {r[1] for r in await cur.fetchall()}
        yield out
    finally:
        await db.close()


@pytest.mark.skipif(
    not _PG_URL,
    reason="OMNI_TEST_PG_URL not set — skip cross-dialect column-parity test",
)
@pytest.mark.asyncio
async def test_alembic_pg_matches_sqlite_column_by_column(
    _sqlite_schema_columns, tmp_path,
):
    """After ``alembic upgrade head`` against a clean PG, every table
    present in live SQLite must have the same column set (modulo the
    explicit exclusion list). Without this guard a ``db.py::_migrate()``
    ALTER-added column that forgets its alembic mirror leaves PG silently
    short — and the cutover migrator crashes at runtime with
    ``UndefinedColumnError``."""
    import subprocess
    import psycopg2  # type: ignore

    # 1. Reset PG public schema + run upgrade head.
    libpq = _PG_URL
    if libpq.startswith("postgresql+psycopg2://"):
        libpq = libpq.replace("postgresql+psycopg2://", "postgresql://", 1)
    if libpq.startswith("postgresql+asyncpg://"):
        libpq = libpq.replace("postgresql+asyncpg://", "postgresql://", 1)

    conn = psycopg2.connect(libpq)
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("DROP SCHEMA IF EXISTS public CASCADE")
        cur.execute("CREATE SCHEMA public")
    finally:
        conn.close()

    backend_dir = _REPO_ROOT / "backend"
    env = os.environ.copy()
    env["SQLALCHEMY_URL"] = _PG_URL
    env["OMNISIGHT_SKIP_FS_MIGRATIONS"] = "1"
    proc = subprocess.run(
        ["alembic", "upgrade", "head"],
        cwd=backend_dir, env=env, capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, (
        f"alembic upgrade head failed: stderr={proc.stderr[-500:]}"
    )

    # 2. Introspect PG column sets.
    conn = psycopg2.connect(libpq)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name != 'alembic_version'"
        )
        pg_cols: dict[str, set[str]] = {}
        for table, col in cur.fetchall():
            pg_cols.setdefault(table, set()).add(col)
    finally:
        conn.close()

    # 3. Diff, ignoring pure-SQLite exclusions.
    mismatches: list[str] = []
    for table, sqlite_cols in _sqlite_schema_columns.items():
        if table not in pg_cols:
            # Table itself missing — the table-level test above will
            # already flag this; keep this message focused on columns.
            continue
        filtered = {c for c in sqlite_cols if (table, c) not in _PG_EXCLUDED_COLUMNS}
        missing_from_pg = filtered - pg_cols[table]
        if missing_from_pg:
            mismatches.append(
                f"  {table}: alembic-PG is missing {sorted(missing_from_pg)}"
            )
    if mismatches:
        pytest.fail(
            "alembic/SQLite column drift detected — the following "
            "columns exist in live SQLite (backend/db.py) but alembic "
            "upgrade head did NOT create them on PG:\n\n"
            + "\n".join(mismatches)
            + "\n\nFix: write a new alembic migration (see "
            "alembic/versions/0016_pg_schema_sync.py for the Phase-3 "
            "reference) that adds the missing columns. DO NOT silence "
            "this test by extending _PG_EXCLUDED_COLUMNS unless the "
            "column is genuinely SQLite-only (documented rationale in "
            "a PR comment required)."
        )
