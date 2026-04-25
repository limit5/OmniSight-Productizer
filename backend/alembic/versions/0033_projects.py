"""Y1 row 2 (#277) — projects table.

Adds the project layer that lives between ``tenants`` and the existing
business tables (workflow_runs / debug_findings / decisions / event_log
/ artifacts / spec_* / user_preferences).  Projects are the unit at
which budgets / quotas / sharing get bound — Y1 row 5 will add
``project_shares`` (cross-tenant guest access) and the membership
backfill row will pour every existing tenant's workload into a single
``(tenant_id, product_line='default', slug='default')`` project.

Why a new table now (before the project_id column is backfilled on
existing business tables)
─────────────────────────────────────────────────────────────────
The dependency arrow points this way: business tables can't carry a
``project_id`` FK until the parent table they FK to actually exists.
So the standalone empty-table create here is the prerequisite for the
later row "所有既有業務表加 project_id 欄位" — no app code changes in
this revision, only the schema is materialised.

Schema decisions
────────────────
* **TEXT primary key with ``p-`` prefix convention** — matches
  ``tenants.id`` (``t-*``) and ``users.id`` (``u-*``).  No INTEGER
  IDENTITY because (a) we want ids that survive cross-DB replay
  without sequence reshuffling, (b) ``project_runs.project_id`` is
  already TEXT (alembic 0006) and we don't want a join-time cast.
  → NOT in ``TABLES_WITH_IDENTITY_ID``.
* **UNIQUE ``(tenant_id, product_line, slug)``** is the contract from
  the TODO row.  ``product_line`` is the second-tier namespace (e.g.
  ``default`` / ``firmware`` / ``algo`` for the embedded-AI camera
  vertical) so two projects with slug ``isp-tuning`` can coexist if
  they're in different product lines.  Implemented as a UNIQUE
  constraint, which on both PG and SQLite materialises a unique index
  whose leading column is ``tenant_id`` — that index is also the
  fast path for "list all projects of tenant X", so we don't add a
  separate single-column ``idx_projects_tenant``.
* **``parent_id`` self-FK with ``ON DELETE SET NULL``** — children of
  a deleted parent are promoted to top-level rather than cascaded
  out.  CASCADE on a self-FK would silently delete sub-trees; SET NULL
  is the safer default for a hierarchy where leaf data (artifacts,
  workflow_runs, ...) eventually FKs into ``projects.id`` and would
  otherwise vanish.  A CHECK ``parent_id <> id`` blocks the trivial
  self-loop; deeper cycle detection is application-layer (a tree
  walk on insert in Y3's POST /projects).
* **Same-tenant FK on parent is NOT enforced at DB level** in this
  revision.  Doing so cleanly needs a composite UNIQUE
  ``(id, tenant_id)`` + composite FK on ``(parent_id, tenant_id)`` —
  doable but adds noise.  The Y3 route layer will validate it before
  insert; the Y1 测试 row will add a drift guard test that no row in
  prod violates the invariant.
* **``plan_override`` / ``disk_budget_bytes`` / ``llm_budget_tokens``
  NULL ⇒ inherit tenant** — encoded as nullable columns; the resolver
  in Y2/Y3 (``project_quota.resolve(tenant, project)``) will coalesce
  ``project.X ?? tenant.X``.  CHECK constraints reject negative
  budgets and unknown plan names so a typo in an admin REST call
  can't poison the resolver.
* **``created_by`` FK with ``ON DELETE SET NULL``** — a project
  outlives the user who created it (audit-only field; ownership is
  separately tracked via Y1 row 3 ``project_members``).  CASCADE
  here would delete the project when a user is hard-deleted, which
  contradicts the audit semantics.
* **``created_at`` is added even though the TODO column list omits
  it** — every other table in this codebase carries one, ``archived_at``
  is meaningless without a ``created_at`` to compute age, and audit
  routes (Y2 GET /admin/tenants/{id}) need it.  Documented here so
  the deviation from the literal TODO is visible.
* **CHECK on ``slug`` / ``name`` / ``product_line`` lengths** — bounds
  storage and stops a rogue 1-MB-string admin payload from poisoning
  the UNIQUE index.  The slug character class is enforced at the
  application layer (Y2 will reject non ``^[a-z0-9-]+$``); the DB
  level check is just length.
* **No same-tenant CHECK between project and tenant for ``parent_id``
  here** — see above; deferred to Y1 测试 row's drift guard.

Indexes
───────
1. UNIQUE ``(tenant_id, product_line, slug)`` — auto-creates the
   composite index that doubles as the per-tenant listing index.
2. Partial ``idx_projects_parent`` on ``parent_id WHERE parent_id IS
   NOT NULL`` — children-of-parent fan-out.  Partial because most
   projects are top-level; a full index over a column that's mostly
   NULL is wasted bytes.
3. Partial ``idx_projects_tenant_active`` on ``(tenant_id) WHERE
   archived_at IS NULL`` — the very common "show me my live projects"
   query never wants archived rows.  Partial keeps it small even
   under heavy churn (hard-deleting projects is policy-discouraged
   so the archived population grows monotonically).
4. Partial ``idx_projects_created_by`` on ``(created_by) WHERE
   created_by IS NOT NULL`` — supports the admin "projects created
   by user X" view without scanning the table.

Module-global / cross-worker state audit
────────────────────────────────────────
Pure DDL; no in-memory cache, no module-level singleton.  Every
worker reads ``projects`` rows from the same PG (or local SQLite in
dev), so cross-worker consistency is the database's problem, not the
process's.

Read-after-write timing audit
─────────────────────────────
No behaviour change: nothing reads from ``projects`` yet.  The first
read path lands with Y2 admin tenant detail (GET
``/api/v1/admin/tenants/{id}``).

Production readiness gate
─────────────────────────
* No new Python / OS package — production image needs no rebuild.
* Schema migration drift guards: ``scripts/migrate_sqlite_to_pg.py``
  ``TABLES_IN_ORDER`` updated in the same commit; the ``projects``
  table is NOT added to ``TABLES_WITH_IDENTITY_ID`` because its PK
  is TEXT.  ``test_migrator_lists_projects`` in this commit's test
  module asserts both.
* Production status after this commit: **dev-only**.  Next gate is
  ``deployed-inactive`` — operator runs ``alembic upgrade head`` on
  the prod PG instance.  No env knob change required (the table is
  empty until a future feature inserts into it).

Dialect handling
────────────────
DDL goes through the ``alembic_pg_compat`` shim (see ``env.py``):

* ``datetime('now')``  → ``to_char(now(), 'YYYY-MM-DD HH24:MI:SS')``
* Plain SQL string after rewrite is consumed by both dialects.

Revision ID: 0033
Revises: 0032
Create Date: 2026-04-25
"""
from __future__ import annotations

