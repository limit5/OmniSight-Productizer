"""Y1 row 3 (#277) — project_members table.

Adds the per-project membership table that overlays the tenant-level
roles from ``user_tenant_memberships`` (Y1 row 1).  The semantics from
the TODO row are:

* A row in ``project_members`` is the **explicit** role binding for
  ``(user_id, project_id)``.
* When **no row exists**, the **tenant-level role** acts as the
  default.  Specifically a ``user_tenant_memberships`` row with role
  ``admin`` (or ``owner``) is treated as ``contributor`` on every
  project of that tenant; ``member`` and ``viewer`` fall through to
  no project access by default.
* Allowed project-level roles: ``owner / contributor / viewer``.

The default-resolution logic itself is **application-level** (Y3 will
add ``project_member.resolve(user, project)``); the DB only stores the
explicit overrides.  Putting the fallback in code keeps the schema
honest — a missing row truly means "use the tenant default", not "no
access" — and avoids the impossible-to-maintain trigger that would be
needed to auto-materialise rows when memberships change.

Why a new table now (before the per-row backfill row)
─────────────────────────────────────────────────────
``project_members`` has FKs into ``users(id)`` and ``projects(id)`` so
both parents must exist first; alembic 0032 (``user_tenant_memberships``)
and 0033 (``projects``) cover that.  This revision is the standalone
empty-table create — no app code changes here, just the schema.

Schema decisions
────────────────
* **Composite PK ``(user_id, project_id)``** — the TODO column list
  matches ``user_preferences`` (PK ``(user_id, pref_key)``) and
  ``user_tenant_memberships`` (PK ``(user_id, tenant_id)``).  Saves a
  synthetic ``id`` column that nobody would join on.  Also satisfies
  the implicit "one role per (user, project)" UNIQUE without a
  separate constraint.
* **Role CHECK** ``IN ('owner', 'contributor', 'viewer')`` — the enum
  is small and stable; pushing it into the DB rejects garbage roles
  even if the application layer regresses.  Cheaper than a lookup
  table, keeps reads single-row.  Crucially the role set is
  **deliberately different** from ``user_tenant_memberships``
  (``owner / admin / member / viewer``) — tenant admin and project
  contributor are distinct concepts and the DB-level CHECK keeps
  them from being confused.
* **FK ``user_id → users(id) ON DELETE CASCADE``** — when a user is
  hard-deleted, their explicit project memberships have no semantic
  value.  Same logic as ``user_tenant_memberships``.
* **FK ``project_id → projects(id) ON DELETE CASCADE``** — when a
  project is hard-deleted, every membership row pointing at it is
  meaningless.  Note: ``projects`` is normally archived (set
  ``archived_at``), not hard-deleted; the CASCADE only fires on the
  rare admin-driven hard delete (e.g. failed tenant onboarding
  rollback).
* **No ``created_by`` audit column on this row** — the TODO row's
  column list is exactly ``(user_id, project_id, role, created_at)``;
  who-granted-what is recorded in the ``audit_log`` row written when
  the membership is upserted (Y3).  Adding ``granted_by`` here would
  be redundant and the column would inevitably end up NULL for the
  Y1 backfill rows (no human granted them; they are seeded from the
  tenant-default fall-through).
* **No ``status`` column** — unlike ``user_tenant_memberships`` which
  needs ``active / suspended`` because a user can be temporarily
  suspended at the tenant level, project membership has only two
  states: row exists (explicit role) or row doesn't (tenant default).
  Suspension at the project layer is achieved by deleting the row,
  which falls back to the tenant default.  If we later need
  "explicit deny", that's a new role value (``denied``) — not a
  separate column.

Indexes
───────
1. Composite PK ``(user_id, project_id)`` — auto-creates the
   per-user-led index.  This covers the "list all projects a user
   has explicit membership on" hot path (e.g. Y3 GET
   ``/users/{uid}/projects``).
2. ``idx_project_members_project`` on ``(project_id)`` — covers the
   reverse fan-out: "list every member of project X" (Y3 GET
   ``/projects/{pid}/members``).  Without it the planner would do a
   full table scan because the composite PK's leading column is
   ``user_id``.
3. No partial index for active-only because there's no
   ``active`` predicate — the row's existence IS the signal.

Module-global / cross-worker state audit
────────────────────────────────────────
Pure DDL; no in-memory cache, no module-level singleton.  Every
worker reads ``project_members`` rows from the same PG (or local
SQLite in dev), so cross-worker consistency is the database's
problem, not the process's.

Read-after-write timing audit
─────────────────────────────
No behaviour change: nothing reads from ``project_members`` yet.
The first read path lands with Y3's ``require_project_member()``
dependency.

Production readiness gate
─────────────────────────
* No new Python / OS package — production image needs no rebuild.
* Schema migration drift guards: ``scripts/migrate_sqlite_to_pg.py``
  ``TABLES_IN_ORDER`` updated in the same commit to include
  ``project_members`` after both parent tables (``users`` and
  ``projects``).  The composite PK ``(user_id, project_id)`` is
  TEXT — NOT in ``TABLES_WITH_IDENTITY_ID``.
  ``test_migrator_lists_project_members`` in this commit's test
  module asserts both.
* Production status after this commit: **dev-only**.  Next gate is
  ``deployed-inactive`` — operator runs ``alembic upgrade head`` on
  the prod PG instance.  No env knob change required; the table is
  empty until a future feature inserts into it.

Dialect handling
────────────────
DDL goes through the ``alembic_pg_compat`` shim (see ``env.py``):

* ``datetime('now')``  → ``to_char(now(), 'YYYY-MM-DD HH24:MI:SS')``

Plain SQL string after the rewrite is consumed by both dialects.

Revision ID: 0034
Revises: 0033
Create Date: 2026-04-25
"""
from __future__ import annotations

from alembic import op


revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS project_members (
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    role        TEXT NOT NULL DEFAULT 'viewer',
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, project_id),
    CHECK (role IN ('owner', 'contributor', 'viewer'))
)
"""

_INDEXES = (
    # Reverse fan-out: "list every member of project X".  Without it
    # the planner would full-scan because the composite PK's leading
    # column is user_id.  No partial because every row is a meaningful
    # explicit-membership signal — there is no "inactive" subset.
    "CREATE INDEX IF NOT EXISTS idx_project_members_project "
    "ON project_members(project_id)",
)


def upgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql(_CREATE_TABLE)
    for stmt in _INDEXES:
        conn.exec_driver_sql(stmt)


def downgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql("DROP INDEX IF EXISTS idx_project_members_project")
    op.execute("DROP TABLE IF EXISTS project_members")
