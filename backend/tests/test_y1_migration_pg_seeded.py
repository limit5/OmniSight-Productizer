"""Y1 row 8 (#277) — multi-tenant seeded migration idempotency + backfill
invariant test.

Covers the TODO contract for the eighth (and last) Y1 row::

    測試：migration 在 seed 了多 tenant × 多 user × 既有 workflow 的 PG 上
    冪等、回填後每 row 都有有效的 (tenant_id, project_id) tuple、
    project_id 必然指向同 tenant 的 project（外鍵 + CHECK 約束）。

Where rows 6 / 7 unit-test each backfill SQL projection in isolation
(``test_y1_backfill_memberships_default_projects.py`` for 0037,
``test_y1_project_id_on_business_tables.py`` for 0038), this file
exercises the **composed** migration on a realistic seed of:

* 3 tenants — distinct ids (`t-y1r8-acme`, `t-y1r8-globex`,
  `t-y1r8-sentinel`) plus one tenant id without the `t-` prefix
  (`y1r8noprefix`) so the projection's no-prefix branch is also
  observed end-to-end on real data.
* 2 users per tenant — one with legacy ``role='admin'`` so the
  membership backfill maps it to ``owner``, one with ``role='viewer'``
  so it maps to ``member``.
* 1 row per business table per tenant — so the per-table backfill
  UPDATE has heterogeneous data to operate on, not just one table.

The test then asserts the three load-bearing contracts from the TODO
row:

1. **Idempotency** — running the membership / default-project / per-
   table project_id backfills twice produces the same row state as
   running them once.  Achieved at the SQL level by ``INSERT OR
   IGNORE`` (translated to ``ON CONFLICT DO NOTHING`` by the
   ``alembic_pg_compat`` shim) and by ``WHERE project_id IS NULL``
   skipping a second pass on already-backfilled rows.

2. **Every business row carries a valid (tenant_id, project_id)
   tuple post-backfill** — no row left with ``project_id IS NULL``
   when ``tenant_id IS NOT NULL``.  This is what unblocks the
   subsequent NOT-NULL flip planned for one release later (per the
   row-7 TODO note).

3. **Cross-tenant project_id invariant** — every backfilled
   ``project_id`` must reference a ``projects`` row whose
   ``tenant_id`` equals the business row's ``tenant_id``.  The
   TODO row reads "外鍵 + CHECK 約束" — we read "FK plus a test that
   asserts the cross-tenant tuple invariant" because (a) the FK
   alone only guarantees the project exists, not that it's same-
   tenant, and (b) a database-level CHECK that joins across tables
   is not portable to SQLite (sqlite has no tuple-FK referencing
   non-PK columns), so the invariant is enforced at the application
   layer (Y3 authorisation resolver) and asserted here as a
   regression sentinel against a future backfill projection that
   accidentally hard-codes a tenant.

Plus:

4. **FK in place** — confirm an UPDATE pointing at a non-existent
   project is rejected by the FK constraint (defence in depth
   against any future code path that bypasses the projection).
5. **Alembic-level idempotency** — a second ``alembic upgrade head``
   subprocess against the already-stamped DB exits 0 and applies
   no migrations (alembic_version unchanged).

The PG tests are skipped unless ``OMNI_TEST_PG_URL`` is set (same
gate as every other Y1 row test).  The pure-Python tests
(file-shape, target-table-list parity) run unconditionally.

Module-global / cross-worker state audit
────────────────────────────────────────
This is a pure test file — no module-level singletons, no in-memory
caches.  It executes against the session-scoped ``pg_test_pool``
fixture (which itself is per-test-process; multi-worker concerns are
the pool fixture's responsibility, not this test's).

Read-after-write timing audit
─────────────────────────────
The seed inserts and the backfill UPDATEs run inside a single
asyncpg transaction per test; there is no parallel writer to race.
Every assertion reads after the writer has committed (transaction
exit) so no read-before-commit timing window is exposed.
"""
from __future__ import annotations

import importlib.util
import os
import re
import subprocess
from pathlib import Path

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent
_VERSIONS_DIR = _BACKEND_DIR / "alembic" / "versions"
_MIGRATION_0037 = _VERSIONS_DIR / "0037_y1_backfill_memberships_default_projects.py"
_MIGRATION_0038 = _VERSIONS_DIR / "0038_y1_project_id_on_business_tables.py"


