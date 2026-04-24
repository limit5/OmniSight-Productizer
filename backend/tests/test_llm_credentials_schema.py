"""Phase 5b-1 — llm_credentials schema contract tests.

Locks the schema landed by alembic migration ``0029_llm_credentials``
+ the SQLite mirror in ``backend/db.py::_SCHEMA``. Four layers,
mirroring the ``test_git_accounts_schema.py`` template:

1. **Migration file sanity** — pure-unit, no DB. Asserts the
   alembic file exists, the revision chain is correct
   (``0029`` → ``0028``), and the SQLite + PG branches mention
   the load-bearing schema fragments (CHECK constraint enumerating
   the 9 providers, partial unique index, FK, ``DOUBLE
   PRECISION`` timestamps, ``version`` optimistic-lock column).
2. **Migrator table-list alignment** — asserts that
   ``scripts/migrate_sqlite_to_pg.py::TABLES_IN_ORDER`` includes
   ``'llm_credentials'`` and that it is NOT in
   ``TABLES_WITH_IDENTITY_ID`` (PK is TEXT, not INTEGER IDENTITY).
3. **Live SQLite contract** — fresh ``backend.db.init`` against a
   tmp DB, introspect ``PRAGMA table_info(llm_credentials)`` and
   ``PRAGMA index_list(llm_credentials)``, assert column set +
   defaults + partial-unique + CHECK.
4. **Live PG contract** (gated on ``OMNI_TEST_PG_URL``) — fresh
   ``alembic upgrade head`` against a clean PG schema, assert the
   table + columns + indexes + partial-unique + CHECK + FK
   cascade all exist on the PG side.

Why this guard exists
─────────────────────
The Phase-5b-1 design doc (docs/phase-5b-llm-credentials/
01-design.md) spells out 13 columns, 3 named indexes, 1 unique
partial index, 1 FK, and 1 CHECK constraint. Future rows (5b-2
through 5b-6) read those columns by name; if any drifts away the
resolver / CRUD / UI all break silently. This file is the schema
lock that fails loud at CI time when someone adds an alembic
migration that drops or renames a column.

Module-global state audit (SOP Step 1, qualified answer #1)
───────────────────────────────────────────────────────────
The SQLite test creates a fresh tmp DB per test function — no
shared state, no module-global mutation. The PG test uses the
conftest ``pg_test_pool`` fixture which owns lifecycle and
rolls back writes on teardown. No singleton, no in-memory cache,
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
    _REPO_ROOT / "backend" / "alembic" / "versions" / "0029_llm_credentials.py"
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Layer 1: Migration file sanity (pure unit, no DB).
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_migration_0029_file_exists():
    assert _MIGRATION_PATH.exists(), (
        f"alembic migration missing at {_MIGRATION_PATH}"
    )


def test_migration_0029_revision_chain():
    spec = importlib.util.spec_from_file_location("m0029", _MIGRATION_PATH)
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert m.revision == "0029"
    assert m.down_revision == "0028", (
        "Phase 5b-1 must chain after 0028_git_accounts_code_verifier; "
        "if a newer migration was inserted between, update down_revision "
        "to point at the actual prior head."
    )


def test_migration_0029_carries_load_bearing_fragments():
    """The migration source must mention the load-bearing schema bits
    so a careless edit that drops them fails this test rather than
    failing in production at first INSERT."""
    src = _MIGRATION_PATH.read_text(encoding="utf-8")
    must_have = [
        # PG branch.
        "JSONB NOT NULL DEFAULT '{}'::jsonb",           # metadata
        "DOUBLE PRECISION",                             # timestamps
        "WHERE is_default = TRUE",                      # PG partial unique
        "REFERENCES tenants(id) ON DELETE CASCADE",     # FK
        # Provider CHECK — all 9 entries must be present so typo'd
        # provider strings can't pass the constraint.
        "'anthropic'",
        "'google'",
        "'openai'",
        "'xai'",
        "'groq'",
        "'deepseek'",
        "'together'",
        "'openrouter'",
        "'ollama'",
        # SQLite branch.
        "WHERE is_default = 1",                         # SQLite partial unique
        # Optimistic-lock from day 1 (J2 / Q.7 / Phase-5-1 lineage).
        "version           INTEGER NOT NULL DEFAULT 0",
    ]
    for fragment in must_have:
        assert fragment in src, (
            f"Phase 5b-1 migration is missing load-bearing fragment: "
            f"{fragment!r}. If you intentionally dropped it, also "
            f"update docs/phase-5b-llm-credentials/01-design.md and "
            f"this test."
        )


def test_migrator_table_list_includes_llm_credentials():
    """Drift guard against scripts/migrate_sqlite_to_pg.py forgetting
    the new table — the same risk Phase-3 F3 added the migrator-
    schema-coverage test for. Catches the case where a later refactor
    accidentally drops ``llm_credentials`` from ``TABLES_IN_ORDER``.
    Belt+braces on top of test_migrator_schema_coverage.py."""
    import sys
    spec = importlib.util.spec_from_file_location(
        "migrate_sqlite_to_pg_g5b_1_probe",
        _REPO_ROOT / "scripts" / "migrate_sqlite_to_pg.py",
    )
    assert spec and spec.loader
    mig = importlib.util.module_from_spec(spec)
    # Register in sys.modules BEFORE exec — the migrator module uses
    # ``@dataclass`` with string annotations; dataclasses resolves
    # those by ``sys.modules.get(cls.__module__).__dict__`` and would
    # NoneType-crash if we skipped this step (matches the pattern in
    # test_migrator_schema_coverage.py and test_git_accounts_schema.py).
    sys.modules[spec.name] = mig
    try:
        spec.loader.exec_module(mig)
        assert "llm_credentials" in mig.TABLES_IN_ORDER, (
            "scripts/migrate_sqlite_to_pg.py::TABLES_IN_ORDER must "
            "include 'llm_credentials' so a SQLite→PG cutover replay "
            "does not silently drop LLM credential rows."
        )
        # PK is TEXT (app-generated id), not INTEGER — must NOT appear
        # in the IDENTITY-id list (would crash sequence reset on PG).
        assert "llm_credentials" not in mig.TABLES_WITH_IDENTITY_ID, (
            "llm_credentials has TEXT PK (app-generated id like "
            "tenant_secrets / git_accounts) — listing it as an "
            "IDENTITY table would crash the migrator at "
            "sequence-reset time on PG."
        )
    finally:
        sys.modules.pop(spec.name, None)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Layer 2: Live SQLite contract.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_EXPECTED_SQLITE_COLUMNS: frozenset[str] = frozenset({
    "id",
    "tenant_id",
    "provider",
    "label",
    "encrypted_value",
    "metadata",
    "auth_type",
    "is_default",
    "enabled",
    "last_used_at",
    "created_at",
    "updated_at",
    "version",
})

_EXPECTED_SQLITE_INDEXES: frozenset[str] = frozenset({
    "idx_llm_credentials_tenant",
    "idx_llm_credentials_tenant_provider",
    "idx_llm_credentials_last_used",
    "uq_llm_credentials_default_per_provider",
})


@pytest.fixture
async def _sqlite_with_llm_credentials(tmp_path):
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
async def test_sqlite_llm_credentials_table_exists(
    _sqlite_with_llm_credentials,
):
    db = _sqlite_with_llm_credentials
    conn = db._conn()
    async with conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name = 'llm_credentials'"
    ) as cur:
        row = await cur.fetchone()
    assert row is not None, (
        "llm_credentials table missing on fresh SQLite init"
    )


@pytest.mark.asyncio
async def test_sqlite_llm_credentials_columns(_sqlite_with_llm_credentials):
    db = _sqlite_with_llm_credentials
    conn = db._conn()
    async with conn.execute("PRAGMA table_info(llm_credentials)") as cur:
        rows = await cur.fetchall()
    cols = {r[1] for r in rows}
    missing = _EXPECTED_SQLITE_COLUMNS - cols
    extra = cols - _EXPECTED_SQLITE_COLUMNS
    assert not missing, (
        f"llm_credentials missing columns: {sorted(missing)}"
    )
    assert not extra, (
        f"llm_credentials has unexpected columns: {sorted(extra)}. "
        "If you added a column on purpose, update "
        "_EXPECTED_SQLITE_COLUMNS and the design doc."
    )


@pytest.mark.asyncio
async def test_sqlite_llm_credentials_indexes(_sqlite_with_llm_credentials):
    db = _sqlite_with_llm_credentials
    conn = db._conn()
    async with conn.execute("PRAGMA index_list(llm_credentials)") as cur:
        rows = await cur.fetchall()
    idx_names = {r[1] for r in rows}
    missing = _EXPECTED_SQLITE_INDEXES - idx_names
    assert not missing, (
        f"llm_credentials missing indexes: {sorted(missing)}. "
        "Performance + uniqueness invariants depend on these."
    )


@pytest.mark.asyncio
async def test_sqlite_llm_credentials_partial_unique_default(
    _sqlite_with_llm_credentials,
):
    """At most one row per (tenant_id, provider) may have
    is_default = 1. Two `INSERT ... is_default = 1` for the same
    (tenant, provider) must collide."""
    db = _sqlite_with_llm_credentials
    conn = db._conn()
    # First insert — should succeed.
    await conn.execute(
        "INSERT INTO llm_credentials (id, tenant_id, provider, "
        "is_default, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("lc-1", "t-default", "anthropic", 1, 1.0, 1.0),
    )
    await conn.commit()
    # Second insert with is_default = 1 for same (tenant, provider)
    # must violate the partial unique index.
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        await conn.execute(
            "INSERT INTO llm_credentials (id, tenant_id, provider, "
            "is_default, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("lc-2", "t-default", "anthropic", 1, 2.0, 2.0),
        )
        await conn.commit()


@pytest.mark.asyncio
async def test_sqlite_llm_credentials_check_constraint_provider(
    _sqlite_with_llm_credentials,
):
    """``provider`` must be one of the nine whitelisted values."""
    db = _sqlite_with_llm_credentials
    conn = db._conn()
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        await conn.execute(
            "INSERT INTO llm_credentials (id, tenant_id, provider, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("lc-bad", "t-default", "not-a-real-provider", 1.0, 1.0),
        )
        await conn.commit()


@pytest.mark.asyncio
async def test_sqlite_llm_credentials_default_column_values(
    _sqlite_with_llm_credentials,
):
    """A minimal INSERT (id + tenant + provider + timestamps) must
    populate every other NOT NULL column from its DEFAULT clause —
    the CRUD code in row 5b-3 relies on this so it doesn't have to
    hand-spell every column on insert."""
    db = _sqlite_with_llm_credentials
    conn = db._conn()
    await conn.execute(
        "INSERT INTO llm_credentials (id, tenant_id, provider, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        ("lc-defaults", "t-default", "openai", 1.0, 1.0),
    )
    await conn.commit()
    async with conn.execute(
        "SELECT label, encrypted_value, metadata, auth_type, "
        "is_default, enabled, last_used_at, version "
        "FROM llm_credentials WHERE id = ?",
        ("lc-defaults",),
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    (label, enc, metadata, auth_type, is_default, enabled,
     last_used_at, version) = row
    assert label == ""
    assert enc == ""
    assert metadata == "{}"
    assert auth_type == "pat"
    assert is_default == 0
    assert enabled == 1
    assert last_used_at is None
    assert version == 0


@pytest.mark.asyncio
async def test_sqlite_llm_credentials_ollama_allowed(
    _sqlite_with_llm_credentials,
):
    """Ollama is a keyless provider but the row 5b-5 legacy migrator
    still creates a row for it (with ``metadata.base_url`` carrying
    the former ``Settings.ollama_base_url`` scalar). The CHECK
    constraint must accept ``'ollama'`` — this test locks that."""
    db = _sqlite_with_llm_credentials
    conn = db._conn()
    await conn.execute(
        "INSERT INTO llm_credentials (id, tenant_id, provider, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        ("lc-ollama", "t-default", "ollama", 1.0, 1.0),
    )
    await conn.commit()
    async with conn.execute(
        "SELECT provider FROM llm_credentials WHERE id = ?",
        ("lc-ollama",),
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == "ollama"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Layer 3: Live PG contract (gated on OMNI_TEST_PG_URL).
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_EXPECTED_PG_COLUMNS = _EXPECTED_SQLITE_COLUMNS  # identical column set


@pytest.mark.asyncio
async def test_pg_llm_credentials_table_exists(pg_test_pool):
    async with pg_test_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' "
            "AND table_name = 'llm_credentials'"
        )
    assert row is not None, (
        "llm_credentials missing on PG — alembic upgrade head did "
        "not run 0029_llm_credentials. Check the revision chain."
    )


@pytest.mark.asyncio
async def test_pg_llm_credentials_columns(pg_test_pool):
    async with pg_test_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' "
            "AND table_name = 'llm_credentials'"
        )
    cols = {r["column_name"] for r in rows}
    missing = _EXPECTED_PG_COLUMNS - cols
    extra = cols - _EXPECTED_PG_COLUMNS
    assert not missing, (
        f"PG llm_credentials missing columns: {sorted(missing)}"
    )
    assert not extra, (
        f"PG llm_credentials has unexpected columns: {sorted(extra)}"
    )


@pytest.mark.asyncio
async def test_pg_llm_credentials_metadata_is_jsonb(pg_test_pool):
    """``metadata`` must be JSONB on PG so the resolver in row 5b-2
    can use ``->>`` / ``@>`` operators and the Phase-5b-4 UI can
    query per-provider metadata without parsing TEXT."""
    async with pg_test_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_schema = 'public' "
            "AND table_name = 'llm_credentials' "
            "AND column_name = 'metadata'"
        )
    assert row is not None
    assert row["data_type"] == "jsonb", (
        f"metadata column must be JSONB, got {row['data_type']}"
    )


@pytest.mark.asyncio
async def test_pg_llm_credentials_indexes(pg_test_pool):
    async with pg_test_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename = 'llm_credentials'"
        )
    idx_names = {r["indexname"] for r in rows}
    expected = {
        "idx_llm_credentials_tenant",
        "idx_llm_credentials_tenant_provider",
        "idx_llm_credentials_last_used",
        "uq_llm_credentials_default_per_provider",
        "llm_credentials_pkey",  # PG auto-creates from PRIMARY KEY
    }
    missing = expected - idx_names
    assert not missing, (
        f"PG llm_credentials missing indexes: {sorted(missing)}"
    )


@pytest.mark.asyncio
async def test_pg_llm_credentials_fk_to_tenants(pg_test_pool):
    """FK ``tenant_id REFERENCES tenants(id) ON DELETE CASCADE`` — if
    this drops, deleting a tenant would orphan its LLM credential
    rows instead of cascading the cleanup."""
    async with pg_test_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                tc.constraint_name,
                rc.delete_rule
            FROM information_schema.table_constraints tc
            JOIN information_schema.referential_constraints rc
              ON tc.constraint_name = rc.constraint_name
            WHERE tc.table_name = 'llm_credentials'
              AND tc.constraint_type = 'FOREIGN KEY'
            """
        )
    assert row is not None, "llm_credentials has no FK to tenants"
    assert row["delete_rule"] == "CASCADE", (
        f"FK delete_rule must be CASCADE, got {row['delete_rule']}"
    )


