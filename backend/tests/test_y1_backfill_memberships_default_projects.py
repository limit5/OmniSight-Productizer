"""Y1 row 6 (#277) — backfill migration 0037 tests.

Mirrors the test layout of the sister Y1 row tests
(``test_user_tenant_memberships_schema.py`` etc): one bucket of PG
tests that exercise the alembic-applied schema and run the backfill
SQL against a live tx, one bucket of pure-Python file-shape tests
(revision chain, fingerprint grep), and one drift-guard test that
the migrator's TABLES_IN_ORDER still has the two backfilled tables
(both already added in 0032 / 0033 — this is a regression sentinel).

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
    / "0037_y1_backfill_memberships_default_projects.py"
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Migration file shape (revision chain + fingerprint scan)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_migration_0037_file_exists():
    assert _MIGRATION_PATH.exists(), str(_MIGRATION_PATH)


def test_migration_0037_revision_chain():
    """Pin the (revision, down_revision) pair so an accidental
    re-numbering in a sister branch can't silently break the chain."""
    spec = importlib.util.spec_from_file_location("m0037", str(_MIGRATION_PATH))
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert m.revision == "0037"
    assert m.down_revision == "0036"


def test_migration_0037_no_compat_fingerprints_in_sql():
    """Pre-commit fingerprint grep, but as a permanent contract test.

    The migration's SQL strings (``_BACKFILL_MEMBERSHIPS`` /
    ``_BACKFILL_DEFAULT_PROJECTS``) must NOT contain the four known
    SQLite-only fingerprints that fail at runtime on PG (the
    alembic_pg_compat shim covers ``INSERT OR IGNORE`` /
    ``datetime('now')`` rewrites at execute time, but a future edit
    that adds e.g. ``?`` placeholders or a raw ``datetime('now')``
    inside a SELECT projection would pass syntax check and only blow
    up against live PG).

    Docstring text matches are filtered out — we only scan the two
    SQL string constants.
    """
    src = _MIGRATION_PATH.read_text()
    sql_blocks: list[str] = []
    for name in ("_BACKFILL_MEMBERSHIPS", "_BACKFILL_DEFAULT_PROJECTS"):
        m = re.search(rf'^{name}\s*=\s*"""(.*?)"""', src, re.MULTILINE | re.DOTALL)
        assert m, f"could not extract {name} from migration source"
        sql_blocks.append(m.group(1))
    sql = "\n".join(sql_blocks)
    # The four canonical compat fingerprints from
    # docs/sop/implement_phase_step.md "Pre-commit compat-fingerprint
    # grep" — we want zero hits inside the actual SQL we'd push to PG.
    forbidden = {
        "_conn(": r"_conn\(",
        "await conn.commit": r"await\s+conn\.commit\b",
        "datetime('now')": r"datetime\s*\(\s*'now'\s*\)",
        "qmark placeholder in VALUES": r"VALUES\s*\([^)]*\?[^)]*\)",
    }
    hits = {
        label: re.findall(pat, sql, re.IGNORECASE)
        for label, pat in forbidden.items()
    }
    bad = {k: v for k, v in hits.items() if v}
    assert not bad, (
        f"compat-fingerprint hit inside backfill SQL: {bad}. "
        f"The shim does not rewrite these at execute time."
    )