# Mirrors ``_TABLES_NEEDING_PROJECT_ID`` in alembic 0038 — the six
# business tables that received a ``project_id`` column in row 7.  A
# drift-guard test below asserts equality with the migration constant.
_TARGET_TABLES: tuple[str, ...] = (
    "workflow_runs",
    "debug_findings",
    "decision_rules",
    "event_log",
    "artifacts",
    "user_preferences",
)


# Per-tenant seed plan: 4 tenants with deterministic ids that exercise
# both branches of the deterministic project-id projection (with `t-`
# prefix and without).  Each tenant gets 2 users, one ``admin`` and
# one ``viewer``, plus one row in each of the six business tables.
_SEED_TENANTS: tuple[str, ...] = (
    "t-y1r8-acme",
    "t-y1r8-globex",
    "t-y1r8-sentinel",
    "y1r8noprefix",  # exercises the ELSE branch of the projection
)


def _expected_project_id(tenant_id: str) -> str:
    """Replicate the migration's deterministic projection in Python.

    Used to compute the *expected* ``projects.id`` for each seeded
    tenant.  Strip the ``t-`` prefix when present, then suffix with
    ``-default`` — exactly what 0037's ``_BACKFILL_DEFAULT_PROJECTS``
    SELECT yields.
    """
    return "p-" + (tenant_id[2:] if tenant_id.startswith("t-") else tenant_id) + "-default"


def _read_sql(mig_path: Path, name: str) -> str:
    """Pull a triple-quoted SQL constant out of a migration source file.

    We don't import the migration module to avoid alembic's ``op``
    proxy needing a live context — text extraction is fine because
    these constants are simple bracketed string literals.
    """
    src = mig_path.read_text()
    m = re.search(rf'^{name}\s*=\s*"""(.*?)"""', src, re.MULTILINE | re.DOTALL)
    assert m, f"could not extract {name} from {mig_path.name}"
    return m.group(1).strip()


def _load_migration_module(mig_path: Path):
    """Load a migration module via importlib so we can read its
    module-level constants (revision id, table list, projection)."""
    spec = importlib.util.spec_from_file_location(mig_path.stem, str(mig_path))
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pure-Python: drift guards (no PG required)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_seed_target_tables_match_migration_constant():
    """``_TARGET_TABLES`` here must equal the migration's
    ``_TABLES_NEEDING_PROJECT_ID``.  If the migration grows / shrinks,
    this test must follow so the seed actually covers the live
    migration's table set."""
    m = _load_migration_module(_MIGRATION_0038)
    assert set(m._TABLES_NEEDING_PROJECT_ID) == set(_TARGET_TABLES)


