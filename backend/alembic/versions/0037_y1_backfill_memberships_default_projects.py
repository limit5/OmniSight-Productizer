"""Y1 row 6 (#277) — backfill ``user_tenant_memberships`` + default ``projects``.

Y1 rows 1-5 created the five new tables (``user_tenant_memberships``,
``projects``, ``project_members``, ``tenant_invites``, ``project_shares``)
in revisions 0032-0036.  This migration is the **backfill** half of the
TODO row that originally read "Alembic 0019 migration: 新建上述 5 表 +
索引；回填 user_tenant_memberships … 回填 projects …".  The "create
five tables + indexes" leg was satisfied by 0032-0036; what remains is
to pour existing single-tenant state into the new N-to-M and project
shapes so that downstream Y1/Y2/Y3 readers (membership resolver,
default-project FK on workload tables, admin REST surfaces) can boot
against a non-empty universe.

Revision numbering note
───────────────────────
The Y1 plan in ``TODO.md`` calls for "Alembic 0019" but slot ``0019``
has been occupied by ``session_revocations`` since Q.1.  The five
table-create rows landed at ``0032``…``0036``; this backfill is the
next free slot ``0037``.

What this migration does
────────────────────────
1. **Backfill ``user_tenant_memberships``** from the existing
   ``users.tenant_id`` cache column.  Every user with a non-NULL
   ``tenant_id`` gets a membership row in that tenant.  The role is
   derived from the legacy ``users.role`` flag:

       users.role = 'admin'   →  membership.role = 'owner'
       users.role = anything  →  membership.role = 'member'

   The mapping is the literal contract from the TODO ("role = `owner`
   若 user.role = `admin`，否則 `member`").  ``status`` defaults to
   ``'active'`` from the column DEFAULT and ``last_active_at`` stays
   NULL (no historical signal — the cache field reflects "primary
   tenant" not "last activity in any tenant").  ``created_at`` falls
   back to the column DEFAULT ``datetime('now')`` so re-running on a
   fresh DB is observable in the timestamp.

2. **Backfill default ``projects``** — one row per tenant with
   ``(tenant_id, product_line='default', slug='default')``.  This is
   the named contract from the TODO row and is the FK target the
   *next* TODO row's "add ``project_id`` to existing business tables"
   will point every legacy workflow_run / debug_finding / decision /
   event_log / artifact / spec_* / user_preference row at.

   The project ``id`` is derived deterministically from the tenant id
   so re-running is a no-op:

       tenant 't-default'    →  project 'p-default-default'
       tenant 't-acme'       →  project 'p-acme-default'
       tenant 'legacy'       →  project 'p-legacy-default'   (no t- prefix)

   Strip the ``t-`` prefix when present (purely cosmetic; the DB
   doesn't enforce the prefix on ``tenants.id``) and tack on
   ``-default``.  ``name = 'Default'``, ``product_line = 'default'``,
   ``slug = 'default'`` — the literal triple from the TODO contract.
   ``plan_override / disk_budget_bytes / llm_budget_tokens`` stay NULL
   so the project transparently inherits its tenant's resolver
   defaults (the Y2/Y3 ``project_quota.resolve(tenant, project)``
   coalesces ``project.X ?? tenant.X``).

What this migration does NOT do
───────────────────────────────
* **Cross-table workload backfill** — i.e. setting
  ``workflow_runs.project_id`` (and similar on ``debug_findings /
  decisions / event_log / artifacts / spec_* / user_preferences``) to
  point at the per-tenant default project — is **explicitly deferred**
  to the *next* TODO row.  The TODO line for THIS row says "via 新欄位
  workflow_runs.project_id 等等的二次回填 script" — the *secondary*
  script clause is parenthetical and presupposes the new ``project_id``
  columns exist.  They do NOT exist yet (the column-add row sits right
  after this one in TODO).  Doing both halves in one revision would
  force this migration to silently ALTER seven business tables, which
  inverts the dependency the next row was sized for.  Splitting keeps
  both revisions single-purpose and reviewable.

* **No FK column adds, no NOT NULL flips, no index churn on existing
  business tables** — the cross-table work is left for revision 0038+
  per the next TODO row.

Idempotency
───────────
Both backfills use ``INSERT OR IGNORE`` so re-running this migration is
a no-op on rows that already exist:

* memberships: PK is the composite ``(user_id, tenant_id)`` — the
  conflict surface — so a second run skips already-materialised pairs.
* projects: the UNIQUE constraint is ``(tenant_id, product_line, slug)``
  and the deterministic id derivation also yields the same PK on retry,
  so both potential conflict surfaces (PK and the UNIQUE triple) are
  covered.

The ``alembic_pg_compat`` shim translates ``INSERT OR IGNORE INTO`` into
``INSERT INTO ... ON CONFLICT DO NOTHING`` for PG (see
``backend/alembic_pg_compat.py::_translate_insert_or_ignore``); the
no-target form is supported on PG 9.5+ and matches against any unique
constraint, including the composite PK and the named UNIQUE triple.

Module-global / cross-worker state audit
────────────────────────────────────────
Pure DML migration.  No in-memory cache, no module-level singleton.
Runs once at ``alembic upgrade head`` time during the offline cutover
window — there is no "every worker" question because every worker
sees the post-backfill state when they boot.  The backfilled rows are
read by Y2/Y3 code that lands in later revisions, so this migration's
output is the ground truth those readers will see.

Read-after-write timing audit
─────────────────────────────
No behaviour change at runtime.  Nothing currently reads from the
``user_tenant_memberships`` or ``projects`` tables at request time
(the Y2 admin REST and the Y3 authorisation resolver are still on the
TODO list).  When those readers land they observe the post-backfill
state — the migration runs strictly before any of them is on the hot
path, so the "read sees pre-backfill state" timing window can't be
hit.

Production readiness gate
─────────────────────────
* No new Python / OS package — production image needs no rebuild.
* No new schema artefacts — TABLES_IN_ORDER /
  TABLES_WITH_IDENTITY_ID stay as 0032-0036 left them.
* The seeded rows are bounded by ``users`` row count + ``tenants``
  row count — ``O(N+M)`` insert, no scan beyond the source tables.
  On a clean dev DB with one user (``u-bootstrap``) and one tenant
  (``t-default``), the upgrade is two single-row inserts.
* Production status after this commit: **dev-only**.  Next gate is
  ``deployed-inactive`` — operator runs ``alembic upgrade head`` on
  the prod PG instance.  No env knob change required (the backfill
  is observed only by Y2/Y3 code that hasn't shipped yet).

Dialect handling
────────────────
DDL-free; the two ``INSERT … SELECT`` statements use the only
SQLite-isms the ``alembic_pg_compat`` shim already covers:

* ``INSERT OR IGNORE INTO``  →  ``INSERT INTO … ON CONFLICT DO NOTHING``
* ``substr(col, n)``         — both PG and SQLite support, identical.
* ``CASE WHEN … END``        — both PG and SQLite support, identical.

No new shim rules required.

Revision ID: 0037
Revises: 0036
Create Date: 2026-04-25
"""
from __future__ import annotations

