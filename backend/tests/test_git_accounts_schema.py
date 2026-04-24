"""Phase 5-1 — git_accounts schema contract tests.

Locks the schema landed by alembic migration ``0027_git_accounts``
+ the SQLite mirror in ``backend/db.py::_SCHEMA``. Two layers:

1. **Migration file sanity** — pure-unit, no DB. Asserts the
   alembic file exists, the revision chain is correct
   (``0027`` → ``0026``), and the SQLite + PG branches mention
   the load-bearing schema fragments (CHECK constraint, partial
   unique index, FK).
2. **Live SQLite contract** — fresh ``backend.db.init`` against a
   tmp DB, introspect ``PRAGMA table_info(git_accounts)`` and
   ``PRAGMA index_list(git_accounts)``, assert column set + types
   + indexes.
3. **Live PG contract** (gated on ``OMNI_TEST_PG_URL``) — fresh
   ``alembic upgrade head`` against a clean PG schema, assert the
   table + columns + indexes + partial-unique constraint exist
   on the PG side.

Why this guard exists
─────────────────────
The Phase-5 design doc (docs/phase-5-multi-account/01-design.md)
spells out 19 columns, 3 indexes, 1 unique partial index, 1 FK,
and 1 CHECK constraint. Future rows (5-2 through 5-11) read those
columns by name; if any drifts away the resolver / CRUD / UI all
break silently. This file is the schema lock that fails loud at
CI time when someone adds an alembic migration that drops or
renames a column.

Module-global state audit (SOP Step 1, qualified answer #1)
───────────────────────────────────────────────────────────
The SQLite test creates a fresh tmp DB per test function — no
shared state, no module-global mutation. The PG test resets
``public`` schema before alembic upgrade, then drops the
test-created rows on teardown. No singleton, no in-memory cache,
no contextvar — pure schema introspection.

Read-after-write timing audit (SOP Step 1)
──────────────────────────────────────────
Schema-only test. No write path is exercised that downstream code
could observe; the partial-index uniqueness assertion does try
two competing inserts in the same connection but commits between
them — there is no parallelism for downstream tests to depend on.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[2]
_MIGRATION_PATH = (
    _REPO_ROOT / "backend" / "alembic" / "versions" / "0027_git_accounts.py"
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Layer 1: Migration file sanity (pure unit, no DB).
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_migration_0027_file_exists():
    assert _MIGRATION_PATH.exists(), (
        f"alembic migration missing at {_MIGRATION_PATH}"
    )


def test_migration_0027_revision_chain():
    spec = importlib.util.spec_from_file_location("m0027", _MIGRATION_PATH)
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert m.revision == "0027"
    assert m.down_revision == "0026", (
        "Phase 5-1 must chain after 0026_chat_sessions; if a newer "
        "migration was inserted between, update down_revision to point "
        "at the actual prior head."
    )


def test_migration_0027_carries_load_bearing_fragments():
    """The migration source must mention the load-bearing schema bits
    so a careless edit that drops them fails this test rather than
    failing in production at first INSERT."""
    src = _MIGRATION_PATH.read_text(encoding="utf-8")
    must_have = [
        # PG branch.
        "JSONB NOT NULL DEFAULT '[]'::jsonb",       # url_patterns
        "JSONB NOT NULL DEFAULT '{}'::jsonb",       # metadata
        "DOUBLE PRECISION",                         # last_used_at + created_at
        "WHERE is_default = TRUE",                  # partial unique index
        "REFERENCES tenants(id) ON DELETE CASCADE", # FK
        "CHECK (platform IN ('github','gitlab','gerrit','jira'))",
        # SQLite branch.
        "CHECK (platform IN ('github','gitlab','gerrit','jira'))",
        "WHERE is_default = 1",                     # SQLite partial unique
        # Optimistic-lock from day 1 (J2 / Q.7 lineage).
        "version                  INTEGER NOT NULL DEFAULT 0",
    ]
    for fragment in must_have:
        assert fragment in src, (
            f"Phase 5-1 migration is missing load-bearing fragment: "
            f"{fragment!r}. If you intentionally dropped it, also "
            f"update docs/phase-5-multi-account/01-design.md and this "
            f"test."
        )


def test_migrator_table_list_includes_git_accounts():
    """Drift guard against scripts/migrate_sqlite_to_pg.py forgetting
    the new table — the same risk Phase-3 F3 added the migrator-
    schema-coverage test for. Catches the case where a later refactor
    accidentally drops ``git_accounts`` from ``TABLES_IN_ORDER``.
    Belt+braces on top of test_migrator_schema_coverage.py."""
    import sys
    spec = importlib.util.spec_from_file_location(
        "migrate_sqlite_to_pg_g5_1_probe",
        _REPO_ROOT / "scripts" / "migrate_sqlite_to_pg.py",
    )
    assert spec and spec.loader
    mig = importlib.util.module_from_spec(spec)
    # Register in sys.modules BEFORE exec — the migrator module uses
    # ``@dataclass`` with string annotations; dataclasses resolves
    # those by ``sys.modules.get(cls.__module__).__dict__`` and would
    # NoneType-crash if we skipped this step (matches the pattern in
    # test_migrator_schema_coverage.py).
    sys.modules[spec.name] = mig
    try:
        spec.loader.exec_module(mig)
        assert "git_accounts" in mig.TABLES_IN_ORDER, (
            "scripts/migrate_sqlite_to_pg.py::TABLES_IN_ORDER must "
            "include 'git_accounts' so a SQLite→PG cutover replay does "
            "not silently drop forge credential rows."
        )
        # PK is TEXT (app-generated id), not INTEGER — must NOT appear
        # in the IDENTITY-id list (would crash sequence reset on PG).
        assert "git_accounts" not in mig.TABLES_WITH_IDENTITY_ID, (
            "git_accounts has TEXT PK (app-generated id like "
            "tenant_secrets) — listing it as an IDENTITY table would "
            "crash the migrator at sequence-reset time on PG."
        )
    finally:
        sys.modules.pop(spec.name, None)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Layer 2: Live SQLite contract.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_EXPECTED_SQLITE_COLUMNS: frozenset[str] = frozenset({
    "id",
    "tenant_id",
    "platform",
    "instance_url",
    "label",
    "username",
    "encrypted_token",
    "encrypted_ssh_key",
    "ssh_host",
    "ssh_port",
    "project",
    "encrypted_webhook_secret",
    "url_patterns",
    "auth_type",
    "is_default",
    "enabled",
    "metadata",
    "last_used_at",
    "created_at",
    "updated_at",
    "version",
    # Phase 5-12 (alembic 0028) — OAuth prep; see
    # docs/phase-5-multi-account/01-design.md §10 for rationale.
    "code_verifier",
})

_EXPECTED_SQLITE_INDEXES: frozenset[str] = frozenset({
    "idx_git_accounts_tenant",
    "idx_git_accounts_tenant_platform",
    "idx_git_accounts_last_used",
    "uq_git_accounts_default_per_platform",
})


@pytest.fixture
async def _sqlite_with_git_accounts(tmp_path):
    db_path = tmp_path / "probe.db"
    os.environ["OMNISIGHT_DATABASE_PATH"] = str(db_path)
    from backend import config as _cfg
    _cfg.settings.database_path = str(db_path)
    from backend import db
    db._DB_PATH = db._resolve_db_path()
    await db.init()
    try:
        yield db
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_sqlite_git_accounts_table_exists(_sqlite_with_git_accounts):
    db = _sqlite_with_git_accounts
    conn = db._conn()
    async with conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name = 'git_accounts'"
    ) as cur:
        row = await cur.fetchone()
    assert row is not None, "git_accounts table missing on fresh SQLite init"


@pytest.mark.asyncio
async def test_sqlite_git_accounts_columns(_sqlite_with_git_accounts):
    db = _sqlite_with_git_accounts
    conn = db._conn()
    async with conn.execute("PRAGMA table_info(git_accounts)") as cur:
        rows = await cur.fetchall()
    cols = {r[1] for r in rows}
    missing = _EXPECTED_SQLITE_COLUMNS - cols
    extra = cols - _EXPECTED_SQLITE_COLUMNS
    assert not missing, f"git_accounts missing columns: {sorted(missing)}"
    assert not extra, (
        f"git_accounts has unexpected columns: {sorted(extra)}. "
        "If you added a column on purpose, update _EXPECTED_SQLITE_COLUMNS "
        "and the design doc."
    )


@pytest.mark.asyncio
async def test_sqlite_git_accounts_indexes(_sqlite_with_git_accounts):
    db = _sqlite_with_git_accounts
    conn = db._conn()
    async with conn.execute("PRAGMA index_list(git_accounts)") as cur:
        rows = await cur.fetchall()
    idx_names = {r[1] for r in rows}
    missing = _EXPECTED_SQLITE_INDEXES - idx_names
    assert not missing, (
        f"git_accounts missing indexes: {sorted(missing)}. "
        "Performance + uniqueness invariants depend on these."
    )


@pytest.mark.asyncio
async def test_sqlite_git_accounts_partial_unique_default(
    _sqlite_with_git_accounts,
):
    """At most one row per (tenant_id, platform) may have
    is_default = 1. Two `INSERT ... is_default = 1` for the same
    (tenant, platform) must collide."""
    db = _sqlite_with_git_accounts
    conn = db._conn()
    # First insert — should succeed.
    await conn.execute(
        "INSERT INTO git_accounts (id, tenant_id, platform, "
        "is_default, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("ga-1", "t-default", "github", 1, 1.0, 1.0),
    )
    await conn.commit()
    # Second insert with is_default = 1 for same (tenant, platform)
    # must violate the partial unique index.
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        await conn.execute(
            "INSERT INTO git_accounts (id, tenant_id, platform, "
            "is_default, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("ga-2", "t-default", "github", 1, 2.0, 2.0),
        )
        await conn.commit()


@pytest.mark.asyncio
async def test_sqlite_git_accounts_check_constraint_platform(
    _sqlite_with_git_accounts,
):
    """``platform`` must be one of the four whitelisted values."""
    db = _sqlite_with_git_accounts
    conn = db._conn()
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        await conn.execute(
            "INSERT INTO git_accounts (id, tenant_id, platform, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("ga-bad", "t-default", "bitbucket", 1.0, 1.0),
        )
        await conn.commit()


@pytest.mark.asyncio
async def test_sqlite_git_accounts_default_column_values(
    _sqlite_with_git_accounts,
):
    """A minimal INSERT (id + tenant + platform + timestamps) must
    populate every other NOT NULL column from its DEFAULT clause —
    the CRUD code in row 5-4 relies on this so it doesn't have to
    hand-spell every column on insert."""
    db = _sqlite_with_git_accounts
    conn = db._conn()
    await conn.execute(
        "INSERT INTO git_accounts (id, tenant_id, platform, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        ("ga-defaults", "t-default", "gitlab", 1.0, 1.0),
    )
    await conn.commit()
    async with conn.execute(
        "SELECT instance_url, label, username, encrypted_token, "
        "encrypted_ssh_key, ssh_host, ssh_port, project, "
        "encrypted_webhook_secret, url_patterns, auth_type, "
        "is_default, enabled, metadata, last_used_at, version "
        "FROM git_accounts WHERE id = ?",
        ("ga-defaults",),
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    (
        instance_url, label, username, enc_token, enc_ssh, ssh_host,
        ssh_port, project, enc_webhook, url_patterns, auth_type,
        is_default, enabled, metadata, last_used_at, version,
    ) = row
    assert instance_url == ""
    assert label == ""
    assert username == ""
    assert enc_token == ""
    assert enc_ssh == ""
    assert ssh_host == ""
    assert ssh_port == 0
    assert project == ""
    assert enc_webhook == ""
    assert url_patterns == "[]"
    assert auth_type == "pat"
    assert is_default == 0
    assert enabled == 1
    assert metadata == "{}"
    assert last_used_at is None
    assert version == 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Layer 3: Live PG contract (gated on OMNI_TEST_PG_URL).
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_EXPECTED_PG_COLUMNS = _EXPECTED_SQLITE_COLUMNS  # identical column set


@pytest.mark.asyncio
async def test_pg_git_accounts_table_exists(pg_test_pool):
    async with pg_test_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = 'git_accounts'"
        )
    assert row is not None, (
        "git_accounts missing on PG — alembic upgrade head did not "
        "run 0027_git_accounts. Check the revision chain."
    )


@pytest.mark.asyncio
async def test_pg_git_accounts_columns(pg_test_pool):
    async with pg_test_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'git_accounts'"
        )
    cols = {r["column_name"] for r in rows}
    missing = _EXPECTED_PG_COLUMNS - cols
    extra = cols - _EXPECTED_PG_COLUMNS
    assert not missing, f"PG git_accounts missing columns: {sorted(missing)}"
    assert not extra, (
        f"PG git_accounts has unexpected columns: {sorted(extra)}"
    )


@pytest.mark.asyncio
async def test_pg_git_accounts_indexes(pg_test_pool):
    async with pg_test_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename = 'git_accounts'"
        )
    idx_names = {r["indexname"] for r in rows}
    expected = {
        "idx_git_accounts_tenant",
        "idx_git_accounts_tenant_platform",
        "idx_git_accounts_last_used",
        "uq_git_accounts_default_per_platform",
        "git_accounts_pkey",  # PG auto-creates from PRIMARY KEY
    }
    missing = expected - idx_names
    assert not missing, (
        f"PG git_accounts missing indexes: {sorted(missing)}"
    )


@pytest.mark.asyncio
async def test_pg_git_accounts_fk_to_tenants(pg_test_pool):
    """FK ``tenant_id REFERENCES tenants(id) ON DELETE CASCADE`` — if
    this drops, deleting a tenant would orphan its credential rows
    instead of cascading the cleanup."""
    async with pg_test_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                tc.constraint_name,
                rc.delete_rule
            FROM information_schema.table_constraints tc
            JOIN information_schema.referential_constraints rc
              ON tc.constraint_name = rc.constraint_name
            WHERE tc.table_name = 'git_accounts'
              AND tc.constraint_type = 'FOREIGN KEY'
            """
        )
    assert row is not None, "git_accounts has no FK to tenants"
    assert row["delete_rule"] == "CASCADE", (
        f"FK delete_rule must be CASCADE, got {row['delete_rule']}"
    )