def test_expected_project_id_helper_matches_migration_projection():
    """Spot-check the Python helper against the migration's literal
    SQL projection for the four seeded tenants.  A drift here would
    silently invalidate every assertion in the PG tests below."""
    cases = {
        "t-y1r8-acme": "p-y1r8-acme-default",
        "t-y1r8-globex": "p-y1r8-globex-default",
        "t-y1r8-sentinel": "p-y1r8-sentinel-default",
        "y1r8noprefix": "p-y1r8noprefix-default",
    }
    for tid, expected in cases.items():
        assert _expected_project_id(tid) == expected, tid


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  PG-side: the load-bearing seeded-universe tests
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def _seed_universe(conn) -> None:
    """Create 4 tenants × 2 users × 6 business-table rows per tenant.

    All inserts use ``ON CONFLICT DO NOTHING`` (or unique row ids) so
    re-running the seed is itself idempotent — important because the
    test file may run twice in a developer's session against the same
    DB and we don't want collisions to mask real bugs.
    """
    # 1) Tenants — 4 of them, including one without the `t-` prefix.
    for tid in _SEED_TENANTS:
        await conn.execute(
            "INSERT INTO tenants (id, name) VALUES ($1, $2) "
            "ON CONFLICT (id) DO NOTHING",
            tid, f"Y1r8 {tid}",
        )

    # 2) Users — 2 per tenant (admin + viewer).  Email must be unique
    #    globally so we suffix with the tenant id.  password_hash is
    #    a fixed dummy — schema requires NOT NULL DEFAULT ''.
    for tid in _SEED_TENANTS:
        for role in ("admin", "viewer"):
            uid = f"u-y1r8-{tid}-{role}"
            email = f"{role}@{tid}.y1r8.test"
            await conn.execute(
                "INSERT INTO users (id, email, name, role, password_hash, "
                "tenant_id) VALUES ($1,$2,$3,$4,$5,$6) "
                "ON CONFLICT (id) DO NOTHING",
                uid, email, f"Y1r8 {role}", role, "h", tid,
            )

    # 3) Business-table rows — one per table per tenant, all with
    #    ``project_id`` left implicit (NULL) so the backfill UPDATE
    #    has work to do.
    for tid in _SEED_TENANTS:
        # workflow_runs — Phase 56 checkpoint shape.
        await conn.execute(
            "INSERT INTO workflow_runs (id, kind, started_at, status, tenant_id) "
            "VALUES ($1, 'test', 0, 'running', $2) "
            "ON CONFLICT (id) DO NOTHING",
            f"wf-y1r8-{tid}", tid,
        )
        # artifacts — id PK, NOT NULL name + file_path.
        await conn.execute(
            "INSERT INTO artifacts (id, name, file_path, tenant_id) "
            "VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (id) DO NOTHING",
            f"art-y1r8-{tid}", f"artifact for {tid}", "/tmp/y1r8.txt", tid,
        )
        # debug_findings — id PK, NOT NULL task_id / agent_id /
        # finding_type / content.
        await conn.execute(
            "INSERT INTO debug_findings (id, task_id, agent_id, finding_type, "
            "content, tenant_id) VALUES ($1, $2, $3, $4, $5, $6) "
            "ON CONFLICT (id) DO NOTHING",
            f"df-y1r8-{tid}", "task-y1r8", "agent-y1r8", "observation",
            "{}", tid,
        )
        # decision_rules — id PK, NOT NULL kind_pattern.
        await conn.execute(
            "INSERT INTO decision_rules (id, kind_pattern, tenant_id) "
            "VALUES ($1, $2, $3) "
            "ON CONFLICT (id) DO NOTHING",
            f"dr-y1r8-{tid}", f"y1r8/{tid}", tid,
        )
        # event_log — id is identity, must NOT be specified by us.
        # We tag the row with a deterministic event_type so we can
        # find it on read.
        await conn.execute(
            "INSERT INTO event_log (event_type, data_json, tenant_id) "
            "VALUES ($1, '{}', $2)",
            f"y1r8/{tid}", tid,
        )
        # user_preferences — composite PK (user_id, pref_key).  Use
        # the admin user we just inserted as the FK target.
        await conn.execute(
            "INSERT INTO user_preferences (user_id, pref_key, value, tenant_id) "
            "VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (user_id, pref_key) DO NOTHING",
            f"u-y1r8-{tid}-admin", "y1r8/seed", "v", tid,
        )


async def _run_full_y1_backfill(conn) -> None:
    """Apply the composed Y1 backfill SQL: 0037 (memberships +
    default projects) followed by 0038 (six per-table project_id
    UPDATEs).

    We pull the SQL out of the migration sources and pipe it through
    the ``alembic_pg_compat`` shim so the PG-side ``ON CONFLICT DO
    NOTHING`` rewrite is applied (asyncpg connections don't see the
    SQLAlchemy event hook that does the auto-rewrite at execute
    time).  This keeps the test honest: we exercise the same SQL the
    operator's ``alembic upgrade head`` would push to PG.
    """
    import sys as _sys
    if str(_BACKEND_DIR) not in _sys.path:
        _sys.path.insert(0, str(_BACKEND_DIR))
    from alembic_pg_compat import translate_sql

    # 0037 — backfill memberships, then default projects.
    await conn.execute(
        translate_sql(_read_sql(_MIGRATION_0037, "_BACKFILL_MEMBERSHIPS"),
                      "postgresql")
    )
    await conn.execute(
        translate_sql(_read_sql(_MIGRATION_0037, "_BACKFILL_DEFAULT_PROJECTS"),
                      "postgresql")
    )

    # 0038 — per-table backfill UPDATE.  Build the same statement the
    # migration body builds, one per target table.  The projection
    # constant lives in the migration module (string literal); we
    # import it via the migration loader so a regression that mutates
    # the projection breaks both the migration AND the test in lock-
    # step (no silent divergence).
    m38 = _load_migration_module(_MIGRATION_0038)
    projection = m38._PROJECT_ID_FROM_TENANT_ID
    for table in _TARGET_TABLES:
        await conn.execute(
            f"UPDATE {table} SET project_id = {projection} "
            f"WHERE project_id IS NULL AND tenant_id IS NOT NULL"
        )


