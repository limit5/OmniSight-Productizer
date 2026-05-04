"""Y1 row 7 (#277) — project_id column on existing business tables.

Mirrors the test layout of the sister Y1 row tests
(``test_y1_backfill_memberships_default_projects.py`` etc): one bucket
of pure-Python file-shape tests (revision chain, fingerprint grep,
target-table list), one bucket of PG tests that exercise the
alembic-applied schema (column existence, FK shape, backfill
projection, idempotency, cross-tenant tuple invariant), and one
drift-guard bucket that checks the in-process SQLite ``_migrate()``
path stays in sync with the alembic migration's column / index set.

The PG tests are skipped unless ``OMNI_TEST_PG_URL`` is set (same
gate as every other Y1 row test).
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest


_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent
    / "alembic" / "versions"
    / "0038_y1_project_id_on_business_tables.py"
)


_TARGET_TABLES = (
    "workflow_runs",
    "debug_findings",
    "decision_rules",
    "event_log",
    "artifacts",
    "user_preferences",
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Migration file shape (revision chain + fingerprint scan)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_migration_0038_file_exists():
    assert _MIGRATION_PATH.exists(), str(_MIGRATION_PATH)


def test_migration_0038_revision_chain():
    """Pin the (revision, down_revision) pair so an accidental
    re-numbering in a sister branch can't silently break the chain."""
    spec = importlib.util.spec_from_file_location("m0038", str(_MIGRATION_PATH))
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert m.revision == "0038"
    assert m.down_revision == "0037"


def test_migration_0038_lists_all_target_tables():
    """The ``_TABLES_NEEDING_PROJECT_ID`` constant must enumerate the
    six business tables from the TODO row (modulo the documented
    decision_rules-vs-decisions and skipped-spec_*/audit_log notes)."""
    spec = importlib.util.spec_from_file_location("m0038", str(_MIGRATION_PATH))
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert set(m._TABLES_NEEDING_PROJECT_ID) == set(_TARGET_TABLES)


def test_migration_0038_no_compat_fingerprints_in_sql():
    """Pre-commit fingerprint grep, but as a permanent contract test.

    The migration's SQL strings — both the inline ALTER TABLE / UPDATE
    statements built per-iteration AND the ``_PROJECT_ID_FROM_TENANT_ID``
    constant — must NOT contain the four known SQLite-only fingerprints
    that fail at runtime on PG.
    """
    src = _MIGRATION_PATH.read_text()
    forbidden = {
        "_conn(": r"_conn\(",
        "await conn.commit": r"await\s+conn\.commit\b",
        "datetime('now')": r"datetime\s*\(\s*'now'\s*\)",
        "qmark placeholder in VALUES": r"VALUES\s*\([^)]*\?[^)]*\)",
    }
    # Filter out docstrings: regex on the module body excluding
    # triple-quoted blocks.  Keep it cheap — drop everything between
    # the first and last triple-quote pair so the four-fingerprint
    # search runs against actual code lines only.
    no_docstrings = re.sub(r'"""[\s\S]*?"""', "", src)
    hits = {
        label: re.findall(pat, no_docstrings, re.IGNORECASE)
        for label, pat in forbidden.items()
    }
    bad = {k: v for k, v in hits.items() if v}
    assert not bad, (
        f"compat-fingerprint hit inside migration code: {bad}"
    )


