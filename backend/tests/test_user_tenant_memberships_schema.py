"""Y1 row 1 (#277) — user_tenant_memberships schema tests.

Mirrors the structure of ``test_tenants_schema.py``: PG fixtures
exercise the alembic-applied schema; pure-SQLite cases exercise the
``_SCHEMA`` bootstrap path that fresh dev DBs go through; and a
revision-chain unit test pins the migration file.

The tests cover the exact contract from the TODO row:
``(user_id, tenant_id, role, status, created_at, last_active_at)`` +
UNIQUE ``(user_id, tenant_id)`` + role/status enum domains.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest


DEFAULT_TENANT = "t-default"
EXPECTED_ROLES = {"owner", "admin", "member", "viewer"}
EXPECTED_STATUSES = {"active", "suspended"}
EXPECTED_COLUMNS = {
    "user_id",
    "tenant_id",
    "role",
    "status",
    "created_at",
    "last_active_at",
}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  PG-side: alembic-applied schema sanity
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.asyncio
async def test_pg_table_exists(pg_test_pool):
    async with pg_test_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_name = 'user_tenant_memberships'"
        )
    assert row is not None


@pytest.mark.asyncio
async def test_pg_table_has_expected_columns(pg_test_pool):
    async with pg_test_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'user_tenant_memberships'"
        )
    cols = {r["column_name"] for r in rows}
    missing = EXPECTED_COLUMNS - cols
    assert not missing, f"missing columns: {missing}"


@pytest.mark.asyncio
async def test_pg_primary_key_is_user_tenant_pair(pg_test_pool):
    """Composite PK ``(user_id, tenant_id)`` doubles as the
    UNIQUE ``(user_id, tenant_id)`` constraint required by the TODO."""
    async with pg_test_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT a.attname AS col
            FROM pg_index i
            JOIN pg_attribute a
              ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
            WHERE i.indrelid = 'user_tenant_memberships'::regclass
              AND i.indisprimary
            """
        )
    pk_cols = {r["col"] for r in rows}
    assert pk_cols == {"user_id", "tenant_id"}


@pytest.mark.asyncio
async def test_pg_indexes_present(pg_test_pool):
    """Both fan-out indexes + the partial active-status index exist
    so the planner has the choices the routes will rely on."""
    async with pg_test_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename = 'user_tenant_memberships'"
        )
    names = {r["indexname"] for r in rows}
    assert "idx_user_tenant_memberships_user" in names
    assert "idx_user_tenant_memberships_tenant" in names
    assert "idx_user_tenant_memberships_active" in names