@pytest.mark.asyncio
async def test_pg_llm_credentials_partial_unique_default(pg_test_pool):
    """Partial-unique index ``WHERE is_default = TRUE`` enforces "at
    most one default per (tenant, provider)" at the database layer.
    Two competing INSERTs with is_default = TRUE on the same
    (tenant, provider) tuple must give the loser a clean unique
    violation — that's what row 5b-3's CRUD relies on instead of
    racy SELECT-then-UPDATE."""
    import asyncpg
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM llm_credentials WHERE id IN ($1, $2, $3)",
            "lc-pg-default-a", "lc-pg-default-b", "lc-pg-default-c",
        )
        await conn.execute(
            "INSERT INTO llm_credentials (id, tenant_id, provider, "
            "is_default, created_at, updated_at) "
            "VALUES ($1, $2, $3, TRUE, $4, $5)",
            "lc-pg-default-a", "t-default", "anthropic", 1.0, 1.0,
        )
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                "INSERT INTO llm_credentials (id, tenant_id, provider, "
                "is_default, created_at, updated_at) "
                "VALUES ($1, $2, $3, TRUE, $4, $5)",
                "lc-pg-default-b", "t-default", "anthropic", 2.0, 2.0,
            )
        # Two non-default rows on the same (tenant, provider) remain
        # legal — the index is partial.
        await conn.execute(
            "INSERT INTO llm_credentials (id, tenant_id, provider, "
            "is_default, created_at, updated_at) "
            "VALUES ($1, $2, $3, FALSE, $4, $5)",
            "lc-pg-default-c", "t-default", "anthropic", 3.0, 3.0,
        )
        # Cleanup so the test is rerunnable inside the same DB.
        await conn.execute(
            "DELETE FROM llm_credentials WHERE id IN ($1, $2, $3)",
            "lc-pg-default-a", "lc-pg-default-b", "lc-pg-default-c",
        )


