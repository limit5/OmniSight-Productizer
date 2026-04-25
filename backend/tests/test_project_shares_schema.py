"""Y1 row 5 (#277) — project_shares schema tests.

Mirrors the structure of ``test_user_tenant_memberships_schema.py``,
``test_projects_schema.py``, ``test_project_members_schema.py``, and
``test_tenant_invites_schema.py``: PG fixtures exercise the alembic-
applied schema; pure-SQLite cases exercise the ``_SCHEMA`` bootstrap
path that fresh dev DBs go through; and a revision-chain unit test
pins the migration file.

The tests cover the exact contract from the TODO row:
``(project_id, guest_tenant_id, role, granted_by, created_at,
expires_at)`` + the deliberate ``role`` enum restriction
``viewer / contributor`` (cross-tenant guest cannot be ``owner``)
+ the UNIQUE ``(project_id, guest_tenant_id)`` invariant + the
NULLable ``expires_at`` for "permanent" shares.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest


EXPECTED_ROLES = {"viewer", "contributor"}
EXPECTED_COLUMNS = {
    "id",
    "project_id",
    "guest_tenant_id",
    "role",
    "granted_by",
    "created_at",
    "expires_at",
}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  PG-side: alembic-applied schema sanity
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.asyncio
async def test_pg_table_exists(pg_test_pool):
    async with pg_test_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_name = 'project_shares'"
        )
    assert row is not None


@pytest.mark.asyncio
async def test_pg_table_has_expected_columns(pg_test_pool):
    async with pg_test_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'project_shares'"
        )
    cols = {r["column_name"] for r in rows}
    missing = EXPECTED_COLUMNS - cols
    extra = cols - EXPECTED_COLUMNS
    assert not missing, f"missing columns: {missing}"
    assert not extra, f"unexpected columns: {extra}"


@pytest.mark.asyncio
async def test_pg_primary_key_is_id(pg_test_pool):
    async with pg_test_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT a.attname AS col
            FROM pg_index i
            JOIN pg_attribute a
              ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
            WHERE i.indrelid = 'project_shares'::regclass
              AND i.indisprimary
            """
        )
    pk_cols = {r["col"] for r in rows}
    assert pk_cols == {"id"}


@pytest.mark.asyncio
async def test_pg_indexes_present(pg_test_pool):
    """The two explicit indexes from the migration must exist:
    guest-tenant reverse fan-out and the partial expiry-sweep target.
    The UNIQUE ``(project_id, guest_tenant_id)`` materialises a btree
    automatically."""
    async with pg_test_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename = 'project_shares'"
        )
    names = {r["indexname"] for r in rows}
    assert "idx_project_shares_guest_tenant" in names
    assert "idx_project_shares_expiry_sweep" in names


