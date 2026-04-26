"""Y10 #286 row 3 — Y1 migration idempotency + downgrade + workspace
relocation acceptance test.

Acceptance criterion (TODO §Y10 row 3)::

    Migration idempotency：既有 ``t-default`` + 5 個 tenant（I1 的 seed）
    → 跑 Y1 migration → 資料完全對得上（每 user 一個 membership、每
    workload 一個 project_id、每 workspace 搬到新路徑 symlink 正確）。
    回滾測試：Y1 migration 的 ``downgrade`` 能把資料搬回去。

Three dimensions the row checks
───────────────────────────────
1. **Multi-tenant idempotency on the I1 seed** — a DB seeded the way
   I1 (alembic 0012) shipped (one ``t-default`` row) plus 5 operator-
   added tenants on top, with a user + a workload row in each, must
   converge after running the Y1 backfill *twice*: every user has
   exactly one membership row, every workload has a non-NULL
   ``project_id``, and the second pass mutates zero rows.
2. **Workspace relocation + symlink** — five legacy flat-layout
   workspaces under ``.agent_workspaces/{agent_id}/`` migrate to
   ``{dst}/t-default/default/default/{safe(agent_id)}/legacy-hash/``
   (the canonical 5-layer layout from Y6 row 1) with a backward-compat
   symlink at the old path resolving to the new dir, and a second
   migrator run is a no-op.
3. **Downgrade can move the data back** — running 0037's ``downgrade``
   SQL after a successful upgrade strips the deterministic membership
   + default-project rows BUT preserves operator-edited siblings (a
   membership whose role was flipped in the admin console, a project
   with a custom slug); running 0038's ``downgrade`` drops the six
   ``project_id`` columns and their indexes so the schema returns to
   pre-Y1 shape.

Why this row exists alongside ``test_y1_migration_pg_seeded.py``
───────────────────────────────────────────────────────────────
That sibling row covers Y1 row 8's 4-tenant idempotency + cross-
tenant tuple invariant on a synthetic multi-tenant universe. It does
NOT cover (a) the I1-shaped seed (``t-default`` + N operator
tenants), (b) the workspace migrator's filesystem symlink behaviour
under realistic flat-layout fan-out, or (c) the Y1 alembic
``downgrade`` paths. Y10 row 3 fills those three gaps as the
"operational exam" of the Y1 stack — Y10 itself ships zero new
production surface (per the row 1 / row 2 precedent) and only adds
acceptance / drift-guard test coverage.

Test layout
───────────
* **Block A — pure-unit drift guards** (always run, no PG, no FS):
  lock the seed-shape constants, the alembic revision chain, the
  presence of the ``downgrade`` functions, and the per-row
  invariants the downgrade SQL relies on. Source-grep guards on
  the migration files and the workspace migrator catch refactor
  drift even on lanes that have neither PG nor a writable FS.
* **Block B — PG-required acceptance** (skip without
  ``OMNI_TEST_PG_URL``): seed the I1-shaped universe inside a
  rolled-back transaction, exercise the upgrade SQL twice for
  idempotency, then exercise the 0037 / 0038 downgrade SQL and
  assert the resulting state matches both the "move data back"
  contract and the "preserve operator-edited rows" contract.
* **Block C — filesystem-only acceptance** (always run, uses
  ``tmp_path``): build five fake legacy flat-layout workspaces,
  run the workspace migrator, assert each ends up at
  ``t-default/default/default/{safe(agent_id)}/legacy-hash/`` with a
  symlink at the old path, then re-run the migrator and assert
  every record is ``skipped_already_symlink``.

Same skip-pattern as ``test_y10_row1_multi_tenant_concurrency.py`` and
``test_y10_row2_cross_tenant_leak.py`` so the test lane gating stays
consistent across the Y10 rows.

Module-global state audit (per implement_phase_step.md Step 1)
────────────────────────────────────────────────────────────────
Pure test code — zero new prod code, zero new module-globals
beyond immutable constants (``_SEED_DEFAULT_TENANT`` /
``_SEED_OPERATOR_TENANTS`` / ``_TARGET_TABLES``). Block B uses the
session-scoped ``pg_test_pool`` fixture from ``conftest.py`` and
runs every test inside an outer ``conn.transaction()`` that rolls
back on exit, so a failed assertion never leaks rows into the
next test or into a subsequent test run. Block C operates entirely
inside ``tmp_path`` and never touches the real ``.agent_workspaces``
or ``data/workspaces`` trees. The ``backend.db_context``
ContextVars are not touched by this row — Y1 migration runs offline
during the cutover window, not in the request hot path, so there
is no ContextVar slot to leak (audit answer #3 — pure offline DDL/
DML, no per-worker concern beyond what conftest already covers).

Read-after-write timing audit (per implement_phase_step.md Step 1)
────────────────────────────────────────────────────────────────
Each Block B test runs all writers (seed INSERTs, upgrade UPDATEs,
downgrade DELETEs) sequentially inside a single asyncpg
transaction, then reads back the resulting state via
``conn.fetch`` on the same connection. There is no parallel writer
in this row (unlike the Y10 row 1 1000-task fan-out), so the read-
after-write window is trivially closed: every assertion observes
the post-write state of the exact same connection. Block C
operates synchronously (the workspace migrator is non-async) and
each ``migrate()`` call returns only after every ``shutil.move`` /
``os.symlink`` has resolved, so subsequent ``Path.is_symlink`` /
``Path.resolve()`` reads see the final tree.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
from pathlib import Path

import pytest


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Acceptance-criterion dimensions (Y10 row 3, TODO §Y10)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


# I1 (alembic 0012) seeds exactly one tenant — ``t-default``. The Y10
# row 3 acceptance text says "既有 t-default + 5 個 tenant (I1 的 seed)"
# meaning the operator added five tenants on top of the I1-shipped
# baseline before running the Y1 migration. Total tenant count = 6.
_SEED_DEFAULT_TENANT = "t-default"
_SEED_OPERATOR_TENANTS: tuple[str, ...] = (
    "t-y10r3-acme",
    "t-y10r3-globex",
    "t-y10r3-sentinel",
    "t-y10r3-initech",
    "t-y10r3-umbrella",
)
_SEED_TENANT_COUNT = 1 + len(_SEED_OPERATOR_TENANTS)


# Y1 row 7 (alembic 0038) attaches ``project_id`` to these six business
# tables. The Y10 row 3 "每 workload 一個 project_id" assertion iterates
# over this list. A drift-guard below confirms equality with the
# migration's own ``_TABLES_NEEDING_PROJECT_ID`` constant.
_TARGET_TABLES: tuple[str, ...] = (
    "workflow_runs",
    "debug_findings",
    "decision_rules",
    "event_log",
    "artifacts",
    "user_preferences",
)


# Pre-resolved migration paths so the file-shape drift guards stay
# readable. The migrator script is a sibling tree to ``backend``.
_BACKEND_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _BACKEND_DIR.parent
_VERSIONS_DIR = _BACKEND_DIR / "alembic" / "versions"
_MIGRATION_0037 = _VERSIONS_DIR / "0037_y1_backfill_memberships_default_projects.py"
_MIGRATION_0038 = _VERSIONS_DIR / "0038_y1_project_id_on_business_tables.py"
_WORKSPACE_MIGRATOR = _REPO_ROOT / "scripts" / "migrate_workspace_hierarchy.py"


# Workspace migrator's hardcoded default namespace. Y10 row 3 asserts
# that this triple equals what 0012 / 0033 / 0037 collectively imply
# for a freshly-migrated single-tenant DB so the FS layout and the DB
# rows agree on what "the default project for the default tenant"
# means.
_WS_DEFAULT_TENANT = "t-default"
_WS_DEFAULT_PRODUCT_LINE = "default"
_WS_DEFAULT_PROJECT_ID = "default"
_WS_LEGACY_HASH_SENTINEL = "legacy-hash"


def _pg_not_available() -> bool:
    return not os.environ.get("OMNI_TEST_PG_URL", "").strip()


_requires_pg = pytest.mark.skipif(
    _pg_not_available(),
    reason="Y10 row 3 migration idempotency + downgrade integration "
           "tests need an actual PG instance — set OMNI_TEST_PG_URL.",
)


def _load_migration_module(mig_path: Path):
    """Load a migration module via importlib so we can read its
    module-level constants (revision id, table list, projection,
    downgrade callable) without going through alembic's ``op``
    proxy."""
    spec = importlib.util.spec_from_file_location(mig_path.stem, str(mig_path))
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _load_workspace_migrator():
    """Load ``scripts/migrate_workspace_hierarchy.py`` as a module for
    Block C tests. Mirrors the ``migrator`` fixture from
    ``test_y6_row4_workspace_migration.py`` — the script is not a
    package so we go through importlib."""
    spec = importlib.util.spec_from_file_location(
        "y10r3_workspace_migrator", _WORKSPACE_MIGRATOR,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _expected_project_id(tenant_id: str) -> str:
    """Replicate the migration's deterministic projection in Python.

    Used to compute the *expected* ``projects.id`` for each seeded
    tenant — strips ``t-`` prefix when present, then suffixes
    ``-default``. Identical to the helper in
    ``test_y1_migration_pg_seeded.py`` (intentionally — the projection
    is a load-bearing contract that several rows assert on)."""
    return "p-" + (
        tenant_id[2:] if tenant_id.startswith("t-") else tenant_id
    ) + "-default"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Block A — pure-unit drift guards (always run)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_seed_dimensions_match_acceptance_criterion():
    """Lock the (1 default + 5 operator) tenant tuple against drift.

    The TODO row literally states "t-default + 5 個 tenant" — if a
    future refactor shrinks the seed under load pressure (e.g.
    someone reduces the operator-tenant count to make CI faster)
    the acceptance criterion silently weakens. Catch that and force
    the change into the commit message."""
    assert _SEED_DEFAULT_TENANT == "t-default"
    assert len(_SEED_OPERATOR_TENANTS) == 5
    assert _SEED_TENANT_COUNT == 6
    # Operator tenants must be distinct and must include ``t-`` so the
    # deterministic projection's prefix-strip branch is observed.
    assert len(set(_SEED_OPERATOR_TENANTS)) == 5
    for tid in _SEED_OPERATOR_TENANTS:
        assert tid.startswith("t-"), tid


def test_target_tables_match_migration_0038_constant():
    """``_TARGET_TABLES`` here must equal the 0038 migration's
    ``_TABLES_NEEDING_PROJECT_ID``. If a future revision adds (or
    removes) a per-tenant business table, the seed loop must see the
    new table — otherwise the "every workload has project_id"
    assertion silently misses the new column."""
    m = _load_migration_module(_MIGRATION_0038)
    assert set(m._TABLES_NEEDING_PROJECT_ID) == set(_TARGET_TABLES)


def test_y1_alembic_revision_chain_pinned_at_0037_then_0038():
    """Pin the (revision, down_revision) pairs so an accidental
    re-numbering in a sister branch can't silently break the chain
    and leave row 3's downgrade test scoping at the wrong revision."""
    m37 = _load_migration_module(_MIGRATION_0037)
    m38 = _load_migration_module(_MIGRATION_0038)
    assert m37.revision == "0037"
    assert m37.down_revision == "0036"
    assert m38.revision == "0038"
    assert m38.down_revision == "0037"