def test_migration_0037_pg_translation_round_trip():
    """The two backfill statements must survive the alembic_pg_compat
    rewrite and emerge as syntactically valid PG.

    We don't actually execute against PG here — that's the job of the
    PG-gated tests below — but we do assert the rewrite produces what
    we expect: ``INSERT OR IGNORE`` becomes ``INSERT ... ON CONFLICT
    DO NOTHING`` and no SQLite-only ``?`` placeholders survive.
    """
    import sys as _sys
    backend_dir = Path(__file__).resolve().parent.parent
    if str(backend_dir) not in _sys.path:
        _sys.path.insert(0, str(backend_dir))
    from alembic_pg_compat import translate_sql

    src = _MIGRATION_PATH.read_text()
    for name in ("_BACKFILL_MEMBERSHIPS", "_BACKFILL_DEFAULT_PROJECTS"):
        m = re.search(rf'^{name}\s*=\s*"""(.*?)"""', src, re.MULTILINE | re.DOTALL)
        assert m, name
        translated = translate_sql(m.group(1), "postgresql")
        # PG cannot speak ``INSERT OR IGNORE`` — the shim must have
        # rewritten it.
        assert "INSERT OR IGNORE" not in translated.upper(), translated
        # And the rewrite must have appended ``ON CONFLICT DO NOTHING``
        # (the no-target form is supported on PG 9.5+ and matches any
        # unique constraint, including the composite PK and the
        # ``(tenant_id, product_line, slug)`` UNIQUE on projects).
        assert "ON CONFLICT" in translated.upper(), translated
        # No ``?`` placeholders should survive (translate_params_qmark
        # would have turned them into %s) — defensive: the migration
        # body shouldn't contain any to begin with.
        assert "?" not in translated, translated


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Drift guard — TABLES_IN_ORDER coverage
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_migrator_replays_backfilled_tables():
    """The SQLite→PG migrator must list both of the tables the
    backfill writes to.  Both were added to ``TABLES_IN_ORDER`` in
    revisions 0032 / 0033; this is a regression sentinel that catches
    anyone who later removes them while leaving the backfill in
    place (which would skip the rows during a SQLite→PG cutover).
    """
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
    assert "user_tenant_memberships" in mig.TABLES_IN_ORDER
    assert "projects" in mig.TABLES_IN_ORDER
    # Ordering invariant: both must replay AFTER ``users`` and
    # ``tenants`` (FK parents).  Spot-checking is enough — the
    # canonical drift guard is ``test_migrator_schema_coverage``.
    order = list(mig.TABLES_IN_ORDER)
    assert order.index("user_tenant_memberships") > order.index("users")
    assert order.index("user_tenant_memberships") > order.index("tenants")
    assert order.index("projects") > order.index("users")
    assert order.index("projects") > order.index("tenants")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  PG-side: live backfill against alembic-applied schema
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _read_sql(name: str) -> str:
    """Pull the named SQL constant out of the migration file as text.

    Importing the migration module would trigger ``from alembic import
    op`` which depends on alembic's runtime. We don't need that here —
    we only need the SQL strings — and reading them as text keeps the
    test independent of alembic's package-import side effects.
    """
    src = _MIGRATION_PATH.read_text()
    m = re.search(rf'^{name}\s*=\s*"""(.*?)"""', src, re.MULTILINE | re.DOTALL)
    assert m, f"could not extract {name} from migration source"
    return m.group(1).strip()


@pytest.mark.asyncio
async def test_pg_membership_backfill_role_admin_to_owner(pg_test_pool):
    """An ``admin`` legacy user becomes ``owner`` of their cache tenant
    after the backfill; a non-admin user becomes ``member``."""
    sql_mem = _read_sql("_BACKFILL_MEMBERSHIPS")
    async with pg_test_pool.acquire() as conn:
        async with conn.transaction():
            tid = "t-bf-roles"
            await conn.execute(
                "INSERT INTO tenants (id, name) VALUES ($1, $2) "
                "ON CONFLICT (id) DO NOTHING",
                tid, "Backfill Roles",
            )
            await conn.execute(
                "INSERT INTO users (id, email, name, role, password_hash, "
                "tenant_id) VALUES ($1,$2,$3,$4,$5,$6)",
                "u-bf-adm", "adm@bf.com", "Adm", "admin", "h", tid,
            )
            await conn.execute(
                "INSERT INTO users (id, email, name, role, password_hash, "
                "tenant_id) VALUES ($1,$2,$3,$4,$5,$6)",
                "u-bf-vw", "vw@bf.com", "Vw", "viewer", "h", tid,
            )
            # Run backfill — translated through the shim by the live
            # connection, but pg_test_pool's asyncpg conn doesn't see
            # the shim, so we run the already-translated SQL.
            from alembic_pg_compat import translate_sql
            await conn.execute(translate_sql(sql_mem, "postgresql"))
            rows = await conn.fetch(
                "SELECT user_id, role FROM user_tenant_memberships "
                "WHERE tenant_id = $1 ORDER BY user_id",
                tid,
            )
    role_by_uid = {r["user_id"]: r["role"] for r in rows}
    assert role_by_uid["u-bf-adm"] == "owner"
    assert role_by_uid["u-bf-vw"] == "member"


