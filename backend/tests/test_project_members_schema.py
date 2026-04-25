"""Y1 row 3 (#277) — project_members schema tests.

Mirrors the structure of ``test_user_tenant_memberships_schema.py`` and
``test_projects_schema.py``: PG fixtures exercise the alembic-applied
schema; pure-SQLite cases exercise the ``_SCHEMA`` bootstrap path that
fresh dev DBs go through; and a revision-chain unit test pins the
migration file.

The tests cover the exact contract from the TODO row:
``(user_id, project_id, role, created_at)`` + role enum
``owner / contributor / viewer`` + the implicit "missing row ⇒
tenant-level role default" semantic (the latter is application-level
in Y3 — at the schema layer we only verify a missing row does NOT
auto-grant access, i.e. there is no row, full stop).
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest


EXPECTED_ROLES = {"owner", "contributor", "viewer"}
EXPECTED_COLUMNS = {
    "user_id",
    "project_id",
    "role",
    "created_at",
}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  PG-side: alembic-applied schema sanity
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.asyncio
async def test_pg_table_exists(pg_test_pool):
    async with pg_test_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_name = 'project_members'"
        )
    assert row is not None


@pytest.mark.asyncio
async def test_pg_table_has_expected_columns(pg_test_pool):
    async with pg_test_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'project_members'"
        )
    cols = {r["column_name"] for r in rows}
    missing = EXPECTED_COLUMNS - cols
    extra = cols - EXPECTED_COLUMNS
    assert not missing, f"missing columns: {missing}"
    assert not extra, f"unexpected columns: {extra}"


@pytest.mark.asyncio
async def test_pg_primary_key_is_user_project_pair(pg_test_pool):
    """Composite PK ``(user_id, project_id)`` doubles as the implicit
    UNIQUE that prevents two role rows for the same (user, project)."""
    async with pg_test_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT a.attname AS col
            FROM pg_index i
            JOIN pg_attribute a
              ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
            WHERE i.indrelid = 'project_members'::regclass
              AND i.indisprimary
            """
        )
    pk_cols = {r["col"] for r in rows}
    assert pk_cols == {"user_id", "project_id"}


@pytest.mark.asyncio
async def test_pg_indexes_present(pg_test_pool):
    """Reverse fan-out index ``idx_project_members_project`` must
    exist so "list every member of project X" doesn't full-scan."""
    async with pg_test_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename = 'project_members'"
        )
    names = {r["indexname"] for r in rows}
    assert "idx_project_members_project" in names


async def _seed_user_project(conn, suffix):
    """Helper: create one tenant, user and project for a test scope."""
    tid = f"t-pm-{suffix}-{os.urandom(3).hex()}"
    uid = f"u-pm-{suffix}-{os.urandom(3).hex()}"
    pid = f"p-pm-{suffix}-{os.urandom(3).hex()}"
    await conn.execute(
        "INSERT INTO tenants (id, name) VALUES ($1, $2) "
        "ON CONFLICT (id) DO NOTHING",
        tid, f"PM-{suffix}",
    )
    await conn.execute(
        "INSERT INTO users (id, email, name, role, password_hash, "
        "tenant_id) VALUES ($1, $2, $3, $4, $5, $6) "
        "ON CONFLICT (id) DO NOTHING",
        uid, f"{uid}@t.com", f"PM-{suffix}", "viewer", "h", tid,
    )
    await conn.execute(
        "INSERT INTO projects (id, tenant_id, name, slug) "
        "VALUES ($1, $2, $3, $4) "
        "ON CONFLICT (id) DO NOTHING",
        pid, tid, f"PM-{suffix}", f"pm-{suffix}",
    )
    return tid, uid, pid


