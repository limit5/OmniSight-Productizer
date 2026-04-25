"""Y1 row 1 (#277) — user_tenant_memberships table.

Introduces the membership table that lets a single ``users.id`` belong
to N tenants with different roles in each.  This is the structural
prerequisite for the rest of Y1 (projects / tenant_invites /
project_shares + the multi-membership backfill).

Why a new table instead of widening ``users.tenant_id``
───────────────────────────────────────────────────────
``users.tenant_id`` is kept as the **cache** of "primary / most-recent
tenant the user logged into" (so the existing UI / RLS path that reads
that column unchanged keeps working during the migration window).  It
stops being the authoritative source: the canonical N-to-M is now
``user_tenant_memberships``.  The cache is repaired on every successful
session establishment in a follow-up row.

Schema decisions
────────────────
* **Composite PK ``(user_id, tenant_id)``** — satisfies the TODO's
  "UNIQUE (user_id, tenant_id)" requirement without a synthetic id
  column.  Same pattern as ``user_preferences`` (PK ``(user_id,
  pref_key)``) so the schema stays internally consistent.
* **CHECK constraints** for ``role`` and ``status`` — the enum is
  small and stable; pushing it into the DB rejects garbage roles
  even if the application layer regresses.  Cheaper than a lookup
  table and keeps reads single-row.
* **``last_active_at`` NULL-able** — a member who was just invited
  but has never actually switched into the tenant has no last-active
  signal yet.  ``NULL`` is the honest representation, not ``epoch=0``.
* **FK ``ON DELETE CASCADE``** on both sides — when a tenant or a
  user is hard-deleted, dangling memberships have no semantic value.
  This keeps the row count bounded under tenant churn.

Revision numbering note
───────────────────────
The Y1 plan in ``TODO.md`` calls for "Alembic 0019" but slot ``0019``
has been occupied by ``session_revocations`` since Q.1.  The latest
revision at the time of this commit is ``0031``; we use the next free
slot ``0032``.  Subsequent Y1 rows (projects / project_members /
tenant_invites / project_shares + the membership backfill) will land
as later revisions ``0033+``.

Dialect handling
────────────────
DDL goes through the ``alembic_pg_compat`` shim (see ``env.py``):

* ``datetime('now')``     → ``to_char(now(), 'YYYY-MM-DD HH24:MI:SS')``
* ``REAL``                → ``DOUBLE PRECISION`` (not used here)

The CREATE TABLE below is plain SQL that both dialects accept after
the rewrite — no need for a manual dialect branch.

Backfill is intentionally NOT done in this migration.  It belongs
to the dedicated "Alembic backfill" row in Y1 which materialises
memberships for every existing ``users.tenant_id`` row in lockstep
with the other four Y1 tables (so the backfill can be reasoned about
as a single unit).  Creating an empty table here is the deliverable
for the row "新表 user_tenant_memberships ...".

Revision ID: 0032
Revises: 0031
Create Date: 2026-04-25
"""
from __future__ import annotations

from alembic import op


revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS user_tenant_memberships (
    user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    tenant_id       TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    role            TEXT NOT NULL DEFAULT 'member',
    status          TEXT NOT NULL DEFAULT 'active',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    last_active_at  TEXT,
    PRIMARY KEY (user_id, tenant_id),
    CHECK (role IN ('owner', 'admin', 'member', 'viewer')),
    CHECK (status IN ('active', 'suspended'))
)
"""

_INDEXES = (
    # Per-user fan-out: "give me every tenant this user belongs to".
    # Hot path for the tenant-switcher in the UI and for session
    # establishment that has to decide which tenants the bearer can
    # masquerade as.
    "CREATE INDEX IF NOT EXISTS idx_user_tenant_memberships_user "
    "ON user_tenant_memberships(user_id)",
    # Per-tenant fan-out: "list all members of this tenant" — the
    # admin-console members tab + the future Y3 invite/membership
    # APIs paginate against this index.
    "CREATE INDEX IF NOT EXISTS idx_user_tenant_memberships_tenant "
    "ON user_tenant_memberships(tenant_id)",
    # Partial index for the active-only common case so the planner
    # can skip suspended rows when answering "who can act in this
    # tenant right now?".  PG supports partial indexes natively;
    # SQLite supports them too (>= 3.8.0, our floor is 3.35+).
    "CREATE INDEX IF NOT EXISTS idx_user_tenant_memberships_active "
    "ON user_tenant_memberships(tenant_id, user_id) "
    "WHERE status = 'active'",
)


def upgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql(_CREATE_TABLE)
    for stmt in _INDEXES:
        conn.exec_driver_sql(stmt)


def downgrade() -> None:
    conn = op.get_bind()
    for idx in (
        "idx_user_tenant_memberships_active",
        "idx_user_tenant_memberships_tenant",
        "idx_user_tenant_memberships_user",
    ):
        conn.exec_driver_sql(f"DROP INDEX IF EXISTS {idx}")
    op.execute("DROP TABLE IF EXISTS user_tenant_memberships")