from alembic import op


revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


# 1. Backfill memberships: every (user, user.tenant_id) pair gets a
#    membership row.  Role mapping is the literal TODO contract:
#    'admin' → 'owner', everything else → 'member'.  ``status`` and
#    ``created_at`` fall back to the column DEFAULTs ('active' and
#    ``datetime('now')``).  ``last_active_at`` stays NULL — the
#    legacy ``users.tenant_id`` is "primary tenant", not "last
#    activity timestamp", so we have no honest value to write here.
_BACKFILL_MEMBERSHIPS = """
INSERT OR IGNORE INTO user_tenant_memberships
    (user_id, tenant_id, role)
SELECT id,
       tenant_id,
       CASE WHEN role = 'admin' THEN 'owner' ELSE 'member' END
FROM users
WHERE tenant_id IS NOT NULL
"""

# 2. Backfill one default project per tenant.  The triple
#    (tenant_id, product_line='default', slug='default') is the
#    literal TODO contract.  Project ``id`` is derived deterministically
#    from the tenant id (strip 't-' prefix when present, then suffix
#    '-default') so a second run produces the same id and hits the
#    PK conflict, not a duplicate insert.
_BACKFILL_DEFAULT_PROJECTS = """
INSERT OR IGNORE INTO projects
    (id, tenant_id, product_line, name, slug)
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
"""


def upgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql(_BACKFILL_MEMBERSHIPS)
    conn.exec_driver_sql(_BACKFILL_DEFAULT_PROJECTS)


def downgrade() -> None:
    # Backfill downgrades are intentionally narrow: we only delete the
    # rows whose shape unambiguously matches what the upgrade inserted.
    # That keeps the downgrade safe even if the operator hand-edited
    # adjacent rows (admin-added memberships / projects) between
    # upgrade and downgrade.
    #
    # Memberships: drop only the (user_id, tenant_id) pairs that are
    # still mirrored in users.tenant_id with the originally-derived
    # role.  A user-edited membership (e.g. role flipped from 'member'
    # to 'admin' in the admin console) NO LONGER matches the derived
    # role and is preserved.
    #
    # Projects: drop only the deterministic-id rows whose triple is
    # the literal default ('default', 'default').  Admin-created
    # projects with custom slug / product_line are preserved.
    conn = op.get_bind()
    conn.exec_driver_sql(
        """
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
    )
    conn.exec_driver_sql(
        """
        DELETE FROM projects
        WHERE product_line = 'default'
          AND slug = 'default'
          AND id = 'p-' || CASE
                WHEN substr(tenant_id, 1, 2) = 't-' THEN substr(tenant_id, 3)
                ELSE tenant_id
            END || '-default'
        """
    )