from alembic import op


revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS projects (
    id                  TEXT PRIMARY KEY,
    tenant_id           TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    product_line        TEXT NOT NULL DEFAULT 'default',
    name                TEXT NOT NULL,
    slug                TEXT NOT NULL,
    parent_id           TEXT REFERENCES projects(id) ON DELETE SET NULL,
    plan_override       TEXT,
    disk_budget_bytes   INTEGER,
    llm_budget_tokens   INTEGER,
    created_by          TEXT REFERENCES users(id) ON DELETE SET NULL,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    archived_at         TEXT,
    UNIQUE (tenant_id, product_line, slug),
    CHECK (parent_id IS NULL OR parent_id <> id),
    CHECK (
        plan_override IS NULL
        OR plan_override IN ('free', 'starter', 'pro', 'enterprise')
    ),
    CHECK (disk_budget_bytes IS NULL OR disk_budget_bytes >= 0),
    CHECK (llm_budget_tokens IS NULL OR llm_budget_tokens >= 0),
    CHECK (length(name) >= 1 AND length(name) <= 200),
    CHECK (length(slug) >= 1 AND length(slug) <= 64),
    CHECK (length(product_line) >= 1 AND length(product_line) <= 64)
)
"""

_INDEXES = (
    # Children-of-parent fan-out for tree views.  Partial because
    # most projects are top-level; indexing the NULL majority would
    # waste bytes without speeding any query (planner skips the
    # index for ``WHERE parent_id IS NULL`` on a partial index that
    # explicitly excludes them).
    "CREATE INDEX IF NOT EXISTS idx_projects_parent "
    "ON projects(parent_id) "
    "WHERE parent_id IS NOT NULL",
    # Hot path for "list my live projects" — the sidebar / project
    # picker / admin tenant detail all want non-archived rows.
    # Partial keeps the index tight under archive churn.
    "CREATE INDEX IF NOT EXISTS idx_projects_tenant_active "
    "ON projects(tenant_id) "
    "WHERE archived_at IS NULL",
    # Admin "projects created by user X" view (Y2 audit detail).
    # Partial because ``created_by`` is nullable (set NULL on user
    # delete) and the NULL rows aren't useful for this lookup.
    "CREATE INDEX IF NOT EXISTS idx_projects_created_by "
    "ON projects(created_by) "
    "WHERE created_by IS NOT NULL",
)


def upgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql(_CREATE_TABLE)
    for stmt in _INDEXES:
        conn.exec_driver_sql(stmt)


def downgrade() -> None:
    conn = op.get_bind()
    for idx in (
        "idx_projects_created_by",
        "idx_projects_tenant_active",
        "idx_projects_parent",
    ):
        conn.exec_driver_sql(f"DROP INDEX IF EXISTS {idx}")
    op.execute("DROP TABLE IF EXISTS projects")