def test_migration_0038_pg_translation_round_trip():
    """The DDL + DML statements built by ``upgrade()`` must survive
    the alembic_pg_compat rewrite — specifically the ``PRAGMA
    table_info`` introspection used for idempotency must rewrite to
    an information_schema query, and the ``substr / CASE`` projection
    in the backfill UPDATE must pass through unchanged.
    """
    import sys as _sys
    backend_dir = Path(__file__).resolve().parent.parent
    if str(backend_dir) not in _sys.path:
        _sys.path.insert(0, str(backend_dir))
    from alembic_pg_compat import translate_sql

    # Build a representative ALTER + UPDATE pair the way upgrade() does
    # for one table. We don't need to execute it here — we just need to
    # confirm the shim doesn't mangle the output.
    table = "workflow_runs"
    pragma = f"PRAGMA table_info({table})"
    alter = (
        f"ALTER TABLE {table} ADD COLUMN project_id TEXT "
        f"REFERENCES projects(id) ON DELETE SET NULL"
    )
    update = (
        f"UPDATE {table} SET project_id = "
        f"'p-' || CASE "
        f"WHEN substr(tenant_id, 1, 2) = 't-' THEN substr(tenant_id, 3) "
        f"ELSE tenant_id END || '-default' "
        f"WHERE project_id IS NULL AND tenant_id IS NOT NULL"
    )
    pragma_pg = translate_sql(pragma, "postgresql")
    alter_pg = translate_sql(alter, "postgresql")
    update_pg = translate_sql(update, "postgresql")

    # PRAGMA must be rewritten — PG can't speak it.
    assert "PRAGMA" not in pragma_pg.upper()
    assert "information_schema.columns" in pragma_pg.lower()
    # The shim's PRAGMA rewrite forces lowercase table name; that's
    # fine because all our business-table names are already lowercase.
    assert f"table_name='{table}'" in pragma_pg.lower()
    # ALTER TABLE doesn't need rewrite, but no SQLite-isms should
    # have crept in.
    assert "REFERENCES projects(id)" in alter_pg
    assert "ON DELETE SET NULL" in alter_pg
    # UPDATE must keep the projection verbatim — substr / CASE are
    # cross-dialect.
    assert "substr(tenant_id, 1, 2)" in update_pg
    assert "WHERE project_id IS NULL" in update_pg


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Drift guard — backend.db._migrate stays in sync with alembic 0038
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_db_migrate_lists_project_id_for_every_target_table():
    """``backend/db.py::_migrate``'s migrations list must contain a
    ``(<table>, "project_id", "TEXT REFERENCES projects(id) ON DELETE
    SET NULL")`` tuple for each of the six target tables.  Otherwise a
    fresh dev SQLite DB would diverge from the alembic-applied PG
    schema (column missing on dev, present on prod).
    """
    db_src = (
        Path(__file__).resolve().parent.parent / "db.py"
    ).read_text()
    for table in _TARGET_TABLES:
        # Look for the literal tuple form. Accept both single and
        # double quotes; the canonical form uses double quotes.
        pattern = rf'\(\s*["\']{re.escape(table)}["\']\s*,\s*["\']project_id["\']'
        assert re.search(pattern, db_src), (
            f"backend/db.py::_migrate() missing project_id entry for {table}; "
            f"alembic 0038 will run against PG but a fresh dev SQLite DB "
            f"will lack the column."
        )


def test_db_schema_inlines_project_id_on_business_tables():
    """``_SCHEMA`` CREATE TABLEs for the six business tables must
    contain a ``project_id`` column declaration so a *fresh* dev DB
    boots with the column.  ``_migrate()``'s ALTER TABLE runs after
    _SCHEMA and is a no-op on fresh DBs (gets ``duplicate column``
    swallowed); without this inline declaration the fresh-DB path
    would have to wait for the next boot's _migrate to land the
    column, leaving a window where reads fail.
    """
    db_src = (
        Path(__file__).resolve().parent.parent / "db.py"
    ).read_text()
    for table in _TARGET_TABLES:
        # Find the table's CREATE block by matching "CREATE TABLE …
        # <table> (" through the trailing ");" close.  Inline SQL
        # comments may legitimately contain ``;`` (we have one in
        # decision_rules' new project_id docstring), so match to the
        # close-paren-on-newline-followed-by-semicolon terminator
        # rather than the first ``;`` we see.
        block_match = re.search(
            rf"CREATE TABLE IF NOT EXISTS {re.escape(table)}\s*\((.*?)\n\);",
            db_src,
            re.DOTALL,
        )
        assert block_match, f"could not locate CREATE TABLE for {table}"
        body = block_match.group(1)
        assert "project_id" in body, (
            f"_SCHEMA CREATE TABLE for {table} is missing the project_id "
            f"column. Fresh dev DB would diverge from alembic 0038's "
            f"post-upgrade shape."
        )
        # Defence in depth: confirm the FK target is present too.
        assert "REFERENCES projects(id)" in body, (
            f"_SCHEMA CREATE TABLE for {table} declares project_id "
            f"without the projects(id) FK; alembic 0038 enforces it."
        )


