"""Y1 row 4 (#277) — tenant_invites table.

Adds the email-keyed invite table that lets a tenant admin invite a
not-yet-account-holding human (or a human with a different primary
tenant) into their tenant.  When the invite is accepted, an entry in
``user_tenant_memberships`` (Y1 row 1, alembic 0032) is materialised
with the role recorded on the invite row.

Why a new table now (before the project_shares + the membership backfill)
─────────────────────────────────────────────────────────────────────────
``tenant_invites`` has FKs into ``tenants(id)`` (already present) and
``users(id)`` (for ``invited_by`` audit).  Both parents exist.  No
business code reads from this table yet — it ships empty and starts
filling when the Y2 admin REST surface ``POST /api/v1/admin/invites``
lands.  Materialising the empty table now lets the rest of Y1 (project
shares, backfill, business-table ``project_id`` columns) plug into a
stable schema.

Schema decisions
────────────────
* **TEXT primary key with ``inv-`` prefix convention** — matches
  ``tenants.id`` (``t-*``), ``users.id`` (``u-*``), ``projects.id``
  (``p-*``).  No INTEGER IDENTITY because (a) we want ids that survive
  cross-DB replay without sequence reshuffling and (b) every other
  Y1 table uses a TEXT PK so the convention stays uniform.
  → NOT in ``TABLES_WITH_IDENTITY_ID``.
* **``tenant_id`` FK ``ON DELETE CASCADE``** — invites for a deleted
  tenant carry no semantic value; cleaning them up with the parent is
  the only sane behaviour and keeps the row count bounded under tenant
  churn.
* **``email`` is the recipient address, NOT a ``user_id`` FK** — the
  invitee may not yet have an account; that's the entire point of an
  invite flow.  Length CHECK (1..320) bounds storage and matches the
  RFC 5321 max local+domain length so a rogue admin payload can't
  stuff a 1 MB string and bloat the index.  Email is stored verbatim
  (case-preserved) but the application layer should compare
  case-insensitively when matching invites to a signing-up user (the
  RFC is local-part-case-sensitive but every real-world MTA treats
  it as case-insensitive).  Encoding the lowercased form into a DB
  generated column was considered but rejected: it adds replay
  complexity for SQLite-side dev DBs (generated columns are SQLite
  3.31+, our floor is fine but the migrator's column-list discovery
  doesn't currently filter generated columns).
* **``role`` CHECK ``IN ('owner', 'admin', 'member', 'viewer')``** —
  matches the **tenant-level** enum from ``user_tenant_memberships``
  exactly because accepting the invite materialises a membership row
  with this role.  Deliberately NOT the project-level enum
  (``owner / contributor / viewer``) — invites grant tenant-scope
  membership, not project-scope.  Pushing the enum into the DB
  rejects garbage roles even if the application layer regresses.
* **``invited_by`` FK ``ON DELETE SET NULL``** — the invite outlives
  the inviter.  Audit-only field; the materialised membership row
  records who-accepted via ``user_tenant_memberships.created_at``
  and the corresponding ``audit_log`` entry.  CASCADE here would
  delete the invite when an admin is hard-deleted, breaking the
  acceptance flow for any pending invite they had open.
* **``token_hash`` TEXT NOT NULL UNIQUE** — only the **hash** is
  stored (e.g. ``hashlib.sha256(token).hexdigest()``).  The plaintext
  token is generated at creation time and returned ONCE in the API
  response, never persisted.  Same pattern as ``api_keys.key_hash``
  (alembic 0011) and ``mfa_backup_codes.code_hash`` (alembic 0012).
  UNIQUE on the hash both prevents collision-based ambiguity at
  acceptance time and lets the acceptance route do a single indexed
  lookup ``WHERE token_hash = $1``.
* **``expires_at`` NOT NULL** — every invite has a TTL (default 7
  days, decided at the application layer).  The DB enforces presence;
  the application enforces the actual length.  When the wall clock
  exceeds ``expires_at`` the application treats the invite as
  expired regardless of the persisted ``status`` (defence in depth);
  a periodic sweep flips the status to ``expired`` for housekeeping.
* **``status`` CHECK ``IN ('pending', 'accepted', 'revoked', 'expired')``**
  with DEFAULT ``'pending'`` — the four-value lifecycle from the
  TODO row.  Invariants:
    - ``pending`` → ``accepted`` (user accepts, membership row
      written transactionally with this status flip)
    - ``pending`` → ``revoked`` (admin cancels)
    - ``pending`` → ``expired`` (sweep job, after ``expires_at``)
    - All other transitions are application-layer rejected; the
      schema does not encode the state machine because trigger-based
      enforcement on both PG and SQLite would double the surface
      area without adding much safety (the only mutator is the Y2
      admin route).
* **``created_at`` is added even though the TODO column list omits
  it** — every other table in this codebase carries one, the audit
  routes (Y2 GET ``/admin/invites``) need it for sorting "newest
  first", and ``expires_at`` alone can't tell you when the invite
  was actually created.  Documented here so the deviation from the
  literal TODO is visible.  No ``accepted_at`` column either — the
  acceptance audit lives in the materialised
  ``user_tenant_memberships.created_at`` and the
  ``audit_log`` row written at acceptance time.

Indexes
───────
1. PK on ``id`` — auto-creates the per-row lookup index used by the
   admin "view this invite" route.
2. UNIQUE on ``token_hash`` — both collision check at creation and
   the single-row lookup the acceptance route does
   (``WHERE token_hash = $1``).  UNIQUE materialises a btree, no
   separate ``idx_*`` needed.
3. ``idx_tenant_invites_tenant_status`` on ``(tenant_id, status)`` —
   covers the admin "list pending invites for tenant X" hot path
   without a tenant-wide table scan.  Composite (tenant, status)
   instead of single-column (tenant) because the only useful query
   shape filters by both, and most invites accumulate in the
   ``accepted/revoked/expired`` tail — partial-index on
   ``WHERE status = 'pending'`` would be tighter but the query
   shape also wants "show me historical invites" for audit, which
   wouldn't hit the partial.  Composite covers both.
4. ``idx_tenant_invites_email_status`` on ``(email, status)`` —
   covers the sign-up flow: "when this email signs up, are there
   pending invites for them across any tenant?" — answered with a
   single indexed lookup rather than a full-table scan.  Composite
   keeps the hot ``WHERE email = $1 AND status = 'pending'`` pattern
   index-only.
5. Partial ``idx_tenant_invites_expiry_sweep`` on ``(expires_at)
   WHERE status = 'pending'`` — supports the housekeeping sweep that
   flips ``pending`` invites past their ``expires_at`` to ``expired``.
   Partial keeps it small even after a year of accepted/revoked rows
   accumulate; PG and SQLite (>= 3.8.0, our floor is 3.35+) both
   support partial indexes natively.

Module-global / cross-worker state audit
────────────────────────────────────────
Pure DDL; no in-memory cache, no module-level singleton.  Every
worker reads ``tenant_invites`` rows from the same PG (or local
SQLite in dev), so cross-worker consistency is the database's
problem, not the process's.  The acceptance route will use a
``SELECT ... FOR UPDATE`` (or SQLite's ``BEGIN IMMEDIATE``) to
serialise the status flip and the membership materialisation in a
single transaction — that lives in Y2's route layer, not here.

Read-after-write timing audit
─────────────────────────────
No behaviour change: nothing reads from ``tenant_invites`` yet.
The first read path lands with Y2 admin REST
(``POST /api/v1/admin/invites`` returning the plaintext token once,
``GET /api/v1/admin/invites?tenant_id=...`` listing for the admin
console, ``POST /api/v1/invites/accept`` consuming the token).

Production readiness gate
─────────────────────────
* No new Python / OS package — production image needs no rebuild.
* Schema migration drift guards: ``scripts/migrate_sqlite_to_pg.py``
  ``TABLES_IN_ORDER`` updated in the same commit to include
  ``tenant_invites`` after both parent tables (``tenants`` and
  ``users``).  TEXT PK — NOT in ``TABLES_WITH_IDENTITY_ID``.
  ``test_migrator_lists_tenant_invites`` in this commit's test
  module asserts both.
* Production status after this commit: **dev-only**.  Next gate is
  ``deployed-inactive`` — operator runs ``alembic upgrade head`` on
  the prod PG instance.  No env knob change required (the table is
  empty until the Y2 admin invite REST starts inserting).

Dialect handling
────────────────
DDL goes through the ``alembic_pg_compat`` shim (see ``env.py``):

* ``datetime('now')``  → ``to_char(now(), 'YYYY-MM-DD HH24:MI:SS')``

Plain SQL string after the rewrite is consumed by both dialects.

Revision ID: 0035
Revises: 0034
Create Date: 2026-04-25
"""
from __future__ import annotations