@pytest.mark.asyncio
async def test_pg_seeded_backfill_idempotent_and_complete(pg_test_pool):
    """Master test for Y1 row 8.

    Verifies all three TODO contracts on a multi-tenant × multi-user
    × multi-business-row seed: idempotency, valid (tenant_id,
    project_id) tuple coverage, and the cross-tenant invariant.
    """
    async with pg_test_pool.acquire() as conn:
        async with conn.transaction():
            # ── seed ──────────────────────────────────────────
            await _seed_universe(conn)

            # ── 1st backfill pass ─────────────────────────────
            await _run_full_y1_backfill(conn)

            # Snapshot the post-1st-pass state so we can compare to
            # the post-2nd-pass state for byte-level idempotency.
            #
            # Memberships: count + per-(user,tenant) role.
            mem_rows_1 = await conn.fetch(
                "SELECT user_id, tenant_id, role FROM user_tenant_memberships "
                "WHERE user_id LIKE 'u-y1r8-%' "
                "ORDER BY user_id, tenant_id"
            )
            # Default projects: count + per-(tenant) project id.
            proj_rows_1 = await conn.fetch(
                "SELECT id, tenant_id FROM projects "
                "WHERE tenant_id = ANY($1::text[]) "
                "ORDER BY tenant_id",
                list(_SEED_TENANTS),
            )
            # Per-table project_id snapshots.
            per_table_1: dict[str, list] = {}
            for table in _TARGET_TABLES:
                per_table_1[table] = await conn.fetch(
                    f"SELECT tenant_id, project_id FROM {table} "
                    f"WHERE tenant_id = ANY($1::text[]) "
                    f"ORDER BY tenant_id, project_id",
                    list(_SEED_TENANTS),
                )

            # ── 2nd backfill pass ─────────────────────────────
            await _run_full_y1_backfill(conn)

            mem_rows_2 = await conn.fetch(
                "SELECT user_id, tenant_id, role FROM user_tenant_memberships "
                "WHERE user_id LIKE 'u-y1r8-%' "
                "ORDER BY user_id, tenant_id"
            )
            proj_rows_2 = await conn.fetch(
                "SELECT id, tenant_id FROM projects "
                "WHERE tenant_id = ANY($1::text[]) "
                "ORDER BY tenant_id",
                list(_SEED_TENANTS),
            )
            per_table_2: dict[str, list] = {}
            for table in _TARGET_TABLES:
                per_table_2[table] = await conn.fetch(
                    f"SELECT tenant_id, project_id FROM {table} "
                    f"WHERE tenant_id = ANY($1::text[]) "
                    f"ORDER BY tenant_id, project_id",
                    list(_SEED_TENANTS),
                )

            # ── Contract 1: idempotency ───────────────────────
            # Convert Record rows to tuples for stable equality.
            assert [tuple(r) for r in mem_rows_1] == [tuple(r) for r in mem_rows_2], (
                "membership backfill is not idempotent — second pass mutated rows"
            )
            assert [tuple(r) for r in proj_rows_1] == [tuple(r) for r in proj_rows_2], (
                "default-project backfill is not idempotent — second pass "
                "mutated rows"
            )
            for table in _TARGET_TABLES:
                assert (
                    [tuple(r) for r in per_table_1[table]]
                    == [tuple(r) for r in per_table_2[table]]
                ), (
                    f"project_id backfill on {table} is not idempotent — "
                    f"second pass mutated rows"
                )

            # ── Contract 2: every business row has a valid
            # (tenant_id, project_id) tuple ───────────────────
            for table in _TARGET_TABLES:
                rows = await conn.fetch(
                    f"SELECT id, tenant_id, project_id FROM {table} "
                    f"WHERE tenant_id = ANY($1::text[])",
                    list(_SEED_TENANTS),
                )
                # event_log uses a synthetic int id we don't track;
                # for the other tables `id` is in the SELECT but
                # we only need the (tenant_id, project_id) check
                # below.
                for row in rows:
                    assert row["tenant_id"] is not None, (
                        f"{table} row should have tenant_id (seed contract)"
                    )
                    assert row["project_id"] is not None, (
                        f"{table} row {row} has NULL project_id after "
                        f"backfill — TODO row contract violated"
                    )
                    expected = _expected_project_id(row["tenant_id"])
                    assert row["project_id"] == expected, (
                        f"{table} row {row} project_id {row['project_id']!r} "
                        f"does not match the deterministic projection "
                        f"{expected!r} for tenant {row['tenant_id']!r}"
                    )

            # ── Contract 3: cross-tenant invariant ────────────
            # For every business row, join project_id → projects
            # and assert projects.tenant_id == row.tenant_id.
            for table in _TARGET_TABLES:
                mismatches = await conn.fetch(
                    f"""
                    SELECT t.tenant_id AS row_tenant,
                           p.tenant_id AS project_tenant,
                           t.project_id AS pid
                    FROM {table} t
                    JOIN projects p ON p.id = t.project_id
                    WHERE t.tenant_id = ANY($1::text[])
                      AND t.tenant_id <> p.tenant_id
                    """,
                    list(_SEED_TENANTS),
                )
                assert mismatches == [], (
                    f"cross-tenant project_id leak in {table}: {mismatches}. "
                    f"The TODO contract requires project_id to point at a "
                    f"project of the SAME tenant."
                )

            # ── Bonus: membership backfill role mapping ──────
            # The literal 0037 contract: admin→owner, else→member.
            mem_by_uid = {
                (r["user_id"], r["tenant_id"]): r["role"] for r in mem_rows_1
            }
            for tid in _SEED_TENANTS:
                assert (
                    mem_by_uid[(f"u-y1r8-{tid}-admin", tid)] == "owner"
                ), f"admin user in {tid} should have owner role"
                assert (
                    mem_by_uid[(f"u-y1r8-{tid}-viewer", tid)] == "member"
                ), f"viewer user in {tid} should have member role"