@pytest.mark.asyncio
async def test_pg_llm_credentials_check_constraint_provider(pg_test_pool):
    """CHECK (provider IN (...)) keeps typo'd provider strings out of
    the table at write-time. Row 5b-2's resolver relies on this — a
    row with ``provider='Anthropic'`` (capitalised) would be
    unreachable because the resolver keys on lowercase."""
    import asyncpg
    async with pg_test_pool.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO llm_credentials (id, tenant_id, provider, "
                "created_at, updated_at) "
                "VALUES ($1, $2, $3, $4, $5)",
                "lc-pg-bad", "t-default", "NotARealProvider", 1.0, 1.0,
            )


@pytest.mark.asyncio
async def test_pg_llm_credentials_cascade_delete_on_tenant(pg_test_pool):
    """Deleting a tenant must cascade-delete its LLM credential rows."""
    async with pg_test_pool.acquire() as conn:
        # Create a throwaway tenant + an account on it.
        await conn.execute(
            "INSERT INTO tenants (id, name) VALUES ($1, $2) "
            "ON CONFLICT (id) DO NOTHING",
            "t-cascade-llm", "Cascade Test LLM",
        )
        await conn.execute(
            "INSERT INTO llm_credentials (id, tenant_id, provider, "
            "created_at, updated_at) "
            "VALUES ($1, $2, $3, $4, $5)",
            "lc-cascade-1", "t-cascade-llm", "openai", 1.0, 1.0,
        )
        await conn.execute(
            "DELETE FROM tenants WHERE id = $1", "t-cascade-llm",
        )
        row = await conn.fetchrow(
            "SELECT id FROM llm_credentials WHERE id = $1",
            "lc-cascade-1",
        )
    assert row is None, (
        "llm_credentials row survived tenant deletion — FK cascade "
        "is not configured correctly."
    )


