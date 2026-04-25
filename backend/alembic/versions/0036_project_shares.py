"""Y1 row 5 (#277) — project_shares table.

Adds the cross-tenant share table that lets ``tenant A`` (the project's
owning tenant) grant ``tenant B`` (the guest tenant) read- or write-
level access to one of A's projects, without materialising membership
rows on either side and without B paying A's egress / quota costs.

Conceptually a ``project_shares`` row says: *"members of guest tenant
``B`` can act on project ``P`` (which belongs to tenant ``A``) as if
they had this project-level role, until ``expires_at``."*  The
authorisation resolver in Y3 will combine this with the existing
``user_tenant_memberships`` (Y1 row 1) and ``project_members`` (Y1
row 3) tables: a request from user ``U`` to act on project ``P`` is
allowed iff at least one of the three sources grants it.

Why a new table now (instead of folding it into project_members)
─────────────────────────────────────────────────────────────────
``project_members`` is per-user; share is per-tenant.  Modeling the
share as N rows in ``project_members`` (one per member of the guest
tenant) would (a) double-write every time the guest tenant adds /
removes a member, (b) leak the project owner's user roster to the
guest tenant's admin console (since the latter would see foreign
``user_id`` rows it can't manage), and (c) make ``expires_at`` per-row
instead of per-share.  A standalone table keeps the share atomic and
its lifecycle independent of either tenant's membership churn.

Schema decisions
────────────────
* **TEXT primary key with ``psh-`` prefix convention** — matches the
  existing app-generated TEXT-PK pattern (``t-*`` tenants, ``u-*``
  users, ``p-*`` projects, ``inv-*`` invites, ``psh-*`` shares).  No
  INTEGER IDENTITY because (a) ids should survive cross-DB replay
  without sequence reshuffling and (b) every other Y1 table uses a
  TEXT PK so the convention stays uniform.
  → NOT in ``TABLES_WITH_IDENTITY_ID``.
* **``project_id`` FK ``ON DELETE CASCADE``** — when the underlying
  project is hard-deleted, the share has no semantic value.  Note:
  the policy is to *archive* projects (``archived_at``), not hard-
  delete, but the rare admin rollback path must clean up cleanly.
  Same logic as ``project_members.project_id``.
* **``guest_tenant_id`` FK ``ON DELETE CASCADE``** — when the guest
  tenant is offboarded, every share they held has nothing to grant.
  Cascading keeps the row count bounded under tenant churn and stops
  the row from referencing a tombstoned tenant.
* **``role`` CHECK ``IN ('viewer', 'contributor')``** — the project-
  level enum minus ``owner``.  A guest tenant fundamentally cannot
  own a project belonging to a different tenant — ownership implies
  the right to delete the project, change its budget, transfer it,
  and close down the parent tenant context, none of which is a sane
  cross-tenant operation.  Pushing the restriction into the DB
  CHECK rejects garbage roles even if a Y3 route layer regresses
  and tries to grant 'owner' through this surface.  Deliberately
  NOT the tenant-level enum (``owner / admin / member / viewer``) —
  shares grant *project-scope* access, not tenant-scope.
* **``granted_by`` FK ``ON DELETE SET NULL``** — the share outlives
  the granter.  Audit-only field; deleting the admin who created the
  share must NOT void the share itself (otherwise rotating an admin
  silently revokes every share they granted, breaking guest access
  during normal personnel changes).  Same pattern as
  ``tenant_invites.invited_by`` and ``projects.created_by``.
* **``expires_at`` is NULLable** — supports the "permanent share"
  semantics (e.g. an internal sister-tenant that should always have
  guest access).  Distinct from ``tenant_invites.expires_at`` which
  is NOT NULL because invites must rot if unused.  Shares are an
  active grant; the granter explicitly chooses TTL or perpetual.
  When NOT NULL the application layer treats wall-clock past
  ``expires_at`` as expired regardless of whether the sweep job has
  already run (defence in depth).
* **UNIQUE ``(project_id, guest_tenant_id)``** — at most one share
  row per (project, guest_tenant) pair.  Two simultaneous role
  grants to the same guest tenant on the same project would be
  ambiguous (which role wins?); making the pair UNIQUE forces an
  explicit upsert / role-change flow rather than silent duplication.
  The composite UNIQUE doubles as the per-project listing index
  (leading column = ``project_id``) so we don't add a separate
  ``idx_project_shares_project``.
* **No CHECK that ``guest_tenant_id <> project.tenant_id``** at the
  schema layer — it would require a composite-FK trick (storing the
  project's owning tenant in the share row and constraining
  ``guest_tenant_id <> owner_tenant_id``).  Deferred to the Y3 route
  layer ("a tenant cannot share a project to itself") + a Y1 测试
  drift guard.  The DB-level invariant remains: the row references
  a real project and a real tenant, both via FK.
* **``created_at`` is the actual grant timestamp** — every other
  table in this codebase carries one and the audit / admin views
  ("show me shares newest first") need it.  Same convention as the
  other Y1 tables (deviation from literal TODO column lists is
  documented in those migrations too — see 0033 / 0034 / 0035).

Indexes
───────
1. PK on ``id`` — auto-creates the per-row lookup index used by the
   admin "show this share" / "revoke this share" routes.
2. UNIQUE ``(project_id, guest_tenant_id)`` — the per-share-pair
   uniqueness constraint.  Materialises a btree whose leading
   column is ``project_id``, which also serves the "list all shares
   on project X" admin route — so we don't add a separate
   single-column ``idx_project_shares_project``.
3. ``idx_project_shares_guest_tenant`` on ``(guest_tenant_id)`` —
   reverse fan-out: "list every project this guest tenant has
   access to" (the guest tenant's "shared with me" sidebar).
   Without it the planner would full-scan because the UNIQUE
   index's leading column is ``project_id``.
4. Partial ``idx_project_shares_expiry_sweep`` on ``(expires_at)
   WHERE expires_at IS NOT NULL`` — supports the housekeeping
   sweep that revokes shares past their TTL.  Partial keeps the
   index empty for permanent shares (NULL ``expires_at``) and
   tight even after a year of expired-and-revoked rows accumulate.
   PG and SQLite (>= 3.8.0, our floor is 3.35+) both support
   partial indexes natively.

Module-global / cross-worker state audit
────────────────────────────────────────
Pure DDL; no in-memory cache, no module-level singleton.  Every
worker reads ``project_shares`` rows from the same PG (or local
SQLite in dev), so cross-worker consistency is the database's
problem, not the process's.  The Y3 share-grant / share-revoke
routes will use a single transaction for the upsert + audit_log
write, and the Y3 authorisation resolver will read ``project_shares``
through the existing connection pool.

Read-after-write timing audit
─────────────────────────────
No behaviour change: nothing reads from ``project_shares`` yet.
The first read path lands with Y3's authorisation resolver and the
admin REST surface (``POST /api/v1/admin/projects/{pid}/shares``,
``DELETE /api/v1/admin/projects/{pid}/shares/{shid}``).

Production readiness gate
─────────────────────────
* No new Python / OS package — production image needs no rebuild.
* Schema migration drift guards: ``scripts/migrate_sqlite_to_pg.py``
  ``TABLES_IN_ORDER`` updated in the same commit to include
  ``project_shares`` after both parent tables (``projects`` and
  ``tenants``).  TEXT PK — NOT in ``TABLES_WITH_IDENTITY_ID``.
  ``test_migrator_lists_project_shares`` in this commit's test
  module asserts both.
* Production status after this commit: **dev-only**.  Next gate is
  ``deployed-inactive`` — operator runs ``alembic upgrade head`` on
  the prod PG instance.  No env knob change required (the table is
  empty until the Y3 admin share REST starts inserting).

Dialect handling
────────────────
DDL goes through the ``alembic_pg_compat`` shim (see ``env.py``):

* ``datetime('now')``  → ``to_char(now(), 'YYYY-MM-DD HH24:MI:SS')``

Plain SQL string after the rewrite is consumed by both dialects.

Revision ID: 0036
Revises: 0035
Create Date: 2026-04-25
"""
from __future__ import annotations