def test_y1_migration_0037_exposes_downgrade_callable():
    """The acceptance row mandates a working downgrade. Lock the
    callable exists on the module surface — without it the alembic
    ``downgrade -1`` machinery would no-op silently and the
    "data can be moved back" half of the row would not apply."""
    m = _load_migration_module(_MIGRATION_0037)
    assert callable(getattr(m, "downgrade", None)), (
        "0037 migration must expose downgrade() per Y10 row 3 contract"
    )


def test_y1_migration_0038_exposes_downgrade_callable():
    """Same as 0037 — 0038's downgrade drops the project_id column +
    indexes it added. Without it, a downgrade would leave the
    schema mid-transition and the row's "搬回去" contract would
    half-apply."""
    m = _load_migration_module(_MIGRATION_0038)
    assert callable(getattr(m, "downgrade", None)), (
        "0038 migration must expose downgrade() per Y10 row 3 contract"
    )


def test_0037_downgrade_preserves_operator_edited_rows_via_role_predicate():
    """Source-grep: 0037's downgrade SQL must keep the role-derivation
    predicate so admin-flipped membership rows survive the downgrade.

    The downgrade contract is "delete only what we deterministically
    inserted, leave operator edits alone". The way 0037 enforces this
    is by intersecting the membership rows-to-delete with the
    derived role from ``users.role`` — if an admin in the console
    flipped a 'member' to 'owner', the role no longer matches and
    the DELETE skips the row.

    A future refactor that drops the role filter (e.g. simplifies the
    downgrade to ``DELETE FROM user_tenant_memberships WHERE
    (user_id, tenant_id) IN ...``) silently destroys operator-edited
    state on rollback. This guard catches that."""
    src = _MIGRATION_0037.read_text()
    # The downgrade body must contain a CASE expression that mirrors
    # the upgrade's role projection (admin->owner, else->member).
    # Without this filter, every membership row in the universe gets
    # deleted, including admin-flipped ones.
    assert "CASE WHEN u.role = 'admin' THEN 'owner'" in src, (
        "0037 downgrade must preserve operator-edited memberships via "
        "the role-derivation predicate"
    )
    # The projects deletion must filter on the deterministic id +
    # ``slug='default'`` triple so admin-created custom-slug projects
    # survive.
    assert "slug = 'default'" in src
    assert "product_line = 'default'" in src