@pytest.mark.asyncio
async def test_pg_llm_credentials_metadata_roundtrip(pg_test_pool):
    """JSONB round-trip through asyncpg — the row 5b-2 resolver and
    row 5b-3 CRUD both marshal Python dicts through ``metadata``
    (``base_url`` / ``org_id`` / ``scopes``). Lock the shape today
    so an accidental TEXT-cast doesn't regress silently."""
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM llm_credentials WHERE id = $1",
            "lc-metadata-probe",
        )
        payload = (
            '{"base_url": "https://openai-gateway.internal/v1",'
            ' "org_id": "org-abc123",'
            ' "scopes": ["model.chat"],'
            ' "notes": "test row"}'
        )
        await conn.execute(
            "INSERT INTO llm_credentials (id, tenant_id, provider, "
            "metadata, created_at, updated_at) "
            "VALUES ($1, $2, $3, $4::jsonb, $5, $6)",
            "lc-metadata-probe", "t-default", "openai",
            payload, 1.0, 1.0,
        )
        row = await conn.fetchrow(
            "SELECT metadata->>'base_url' AS base_url, "
            "metadata->>'org_id' AS org_id "
            "FROM llm_credentials WHERE id = $1",
            "lc-metadata-probe",
        )
        assert row is not None
        assert row["base_url"] == "https://openai-gateway.internal/v1"
        assert row["org_id"] == "org-abc123"
        # Cleanup.
        await conn.execute(
            "DELETE FROM llm_credentials WHERE id = $1",
            "lc-metadata-probe",
        )