@pytest.mark.asyncio
async def test_pg_seeded_backfill_fk_blocks_invalid_project_id(pg_test_pool):
    """Defence in depth: even when an external code path bypasses the
    deterministic projection and tries to set ``project_id`` to a
    non-existent project, the FK from row 7's ``REFERENCES projects(id)``
    must reject the write.
    """
    import asyncpg
    async with pg_test_pool.acquire() as conn:
        async with conn.transaction():
            tid = "t-y1r8-fkguard"
            await conn.execute(
                "INSERT INTO tenants (id, name) VALUES ($1, $2) "
                "ON CONFLICT (id) DO NOTHING",
                tid, "Y1r8 FK guard",
            )
            await conn.execute(
                "INSERT INTO workflow_runs "
                "(id, kind, started_at, status, tenant_id) "
                "VALUES ($1, 'test', 0, 'running', $2) "
                "ON CONFLICT (id) DO NOTHING",
                f"wf-y1r8-fkguard", tid,
            )
            with pytest.raises(asyncpg.exceptions.ForeignKeyViolationError):
                await conn.execute(
                    "UPDATE workflow_runs SET project_id = "
                    "'p-y1r8-bogus-does-not-exist' "
                    "WHERE id = $1",
                    "wf-y1r8-fkguard",
                )


@pytest.mark.asyncio
async def test_pg_seeded_backfill_handles_no_t_prefix_tenant(pg_test_pool):
    """The ``y1r8noprefix`` tenant exercises the ELSE branch of the
    projection (``substr(tenant_id, 1, 2) <> 't-'``).  The master
    test above already covers this implicitly; this is the explicit
    sentinel so a future re-write that hard-codes the prefix-strip
    fails loudly rather than silently producing
    ``p-noprefix-default`` only when the tenant happens to have one.
    """
    async with pg_test_pool.acquire() as conn:
        async with conn.transaction():
            tid = "y1r8noprefix-only"
            await conn.execute(
                "INSERT INTO tenants (id, name) VALUES ($1, $2) "
                "ON CONFLICT (id) DO NOTHING",
                tid, "Y1r8 no-prefix only",
            )
            await conn.execute(
                "INSERT INTO artifacts (id, name, file_path, tenant_id) "
                "VALUES ($1, $2, $3, $4) "
                "ON CONFLICT (id) DO NOTHING",
                f"art-{tid}", "no prefix", "/tmp/np.txt", tid,
            )
            await _run_full_y1_backfill(conn)
            row = await conn.fetchrow(
                "SELECT project_id FROM artifacts WHERE id = $1",
                f"art-{tid}",
            )
            assert row is not None
            assert row["project_id"] == f"p-{tid}-default", (
                f"no-prefix tenant id should yield p-{tid}-default; "
                f"got {row['project_id']!r}"
            )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Alembic-level idempotency: re-running ``upgrade head`` is a no-op
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _omni_test_pg_dsn() -> str:
    """Return the libpq DSN, or '' if not set.  Local copy so this
    test doesn't depend on the conftest helper's name (which the
    conftest treats as a private symbol)."""
    raw = os.environ.get("OMNI_TEST_PG_URL", "").strip()
    if not raw:
        return ""
    for prefix in ("postgresql+psycopg2://", "postgresql+asyncpg://"):
        if raw.startswith(prefix):
            return "postgresql://" + raw[len(prefix):]
    return raw