@pytest.mark.asyncio
async def test_pg_membership_backfill_idempotent(pg_test_pool):
    """Running the backfill twice produces the same row count as
    running it once — ``ON CONFLICT DO NOTHING`` swallows the dup."""
    sql_mem = _read_sql("_BACKFILL_MEMBERSHIPS")
    from alembic_pg_compat import translate_sql
    pg_sql = translate_sql(sql_mem, "postgresql")
    async with pg_test_pool.acquire() as conn:
        async with conn.transaction():
            tid = "t-bf-idem"
            await conn.execute(
                "INSERT INTO tenants (id, name) VALUES ($1, $2) "
                "ON CONFLICT (id) DO NOTHING",
                tid, "Backfill Idempotent",
            )
            await conn.execute(
                "INSERT INTO users (id, email, name, role, password_hash, "
                "tenant_id) VALUES ($1,$2,$3,$4,$5,$6)",
                "u-bf-i", "i@bf.com", "I", "viewer", "h", tid,
            )
            await conn.execute(pg_sql)
            n1 = await conn.fetchval(
                "SELECT COUNT(*) FROM user_tenant_memberships "
                "WHERE user_id = $1 AND tenant_id = $2",
                "u-bf-i", tid,
            )
            await conn.execute(pg_sql)
            n2 = await conn.fetchval(
                "SELECT COUNT(*) FROM user_tenant_memberships "
                "WHERE user_id = $1 AND tenant_id = $2",
                "u-bf-i", tid,
            )
    assert n1 == 1
    assert n2 == 1


@pytest.mark.asyncio
async def test_pg_membership_backfill_skips_user_without_tenant(pg_test_pool):
    """``WHERE tenant_id IS NOT NULL`` — but ``users.tenant_id`` is
    declared NOT NULL with a default of ``t-default``, so this is a
    defensive check that we don't crash on the empty-set edge case
    (no users in the table). Useful when the test fixture's TRUNCATE
    has emptied users."""
    sql_mem = _read_sql("_BACKFILL_MEMBERSHIPS")
    from alembic_pg_compat import translate_sql
    pg_sql = translate_sql(sql_mem, "postgresql")
    async with pg_test_pool.acquire() as conn:
        async with conn.transaction():
            # Empty universe — TRUNCATE in pg_test_conn would handle
            # this, but pg_test_pool does NOT auto-truncate; we just
            # verify the SQL doesn't raise on an empty SELECT result.
            await conn.execute(pg_sql)
    # If we got here without raising the test passes.


@pytest.mark.asyncio
async def test_pg_default_project_backfill_creates_one_per_tenant(pg_test_pool):
    """Each tenant gets exactly one project with the literal contract
    triple (product_line='default', slug='default')."""
    sql_proj = _read_sql("_BACKFILL_DEFAULT_PROJECTS")
    from alembic_pg_compat import translate_sql
    pg_sql = translate_sql(sql_proj, "postgresql")
    async with pg_test_pool.acquire() as conn:
        async with conn.transaction():
            for tid in ("t-bf-p1", "t-bf-p2"):
                await conn.execute(
                    "INSERT INTO tenants (id, name) VALUES ($1, $2) "
                    "ON CONFLICT (id) DO NOTHING",
                    tid, f"BF Proj {tid}",
                )
            await conn.execute(pg_sql)
            rows = await conn.fetch(
                "SELECT id, tenant_id, product_line, slug, name "
                "FROM projects WHERE tenant_id IN ('t-bf-p1','t-bf-p2') "
                "ORDER BY tenant_id"
            )
    assert len(rows) == 2
    by_tid = {r["tenant_id"]: r for r in rows}
    for tid in ("t-bf-p1", "t-bf-p2"):
        assert by_tid[tid]["product_line"] == "default"
        assert by_tid[tid]["slug"] == "default"
        assert by_tid[tid]["name"] == "Default"
        # Deterministic id: strip 't-' prefix, suffix '-default'.
        expected_id = "p-" + tid.removeprefix("t-") + "-default"
        assert by_tid[tid]["id"] == expected_id