from alembic import op


revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS tenant_invites (
    id          TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    email       TEXT NOT NULL,
    role        TEXT NOT NULL DEFAULT 'member',
    invited_by  TEXT REFERENCES users(id) ON DELETE SET NULL,
    token_hash  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (token_hash),
    CHECK (role IN ('owner', 'admin', 'member', 'viewer')),
    CHECK (status IN ('pending', 'accepted', 'revoked', 'expired')),
    CHECK (length(email) >= 1 AND length(email) <= 320),
    CHECK (length(token_hash) >= 16)
)
"""

_INDEXES = (
    # Admin "list invites for tenant X" — composite (tenant, status)
    # because the route filters on both.  Also serves the historical
    # "show all invites this tenant ever issued" audit view, which a
    # ``WHERE status = 'pending'`` partial would not.
    "CREATE INDEX IF NOT EXISTS idx_tenant_invites_tenant_status "
    "ON tenant_invites(tenant_id, status)",
    # Sign-up cross-reference: "for this email, are there pending
    # invites across any tenant?" answered with one indexed lookup.
    # Composite (email, status) keeps the hot
    # ``WHERE email = $1 AND status = 'pending'`` query index-only.
    "CREATE INDEX IF NOT EXISTS idx_tenant_invites_email_status "
    "ON tenant_invites(email, status)",
    # Housekeeping sweep target: flip pending invites past their
    # ``expires_at`` to ``expired``.  Partial keeps the index tight
    # under accepted/revoked/expired tail accumulation.
    "CREATE INDEX IF NOT EXISTS idx_tenant_invites_expiry_sweep "
    "ON tenant_invites(expires_at) "
    "WHERE status = 'pending'",
)


def upgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql(_CREATE_TABLE)
    for stmt in _INDEXES:
        conn.exec_driver_sql(stmt)


def downgrade() -> None:
    conn = op.get_bind()
    for idx in (
        "idx_tenant_invites_expiry_sweep",
        "idx_tenant_invites_email_status",
        "idx_tenant_invites_tenant_status",
    ):
        conn.exec_driver_sql(f"DROP INDEX IF EXISTS {idx}")
    op.execute("DROP TABLE IF EXISTS tenant_invites")
