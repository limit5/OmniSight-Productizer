"""Y1 row 2 (#277) — projects schema tests.

Mirrors the structure of ``test_user_tenant_memberships_schema.py``:
PG fixtures exercise the alembic-applied schema; pure-SQLite cases
exercise the ``_SCHEMA`` bootstrap path that fresh dev DBs go through;
and a revision-chain unit test pins the migration file.

The tests cover the exact contract from the TODO row:
``(id, tenant_id, product_line, name, slug, parent_id, plan_override,
disk_budget_bytes, llm_budget_tokens, created_by, archived_at)`` +
UNIQUE ``(tenant_id, product_line, slug)`` + the
"NULL ⇒ inherit tenant" semantic for the three override columns
(modeled at the schema layer as nullable, with the resolver living in
Y2/Y3 application code).
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest


EXPECTED_COLUMNS = {
    "id",
    "tenant_id",
    "product_line",
    "name",
    "slug",
    "parent_id",
    "plan_override",
    "disk_budget_bytes",
    "llm_budget_tokens",
    "created_by",
    "created_at",
    "archived_at",
}
VALID_PLANS = {"free", "starter", "pro", "enterprise"}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  PG-side: alembic-applied schema sanity
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.asyncio
async def test_pg_table_exists(pg_test_pool):
    async with pg_test_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_name = 'projects'"
        )
    assert row is not None


@pytest.mark.asyncio
async def test_pg_table_has_expected_columns(pg_test_pool):
    async with pg_test_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'projects'"
        )
    cols = {r["column_name"] for r in rows}
    missing = EXPECTED_COLUMNS - cols
    assert not missing, f"missing columns: {missing}"


@pytest.mark.asyncio
async def test_pg_primary_key_is_id(pg_test_pool):
    async with pg_test_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT a.attname AS col
            FROM pg_index i
            JOIN pg_attribute a
              ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
            WHERE i.indrelid = 'projects'::regclass
              AND i.indisprimary
            """
        )
    pk_cols = {r["col"] for r in rows}
    assert pk_cols == {"id"}


@pytest.mark.asyncio
async def test_pg_unique_tenant_product_slug(pg_test_pool):
    """Contract from the TODO row: UNIQUE(tenant_id, product_line, slug).
    Verified by attempting a duplicate insert rather than introspecting
    constraint metadata — the behavioural assertion is what callers
    actually rely on."""
    async with pg_test_pool.acquire() as conn:
        tid = f"t-uni-{os.urandom(3).hex()}"
        await conn.execute(
            "INSERT INTO tenants (id, name) VALUES ($1, $2) "
            "ON CONFLICT (id) DO NOTHING",
            tid, "Uni",
        )
        pid_a = f"p-a-{os.urandom(3).hex()}"
        pid_b = f"p-b-{os.urandom(3).hex()}"
        await conn.execute(
            "INSERT INTO projects (id, tenant_id, product_line, name, slug) "
            "VALUES ($1, $2, 'default', 'A', 'isp-tuning')",
            pid_a, tid,
        )
        with pytest.raises(Exception):  # UniqueViolationError
            await conn.execute(
                "INSERT INTO projects (id, tenant_id, product_line, name, slug) "
                "VALUES ($1, $2, 'default', 'A2', 'isp-tuning')",
                pid_b, tid,
            )


@pytest.mark.asyncio
async def test_pg_unique_allows_same_slug_different_product_line(pg_test_pool):
    """Two rows with the same tenant + slug but different
    ``product_line`` must coexist — that's the whole point of making
    ``product_line`` part of the UNIQUE tuple."""
    async with pg_test_pool.acquire() as conn:
        tid = f"t-pl-{os.urandom(3).hex()}"
        await conn.execute(
            "INSERT INTO tenants (id, name) VALUES ($1, $2) "
            "ON CONFLICT (id) DO NOTHING",
            tid, "PL",
        )
        pid_a = f"p-pl-a-{os.urandom(3).hex()}"
        pid_b = f"p-pl-b-{os.urandom(3).hex()}"
        await conn.execute(
            "INSERT INTO projects (id, tenant_id, product_line, name, slug) "
            "VALUES ($1, $2, 'firmware', 'F', 'isp-tuning')",
            pid_a, tid,
        )
        await conn.execute(
            "INSERT INTO projects (id, tenant_id, product_line, name, slug) "
            "VALUES ($1, $2, 'algo', 'A', 'isp-tuning')",
            pid_b, tid,
        )
        # Both inserts succeeded — count is 2.
        row = await conn.fetchrow(
            "SELECT COUNT(*) AS n FROM projects WHERE tenant_id = $1",
            tid,
        )
        assert row["n"] == 2