from alembic import op


revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS project_shares (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    guest_tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    role            TEXT NOT NULL DEFAULT 'viewer',
    granted_by      TEXT REFERENCES users(id) ON DELETE SET NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at      TEXT,
    UNIQUE (project_id, guest_tenant_id),
    CHECK (role IN ('viewer', 'contributor'))
)
"""

_INDEXES = (
    # Reverse fan-out: "list every project this guest tenant has
    # access to" (the guest tenant's "shared with me" sidebar).  The
    # UNIQUE composite index's leading column is ``project_id`` so
    # without this the planner would full-scan for the guest-tenant
    # lookup shape.
    "CREATE INDEX IF NOT EXISTS idx_project_shares_guest_tenant "
    "ON project_shares(guest_tenant_id)",
    # Housekeeping sweep target: revoke shares past their ``expires_at``.
    # Partial keeps the index empty for permanent shares (NULL
    # ``expires_at``) and tight under expired-tail accumulation.
    "CREATE INDEX IF NOT EXISTS idx_project_shares_expiry_sweep "
    "ON project_shares(expires_at) "
    "WHERE expires_at IS NOT NULL",
)


def upgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql(_CREATE_TABLE)
    for stmt in _INDEXES:
        conn.exec_driver_sql(stmt)


def downgrade() -> None:
    conn = op.get_bind()
    for idx in (
        "idx_project_shares_expiry_sweep",
        "idx_project_shares_guest_tenant",
    ):
        conn.exec_driver_sql(f"DROP INDEX IF EXISTS {idx}")
    op.execute("DROP TABLE IF EXISTS project_shares")