@pytest.mark.asyncio
async def test_pg_default_project_backfill_idempotent(pg_test_pool):
    """Re-running the project backfill is a no-op (deterministic id +
    UNIQUE (tenant_id, product_line, slug) → ON CONFLICT DO NOTHING)."""
    sql_proj = _read_sql("_BACKFILL_DEFAULT_PROJECTS")
    from alembic_pg_compat import translate_sql
    pg_sql = translate_sql(sql_proj, "postgresql")
    async with pg_test_pool.acquire() as conn:
        async with conn.transaction():
            tid = "t-bf-pidem"
            await conn.execute(
                "INSERT INTO tenants (id, name) VALUES ($1, $2) "
                "ON CONFLICT (id) DO NOTHING",
                tid, "BF Proj Idem",
            )
            await conn.execute(pg_sql)
            n1 = await conn.fetchval(
                "SELECT COUNT(*) FROM projects WHERE tenant_id = $1", tid,
            )
            await conn.execute(pg_sql)
            n2 = await conn.fetchval(
                "SELECT COUNT(*) FROM projects WHERE tenant_id = $1", tid,
            )
    assert n1 == 1
    assert n2 == 1


@pytest.mark.asyncio
async def test_pg_default_project_backfill_handles_no_t_prefix(pg_test_pool):
    """A tenant id that does NOT start with ``t-`` (legacy /
    convention violation) still gets a default project; the id
    derivation falls back to using the raw tenant_id."""
    sql_proj = _read_sql("_BACKFILL_DEFAULT_PROJECTS")
    from alembic_pg_compat import translate_sql
    pg_sql = translate_sql(sql_proj, "postgresql")
    async with pg_test_pool.acquire() as conn:
        async with conn.transaction():
            tid = "noprefixbf"  # no t- prefix
            await conn.execute(
                "INSERT INTO tenants (id, name) VALUES ($1, $2) "
                "ON CONFLICT (id) DO NOTHING",
                tid, "No Prefix",
            )
            await conn.execute(pg_sql)
            row = await conn.fetchrow(
                "SELECT id FROM projects WHERE tenant_id = $1", tid,
            )
    assert row is not None
    assert row["id"] == "p-noprefixbf-default"


@pytest.mark.asyncio
async def test_pg_membership_and_project_backfill_compose(pg_test_pool):
    """Running both backfills together leaves every (user, default-
    project-of-user's-tenant) pair reachable via a single join.

    This is the load-bearing post-condition the next TODO row
    (cross-table workload mapping) will rely on: every row of every
    business table can map ``tenant_id`` → the deterministic
    ``p-<tid>-default`` project id.
    """
    sql_mem = _read_sql("_BACKFILL_MEMBERSHIPS")
    sql_proj = _read_sql("_BACKFILL_DEFAULT_PROJECTS")
    from alembic_pg_compat import translate_sql
    pg_mem = translate_sql(sql_mem, "postgresql")
    pg_proj = translate_sql(sql_proj, "postgresql")
    async with pg_test_pool.acquire() as conn:
        async with conn.transaction():
            tid = "t-bf-compose"
            await conn.execute(
                "INSERT INTO tenants (id, name) VALUES ($1, $2) "
                "ON CONFLICT (id) DO NOTHING",
                tid, "Compose",
            )
            await conn.execute(
                "INSERT INTO users (id, email, name, role, password_hash, "
                "tenant_id) VALUES ($1,$2,$3,$4,$5,$6)",
                "u-bf-c", "c@bf.com", "C", "viewer", "h", tid,
            )
            await conn.execute(pg_mem)
            await conn.execute(pg_proj)
            row = await conn.fetchrow(
                """
                SELECT m.user_id, m.tenant_id, m.role, p.id AS project_id,
                       p.product_line, p.slug
                FROM user_tenant_memberships m
                JOIN projects p ON p.tenant_id = m.tenant_id
                WHERE m.user_id = $1 AND m.tenant_id = $2
                """,
                "u-bf-c", tid,
            )
    assert row is not None
    assert row["project_id"] == "p-bf-compose-default"
    assert row["product_line"] == "default"
    assert row["slug"] == "default"
    assert row["role"] == "member"