@pytest.mark.asyncio
async def test_pg_unique_project_guest_tenant_pair(pg_test_pool):
    """The UNIQUE composite must exist — without it two concurrent
    role grants on the same (project, guest tenant) would be
    ambiguous (which role wins?)."""
    async with pg_test_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT indexdef FROM pg_indexes
            WHERE tablename = 'project_shares'
              AND indexdef ILIKE '%UNIQUE%'
            """
        )
    defs = " ".join(r["indexdef"].lower() for r in rows)
    assert "project_id" in defs and "guest_tenant_id" in defs


async def _seed_project_and_guest(conn, suffix):
    """Helper: create the owner tenant + project + a separate guest
    tenant + an admin user (the ``granted_by`` source).  Returns
    ``(owner_tid, guest_tid, uid, pid)``."""
    owner_tid = f"t-psh-own-{suffix}-{os.urandom(3).hex()}"
    guest_tid = f"t-psh-gst-{suffix}-{os.urandom(3).hex()}"
    uid = f"u-psh-{suffix}-{os.urandom(3).hex()}"
    pid = f"p-psh-{suffix}-{os.urandom(3).hex()}"
    await conn.execute(
        "INSERT INTO tenants (id, name) VALUES ($1, $2) "
        "ON CONFLICT (id) DO NOTHING",
        owner_tid, f"PSH-OWN-{suffix}",
    )
    await conn.execute(
        "INSERT INTO tenants (id, name) VALUES ($1, $2) "
        "ON CONFLICT (id) DO NOTHING",
        guest_tid, f"PSH-GST-{suffix}",
    )
    await conn.execute(
        "INSERT INTO users (id, email, name, role, password_hash, "
        "tenant_id) VALUES ($1, $2, $3, $4, $5, $6) "
        "ON CONFLICT (id) DO NOTHING",
        uid, f"{uid}@t.com", f"PSH-{suffix}", "admin", "h", owner_tid,
    )
    await conn.execute(
        "INSERT INTO projects (id, tenant_id, name, slug) "
        "VALUES ($1, $2, $3, $4) "
        "ON CONFLICT (id) DO NOTHING",
        pid, owner_tid, f"PSH-{suffix}", f"psh-{suffix}",
    )
    return owner_tid, guest_tid, uid, pid


@pytest.mark.asyncio
async def test_pg_default_role_is_viewer(pg_test_pool):
    """Inserting only the required columns sets sensible defaults:
    role='viewer' (least-privilege), created_at populated,
    expires_at NULL (= permanent share)."""
    async with pg_test_pool.acquire() as conn:
        _, gtid, uid, pid = await _seed_project_and_guest(conn, "def")
        sid = f"psh-{os.urandom(4).hex()}"
        await conn.execute(
            "INSERT INTO project_shares "
            "(id, project_id, guest_tenant_id, granted_by) "
            "VALUES ($1, $2, $3, $4)",
            sid, pid, gtid, uid,
        )
        row = await conn.fetchrow(
            "SELECT role, created_at, expires_at FROM project_shares "
            "WHERE id = $1",
            sid,
        )
    assert row["role"] == "viewer"
    assert row["created_at"] is not None
    assert row["expires_at"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_role", ["owner", "admin", "member", "OWNER", "wizard", ""],
)
async def test_pg_role_check_rejects_invalid(pg_test_pool, bad_role):
    """``owner`` is the most important rejection — guest tenant cannot
    own a project belonging to a different tenant.  ``admin`` and
    ``member`` are tenant-level enums and don't apply to project
    scope.  Empty / wizard catches generic garbage."""
    async with pg_test_pool.acquire() as conn:
        _, gtid, uid, pid = await _seed_project_and_guest(conn, "br")
        sid = f"psh-{os.urandom(4).hex()}"
        with pytest.raises(Exception):  # asyncpg.CheckViolationError
            await conn.execute(
                "INSERT INTO project_shares "
                "(id, project_id, guest_tenant_id, role, granted_by) "
                "VALUES ($1, $2, $3, $4, $5)",
                sid, pid, gtid, bad_role, uid,
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("role", sorted(EXPECTED_ROLES))
async def test_pg_role_check_accepts_known(pg_test_pool, role):
    async with pg_test_pool.acquire() as conn:
        _, gtid, uid, pid = await _seed_project_and_guest(conn, f"ok-{role}")
        sid = f"psh-{os.urandom(4).hex()}"
        await conn.execute(
            "INSERT INTO project_shares "
            "(id, project_id, guest_tenant_id, role, granted_by) "
            "VALUES ($1, $2, $3, $4, $5)",
            sid, pid, gtid, role, uid,
        )
        row = await conn.fetchrow(
            "SELECT role FROM project_shares WHERE id = $1", sid,
        )
    assert row["role"] == role


@pytest.mark.asyncio
async def test_pg_unique_project_guest_pair_rejects_duplicate(pg_test_pool):
    """At most one share row per (project, guest_tenant) pair.  Two
    simultaneous role grants would be ambiguous; a role-change must
    be an explicit UPDATE (or DELETE + INSERT), not silent
    duplication."""
    async with pg_test_pool.acquire() as conn:
        _, gtid, uid, pid = await _seed_project_and_guest(conn, "uni")
        s1 = f"psh-{os.urandom(4).hex()}"
        s2 = f"psh-{os.urandom(4).hex()}"
        await conn.execute(
            "INSERT INTO project_shares "
            "(id, project_id, guest_tenant_id, role, granted_by) "
            "VALUES ($1, $2, $3, 'viewer', $4)",
            s1, pid, gtid, uid,
        )
        with pytest.raises(Exception):  # UniqueViolationError
            await conn.execute(
                "INSERT INTO project_shares "
                "(id, project_id, guest_tenant_id, role, granted_by) "
                "VALUES ($1, $2, $3, 'contributor', $4)",
                s2, pid, gtid, uid,
            )


@pytest.mark.asyncio
async def test_pg_project_cascade_delete(pg_test_pool):
    """Deleting the underlying project removes all of its shares
    (FK ON DELETE CASCADE).  Note: hard-deleting projects is
    policy-discouraged in favour of archive (set archived_at), but
    the rare rollback path must clean up cleanly."""
    async with pg_test_pool.acquire() as conn:
        _, gtid, uid, pid = await _seed_project_and_guest(conn, "pcas")
        sid = f"psh-{os.urandom(4).hex()}"
        await conn.execute(
            "INSERT INTO project_shares "
            "(id, project_id, guest_tenant_id, granted_by) "
            "VALUES ($1, $2, $3, $4)",
            sid, pid, gtid, uid,
        )
        await conn.execute("DELETE FROM projects WHERE id = $1", pid)
        row = await conn.fetchrow(
            "SELECT 1 FROM project_shares WHERE id = $1", sid,
        )
    assert row is None


@pytest.mark.asyncio
async def test_pg_guest_tenant_cascade_delete(pg_test_pool):
    """Deleting the guest tenant removes its shares (FK ON DELETE
    CASCADE).  The share has nothing to grant once the recipient
    tenant is offboarded — keeping the row would leave a dangling
    grant pointing at a tombstoned tenant id."""
    async with pg_test_pool.acquire() as conn:
        _, gtid, uid, pid = await _seed_project_and_guest(conn, "gcas")
        sid = f"psh-{os.urandom(4).hex()}"
        await conn.execute(
            "INSERT INTO project_shares "
            "(id, project_id, guest_tenant_id, granted_by) "
            "VALUES ($1, $2, $3, $4)",
            sid, pid, gtid, uid,
        )
        await conn.execute("DELETE FROM tenants WHERE id = $1", gtid)
        row = await conn.fetchrow(
            "SELECT 1 FROM project_shares WHERE id = $1", sid,
        )
    assert row is None


@pytest.mark.asyncio
async def test_pg_owner_tenant_cascade_via_project(pg_test_pool):
    """Deleting the *owner* tenant cascades through projects →
    project_shares (two CASCADE hops).  Common "tenant offboarding"
    cleanup path; must not leave orphan shares whose project_id
    points at a tombstoned project row."""
    async with pg_test_pool.acquire() as conn:
        otid, gtid, uid, pid = await _seed_project_and_guest(conn, "ocas")
        sid = f"psh-{os.urandom(4).hex()}"
        await conn.execute(
            "INSERT INTO project_shares "
            "(id, project_id, guest_tenant_id, granted_by) "
            "VALUES ($1, $2, $3, $4)",
            sid, pid, gtid, uid,
        )
        # Drop the inviter user first so users.tenant_id FK on the
        # owner tenant clears (mirrors the real offboarding flow).
        await conn.execute("DELETE FROM users WHERE id = $1", uid)
        await conn.execute("DELETE FROM tenants WHERE id = $1", otid)
        row = await conn.fetchrow(
            "SELECT 1 FROM project_shares WHERE id = $1", sid,
        )
    assert row is None


@pytest.mark.asyncio
async def test_pg_granter_set_null_on_delete(pg_test_pool):
    """Deleting the granter must NOT delete the share — otherwise
    rotating an admin silently revokes every share they granted,
    breaking guest access during normal personnel changes."""
    async with pg_test_pool.acquire() as conn:
        _, gtid, uid, pid = await _seed_project_and_guest(conn, "gset")
        sid = f"psh-{os.urandom(4).hex()}"
        await conn.execute(
            "INSERT INTO project_shares "
            "(id, project_id, guest_tenant_id, granted_by) "
            "VALUES ($1, $2, $3, $4)",
            sid, pid, gtid, uid,
        )
        await conn.execute("DELETE FROM users WHERE id = $1", uid)
        row = await conn.fetchrow(
            "SELECT granted_by FROM project_shares WHERE id = $1", sid,
        )
    assert row is not None, "share must survive granter deletion"
    assert row["granted_by"] is None


@pytest.mark.asyncio
async def test_pg_expires_at_nullable_for_permanent_share(pg_test_pool):
    """``expires_at`` NULL is the "permanent share" semantic — distinct
    from ``tenant_invites.expires_at`` which is NOT NULL because invites
    must rot if unused."""
    async with pg_test_pool.acquire() as conn:
        _, gtid, uid, pid = await _seed_project_and_guest(conn, "perm")
        sid = f"psh-{os.urandom(4).hex()}"
        await conn.execute(
            "INSERT INTO project_shares "
            "(id, project_id, guest_tenant_id, granted_by, expires_at) "
            "VALUES ($1, $2, $3, $4, NULL)",
            sid, pid, gtid, uid,
        )
        row = await conn.fetchrow(
            "SELECT expires_at FROM project_shares WHERE id = $1", sid,
        )
    assert row["expires_at"] is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SQLite-side: _SCHEMA bootstrap mirrors the alembic table 1:1
#  (so fresh dev DBs are not silently missing the table)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.fixture
async def _fresh_sqlite_db(tmp_path):
    """Boot a clean SQLite via ``backend.db.init`` so we exercise the
    same _SCHEMA + _migrate path production dev-mode goes through."""
    db_path = tmp_path / "project_shares_probe.db"
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
        "WHERE type='table' AND name='project_shares'"
    ) as cur:
        row = await cur.fetchone()
    assert row is not None


@pytest.mark.asyncio
async def test_sqlite_columns_match_contract(_fresh_sqlite_db):
    conn = _fresh_sqlite_db
    async with conn.execute("PRAGMA table_info(project_shares)") as cur:
        rows = await cur.fetchall()
    cols = {r[1] for r in rows}
    assert EXPECTED_COLUMNS == cols, (
        f"missing: {EXPECTED_COLUMNS - cols}; extra: {cols - EXPECTED_COLUMNS}"
    )


@pytest.mark.asyncio
async def test_sqlite_primary_key_is_id(_fresh_sqlite_db):
    conn = _fresh_sqlite_db
    async with conn.execute("PRAGMA table_info(project_shares)") as cur:
        rows = await cur.fetchall()
    pk_cols = {r[1] for r in rows if r[5] > 0}
    assert pk_cols == {"id"}


async def _sl_seed(conn, suffix):
    """SQLite-side seed helper. Uses INSERT OR IGNORE for idempotency."""
    otid = f"t-sl-psh-own-{suffix}"
    gtid = f"t-sl-psh-gst-{suffix}"
    uid = f"u-sl-psh-{suffix}"
    pid = f"p-sl-psh-{suffix}"
    await conn.execute(
        "INSERT OR IGNORE INTO tenants (id, name, plan) "
        f"VALUES ('{otid}', 'OWN', 'free')"
    )
    await conn.execute(
        "INSERT OR IGNORE INTO tenants (id, name, plan) "
        f"VALUES ('{gtid}', 'GST', 'free')"
    )
    await conn.execute(
        "INSERT OR IGNORE INTO users (id, email, name, role, "
        f"password_hash, tenant_id) VALUES "
        f"('{uid}', '{uid}@t.com', 'PSH', 'admin', 'h', '{otid}')"
    )
    await conn.execute(
        "INSERT OR IGNORE INTO projects (id, tenant_id, name, slug) "
        f"VALUES ('{pid}', '{otid}', 'P', 'p-{suffix}')"
    )
    return otid, gtid, uid, pid


@pytest.mark.asyncio
async def test_sqlite_default_role_is_viewer(_fresh_sqlite_db):
    conn = _fresh_sqlite_db
    _, gtid, uid, pid = await _sl_seed(conn, "def")
    sid = "psh-sldef"
    await conn.execute(
        "INSERT INTO project_shares "
        "(id, project_id, guest_tenant_id, granted_by) "
        f"VALUES ('{sid}', '{pid}', '{gtid}', '{uid}')"
    )
    await conn.commit()
    async with conn.execute(
        "SELECT role, created_at, expires_at FROM project_shares "
        f"WHERE id = '{sid}'"
    ) as cur:
        row = await cur.fetchone()
    assert row[0] == "viewer"
    assert row[1] is not None
    assert row[2] is None


@pytest.mark.asyncio
async def test_sqlite_role_check_rejects_owner(_fresh_sqlite_db):
    """``owner`` is the most important rejection: cross-tenant guest
    cannot own a project of another tenant.  Must be enforced even
    on the dev SQLite path so a regression can't slip through local
    dev with green tests."""
    conn = _fresh_sqlite_db
    _, gtid, uid, pid = await _sl_seed(conn, "ownbad")
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        await conn.execute(
            "INSERT INTO project_shares "
            "(id, project_id, guest_tenant_id, role, granted_by) "
            f"VALUES ('psh-slown', '{pid}', '{gtid}', 'owner', '{uid}')"
        )


@pytest.mark.asyncio
async def test_sqlite_role_check_rejects_invalid(_fresh_sqlite_db):
    """Generic garbage role rejected — same DB-level enum as PG."""
    conn = _fresh_sqlite_db
    _, gtid, uid, pid = await _sl_seed(conn, "rolebad")
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        await conn.execute(
            "INSERT INTO project_shares "
            "(id, project_id, guest_tenant_id, role, granted_by) "
            f"VALUES ('psh-slbad', '{pid}', '{gtid}', 'wizard', '{uid}')"
        )


@pytest.mark.asyncio
async def test_sqlite_unique_project_guest_pair(_fresh_sqlite_db):
    conn = _fresh_sqlite_db
    _, gtid, uid, pid = await _sl_seed(conn, "uni")
    await conn.execute(
        "INSERT INTO project_shares "
        "(id, project_id, guest_tenant_id, role, granted_by) "
        f"VALUES ('psh-sl1', '{pid}', '{gtid}', 'viewer', '{uid}')"
    )
    await conn.commit()
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        await conn.execute(
            "INSERT INTO project_shares "
            "(id, project_id, guest_tenant_id, role, granted_by) "
            f"VALUES ('psh-sl2', '{pid}', '{gtid}', 'contributor', "
            f"'{uid}')"
        )


@pytest.mark.asyncio
async def test_sqlite_project_cascade_delete(_fresh_sqlite_db):
    """SQLite enforces FK only when ``PRAGMA foreign_keys=ON`` —
    db.init sets it on init.  Deleting the project cascades to its
    shares."""
    conn = _fresh_sqlite_db
    _, gtid, uid, pid = await _sl_seed(conn, "pcas")
    await conn.execute(
        "INSERT INTO project_shares "
        "(id, project_id, guest_tenant_id, granted_by) "
        f"VALUES ('psh-slpcas', '{pid}', '{gtid}', '{uid}')"
    )
    await conn.commit()
    await conn.execute(f"DELETE FROM projects WHERE id = '{pid}'")
    await conn.commit()
    async with conn.execute(
        "SELECT 1 FROM project_shares WHERE id = 'psh-slpcas'"
    ) as cur:
        row = await cur.fetchone()
    assert row is None


@pytest.mark.asyncio
async def test_sqlite_guest_tenant_cascade_delete(_fresh_sqlite_db):
    """Deleting the guest tenant cascades to its shares."""
    conn = _fresh_sqlite_db
    _, gtid, uid, pid = await _sl_seed(conn, "gcas")
    await conn.execute(
        "INSERT INTO project_shares "
        "(id, project_id, guest_tenant_id, granted_by) "
        f"VALUES ('psh-slgcas', '{pid}', '{gtid}', '{uid}')"
    )
    await conn.commit()
    await conn.execute(f"DELETE FROM tenants WHERE id = '{gtid}'")
    await conn.commit()
    async with conn.execute(
        "SELECT 1 FROM project_shares WHERE id = 'psh-slgcas'"
    ) as cur:
        row = await cur.fetchone()
    assert row is None


@pytest.mark.asyncio
async def test_sqlite_granter_set_null_on_delete(_fresh_sqlite_db):
    """Deleting the granter sets ``granted_by`` to NULL but keeps the
    share alive — a rotated admin must not silently revoke every
    share they ever granted."""
    conn = _fresh_sqlite_db
    _, gtid, uid, pid = await _sl_seed(conn, "gset")
    await conn.execute(
        "INSERT INTO project_shares "
        "(id, project_id, guest_tenant_id, granted_by) "
        f"VALUES ('psh-slgset', '{pid}', '{gtid}', '{uid}')"
    )
    await conn.commit()
    await conn.execute(f"DELETE FROM users WHERE id = '{uid}'")
    await conn.commit()
    async with conn.execute(
        "SELECT granted_by FROM project_shares WHERE id = 'psh-slgset'"
    ) as cur:
        row = await cur.fetchone()
    assert row is not None, "share must survive granter deletion"
    assert row[0] is None


@pytest.mark.asyncio
async def test_sqlite_expires_at_nullable(_fresh_sqlite_db):
    """``expires_at`` NULL = permanent share; the column must accept
    NULL (distinct from ``tenant_invites.expires_at`` which is NOT
    NULL)."""
    conn = _fresh_sqlite_db
    _, gtid, uid, pid = await _sl_seed(conn, "perm")
    await conn.execute(
        "INSERT INTO project_shares "
        "(id, project_id, guest_tenant_id, granted_by, expires_at) "
        f"VALUES ('psh-slperm', '{pid}', '{gtid}', '{uid}', NULL)"
    )
    await conn.commit()
    async with conn.execute(
        "SELECT expires_at FROM project_shares WHERE id = 'psh-slperm'"
    ) as cur:
        row = await cur.fetchone()
    assert row[0] is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Migration file sanity (revision chain)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_MIGRATION = (
    Path(__file__).resolve().parent.parent
    / "alembic" / "versions" / "0036_project_shares.py"
)


def test_migration_0036_file_exists():
    assert _MIGRATION.exists(), str(_MIGRATION)


def test_migration_0036_revision_chain():
    spec = importlib.util.spec_from_file_location("m0036", str(_MIGRATION))
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert m.revision == "0036"
    assert m.down_revision == "0035"


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


def test_migrator_lists_project_shares():
    """The SQLite→PG migrator must replay the new table.  The
    ``test_migrator_schema_coverage`` drift guard would also catch
    this, but the explicit assertion here makes the contract
    visible at the point the new table is added."""
    mig = _load_migrator()
    assert "project_shares" in mig.TABLES_IN_ORDER
    # TEXT PK — must NOT be in the identity-reset list (would crash
    # sequence reset since ``psh-*`` is not an INTEGER IDENTITY).
    assert "project_shares" not in mig.TABLES_WITH_IDENTITY_ID


def test_migrator_orders_project_shares_after_parents():
    """``project_shares.project_id → projects(id)`` (CASCADE),
    ``project_shares.guest_tenant_id → tenants(id)`` (CASCADE),
    ``project_shares.granted_by → users(id)`` (SET NULL) mean replay
    order must put all three parents first."""
    mig = _load_migrator()
    order = mig.TABLES_IN_ORDER
    assert order.index("project_shares") > order.index("projects")
    assert order.index("project_shares") > order.index("tenants")
    assert order.index("project_shares") > order.index("users")