def test_db_migrate_creates_project_indexes():
    """``backend/db.py::_migrate`` must create
    ``idx_<table>_project ON <table>(project_id)`` for each target
    table — same pattern as ``idx_<table>_tenant`` from I1 and as
    alembic 0038's per-table ``CREATE INDEX IF NOT EXISTS``.
    """
    db_src = (
        Path(__file__).resolve().parent.parent / "db.py"
    ).read_text()
    # The list of project-indexed tables sits in `_project_tables`.
    list_match = re.search(
        r"_project_tables\s*=\s*\[(.+?)\]", db_src, re.DOTALL
    )
    assert list_match, "_project_tables list not found in backend/db.py"
    listed = set(re.findall(r'["\']([a-z_][a-z0-9_]*)["\']', list_match.group(1)))
    missing = set(_TARGET_TABLES) - listed
    assert not missing, (
        f"backend/db.py::_migrate() _project_tables missing: {missing}"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  PG-side: live alembic-applied schema introspection
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.asyncio
@pytest.mark.parametrize("table", _TARGET_TABLES)
async def test_pg_business_table_has_project_id_column(pg_test_pool, table):
    """Each of the six business tables must carry a ``project_id``
    column after ``alembic upgrade head``."""
    async with pg_test_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT data_type, is_nullable FROM information_schema.columns "
            "WHERE table_name = $1 AND column_name = 'project_id'",
            table,
        )
    assert row is not None, f"{table}.project_id missing on PG"
    # Column is TEXT — PG canonicalises to "text".
    assert row["data_type"].lower() == "text"
    # NULLable per the TODO row's explicit "NULL 暫時允許".
    assert row["is_nullable"] == "YES"