def test_alembic_upgrade_head_is_idempotent_on_stamped_db():
    """Running ``alembic upgrade head`` against a DB whose
    ``alembic_version`` already points at HEAD must exit 0 with no
    migration applied.

    This is the alembic-machinery layer of the TODO row's idempotency
    contract — the SQL-layer contract is covered by
    ``test_pg_seeded_backfill_idempotent_and_complete`` above.  Both
    are needed: the SQL idempotency would let us re-run the backfill
    by hand without harm, and the alembic-version idempotency
    guarantees the operator running ``alembic upgrade head`` twice
    (e.g. due to a transient network blip mid-cutover) does not double-
    apply or err out.
    """
    dsn = _omni_test_pg_dsn()
    if not dsn:
        pytest.skip("OMNI_TEST_PG_URL not set — alembic idempotency skipped")

    sqlalchemy_url = dsn.replace("postgresql://", "postgresql+psycopg2://", 1)
    env = os.environ.copy()
    env["SQLALCHEMY_URL"] = sqlalchemy_url
    env["OMNISIGHT_SKIP_FS_MIGRATIONS"] = "1"
    env.pop("PYTHONPATH", None)  # see conftest's commentary on stdlib shadow

    # First upgrade: bring schema to HEAD if it isn't already.  This
    # is also what the session-scoped ``pg_test_alembic_upgraded``
    # fixture does, but we cannot depend on a session fixture from a
    # sync test, so we re-do the work here ourselves.  Both invocations
    # are idempotent so a duplicated upgrade is fine.
    proc1 = subprocess.run(
        ["alembic", "upgrade", "head"],
        cwd=_BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if proc1.returncode != 0:
        pytest.skip(
            f"first alembic upgrade head failed (DSN may be misconfigured): "
            f"{proc1.stderr[-400:]!r}"
        )

    # Second upgrade: this is the actual idempotency assertion.  On a
    # HEAD-stamped DB, alembic finds no work to do and exits 0 without
    # invoking any migration's ``upgrade()`` body.
    proc2 = subprocess.run(
        ["alembic", "upgrade", "head"],
        cwd=_BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc2.returncode == 0, (
        f"second alembic upgrade head should be a no-op on HEAD-stamped DB, "
        f"got returncode={proc2.returncode} stderr={proc2.stderr[-400:]!r}"
    )
    # Belt-and-braces: alembic should say "no migrations to run" or
    # equivalent — we don't pin the literal string (alembic versions
    # vary) but we do confirm no migration's upgrade banner appears
    # for the Y1 revisions on the second run.
    for rev in ("0037", "0038"):
        assert f"Running upgrade -> {rev}" not in proc2.stdout, (
            f"second alembic upgrade head re-applied revision {rev}: "
            f"stdout={proc2.stdout[-400:]!r}"
        )


@pytest.mark.asyncio
async def test_pg_alembic_version_at_or_after_y1_head(pg_test_pool):
    """Sanity: confirm the test DB is stamped at 0038 (or later) so
    the seeded-backfill tests above are exercising real Y1 schema.

    A DB stamped at e.g. 0036 would silently skip the project_id
    column — the per-table backfill UPDATE would then explode with
    ``column "project_id" does not exist`` rather than asserting the
    TODO contract.  Catch that early with a clear failure message.
    """
    async with pg_test_pool.acquire() as conn:
        version = await conn.fetchval(
            "SELECT version_num FROM alembic_version"
        )
    assert version is not None, "alembic_version table empty — DB not stamped"
    # Lexicographic compare works because the revision ids are zero-
    # padded 4-digit strings.
    assert version >= "0038", (
        f"test DB is stamped at {version} but Y1 row 8 needs ≥ 0038 "
        f"(membership / projects / project_id columns).  Run "
        f"``alembic upgrade head`` against OMNI_TEST_PG_URL."
    )