@pytest.mark.asyncio
async def test_pg_default_role_is_viewer(pg_test_pool):
    """Inserting only the required columns sets sensible defaults:
    role='viewer' (least-privilege), created_at populated."""
    async with pg_test_pool.acquire() as conn:
        _, uid, pid = await _seed_user_project(conn, "def")
        await conn.execute(
            "INSERT INTO project_members (user_id, project_id) "
            "VALUES ($1, $2)",
            uid, pid,
        )
        row = await conn.fetchrow(
            "SELECT role, created_at FROM project_members "
            "WHERE user_id = $1 AND project_id = $2",
            uid, pid,
        )
    assert row["role"] == "viewer"
    assert row["created_at"] is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_role", ["wizard", "OWNER", "", "admin", "member"])
async def test_pg_role_check_rejects_invalid(pg_test_pool, bad_role):
    """``admin`` and ``member`` are deliberately not valid project-level
    roles — they belong to the tenant-level enum.  The DB CHECK
    prevents accidentally storing them on this table."""
    async with pg_test_pool.acquire() as conn:
        _, uid, pid = await _seed_user_project(conn, "bad")
        with pytest.raises(Exception):  # asyncpg.CheckViolationError
            await conn.execute(
                "INSERT INTO project_members (user_id, project_id, role) "
                "VALUES ($1, $2, $3)",
                uid, pid, bad_role,
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("role", sorted(EXPECTED_ROLES))
async def test_pg_role_check_accepts_known(pg_test_pool, role):
    async with pg_test_pool.acquire() as conn:
        _, uid, pid = await _seed_user_project(conn, f"ok-{role}")
        await conn.execute(
            "INSERT INTO project_members (user_id, project_id, role) "
            "VALUES ($1, $2, $3)",
            uid, pid, role,
        )
        row = await conn.fetchrow(
            "SELECT role FROM project_members "
            "WHERE user_id = $1 AND project_id = $2",
            uid, pid,
        )
    assert row["role"] == role


@pytest.mark.asyncio
async def test_pg_user_project_unique(pg_test_pool):
    """Inserting the same ``(user_id, project_id)`` twice must fail.
    Composite PK doubles as the implicit UNIQUE — one role per pair."""
    async with pg_test_pool.acquire() as conn:
        _, uid, pid = await _seed_user_project(conn, "uni")
        await conn.execute(
            "INSERT INTO project_members (user_id, project_id, role) "
            "VALUES ($1, $2, 'owner')",
            uid, pid,
        )
        with pytest.raises(Exception):  # UniqueViolationError
            await conn.execute(
                "INSERT INTO project_members (user_id, project_id, role) "
                "VALUES ($1, $2, 'viewer')",
                uid, pid,
            )


@pytest.mark.asyncio
async def test_pg_user_cascade_delete(pg_test_pool):
    """Deleting the user removes its project memberships
    (FK ON DELETE CASCADE)."""
    async with pg_test_pool.acquire() as conn:
        _, uid, pid = await _seed_user_project(conn, "ucas")
        await conn.execute(
            "INSERT INTO project_members (user_id, project_id, role) "
            "VALUES ($1, $2, 'contributor')",
            uid, pid,
        )
        # Sanity: row exists.
        row = await conn.fetchrow(
            "SELECT 1 FROM project_members "
            "WHERE user_id = $1 AND project_id = $2",
            uid, pid,
        )
        assert row is not None
        await conn.execute("DELETE FROM users WHERE id = $1", uid)
        row = await conn.fetchrow(
            "SELECT 1 FROM project_members WHERE user_id = $1",
            uid,
        )
    assert row is None


@pytest.mark.asyncio
async def test_pg_project_cascade_delete(pg_test_pool):
    """Deleting the project removes all of its memberships
    (FK ON DELETE CASCADE).  Note: hard-deleting projects is policy-
    discouraged in favour of archive (set archived_at), but the rare
    rollback path must clean up cleanly."""
    async with pg_test_pool.acquire() as conn:
        _, uid, pid = await _seed_user_project(conn, "pcas")
        await conn.execute(
            "INSERT INTO project_members (user_id, project_id, role) "
            "VALUES ($1, $2, 'owner')",
            uid, pid,
        )
        await conn.execute("DELETE FROM projects WHERE id = $1", pid)
        row = await conn.fetchrow(
            "SELECT 1 FROM project_members WHERE project_id = $1",
            pid,
        )
    assert row is None


@pytest.mark.asyncio
async def test_pg_tenant_cascade_delete_via_project(pg_test_pool):
    """Deleting the tenant cascades through projects → project_members
    (two hops of CASCADE).  This is the common "tenant offboarding"
    cleanup path and must not leave orphan rows."""
    async with pg_test_pool.acquire() as conn:
        tid, uid, pid = await _seed_user_project(conn, "tcas")
        await conn.execute(
            "INSERT INTO project_members (user_id, project_id, role) "
            "VALUES ($1, $2, 'owner')",
            uid, pid,
        )
        await conn.execute("DELETE FROM tenants WHERE id = $1", tid)
        row = await conn.fetchrow(
            "SELECT 1 FROM project_members WHERE project_id = $1",
            pid,
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
    db_path = tmp_path / "project_members_probe.db"
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
        "WHERE type='table' AND name='project_members'"
    ) as cur:
        row = await cur.fetchone()
    assert row is not None


@pytest.mark.asyncio
async def test_sqlite_columns_match_contract(_fresh_sqlite_db):
    conn = _fresh_sqlite_db
    async with conn.execute("PRAGMA table_info(project_members)") as cur:
        rows = await cur.fetchall()
    cols = {r[1] for r in rows}
    assert EXPECTED_COLUMNS == cols, (
        f"missing: {EXPECTED_COLUMNS - cols}; extra: {cols - EXPECTED_COLUMNS}"
    )


@pytest.mark.asyncio
async def test_sqlite_primary_key_is_user_project_pair(_fresh_sqlite_db):
    conn = _fresh_sqlite_db
    async with conn.execute("PRAGMA table_info(project_members)") as cur:
        rows = await cur.fetchall()
    pk_cols = {r[1] for r in rows if r[5] > 0}
    assert pk_cols == {"user_id", "project_id"}


async def _sl_seed(conn, suffix):
    """SQLite-side seed helper. Uses INSERT OR IGNORE for idempotency."""
    tid = f"t-sl-{suffix}"
    uid = f"u-sl-{suffix}"
    pid = f"p-sl-{suffix}"
    await conn.execute(
        "INSERT OR IGNORE INTO tenants (id, name, plan) "
        f"VALUES ('{tid}', 'SL', 'free')"
    )
    await conn.execute(
        "INSERT OR IGNORE INTO users (id, email, name, role, "
        f"password_hash, tenant_id) VALUES "
        f"('{uid}', '{uid}@t.com', 'SL', 'viewer', 'h', '{tid}')"
    )
    await conn.execute(
        "INSERT OR IGNORE INTO projects (id, tenant_id, name, slug) "
        f"VALUES ('{pid}', '{tid}', 'P', 'p-{suffix}')"
    )
    return tid, uid, pid


@pytest.mark.asyncio
async def test_sqlite_default_role_is_viewer(_fresh_sqlite_db):
    conn = _fresh_sqlite_db
    _, uid, pid = await _sl_seed(conn, "def")
    await conn.execute(
        "INSERT INTO project_members (user_id, project_id) "
        f"VALUES ('{uid}', '{pid}')"
    )
    await conn.commit()
    async with conn.execute(
        "SELECT role, created_at FROM project_members "
        f"WHERE user_id = '{uid}' AND project_id = '{pid}'"
    ) as cur:
        row = await cur.fetchone()
    assert row[0] == "viewer"
    assert row[1] is not None


@pytest.mark.asyncio
async def test_sqlite_role_check_rejects_invalid(_fresh_sqlite_db):
    """CHECK on role enforced at DB level even on the SQLite dev path —
    catches application-side regressions before they hit prod.  In
    particular ``admin`` and ``member`` are tenant-level roles and
    must NOT be storable on this table."""
    conn = _fresh_sqlite_db
    _, uid, pid = await _sl_seed(conn, "rolebad")
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        await conn.execute(
            "INSERT INTO project_members (user_id, project_id, role) "
            f"VALUES ('{uid}', '{pid}', 'admin')"
        )


@pytest.mark.asyncio
async def test_sqlite_user_project_unique(_fresh_sqlite_db):
    conn = _fresh_sqlite_db
    _, uid, pid = await _sl_seed(conn, "uni")
    await conn.execute(
        "INSERT INTO project_members (user_id, project_id, role) "
        f"VALUES ('{uid}', '{pid}', 'owner')"
    )
    await conn.commit()
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        await conn.execute(
            "INSERT INTO project_members (user_id, project_id, role) "
            f"VALUES ('{uid}', '{pid}', 'viewer')"
        )


@pytest.mark.asyncio
async def test_sqlite_user_cascade_delete(_fresh_sqlite_db):
    """SQLite enforces FK only when ``PRAGMA foreign_keys=ON`` —
    db.init sets it on init.  Cascade deletes the membership when the
    parent user is removed."""
    conn = _fresh_sqlite_db
    _, uid, pid = await _sl_seed(conn, "ucas")
    await conn.execute(
        "INSERT INTO project_members (user_id, project_id, role) "
        f"VALUES ('{uid}', '{pid}', 'contributor')"
    )
    await conn.commit()
    await conn.execute(f"DELETE FROM users WHERE id = '{uid}'")
    await conn.commit()
    async with conn.execute(
        f"SELECT 1 FROM project_members WHERE user_id = '{uid}'"
    ) as cur:
        row = await cur.fetchone()
    assert row is None


@pytest.mark.asyncio
async def test_sqlite_project_cascade_delete(_fresh_sqlite_db):
    conn = _fresh_sqlite_db
    _, uid, pid = await _sl_seed(conn, "pcas")
    await conn.execute(
        "INSERT INTO project_members (user_id, project_id, role) "
        f"VALUES ('{uid}', '{pid}', 'owner')"
    )
    await conn.commit()
    await conn.execute(f"DELETE FROM projects WHERE id = '{pid}'")
    await conn.commit()
    async with conn.execute(
        f"SELECT 1 FROM project_members WHERE project_id = '{pid}'"
    ) as cur:
        row = await cur.fetchone()
    assert row is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Migration file sanity (revision chain)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_MIGRATION = (
    Path(__file__).resolve().parent.parent
    / "alembic" / "versions" / "0034_project_members.py"
)


def test_migration_0034_file_exists():
    assert _MIGRATION.exists(), str(_MIGRATION)


def test_migration_0034_revision_chain():
    spec = importlib.util.spec_from_file_location("m0034", str(_MIGRATION))
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert m.revision == "0034"
    assert m.down_revision == "0033"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Migrator coverage (drift guard hand-off)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _load_migrator():
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
    return mig


def test_migrator_lists_project_members():
    """The SQLite→PG migrator must replay the new table.  The
    ``test_migrator_schema_coverage`` drift guard would also catch
    this, but the explicit assertion here makes the contract
    visible at the point the new table is added."""
    mig = _load_migrator()
    assert "project_members" in mig.TABLES_IN_ORDER
    # Composite PK is TEXT — must NOT be in the identity-reset list
    # (would crash sequence reset).
    assert "project_members" not in mig.TABLES_WITH_IDENTITY_ID


def test_migrator_orders_project_members_after_users_and_projects():
    """``project_members.user_id → users(id)`` and
    ``project_members.project_id → projects(id)`` mean replay order
    must put both parents first."""
    mig = _load_migrator()
    order = mig.TABLES_IN_ORDER
    assert order.index("project_members") > order.index("users")
    assert order.index("project_members") > order.index("projects")