@pytest.mark.asyncio
@pytest.mark.parametrize("table", _TARGET_TABLES)
async def test_pg_business_table_project_id_fk_targets_projects(
    pg_test_pool, table
):
    """The ``project_id`` FK must reference ``projects(id)`` with
    ``ON DELETE SET NULL`` — distinct from ``tenant_id`` which has no
    explicit ON DELETE clause from I1."""
    async with pg_test_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                tc.constraint_name,
                rc.delete_rule,
                ccu.table_name AS target_table,
                ccu.column_name AS target_column
            FROM information_schema.table_constraints tc
            JOIN information_schema.referential_constraints rc
              ON tc.constraint_name = rc.constraint_name
            JOIN information_schema.constraint_column_usage ccu
              ON tc.constraint_name = ccu.constraint_name
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
            WHERE tc.table_name = $1
              AND tc.constraint_type = 'FOREIGN KEY'
              AND kcu.column_name = 'project_id'
            """,
            table,
        )
    assert row is not None, (
        f"{table}.project_id has no FK constraint — alembic 0038's "
        f"REFERENCES projects(id) didn't make it through."
    )
    assert row["target_table"] == "projects"
    assert row["target_column"] == "id"
    assert row["delete_rule"] == "SET NULL"


@pytest.mark.asyncio
@pytest.mark.parametrize("table", _TARGET_TABLES)
async def test_pg_business_table_has_project_index(pg_test_pool, table):
    """``idx_<table>_project ON <table>(project_id)`` is part of the
    alembic 0038 shape and underpins the per-project listing query
    plan that Y2 admin REST will run."""
    async with pg_test_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename = $1 AND indexname = $2",
            table, f"idx_{table}_project",
        )
    assert row is not None, f"idx_{table}_project missing on PG"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  PG-side: backfill projection + cross-tenant tuple invariant
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.asyncio
async def test_pg_default_tenant_workflow_run_lands_on_default_project(
    pg_test_pool,
):
    """Insert a workflow_run with tenant_id='t-default' (no
    project_id) and confirm a follow-up backfill lands the
    deterministic ``p-default-default`` project_id.

    Uses the same SQL the alembic migration writes to verify the
    projection holds end-to-end.
    """
    async with pg_test_pool.acquire() as conn:
        async with conn.transaction():
            # Ensure the default tenant + project exist (idempotent).
            await conn.execute(
                "INSERT INTO tenants (id, name) VALUES ('t-default', 'Def') "
                "ON CONFLICT (id) DO NOTHING"
            )
            await conn.execute(
                "INSERT INTO projects (id, tenant_id, product_line, name, slug) "
                "VALUES ('p-default-default', 't-default', 'default', 'Default', 'default') "
                "ON CONFLICT (tenant_id, product_line, slug) DO NOTHING"
            )
            wf_id = "wf-y1r7-default"
            await conn.execute(
                "INSERT INTO workflow_runs "
                "(id, kind, started_at, status, tenant_id) "
                "VALUES ($1, 'test', 0, 'running', 't-default')",
                wf_id,
            )
            # Run the same UPDATE the migration would.
            await conn.execute(
                """
                UPDATE workflow_runs SET project_id =
                    'p-' || CASE
                        WHEN substr(tenant_id, 1, 2) = 't-' THEN substr(tenant_id, 3)
                        ELSE tenant_id
                    END || '-default'
                WHERE project_id IS NULL AND tenant_id IS NOT NULL
                """
            )
            row = await conn.fetchrow(
                "SELECT project_id FROM workflow_runs WHERE id = $1", wf_id
            )
    assert row["project_id"] == "p-default-default"


@pytest.mark.asyncio
async def test_pg_backfill_preserves_tenant_project_tuple_invariant(
    pg_test_pool,
):
    """The next TODO row asks for "回填後每 row 都有有效的 (tenant_id,
    project_id) tuple、project_id 必然指向同 tenant 的 project". This
    test is the load-bearing assertion for that invariant: after the
    backfill, every business row's (tenant_id, project_id) joins to
    a projects row whose tenant_id equals the business row's
    tenant_id.

    Uses two distinct tenants so a regression that hard-codes
    't-default' in the projection would surface here.
    """
    async with pg_test_pool.acquire() as conn:
        async with conn.transaction():
            for tid in ("t-y1r7-acme", "t-y1r7-globex"):
                await conn.execute(
                    "INSERT INTO tenants (id, name) VALUES ($1, $2) "
                    "ON CONFLICT (id) DO NOTHING",
                    tid, f"Y1r7 {tid}",
                )
                pid = "p-" + tid.removeprefix("t-") + "-default"
                await conn.execute(
                    "INSERT INTO projects "
                    "(id, tenant_id, product_line, name, slug) "
                    "VALUES ($1, $2, 'default', 'Default', 'default') "
                    "ON CONFLICT (tenant_id, product_line, slug) DO NOTHING",
                    pid, tid,
                )
                await conn.execute(
                    "INSERT INTO debug_findings "
                    "(id, task_id, agent_id, finding_type, content, tenant_id) "
                    "VALUES ($1, 't', 'a', 'observation', '{}', $2)",
                    f"df-y1r7-{tid}", tid,
                )
            await conn.execute(
                """
                UPDATE debug_findings SET project_id =
                    'p-' || CASE
                        WHEN substr(tenant_id, 1, 2) = 't-' THEN substr(tenant_id, 3)
                        ELSE tenant_id
                    END || '-default'
                WHERE project_id IS NULL AND tenant_id IS NOT NULL
                """
            )
            # Cross-tenant tuple invariant: every backfilled row joins
            # to a projects row whose tenant_id matches.
            mismatches = await conn.fetch(
                """
                SELECT df.id, df.tenant_id AS row_tenant,
                       p.tenant_id AS project_tenant
                FROM debug_findings df
                JOIN projects p ON p.id = df.project_id
                WHERE df.id LIKE 'df-y1r7-%'
                  AND df.tenant_id <> p.tenant_id
                """
            )
    assert mismatches == [], (
        f"cross-tenant project_id leak: {mismatches}. The backfill "
        f"projection must keep tenant_id and project_id on the same "
        f"tenant."
    )


@pytest.mark.asyncio
async def test_pg_backfill_is_idempotent(pg_test_pool):
    """Running the backfill UPDATE twice produces the same row state
    as running it once.  ``WHERE project_id IS NULL`` skips the second
    pass so the row count + project_id mapping is invariant."""
    async with pg_test_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO tenants (id, name) VALUES ('t-y1r7-idem', 'idem') "
                "ON CONFLICT (id) DO NOTHING"
            )
            await conn.execute(
                "INSERT INTO projects (id, tenant_id, product_line, name, slug) "
                "VALUES ('p-y1r7-idem-default', 't-y1r7-idem', 'default', 'Default', 'default') "
                "ON CONFLICT (tenant_id, product_line, slug) DO NOTHING"
            )
            await conn.execute(
                "INSERT INTO artifacts (id, name, file_path, tenant_id) "
                "VALUES ('art-y1r7-idem', 'a', '/tmp/a', 't-y1r7-idem')"
            )
            update = """
                UPDATE artifacts SET project_id =
                    'p-' || CASE
                        WHEN substr(tenant_id, 1, 2) = 't-' THEN substr(tenant_id, 3)
                        ELSE tenant_id
                    END || '-default'
                WHERE project_id IS NULL AND tenant_id IS NOT NULL
            """
            await conn.execute(update)
            pid1 = await conn.fetchval(
                "SELECT project_id FROM artifacts WHERE id = $1",
                "art-y1r7-idem",
            )
            await conn.execute(update)
            pid2 = await conn.fetchval(
                "SELECT project_id FROM artifacts WHERE id = $1",
                "art-y1r7-idem",
            )
    assert pid1 == "p-y1r7-idem-default"
    assert pid2 == pid1


@pytest.mark.asyncio
async def test_pg_fk_blocks_invalid_project_id(pg_test_pool):
    """The FK constraint must reject an UPDATE that points
    ``project_id`` at a non-existent project — defence in depth
    against a future code path that bypasses the deterministic
    backfill projection."""
    import asyncpg

    async with pg_test_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO tenants (id, name) VALUES ('t-y1r7-fk', 'fk') "
                "ON CONFLICT (id) DO NOTHING"
            )
            await conn.execute(
                "INSERT INTO event_log (event_type, data_json, tenant_id) "
                "VALUES ('test', '{}', 't-y1r7-fk')"
            )
            with pytest.raises(asyncpg.exceptions.ForeignKeyViolationError):
                await conn.execute(
                    "UPDATE event_log SET project_id = 'p-does-not-exist' "
                    "WHERE tenant_id = 't-y1r7-fk'"
                )


@pytest.mark.asyncio
async def test_pg_alter_table_is_idempotent_via_pragma_guard(pg_test_pool):
    """Manually exercise the migration's idempotency guard: introspect
    ``information_schema.columns`` (the shim's PRAGMA rewrite target),
    confirm ``project_id`` already exists on every target table, and
    verify a second ``ALTER TABLE ADD COLUMN`` would have been
    skipped — the second pass MUST raise ``DuplicateColumnError`` if
    we ever bypassed the guard.
    """
    import asyncpg

    async with pg_test_pool.acquire() as conn:
        for table in _TARGET_TABLES:
            cols = await conn.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = $1",
                table,
            )
            col_names = {r["column_name"] for r in cols}
            assert "project_id" in col_names
            # Demonstrate the unguarded ALTER would fail. We don't
            # actually run it inside a transaction we want to keep —
            # use a savepoint so the failure rolls back cleanly.
            async with conn.transaction():
                with pytest.raises(asyncpg.exceptions.DuplicateColumnError):
                    await conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN project_id TEXT"
                    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SQLite-side: dev bootstrap path
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _bootstrap_fresh_sqlite() -> Path:
    """Run ``backend.db.init`` against a fresh SQLite tempfile.

    Subprocess-based isolation:  ``backend.db`` and ``backend.config``
    both read the database path at module-import time, so the only
    reliable way to bootstrap a tempfile DB without polluting the
    parent test process's module cache is to spawn a fresh
    interpreter with ``OMNISIGHT_DATABASE_PATH`` pre-set.

    Returns the resulting tempfile path so the caller can introspect
    the schema with a vanilla ``sqlite3`` connection.
    """
    import os
    import subprocess
    import sys
    import tempfile

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_path = Path(tmp.name)
    # Delete the empty tempfile so init() creates a fresh DB rather
    # than opening the zero-byte file and finding no schema.
    db_path.unlink()

    backend_dir = Path(__file__).resolve().parent.parent
    repo_root = backend_dir.parent

    env = os.environ.copy()
    env["OMNISIGHT_DATABASE_PATH"] = str(db_path)
    env.pop("OMNISIGHT_DATABASE_URL", None)
    env.pop("DATABASE_URL", None)
    env["PYTHONPATH"] = str(repo_root)

    # Single-statement bootstrap: import backend.db, run init() + close()
    # in the subprocess's event loop. Capture stderr so a silent crash
    # surfaces in the assertion message.
    bootstrap = (
        "import asyncio; "
        "from backend import db; "
        "asyncio.run(db.init()); "
        "asyncio.run(db.close())"
    )
    proc = subprocess.run(
        [sys.executable, "-c", bootstrap],
        env=env,
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"bootstrap subprocess failed:\n"
            f"stdout: {proc.stdout!r}\n"
            f"stderr: {proc.stderr!r}"
        )
    return db_path


def test_sqlite_bootstrap_has_project_id_on_every_target_table(tmp_path):
    """A fresh dev SQLite DB (the path real developers boot against)
    must show ``project_id`` on every business table after
    ``backend.db.init`` returns.  Catches the regression where
    ``_SCHEMA`` is updated but ``_migrate`` is not (or vice versa).
    """
    import sqlite3
    db_path = _bootstrap_fresh_sqlite()
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            for table in _TARGET_TABLES:
                cols = {
                    row[1]
                    for row in conn.execute(f"PRAGMA table_info({table})")
                }
                assert "project_id" in cols, (
                    f"fresh SQLite dev DB is missing {table}.project_id; "
                    f"_SCHEMA / _migrate drift suspected."
                )
        finally:
            conn.close()
    finally:
        try:
            db_path.unlink()
        except FileNotFoundError:
            pass


def test_sqlite_bootstrap_seeds_default_project():
    """``_migrate()`` must seed ``p-default-default`` so the dev DB
    has the FK target the next-release NOT NULL flip will rely on
    (and so dev-side feature work doesn't trip over a bare projects
    table)."""
    import sqlite3
    db_path = _bootstrap_fresh_sqlite()
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT id, tenant_id, product_line, slug, name "
                "FROM projects WHERE id = 'p-default-default'"
            ).fetchone()
            assert row is not None, (
                "dev SQLite did not seed p-default-default — "
                "_migrate() Y1 row 6 seed regressed."
            )
            assert row[1] == "t-default"
            assert row[2] == "default"
            assert row[3] == "default"
            assert row[4] == "Default"
        finally:
            conn.close()
    finally:
        try:
            db_path.unlink()
        except FileNotFoundError:
            pass


def test_sqlite_bootstrap_creates_project_indexes():
    """Each target table must have an ``idx_<table>_project`` index
    on the dev SQLite path too — same pattern as the alembic 0038
    one created on PG."""
    import sqlite3
    db_path = _bootstrap_fresh_sqlite()
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            for table in _TARGET_TABLES:
                idx = f"idx_{table}_project"
                row = conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='index' AND name=?",
                    (idx,),
                ).fetchone()
                assert row is not None, (
                    f"{idx} missing on dev SQLite — _migrate() index "
                    f"loop didn't create it."
                )
        finally:
            conn.close()
    finally:
        try:
            db_path.unlink()
        except FileNotFoundError:
            pass