@pytest.mark.asyncio
async def test_pg_indexes_present(pg_test_pool):
    """Three explicit partial indexes + the UNIQUE composite index
    must all be present.  Test asserts the explicit names; the
    UNIQUE-backing index has an auto-generated name on PG which
    we don't pin."""
    async with pg_test_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename = 'projects'"
        )
    names = {r["indexname"] for r in rows}
    assert "idx_projects_parent" in names
    assert "idx_projects_tenant_active" in names
    assert "idx_projects_created_by" in names


@pytest.mark.asyncio
async def test_pg_insert_minimum_fields_inherit_defaults(pg_test_pool):
    """Inserting only the required columns sets sensible defaults:
    product_line='default', plan_override IS NULL (inherit tenant),
    budgets IS NULL (inherit tenant), created_at populated,
    archived_at IS NULL."""
    async with pg_test_pool.acquire() as conn:
        tid = f"t-def-{os.urandom(3).hex()}"
        await conn.execute(
            "INSERT INTO tenants (id, name) VALUES ($1, $2) "
            "ON CONFLICT (id) DO NOTHING",
            tid, "Def",
        )
        pid = f"p-def-{os.urandom(3).hex()}"
        await conn.execute(
            "INSERT INTO projects (id, tenant_id, name, slug) "
            "VALUES ($1, $2, 'My Project', 'my-proj')",
            pid, tid,
        )
        row = await conn.fetchrow(
            "SELECT product_line, plan_override, disk_budget_bytes, "
            "llm_budget_tokens, created_at, archived_at "
            "FROM projects WHERE id = $1",
            pid,
        )
    assert row["product_line"] == "default"
    assert row["plan_override"] is None
    assert row["disk_budget_bytes"] is None
    assert row["llm_budget_tokens"] is None
    assert row["created_at"] is not None
    assert row["archived_at"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_plan", ["wizard", "FREE", "", "platinum"])
async def test_pg_plan_override_check_rejects_invalid(pg_test_pool, bad_plan):
    async with pg_test_pool.acquire() as conn:
        tid = f"t-plchk-{os.urandom(3).hex()}"
        await conn.execute(
            "INSERT INTO tenants (id, name) VALUES ($1, $2) "
            "ON CONFLICT (id) DO NOTHING",
            tid, "PlChk",
        )
        pid = f"p-plchk-{os.urandom(3).hex()}"
        with pytest.raises(Exception):  # CheckViolationError
            await conn.execute(
                "INSERT INTO projects (id, tenant_id, name, slug, plan_override) "
                "VALUES ($1, $2, 'P', 's', $3)",
                pid, tid, bad_plan,
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("plan", sorted(VALID_PLANS))
async def test_pg_plan_override_check_accepts_known(pg_test_pool, plan):
    async with pg_test_pool.acquire() as conn:
        tid = f"t-plok-{os.urandom(3).hex()}"
        await conn.execute(
            "INSERT INTO tenants (id, name) VALUES ($1, $2) "
            "ON CONFLICT (id) DO NOTHING",
            tid, "PlOk",
        )
        pid = f"p-plok-{plan}-{os.urandom(3).hex()}"
        await conn.execute(
            "INSERT INTO projects (id, tenant_id, name, slug, plan_override) "
            "VALUES ($1, $2, $3, $4, $5)",
            pid, tid, f"P-{plan}", f"slug-{plan}", plan,
        )
        row = await conn.fetchrow(
            "SELECT plan_override FROM projects WHERE id = $1", pid,
        )
    assert row["plan_override"] == plan


@pytest.mark.asyncio
async def test_pg_negative_budgets_rejected(pg_test_pool):
    async with pg_test_pool.acquire() as conn:
        tid = f"t-neg-{os.urandom(3).hex()}"
        await conn.execute(
            "INSERT INTO tenants (id, name) VALUES ($1, $2) "
            "ON CONFLICT (id) DO NOTHING",
            tid, "Neg",
        )
        pid_d = f"p-negd-{os.urandom(3).hex()}"
        with pytest.raises(Exception):  # CheckViolationError
            await conn.execute(
                "INSERT INTO projects (id, tenant_id, name, slug, "
                "disk_budget_bytes) VALUES ($1, $2, 'P', 'sd', $3)",
                pid_d, tid, -1,
            )
        pid_l = f"p-negl-{os.urandom(3).hex()}"
        with pytest.raises(Exception):  # CheckViolationError
            await conn.execute(
                "INSERT INTO projects (id, tenant_id, name, slug, "
                "llm_budget_tokens) VALUES ($1, $2, 'P', 'sl', $3)",
                pid_l, tid, -1,
            )


@pytest.mark.asyncio
async def test_pg_self_parent_rejected(pg_test_pool):
    """``parent_id <> id`` blocks the trivial self-loop.  Deeper
    cycle detection is application-level (Y3 POST /projects)."""
    async with pg_test_pool.acquire() as conn:
        tid = f"t-self-{os.urandom(3).hex()}"
        await conn.execute(
            "INSERT INTO tenants (id, name) VALUES ($1, $2) "
            "ON CONFLICT (id) DO NOTHING",
            tid, "Self",
        )
        pid = f"p-self-{os.urandom(3).hex()}"
        # Insert the row first with parent_id = NULL.
        await conn.execute(
            "INSERT INTO projects (id, tenant_id, name, slug) "
            "VALUES ($1, $2, 'Self', 'self')",
            pid, tid,
        )
        # Updating it to point parent_id at itself must fail.
        with pytest.raises(Exception):  # CheckViolationError
            await conn.execute(
                "UPDATE projects SET parent_id = $1 WHERE id = $1", pid,
            )


@pytest.mark.asyncio
async def test_pg_parent_set_null_on_parent_delete(pg_test_pool):
    """Deleting a parent project promotes its children to top-level
    (ON DELETE SET NULL) — children survive, parent_id becomes NULL.
    Explicitly contrasts with CASCADE which would silently delete
    sub-trees of attached workloads."""
    async with pg_test_pool.acquire() as conn:
        tid = f"t-pdel-{os.urandom(3).hex()}"
        await conn.execute(
            "INSERT INTO tenants (id, name) VALUES ($1, $2) "
            "ON CONFLICT (id) DO NOTHING",
            tid, "PDel",
        )
        pid_p = f"p-pdel-p-{os.urandom(3).hex()}"
        pid_c = f"p-pdel-c-{os.urandom(3).hex()}"
        await conn.execute(
            "INSERT INTO projects (id, tenant_id, name, slug) "
            "VALUES ($1, $2, 'Parent', 'parent')",
            pid_p, tid,
        )
        await conn.execute(
            "INSERT INTO projects (id, tenant_id, name, slug, parent_id) "
            "VALUES ($1, $2, 'Child', 'child', $3)",
            pid_c, tid, pid_p,
        )
        await conn.execute("DELETE FROM projects WHERE id = $1", pid_p)
        row = await conn.fetchrow(
            "SELECT parent_id FROM projects WHERE id = $1", pid_c,
        )
    assert row is not None, "child should survive parent delete"
    assert row["parent_id"] is None, "parent_id should be SET NULL"


@pytest.mark.asyncio
async def test_pg_tenant_cascade_delete(pg_test_pool):
    """Deleting the parent tenant cascades the project away
    (FK ON DELETE CASCADE)."""
    async with pg_test_pool.acquire() as conn:
        tid = f"t-tcas-{os.urandom(3).hex()}"
        await conn.execute(
            "INSERT INTO tenants (id, name) VALUES ($1, $2) "
            "ON CONFLICT (id) DO NOTHING",
            tid, "TCas",
        )
        pid = f"p-tcas-{os.urandom(3).hex()}"
        await conn.execute(
            "INSERT INTO projects (id, tenant_id, name, slug) "
            "VALUES ($1, $2, 'P', 'p')",
            pid, tid,
        )
        await conn.execute("DELETE FROM tenants WHERE id = $1", tid)
        row = await conn.fetchrow(
            "SELECT 1 FROM projects WHERE id = $1", pid,
        )
    assert row is None


@pytest.mark.asyncio
async def test_pg_created_by_set_null_on_user_delete(pg_test_pool):
    """``created_by`` is audit-only; deleting the user should not
    delete projects they created — instead the ref becomes NULL."""
    async with pg_test_pool.acquire() as conn:
        tid = f"t-cb-{os.urandom(3).hex()}"
        await conn.execute(
            "INSERT INTO tenants (id, name) VALUES ($1, $2) "
            "ON CONFLICT (id) DO NOTHING",
            tid, "CB",
        )
        uid = f"u-cb-{os.urandom(3).hex()}"
        await conn.execute(
            "INSERT INTO users (id, email, name, role, password_hash, "
            "tenant_id) VALUES ($1, $2, $3, $4, $5, $6) "
            "ON CONFLICT (id) DO NOTHING",
            uid, f"{uid}@t.com", "CB", "viewer", "h", tid,
        )
        pid = f"p-cb-{os.urandom(3).hex()}"
        await conn.execute(
            "INSERT INTO projects (id, tenant_id, name, slug, created_by) "
            "VALUES ($1, $2, 'P', 'p', $3)",
            pid, tid, uid,
        )
        await conn.execute("DELETE FROM users WHERE id = $1", uid)
        row = await conn.fetchrow(
            "SELECT created_by FROM projects WHERE id = $1", pid,
        )
    assert row is not None, "project should survive creator delete"
    assert row["created_by"] is None


@pytest.mark.asyncio
async def test_pg_length_check_rejects_oversize_slug(pg_test_pool):
    async with pg_test_pool.acquire() as conn:
        tid = f"t-ln-{os.urandom(3).hex()}"
        await conn.execute(
            "INSERT INTO tenants (id, name) VALUES ($1, $2) "
            "ON CONFLICT (id) DO NOTHING",
            tid, "Ln",
        )
        pid = f"p-ln-{os.urandom(3).hex()}"
        with pytest.raises(Exception):  # CheckViolationError on slug
            await conn.execute(
                "INSERT INTO projects (id, tenant_id, name, slug) "
                "VALUES ($1, $2, 'P', $3)",
                pid, tid, "x" * 65,  # 65 > 64 limit
            )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SQLite-side: _SCHEMA bootstrap mirrors the alembic table 1:1
#  (so fresh dev DBs are not silently missing the table)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.fixture
async def _fresh_sqlite_db(tmp_path):
    """Boot a clean SQLite via ``backend.db.init`` so we exercise the
    same _SCHEMA + _migrate path production dev-mode goes through."""
    db_path = tmp_path / "projects_probe.db"
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
        "WHERE type='table' AND name='projects'"
    ) as cur:
        row = await cur.fetchone()
    assert row is not None


@pytest.mark.asyncio
async def test_sqlite_columns_match_contract(_fresh_sqlite_db):
    conn = _fresh_sqlite_db
    async with conn.execute("PRAGMA table_info(projects)") as cur:
        rows = await cur.fetchall()
    cols = {r[1] for r in rows}
    assert EXPECTED_COLUMNS == cols, (
        f"missing: {EXPECTED_COLUMNS - cols}; extra: {cols - EXPECTED_COLUMNS}"
    )


@pytest.mark.asyncio
async def test_sqlite_primary_key_is_id(_fresh_sqlite_db):
    conn = _fresh_sqlite_db
    async with conn.execute("PRAGMA table_info(projects)") as cur:
        rows = await cur.fetchall()
    pk_cols = {r[1] for r in rows if r[5] > 0}
    assert pk_cols == {"id"}


@pytest.mark.asyncio
async def test_sqlite_default_product_line_and_nulls(_fresh_sqlite_db):
    conn = _fresh_sqlite_db
    await conn.execute(
        "INSERT OR IGNORE INTO tenants (id, name, plan) "
        "VALUES ('t-sl-d', 'SL', 'free')"
    )
    await conn.execute(
        "INSERT INTO projects (id, tenant_id, name, slug) "
        "VALUES ('p-sl-d', 't-sl-d', 'P', 'p')"
    )
    await conn.commit()
    async with conn.execute(
        "SELECT product_line, plan_override, disk_budget_bytes, "
        "llm_budget_tokens, archived_at "
        "FROM projects WHERE id = 'p-sl-d'"
    ) as cur:
        row = await cur.fetchone()
    assert row[0] == "default"
    assert row[1] is None
    assert row[2] is None
    assert row[3] is None
    assert row[4] is None


@pytest.mark.asyncio
async def test_sqlite_unique_tenant_product_slug(_fresh_sqlite_db):
    conn = _fresh_sqlite_db
    await conn.execute(
        "INSERT OR IGNORE INTO tenants (id, name, plan) "
        "VALUES ('t-sl-u', 'SL', 'free')"
    )
    await conn.execute(
        "INSERT INTO projects (id, tenant_id, name, slug) "
        "VALUES ('p-sl-u1', 't-sl-u', 'A', 'isp')"
    )
    await conn.commit()
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        await conn.execute(
            "INSERT INTO projects (id, tenant_id, name, slug) "
            "VALUES ('p-sl-u2', 't-sl-u', 'A2', 'isp')"
        )


@pytest.mark.asyncio
async def test_sqlite_plan_override_check_rejects_invalid(_fresh_sqlite_db):
    conn = _fresh_sqlite_db
    await conn.execute(
        "INSERT OR IGNORE INTO tenants (id, name, plan) "
        "VALUES ('t-sl-pl', 'SL', 'free')"
    )
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        await conn.execute(
            "INSERT INTO projects (id, tenant_id, name, slug, plan_override) "
            "VALUES ('p-sl-pl', 't-sl-pl', 'P', 'p', 'platinum')"
        )


@pytest.mark.asyncio
async def test_sqlite_self_parent_rejected(_fresh_sqlite_db):
    conn = _fresh_sqlite_db
    await conn.execute(
        "INSERT OR IGNORE INTO tenants (id, name, plan) "
        "VALUES ('t-sl-sp', 'SL', 'free')"
    )
    await conn.execute(
        "INSERT INTO projects (id, tenant_id, name, slug) "
        "VALUES ('p-sl-sp', 't-sl-sp', 'P', 'p')"
    )
    await conn.commit()
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        await conn.execute(
            "UPDATE projects SET parent_id = 'p-sl-sp' WHERE id = 'p-sl-sp'"
        )


@pytest.mark.asyncio
async def test_sqlite_tenant_cascade_delete(_fresh_sqlite_db):
    """SQLite enforces FK only when ``PRAGMA foreign_keys=ON`` —
    db.init sets it on init.  Cascade deletes the project when the
    parent tenant is removed."""
    conn = _fresh_sqlite_db
    await conn.execute(
        "INSERT OR IGNORE INTO tenants (id, name, plan) "
        "VALUES ('t-sl-tc', 'SL', 'free')"
    )
    await conn.execute(
        "INSERT INTO projects (id, tenant_id, name, slug) "
        "VALUES ('p-sl-tc', 't-sl-tc', 'P', 'p')"
    )
    await conn.commit()
    await conn.execute("DELETE FROM tenants WHERE id = 't-sl-tc'")
    await conn.commit()
    async with conn.execute(
        "SELECT 1 FROM projects WHERE id = 'p-sl-tc'"
    ) as cur:
        row = await cur.fetchone()
    assert row is None


@pytest.mark.asyncio
async def test_sqlite_parent_set_null_on_parent_delete(_fresh_sqlite_db):
    conn = _fresh_sqlite_db
    await conn.execute(
        "INSERT OR IGNORE INTO tenants (id, name, plan) "
        "VALUES ('t-sl-ps', 'SL', 'free')"
    )
    await conn.execute(
        "INSERT INTO projects (id, tenant_id, name, slug) "
        "VALUES ('p-sl-ps-p', 't-sl-ps', 'Parent', 'parent')"
    )
    await conn.execute(
        "INSERT INTO projects (id, tenant_id, name, slug, parent_id) "
        "VALUES ('p-sl-ps-c', 't-sl-ps', 'Child', 'child', 'p-sl-ps-p')"
    )
    await conn.commit()
    await conn.execute("DELETE FROM projects WHERE id = 'p-sl-ps-p'")
    await conn.commit()
    async with conn.execute(
        "SELECT parent_id FROM projects WHERE id = 'p-sl-ps-c'"
    ) as cur:
        row = await cur.fetchone()
    assert row is not None, "child should survive parent delete"
    assert row[0] is None, "parent_id should be SET NULL"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Migration file sanity (revision chain)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_MIGRATION = (
    Path(__file__).resolve().parent.parent
    / "alembic" / "versions" / "0033_projects.py"
)


def test_migration_0033_file_exists():
    assert _MIGRATION.exists(), str(_MIGRATION)


def test_migration_0033_revision_chain():
    spec = importlib.util.spec_from_file_location("m0033", str(_MIGRATION))
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert m.revision == "0033"
    assert m.down_revision == "0032"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Migrator coverage (drift guard hand-off)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_migrator_lists_projects():
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
    assert "projects" in mig.TABLES_IN_ORDER
    # PK is TEXT (``p-*``), not INTEGER IDENTITY — must NOT be in
    # the identity-reset list (would crash sequence reset).
    assert "projects" not in mig.TABLES_WITH_IDENTITY_ID


def test_migrator_orders_projects_after_tenants_and_users():
    """``projects.tenant_id → tenants(id)`` and ``projects.created_by →
    users(id)`` mean replay order must put both parents first."""
    import sys as _sys
    spec = importlib.util.spec_from_file_location(
        "migrate_sqlite_to_pg",
        Path(__file__).resolve().parents[2]
        / "scripts" / "migrate_sqlite_to_pg.py",
    )
    assert spec and spec.loader
    mig = importlib.util.module_from_spec(spec)
    _sys.modules["migrate_sqlite_to_pg"] = mig
    spec.loader.exec_module(mig)
    order = mig.TABLES_IN_ORDER
    assert order.index("projects") > order.index("tenants")
    assert order.index("projects") > order.index("users")