@pytest.mark.asyncio
async def test_pg_git_accounts_partial_unique_default(pg_test_pool):
    """Partial-unique index ``WHERE is_default = TRUE`` enforces "at
    most one default per (tenant, platform)" at the database layer.
    Two competing INSERTs with is_default = TRUE on the same
    (tenant, platform) tuple must give the loser a clean unique
    violation — that's what row 5-4's CRUD relies on instead of
    racy SELECT-then-UPDATE."""
    import asyncpg
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM git_accounts WHERE id IN ($1, $2)",
            "ga-pg-default-a", "ga-pg-default-b",
        )
        await conn.execute(
            "INSERT INTO git_accounts (id, tenant_id, platform, "
            "is_default, created_at, updated_at) "
            "VALUES ($1, $2, $3, TRUE, $4, $5)",
            "ga-pg-default-a", "t-default", "github", 1.0, 1.0,
        )
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                "INSERT INTO git_accounts (id, tenant_id, platform, "
                "is_default, created_at, updated_at) "
                "VALUES ($1, $2, $3, TRUE, $4, $5)",
                "ga-pg-default-b", "t-default", "github", 2.0, 2.0,
            )
        # Two non-default rows on the same (tenant, platform) remain
        # legal — the index is partial.
        await conn.execute(
            "INSERT INTO git_accounts (id, tenant_id, platform, "
            "is_default, created_at, updated_at) "
            "VALUES ($1, $2, $3, FALSE, $4, $5)",
            "ga-pg-default-c", "t-default", "github", 3.0, 3.0,
        )
        # Cleanup so the test is rerunnable inside the same DB.
        await conn.execute(
            "DELETE FROM git_accounts WHERE id IN ($1, $2, $3)",
            "ga-pg-default-a", "ga-pg-default-b", "ga-pg-default-c",
        )