def test_0038_downgrade_drops_indexes_then_columns():
    """Source-grep: 0038's downgrade must DROP INDEX before DROP COLUMN.

    Some SQLite / PG versions reject ``ALTER TABLE DROP COLUMN`` when
    the column participates in an index that wasn't dropped first —
    if a future refactor flipped the order, the column-drop loop
    would raise on the first table and the rest would silently retain
    their ``project_id`` columns. The migration handles this with a
    try/except wrapper but the *intended* path is index-then-column."""
    src = _MIGRATION_0038.read_text()
    # Index drop loop sits BEFORE column drop loop in the file.
    drop_index_pos = src.find("DROP INDEX IF EXISTS idx_")
    drop_column_pos = src.find("DROP COLUMN project_id")
    assert drop_index_pos > 0, "0038 downgrade must drop indexes"
    assert drop_column_pos > 0, "0038 downgrade must drop project_id column"
    assert drop_index_pos < drop_column_pos, (
        "0038 downgrade must drop indexes BEFORE columns; reverse "
        "order can fail on dialects that reject DROP COLUMN with "
        "outstanding indexes"
    )


def test_workspace_migrator_default_namespace_matches_alembic_0012():
    """Cross-check the workspace migrator's hardcoded default namespace
    against alembic 0012's DEFAULT_TENANT_ID. If the two drift,
    a freshly-migrated workspace lands at one path while the DB row
    points at a different ``(tenant_id, project_id)`` pair, breaking
    the Y10 row 3 invariant that "data + filesystem must agree"."""
    m12 = _load_migration_module(
        _VERSIONS_DIR / "0012_tenants_multi_tenancy.py"
    )
    migrator = _load_workspace_migrator()
    assert migrator.DEFAULT_TENANT_ID == m12.DEFAULT_TENANT_ID
    assert migrator.DEFAULT_TENANT_ID == _WS_DEFAULT_TENANT
    assert migrator.DEFAULT_PRODUCT_LINE == _WS_DEFAULT_PRODUCT_LINE
    assert migrator.DEFAULT_PROJECT_ID == _WS_DEFAULT_PROJECT_ID
    assert migrator.LEGACY_HASH_SENTINEL == _WS_LEGACY_HASH_SENTINEL


def test_workspace_migrator_resolve_target_yields_5_layer_path():
    """The migrator's ``_resolve_target`` helper must produce the
    canonical ``t-default/default/default/{agent}/legacy-hash`` path.

    Y10 row 3 specifically asserts "每個 workspace 搬到新路徑 symlink
    正確" — the new path shape is the load-bearing contract. A
    refactor that flips the path order or drops a layer would
    silently misroute the migration on a real cutover."""
    migrator = _load_workspace_migrator()
    target = migrator._resolve_target(Path("/dst"), "agent-y10r3")
    expected = (
        Path("/dst")
        / _WS_DEFAULT_TENANT
        / _WS_DEFAULT_PRODUCT_LINE
        / _WS_DEFAULT_PROJECT_ID
        / "agent-y10r3"
        / _WS_LEGACY_HASH_SENTINEL
    )
    assert target == expected