@pytest.mark.asyncio
async def test_pg_membership_insert_default_role_status(pg_test_pool):
    async with pg_test_pool.acquire() as conn:
        uid = f"u-mem-{os.urandom(3).hex()}"
        tid = f"t-mem-{os.urandom(3).hex()}"
        await conn.execute(
            "INSERT INTO tenants (id, name) VALUES ($1, $2) "
            "ON CONFLICT (id) DO NOTHING",
            tid, "Mem Tenant",
        )
        await conn.execute(
            "INSERT INTO users (id, email, name, role, password_hash, "
            "tenant_id) VALUES ($1, $2, $3, $4, $5, $6) "
            "ON CONFLICT (id) DO NOTHING",
            uid, f"{uid}@t.com", "Mem", "viewer", "h", tid,
        )
        await conn.execute(
            "INSERT INTO user_tenant_memberships (user_id, tenant_id) "
            "VALUES ($1, $2)",
            uid, tid,
        )
        row = await conn.fetchrow(
            "SELECT role, status, created_at, last_active_at "
            "FROM user_tenant_memberships "
            "WHERE user_id = $1 AND tenant_id = $2",
            uid, tid,
        )
    assert row["role"] == "member"
    assert row["status"] == "active"
    assert row["created_at"] is not None
    assert row["last_active_at"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_role", ["wizard", "ADMIN", "", "guest"])
async def test_pg_role_check_rejects_invalid(pg_test_pool, bad_role):
    async with pg_test_pool.acquire() as conn:
        uid = f"u-bad-{os.urandom(3).hex()}"
        tid = f"t-bad-{os.urandom(3).hex()}"
        await conn.execute(
            "INSERT INTO tenants (id, name) VALUES ($1, $2) "
            "ON CONFLICT (id) DO NOTHING",
            tid, "Bad",
        )
        await conn.execute(
            "INSERT INTO users (id, email, name, role, password_hash, "
            "tenant_id) VALUES ($1, $2, $3, $4, $5, $6) "
            "ON CONFLICT (id) DO NOTHING",
            uid, f"{uid}@t.com", "Bad", "viewer", "h", tid,
        )
        with pytest.raises(Exception):  # asyncpg.CheckViolationError
            await conn.execute(
                "INSERT INTO user_tenant_memberships "
                "(user_id, tenant_id, role) VALUES ($1, $2, $3)",
                uid, tid, bad_role,
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_status", ["banned", "ACTIVE", "", "pending"])
async def test_pg_status_check_rejects_invalid(pg_test_pool, bad_status):
    async with pg_test_pool.acquire() as conn:
        uid = f"u-stat-{os.urandom(3).hex()}"
        tid = f"t-stat-{os.urandom(3).hex()}"
        await conn.execute(
            "INSERT INTO tenants (id, name) VALUES ($1, $2) "
            "ON CONFLICT (id) DO NOTHING",
            tid, "Stat",
        )
        await conn.execute(
            "INSERT INTO users (id, email, name, role, password_hash, "
            "tenant_id) VALUES ($1, $2, $3, $4, $5, $6) "
            "ON CONFLICT (id) DO NOTHING",
            uid, f"{uid}@t.com", "Stat", "viewer", "h", tid,
        )
        with pytest.raises(Exception):
            await conn.execute(
                "INSERT INTO user_tenant_memberships "
                "(user_id, tenant_id, role, status) VALUES ($1, $2, $3, $4)",
                uid, tid, "member", bad_status,
            )


@pytest.mark.asyncio
async def test_pg_user_tenant_unique(pg_test_pool):
    """Inserting the same ``(user_id, tenant_id)`` twice must fail.
    PK doubles as the UNIQUE constraint required by the TODO."""
    async with pg_test_pool.acquire() as conn:
        uid = f"u-uni-{os.urandom(3).hex()}"
        tid = f"t-uni-{os.urandom(3).hex()}"
        await conn.execute(
            "INSERT INTO tenants (id, name) VALUES ($1, $2) "
            "ON CONFLICT (id) DO NOTHING",
            tid, "Uni",
        )
        await conn.execute(
            "INSERT INTO users (id, email, name, role, password_hash, "
            "tenant_id) VALUES ($1, $2, $3, $4, $5, $6) "
            "ON CONFLICT (id) DO NOTHING",
            uid, f"{uid}@t.com", "Uni", "viewer", "h", tid,
        )
        await conn.execute(
            "INSERT INTO user_tenant_memberships (user_id, tenant_id, role) "
            "VALUES ($1, $2, 'admin')",
            uid, tid,
        )
        with pytest.raises(Exception):  # UniqueViolationError
            await conn.execute(
                "INSERT INTO user_tenant_memberships "
                "(user_id, tenant_id, role) VALUES ($1, $2, 'member')",
                uid, tid,
            )


@pytest.mark.asyncio
async def test_pg_user_cascade_delete(pg_test_pool):
    """Deleting the user removes its memberships (FK ON DELETE CASCADE)."""
    async with pg_test_pool.acquire() as conn:
        uid = f"u-cas-{os.urandom(3).hex()}"
        tid = f"t-cas-{os.urandom(3).hex()}"
        await conn.execute(
            "INSERT INTO tenants (id, name) VALUES ($1, $2) "
            "ON CONFLICT (id) DO NOTHING",
            tid, "Cas",
        )
        await conn.execute(
            "INSERT INTO users (id, email, name, role, password_hash, "
            "tenant_id) VALUES ($1, $2, $3, $4, $5, $6) "
            "ON CONFLICT (id) DO NOTHING",
            uid, f"{uid}@t.com", "Cas", "viewer", "h", tid,
        )
        await conn.execute(
            "INSERT INTO user_tenant_memberships (user_id, tenant_id) "
            "VALUES ($1, $2)",
            uid, tid,
        )
        # Sanity: row exists.
        row = await conn.fetchrow(
            "SELECT 1 FROM user_tenant_memberships "
            "WHERE user_id = $1 AND tenant_id = $2",
            uid, tid,
        )
        assert row is not None
        await conn.execute("DELETE FROM users WHERE id = $1", uid)
        row = await conn.fetchrow(
            "SELECT 1 FROM user_tenant_memberships WHERE user_id = $1",
            uid,
        )
    assert row is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SQLite-side: _SCHEMA bootstrap mirrors the alembic table 1:1
#  (so fresh dev DBs are not silently missing the table)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.fixture
async def _fresh_sqlite_db(tmp_path):
    """Boot a clean SQLite via ``backend.db.init`` so we exercise the
    same _SCHEMA + _migrate path production dev-mode goes through."""
    db_path = tmp_path / "membership_probe.db"
    os.environ["OMNISIGHT_DATABASE_PATH"] = str(db_path)
    from backend import config as _cfg
    _cfg.settings.database_path = str(db_path)
    from backend import db
    db._DB_PATH = db._resolve_db_path()
    await db.init()
    try:
        yield db._conn()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_sqlite_table_exists(_fresh_sqlite_db):
    conn = _fresh_sqlite_db
    async with conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='user_tenant_memberships'"
    ) as cur:
        row = await cur.fetchone()
    assert row is not None


@pytest.mark.asyncio
async def test_sqlite_columns_match_contract(_fresh_sqlite_db):
    conn = _fresh_sqlite_db
    async with conn.execute(
        "PRAGMA table_info(user_tenant_memberships)"
    ) as cur:
        rows = await cur.fetchall()
    cols = {r[1] for r in rows}
    assert EXPECTED_COLUMNS == cols, (
        f"missing: {EXPECTED_COLUMNS - cols}; extra: {cols - EXPECTED_COLUMNS}"
    )


@pytest.mark.asyncio
async def test_sqlite_primary_key_is_user_tenant_pair(_fresh_sqlite_db):
    conn = _fresh_sqlite_db
    async with conn.execute(
        "PRAGMA table_info(user_tenant_memberships)"
    ) as cur:
        rows = await cur.fetchall()
    pk_cols = {r[1] for r in rows if r[5] > 0}
    assert pk_cols == {"user_id", "tenant_id"}


@pytest.mark.asyncio
async def test_sqlite_role_check_rejects_invalid(_fresh_sqlite_db):
    """CHECK on role enforced at DB level even on the SQLite dev path —
    catches application-side regressions before they hit prod."""
    conn = _fresh_sqlite_db
    await conn.execute(
        "INSERT OR IGNORE INTO tenants (id, name, plan) "
        "VALUES ('t-sl-r', 'SL', 'free')"
    )
    await conn.execute(
        "INSERT OR IGNORE INTO users (id, email, name, role, "
        "password_hash, tenant_id) VALUES "
        "('u-sl-r', 'sl-r@t.com', 'SL', 'viewer', 'h', 't-sl-r')"
    )
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        await conn.execute(
            "INSERT INTO user_tenant_memberships (user_id, tenant_id, role) "
            "VALUES ('u-sl-r', 't-sl-r', 'wizard')"
        )


@pytest.mark.asyncio
async def test_sqlite_status_check_rejects_invalid(_fresh_sqlite_db):
    conn = _fresh_sqlite_db
    await conn.execute(
        "INSERT OR IGNORE INTO tenants (id, name, plan) "
        "VALUES ('t-sl-s', 'SL', 'free')"
    )
    await conn.execute(
        "INSERT OR IGNORE INTO users (id, email, name, role, "
        "password_hash, tenant_id) VALUES "
        "('u-sl-s', 'sl-s@t.com', 'SL', 'viewer', 'h', 't-sl-s')"
    )
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        await conn.execute(
            "INSERT INTO user_tenant_memberships "
            "(user_id, tenant_id, role, status) "
            "VALUES ('u-sl-s', 't-sl-s', 'member', 'banned')"
        )


@pytest.mark.asyncio
async def test_sqlite_default_role_and_status(_fresh_sqlite_db):
    conn = _fresh_sqlite_db
    await conn.execute(
        "INSERT OR IGNORE INTO tenants (id, name, plan) "
        "VALUES ('t-sl-d', 'SL', 'free')"
    )
    await conn.execute(
        "INSERT OR IGNORE INTO users (id, email, name, role, "
        "password_hash, tenant_id) VALUES "
        "('u-sl-d', 'sl-d@t.com', 'SL', 'viewer', 'h', 't-sl-d')"
    )
    await conn.execute(
        "INSERT INTO user_tenant_memberships (user_id, tenant_id) "
        "VALUES ('u-sl-d', 't-sl-d')"
    )
    await conn.commit()
    async with conn.execute(
        "SELECT role, status, last_active_at "
        "FROM user_tenant_memberships "
        "WHERE user_id = 'u-sl-d' AND tenant_id = 't-sl-d'"
    ) as cur:
        row = await cur.fetchone()
    assert row[0] == "member"
    assert row[1] == "active"
    assert row[2] is None


@pytest.mark.asyncio
async def test_sqlite_user_cascade_delete(_fresh_sqlite_db):
    """SQLite enforces FK only when ``PRAGMA foreign_keys=ON`` — db.init
    sets it on init.  Cascade deletes the membership when the parent
    user is removed."""
    conn = _fresh_sqlite_db
    await conn.execute(
        "INSERT OR IGNORE INTO tenants (id, name, plan) "
        "VALUES ('t-sl-c', 'SL', 'free')"
    )
    await conn.execute(
        "INSERT OR IGNORE INTO users (id, email, name, role, "
        "password_hash, tenant_id) VALUES "
        "('u-sl-c', 'sl-c@t.com', 'SL', 'viewer', 'h', 't-sl-c')"
    )
    await conn.execute(
        "INSERT INTO user_tenant_memberships (user_id, tenant_id) "
        "VALUES ('u-sl-c', 't-sl-c')"
    )
    await conn.commit()
    await conn.execute("DELETE FROM users WHERE id = 'u-sl-c'")
    await conn.commit()
    async with conn.execute(
        "SELECT 1 FROM user_tenant_memberships WHERE user_id = 'u-sl-c'"
    ) as cur:
        row = await cur.fetchone()
    assert row is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Migration file sanity (revision chain)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_MIGRATION = (
    Path(__file__).resolve().parent.parent
    / "alembic" / "versions" / "0032_user_tenant_memberships.py"
)


def test_migration_0032_file_exists():
    assert _MIGRATION.exists(), str(_MIGRATION)


def test_migration_0032_revision_chain():
    spec = importlib.util.spec_from_file_location("m0032", str(_MIGRATION))
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert m.revision == "0032"
    assert m.down_revision == "0031"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Migrator coverage (drift guard hand-off)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_migrator_lists_user_tenant_memberships():
    """The SQLite→PG migrator must replay the new table.  The
    ``test_migrator_schema_coverage`` drift guard would also catch
    this, but the explicit assertion here makes the contract
    visible at the point the new table is added."""
    import sys as _sys
    spec = importlib.util.spec_from_file_location(
        "migrate_sqlite_to_pg",
        Path(__file__).resolve().parents[2]
        / "scripts" / "migrate_sqlite_to_pg.py",
    )
    assert spec and spec.loader
    mig = importlib.util.module_from_spec(spec)
    # Register before exec so dataclass-decorated members can resolve
    # their forward-referenced types via ``sys.modules`` lookups.
    _sys.modules["migrate_sqlite_to_pg"] = mig
    spec.loader.exec_module(mig)
    assert "user_tenant_memberships" in mig.TABLES_IN_ORDER
    # PK is composite TEXT, not INTEGER IDENTITY — must NOT be in
    # the identity-reset list (would crash sequence reset).
    assert "user_tenant_memberships" not in mig.TABLES_WITH_IDENTITY_ID