@pytest.mark.asyncio
async def test_pg_git_accounts_check_constraint_platform(pg_test_pool):
    """CHECK (platform IN (...)) keeps typo'd platform strings out of
    the table at write-time."""
    import asyncpg
    async with pg_test_pool.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO git_accounts (id, tenant_id, platform, "
                "created_at, updated_at) VALUES ($1, $2, $3, $4, $5)",
                "ga-pg-bad", "t-default", "bitbucket", 1.0, 1.0,
            )


@pytest.mark.asyncio
async def test_pg_git_accounts_cascade_delete_on_tenant(pg_test_pool):
    """Deleting a tenant must cascade-delete its credential rows."""
    async with pg_test_pool.acquire() as conn:
        # Create a throwaway tenant + an account on it.
        await conn.execute(
            "INSERT INTO tenants (id, name) VALUES ($1, $2) "
            "ON CONFLICT (id) DO NOTHING",
            "t-cascade-test", "Cascade Test",
        )
        await conn.execute(
            "INSERT INTO git_accounts (id, tenant_id, platform, "
            "created_at, updated_at) VALUES ($1, $2, $3, $4, $5)",
            "ga-cascade-1", "t-cascade-test", "gitlab", 1.0, 1.0,
        )
        await conn.execute("DELETE FROM tenants WHERE id = $1",
                           "t-cascade-test")
        row = await conn.fetchrow(
            "SELECT id FROM git_accounts WHERE id = $1",
            "ga-cascade-1",
        )
    assert row is None, (
        "git_accounts row survived tenant deletion — FK cascade is "
        "not configured correctly."
    )