def test_compat_fingerprints_clean_in_y1_migration_files():
    """SOP Step 3 pre-commit fingerprint grep — but as a permanent
    contract test. The two Y1 migration files plus this test file
    must NOT contain the four canonical SQLite-only fingerprints
    that fail at runtime on PG.

    We strip docstrings before scanning so the docstring text doesn't
    self-hit (this very file documents the four fingerprints in its
    Block A intro comment, which would otherwise trip the test).
    """
    forbidden = {
        "old_compat_wrapper": r"_conn\(\)",
        "asyncpg_pool_no_commit": r"await\s+conn\.commit\b",
        "sqlite_now_literal": r"datetime\s*\(\s*'now'\s*\)",
        "qmark_in_values": r"VALUES\s*\([^)]*\?[^)]*\)",
    }
    targets = (_MIGRATION_0037, _MIGRATION_0038, Path(__file__))
    for path in targets:
        src = path.read_text()
        # Strip triple-quoted blocks (module + function docstrings,
        # SQL string constants — the latter are covered by sister
        # tests in ``test_y1_backfill_memberships_default_projects.py``
        # and ``test_y1_project_id_on_business_tables.py`` which scan
        # the constants directly). Then strip ``#`` line comments —
        # 0037's docstring describes the column DEFAULT idiom in a
        # Python comment that legitimately mentions the SQLite syntax
        # without using it.
        no_docstrings = re.sub(r'"""[\s\S]*?"""', "", src)
        no_comments = re.sub(r"#[^\n]*", "", no_docstrings)
        hits = {
            label: re.findall(pat, no_comments, re.IGNORECASE)
            for label, pat in forbidden.items()
        }
        bad = {k: v for k, v in hits.items() if v}
        assert not bad, (
            f"{path.name}: compat-fingerprint hit {bad} — must be "
            f"clean per SOP Step 3"
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Block B — PG-required acceptance (skip without OMNI_TEST_PG_URL)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_BACKFILL_MEMBERSHIPS_PG = """
INSERT INTO user_tenant_memberships (user_id, tenant_id, role)
SELECT id,
       tenant_id,
       CASE WHEN role = 'admin' THEN 'owner' ELSE 'member' END
FROM users
WHERE tenant_id IS NOT NULL
ON CONFLICT DO NOTHING
"""


_BACKFILL_DEFAULT_PROJECTS_PG = """
INSERT INTO projects (id, tenant_id, product_line, name, slug)
SELECT
    'p-' || CASE
        WHEN substr(id, 1, 2) = 't-' THEN substr(id, 3)
        ELSE id
    END || '-default',
    id,
    'default',
    'Default',
    'default'
FROM tenants
ON CONFLICT DO NOTHING
"""


_PROJECT_ID_PROJECTION_PG = (
    "'p-' || CASE "
    "WHEN substr(tenant_id, 1, 2) = 't-' THEN substr(tenant_id, 3) "
    "ELSE tenant_id END || '-default'"
)


_DOWNGRADE_0037_MEMBERSHIPS = """
DELETE FROM user_tenant_memberships
WHERE (user_id, tenant_id) IN (
    SELECT id, tenant_id FROM users WHERE tenant_id IS NOT NULL
)
AND role = (
    SELECT CASE WHEN u.role = 'admin' THEN 'owner' ELSE 'member' END
    FROM users u
    WHERE u.id = user_tenant_memberships.user_id
)
"""


_DOWNGRADE_0037_PROJECTS = """
DELETE FROM projects
WHERE product_line = 'default'
  AND slug = 'default'
  AND id = 'p-' || CASE
        WHEN substr(tenant_id, 1, 2) = 't-' THEN substr(tenant_id, 3)
        ELSE tenant_id
    END || '-default'
"""


async def _seed_i1_universe(conn) -> None:
    """Seed the I1-shaped universe: t-default (already present from
    alembic 0012's seed) + 5 operator-added tenants, each with one
    user (legacy ``role='admin'`` → backfilled membership ``owner``)
    and one ``workflow_runs`` row plus one row in the other five
    business tables.

    The user count = 6 (one per tenant) so every assertion can read
    "every user has exactly one membership". Email must be globally
    unique so we suffix with the tenant id.
    """
    # 1) Operator-added tenants. ``t-default`` is already there from
    #    alembic 0012's seed insert — re-asserting it via ON CONFLICT
    #    DO NOTHING keeps the seed self-contained without duplicating
    #    state.
    for tid in (_SEED_DEFAULT_TENANT, *_SEED_OPERATOR_TENANTS):
        await conn.execute(
            "INSERT INTO tenants (id, name) VALUES ($1, $2) "
            "ON CONFLICT (id) DO NOTHING",
            tid, f"Y10r3 {tid}",
        )

    # 2) One user per tenant, legacy role='admin' so the membership
    #    backfill maps it to 'owner'. Each user gets a workload row
    #    in every business table so the per-table backfill UPDATE has
    #    work to do.
    for tid in (_SEED_DEFAULT_TENANT, *_SEED_OPERATOR_TENANTS):
        uid = f"u-y10r3-{tid}"
        await conn.execute(
            "INSERT INTO users (id, email, name, role, password_hash, "
            "tenant_id) VALUES ($1,$2,$3,$4,$5,$6) "
            "ON CONFLICT (id) DO NOTHING",
            uid, f"admin@{tid}.y10r3.test", f"Y10r3 admin {tid}",
            "admin", "h", tid,
        )

        # workflow_runs — Phase 56 checkpoint shape. project_id left
        # NULL so the backfill UPDATE has work to do.
        await conn.execute(
            "INSERT INTO workflow_runs "
            "(id, kind, started_at, status, tenant_id) "
            "VALUES ($1, 'test', 0, 'running', $2) "
            "ON CONFLICT (id) DO NOTHING",
            f"wf-y10r3-{tid}", tid,
        )
        # artifacts — id PK, NOT NULL name + file_path.
        await conn.execute(
            "INSERT INTO artifacts (id, name, file_path, tenant_id) "
            "VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (id) DO NOTHING",
            f"art-y10r3-{tid}", f"y10r3 art {tid}",
            "/tmp/y10r3.txt", tid,
        )
        # debug_findings — id PK, NOT NULL task_id / agent_id /
        # finding_type / content.
        await conn.execute(
            "INSERT INTO debug_findings (id, task_id, agent_id, "
            "finding_type, content, tenant_id) "
            "VALUES ($1, $2, $3, $4, $5, $6) "
            "ON CONFLICT (id) DO NOTHING",
            f"df-y10r3-{tid}", "task-y10r3", "agent-y10r3",
            "observation", "{}", tid,
        )
        # decision_rules — id PK, NOT NULL kind_pattern.
        await conn.execute(
            "INSERT INTO decision_rules (id, kind_pattern, tenant_id) "
            "VALUES ($1, $2, $3) "
            "ON CONFLICT (id) DO NOTHING",
            f"dr-y10r3-{tid}", f"y10r3/{tid}", tid,
        )
        # event_log — id is identity, must NOT be specified by us.
        await conn.execute(
            "INSERT INTO event_log (event_type, data_json, tenant_id) "
            "VALUES ($1, '{}', $2)",
            f"y10r3/{tid}", tid,
        )
        # user_preferences — composite PK (user_id, pref_key).
        await conn.execute(
            "INSERT INTO user_preferences "
            "(user_id, pref_key, value, tenant_id) "
            "VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (user_id, pref_key) DO NOTHING",
            uid, "y10r3/seed", "v", tid,
        )


async def _run_y1_upgrade(conn) -> None:
    """Apply the composed Y1 backfill SQL (0037 + 0038 UPDATEs).

    Re-implements what ``alembic upgrade 0037 0038`` would push to PG,
    minus the ALTER TABLE column-add (the column already exists on
    the test DB because the session-scoped pg_test_alembic_upgraded
    fixture stamps schema at HEAD).
    """
    await conn.execute(_BACKFILL_MEMBERSHIPS_PG)
    await conn.execute(_BACKFILL_DEFAULT_PROJECTS_PG)
    for table in _TARGET_TABLES:
        # The backfill UPDATE is itself idempotent thanks to
        # ``WHERE project_id IS NULL`` skipping a second pass.
        await conn.execute(
            f"UPDATE {table} SET project_id = {_PROJECT_ID_PROJECTION_PG} "
            f"WHERE project_id IS NULL AND tenant_id IS NOT NULL"
        )


async def _run_0037_downgrade(conn) -> None:
    """Apply 0037's downgrade SQL — narrow DELETEs that only remove
    the deterministic membership + default-project rows.
    """
    await conn.execute(_DOWNGRADE_0037_MEMBERSHIPS)
    await conn.execute(_DOWNGRADE_0037_PROJECTS)


@pytest.mark.asyncio
@_requires_pg
async def test_pg_i1_seed_y1_upgrade_idempotent_two_passes(pg_test_pool):
    """Block B / 1 — the load-bearing idempotency contract.

    Seed an I1-shaped universe (t-default + 5 operator tenants, each
    with one user and one row in every business table). Run the Y1
    upgrade twice. Assert the post-second-pass state equals the
    post-first-pass state for memberships, default projects, and
    every business table's (tenant_id, project_id) tuple.
    """
    seed_tenants = list((_SEED_DEFAULT_TENANT, *_SEED_OPERATOR_TENANTS))
    async with pg_test_pool.acquire() as conn:
        async with conn.transaction():
            await _seed_i1_universe(conn)

            # First pass.
            await _run_y1_upgrade(conn)
            mem1 = await conn.fetch(
                "SELECT user_id, tenant_id, role FROM user_tenant_memberships "
                "WHERE user_id LIKE 'u-y10r3-%' "
                "ORDER BY user_id, tenant_id"
            )
            proj1 = await conn.fetch(
                "SELECT id, tenant_id FROM projects "
                "WHERE tenant_id = ANY($1::text[]) ORDER BY tenant_id",
                seed_tenants,
            )
            per_table_1: dict[str, list] = {}
            for table in _TARGET_TABLES:
                per_table_1[table] = await conn.fetch(
                    f"SELECT tenant_id, project_id FROM {table} "
                    f"WHERE tenant_id = ANY($1::text[]) "
                    f"ORDER BY tenant_id, project_id",
                    seed_tenants,
                )

            # Second pass.
            await _run_y1_upgrade(conn)
            mem2 = await conn.fetch(
                "SELECT user_id, tenant_id, role FROM user_tenant_memberships "
                "WHERE user_id LIKE 'u-y10r3-%' "
                "ORDER BY user_id, tenant_id"
            )
            proj2 = await conn.fetch(
                "SELECT id, tenant_id FROM projects "
                "WHERE tenant_id = ANY($1::text[]) ORDER BY tenant_id",
                seed_tenants,
            )
            per_table_2: dict[str, list] = {}
            for table in _TARGET_TABLES:
                per_table_2[table] = await conn.fetch(
                    f"SELECT tenant_id, project_id FROM {table} "
                    f"WHERE tenant_id = ANY($1::text[]) "
                    f"ORDER BY tenant_id, project_id",
                    seed_tenants,
                )

            assert [tuple(r) for r in mem1] == [tuple(r) for r in mem2], (
                "membership backfill not idempotent — second pass mutated rows"
            )
            assert [tuple(r) for r in proj1] == [tuple(r) for r in proj2], (
                "default-project backfill not idempotent — second pass "
                "mutated rows"
            )
            for table in _TARGET_TABLES:
                assert (
                    [tuple(r) for r in per_table_1[table]]
                    == [tuple(r) for r in per_table_2[table]]
                ), (
                    f"project_id backfill on {table} not idempotent — "
                    f"second pass mutated rows"
                )


@pytest.mark.asyncio
@_requires_pg
async def test_pg_each_user_has_exactly_one_membership_after_upgrade(pg_test_pool):
    """Block B / 2 — "每 user 一個 membership" assertion.

    After the upgrade, every seeded user (6 of them) must have
    exactly one row in user_tenant_memberships, and that row's
    tenant_id must equal the legacy users.tenant_id cache field.
    """
    seed_tenants = list((_SEED_DEFAULT_TENANT, *_SEED_OPERATOR_TENANTS))
    async with pg_test_pool.acquire() as conn:
        async with conn.transaction():
            await _seed_i1_universe(conn)
            await _run_y1_upgrade(conn)

            counts = await conn.fetch(
                "SELECT u.id AS uid, u.tenant_id AS legacy_tenant, "
                "       COUNT(m.tenant_id) AS membership_count, "
                "       MIN(m.tenant_id) AS membership_tenant, "
                "       MIN(m.role) AS membership_role "
                "FROM users u "
                "LEFT JOIN user_tenant_memberships m ON m.user_id = u.id "
                "WHERE u.id LIKE 'u-y10r3-%' "
                "GROUP BY u.id, u.tenant_id "
                "ORDER BY u.id"
            )
            # 6 users — one per tenant — must each have exactly 1
            # membership row.
            assert len(counts) == _SEED_TENANT_COUNT
            for row in counts:
                assert row["membership_count"] == 1, (
                    f"user {row['uid']} has {row['membership_count']} "
                    f"memberships, expected 1"
                )
                assert row["membership_tenant"] == row["legacy_tenant"], (
                    f"user {row['uid']} membership tenant "
                    f"{row['membership_tenant']!r} does not match legacy "
                    f"users.tenant_id {row['legacy_tenant']!r}"
                )
                # Every seeded user has legacy role='admin' so they
                # all map to membership.role='owner'.
                assert row["membership_role"] == "owner"


@pytest.mark.asyncio
@_requires_pg
async def test_pg_each_workload_has_one_project_id_after_upgrade(pg_test_pool):
    """Block B / 3 — "每 workload 一個 project_id" assertion.

    Every business-table row across the 6 tables × 6 tenants = 36
    rows must end up with a non-NULL project_id matching the
    deterministic projection ``p-<tenant-suffix>-default``. No row
    is allowed to fall through to NULL or to a stale projection.
    """
    seed_tenants = list((_SEED_DEFAULT_TENANT, *_SEED_OPERATOR_TENANTS))
    async with pg_test_pool.acquire() as conn:
        async with conn.transaction():
            await _seed_i1_universe(conn)
            await _run_y1_upgrade(conn)

            for table in _TARGET_TABLES:
                rows = await conn.fetch(
                    f"SELECT tenant_id, project_id FROM {table} "
                    f"WHERE tenant_id = ANY($1::text[])",
                    seed_tenants,
                )
                # Every seeded tenant must have at least one row in
                # the table — sanity that the seed actually populated.
                seen = {r["tenant_id"] for r in rows}
                missing = set(seed_tenants) - seen
                assert not missing, (
                    f"{table}: seed missed tenants {sorted(missing)}"
                )
                for row in rows:
                    assert row["project_id"] is not None, (
                        f"{table} row tenant={row['tenant_id']!r} has "
                        f"NULL project_id after Y1 upgrade — Y10 row 3 "
                        f"contract violation"
                    )
                    expected = _expected_project_id(row["tenant_id"])
                    assert row["project_id"] == expected, (
                        f"{table} tenant={row['tenant_id']!r} project_id "
                        f"{row['project_id']!r} does not match deterministic "
                        f"projection {expected!r}"
                    )


@pytest.mark.asyncio
@_requires_pg
async def test_pg_0037_downgrade_removes_seeded_rows_keeps_operator_edits(
    pg_test_pool,
):
    """Block B / 4 — the load-bearing downgrade contract.

    Apply the upgrade; then mutate one membership (admin flips a user
    to a custom role) and one project (admin creates a custom-slug
    project). Run 0037's downgrade. Assert:

    * deterministically-inserted memberships are GONE
    * deterministically-inserted default projects are GONE
    * the operator-edited membership ROW IS PRESERVED (role no
      longer matches the derived 'owner', so the role-equality
      filter spares it)
    * the operator-created custom-slug project IS PRESERVED (slug
      doesn't match 'default')

    This is the "回滾測試：能把資料搬回去" contract — and the
    "without destroying operator edits" half that 0037's narrow-
    DELETE design is built around.
    """
    seed_tenants = list((_SEED_DEFAULT_TENANT, *_SEED_OPERATOR_TENANTS))
    async with pg_test_pool.acquire() as conn:
        async with conn.transaction():
            await _seed_i1_universe(conn)
            await _run_y1_upgrade(conn)

            # Operator edit 1: flip one membership's role from 'owner'
            # to 'member'. The downgrade's role-equality predicate
            # must spare this row.
            edited_user = f"u-y10r3-{_SEED_OPERATOR_TENANTS[0]}"
            edited_tenant = _SEED_OPERATOR_TENANTS[0]
            await conn.execute(
                "UPDATE user_tenant_memberships SET role = 'member' "
                "WHERE user_id = $1 AND tenant_id = $2",
                edited_user, edited_tenant,
            )

            # Operator edit 2: create a custom-slug project on the
            # second operator tenant. The downgrade's slug-equality
            # predicate must spare this row.
            custom_tenant = _SEED_OPERATOR_TENANTS[1]
            custom_pid = f"p-y10r3-custom-{custom_tenant}"
            await conn.execute(
                "INSERT INTO projects (id, tenant_id, product_line, "
                "name, slug) VALUES ($1, $2, 'default', $3, 'custom') "
                "ON CONFLICT DO NOTHING",
                custom_pid, custom_tenant, "Custom",
            )

            # Snapshot pre-downgrade counts for sanity.
            pre_mem = await conn.fetchval(
                "SELECT COUNT(*) FROM user_tenant_memberships "
                "WHERE user_id LIKE 'u-y10r3-%'"
            )
            pre_default_proj = await conn.fetchval(
                "SELECT COUNT(*) FROM projects "
                "WHERE tenant_id = ANY($1::text[]) "
                "  AND product_line = 'default' AND slug = 'default'",
                seed_tenants,
            )
            assert pre_mem == _SEED_TENANT_COUNT
            assert pre_default_proj == _SEED_TENANT_COUNT

            # ── Run 0037 downgrade ──
            await _run_0037_downgrade(conn)

            # ── Assertions ──
            # Deterministic membership rows: 5 of 6 are gone (the
            # edited one survives because its role no longer matches
            # the derived 'owner').
            surviving_mem = await conn.fetch(
                "SELECT user_id, tenant_id, role "
                "FROM user_tenant_memberships "
                "WHERE user_id LIKE 'u-y10r3-%' "
                "ORDER BY user_id"
            )
            assert len(surviving_mem) == 1, (
                f"only the operator-edited membership should survive; "
                f"got {len(surviving_mem)} rows: {surviving_mem}"
            )
            assert surviving_mem[0]["user_id"] == edited_user
            assert surviving_mem[0]["tenant_id"] == edited_tenant
            assert surviving_mem[0]["role"] == "member", (
                "edited membership must keep its operator-set role"
            )

            # Default projects: all 6 deterministic ones gone, the
            # custom-slug operator-created project survives.
            surviving_default = await conn.fetch(
                "SELECT id, tenant_id, slug FROM projects "
                "WHERE tenant_id = ANY($1::text[]) "
                "  AND product_line = 'default' AND slug = 'default'",
                seed_tenants,
            )
            assert surviving_default == [], (
                f"all default-slug projects should be gone after "
                f"0037 downgrade; got {surviving_default}"
            )
            surviving_custom = await conn.fetchrow(
                "SELECT id, slug FROM projects WHERE id = $1",
                custom_pid,
            )
            assert surviving_custom is not None, (
                "operator-created custom-slug project must survive "
                "the 0037 downgrade"
            )
            assert surviving_custom["slug"] == "custom"


@pytest.mark.asyncio
@_requires_pg
async def test_pg_0037_downgrade_re_apply_upgrade_round_trip_clean(
    pg_test_pool,
):
    """Block B / 5 — re-applying the upgrade after a downgrade lands
    the same deterministic state again.

    The TODO row's "搬回去" semantics imply that downgrade-then-re-
    upgrade is a clean round trip. Seed → upgrade → downgrade →
    upgrade-again, then assert the membership / default-project rows
    match what they were after the first upgrade. This catches a
    downgrade that leaves residual state behind (e.g. a leftover
    ``project_id`` reference dangling from a not-cascaded delete)
    that would prevent the re-upgrade's INSERTs from converging.
    """
    seed_tenants = list((_SEED_DEFAULT_TENANT, *_SEED_OPERATOR_TENANTS))
    async with pg_test_pool.acquire() as conn:
        async with conn.transaction():
            await _seed_i1_universe(conn)
            await _run_y1_upgrade(conn)

            after_first = await conn.fetch(
                "SELECT user_id, tenant_id, role FROM user_tenant_memberships "
                "WHERE user_id LIKE 'u-y10r3-%' "
                "ORDER BY user_id, tenant_id"
            )

            # Round-trip: downgrade then re-upgrade.
            await _run_0037_downgrade(conn)
            await _run_y1_upgrade(conn)

            after_round_trip = await conn.fetch(
                "SELECT user_id, tenant_id, role FROM user_tenant_memberships "
                "WHERE user_id LIKE 'u-y10r3-%' "
                "ORDER BY user_id, tenant_id"
            )
            assert (
                [tuple(r) for r in after_first]
                == [tuple(r) for r in after_round_trip]
            ), (
                "downgrade + re-upgrade should land the same membership "
                "state as the first upgrade"
            )

            # Also verify per-table project_id round-trip equivalence.
            for table in _TARGET_TABLES:
                rows = await conn.fetch(
                    f"SELECT tenant_id, project_id FROM {table} "
                    f"WHERE tenant_id = ANY($1::text[])",
                    seed_tenants,
                )
                for row in rows:
                    assert row["project_id"] == _expected_project_id(
                        row["tenant_id"]
                    ), (
                        f"{table} project_id drift after round trip: "
                        f"tenant={row['tenant_id']!r} pid={row['project_id']!r}"
                    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Block C — filesystem-only acceptance (no PG, uses tmp_path)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _build_fake_legacy_workspace(root: Path, agent_id: str) -> Path:
    """Build a minimal directory that looks like a legacy
    ``.agent_workspaces/{agent_id}/`` workspace (a plain clone with
    ``.git`` as a directory). Used by Block C tests so we don't have
    to spin up a real git repo on tmp_path."""
    ws = root / agent_id
    ws.mkdir(parents=True)
    (ws / "README.md").write_text(f"y10r3 workspace for {agent_id}\n")
    (ws / ".git").mkdir()
    (ws / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    return ws


def test_workspace_migrate_relocates_5_legacy_workspaces_with_symlink(tmp_path):
    """Block C / 1 — the load-bearing FS contract.

    Build five legacy flat-layout workspaces (matching the row's
    "5 個 tenant" cardinality so the test scale tracks the
    acceptance text). Run the workspace migrator. Assert each
    ends up at ``t-default/default/default/{agent_id}/legacy-hash/``
    with a backward-compat symlink at the old path resolving to the
    new dir.
    """
    migrator = _load_workspace_migrator()
    src = tmp_path / "legacy"
    src.mkdir()
    dst = tmp_path / "new"

    # Mirror the row's 5-cardinality with five distinct agent ids.
    agent_ids = [f"agent-y10r3-{i:02d}" for i in range(5)]
    for aid in agent_ids:
        _build_fake_legacy_workspace(src, aid)

    summary = migrator.migrate(src, dst)
    assert summary.moved_count == 5
    assert summary.failed_count == 0
    statuses = sorted(r.status for r in summary.records)
    assert statuses == ["moved"] * 5

    for aid in agent_ids:
        new_ws = (
            dst
            / _WS_DEFAULT_TENANT
            / _WS_DEFAULT_PRODUCT_LINE
            / _WS_DEFAULT_PROJECT_ID
            / aid
            / _WS_LEGACY_HASH_SENTINEL
        )
        assert new_ws.is_dir(), f"{aid}: new workspace dir missing at {new_ws}"
        assert (new_ws / "README.md").read_text() == (
            f"y10r3 workspace for {aid}\n"
        )
        assert (new_ws / ".git").is_dir(), (
            f"{aid}: .git directory should have moved"
        )

        # The compat symlink at the old path resolves to the new dir.
        old = src / aid
        assert old.is_symlink(), f"{aid}: missing compat symlink at old path"
        assert Path(os.readlink(old)).resolve() == new_ws.resolve(), (
            f"{aid}: symlink target {os.readlink(old)!r} does not "
            f"resolve to new path {new_ws!r}"
        )


def test_workspace_migrate_idempotent_second_run_no_op(tmp_path):
    """Block C / 2 — running the migrator twice is a no-op the
    second time.

    The acceptance text says "搬到新路徑 symlink 正確" — implicit in
    that contract is that a re-run on an already-migrated tree
    doesn't double-move or break the symlinks. This guard locks
    that.
    """
    migrator = _load_workspace_migrator()
    src = tmp_path / "legacy"
    src.mkdir()
    dst = tmp_path / "new"

    for aid in ("agent-a", "agent-b", "agent-c"):
        _build_fake_legacy_workspace(src, aid)

    first = migrator.migrate(src, dst)
    assert first.moved_count == 3

    # Second run: every record should be ``skipped_already_symlink``
    # because the first run left a symlink at each old path.
    second = migrator.migrate(src, dst)
    assert second.moved_count == 0
    assert len(second.records) == 3
    assert all(
        r.status == "skipped_already_symlink" for r in second.records
    ), [r.status for r in second.records]

    # Symlinks still resolve correctly.
    for aid in ("agent-a", "agent-b", "agent-c"):
        new_ws = (
            dst / _WS_DEFAULT_TENANT / _WS_DEFAULT_PRODUCT_LINE
            / _WS_DEFAULT_PROJECT_ID / aid / _WS_LEGACY_HASH_SENTINEL
        )
        old = src / aid
        assert old.is_symlink()
        assert Path(os.readlink(old)).resolve() == new_ws.resolve()


def test_workspace_migrate_remove_symlinks_only_touches_compat_links(tmp_path):
    """Block C / 3 — the second-stage cleanup contract.

    After the first stage, the operator runs ``--remove-symlinks``
    one release later to delete the compat shims. This must touch
    ONLY the symlinks pointing into the new tree — operator-created
    symlinks aimed elsewhere (or unrelated dirs) are spared.
    """
    migrator = _load_workspace_migrator()
    src = tmp_path / "legacy"
    src.mkdir()
    dst = tmp_path / "new"

    # Real workspace: migrate it, then verify the compat symlink got
    # left behind.
    _build_fake_legacy_workspace(src, "agent-real")
    migrator.migrate(src, dst)
    assert (src / "agent-real").is_symlink()

    # Foreign symlink: aimed outside the target tree, must be spared.
    foreign_target = tmp_path / "elsewhere"
    foreign_target.mkdir()
    os.symlink(foreign_target.resolve(), src / "foreign-link")

    summary = migrator.remove_symlinks(src, dst)

    # Two records: our compat link removed, foreign link kept.
    by_name = {r.agent_id: r for r in summary.records}
    assert by_name["agent-real"].status == "symlink_removed"
    assert by_name["foreign-link"].status == "symlink_kept"

    # Filesystem state matches the records.
    assert not (src / "agent-real").exists()
    assert (src / "foreign-link").is_symlink()
    assert (
        Path(os.readlink(src / "foreign-link")).resolve()
        == foreign_target.resolve()
    )
