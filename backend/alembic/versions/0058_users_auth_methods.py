"""AS.0.3 — users.auth_methods JSONB column + account-linking schema scaffold.

Why
───
Phase AS (Auth & Security shared library) introduces an OAuth login
client (AS.1) on top of the existing password-only auth surface.
The classic account-takeover vector is:

  1. Victim has an OmniSight account ``foo@x.com`` with a password.
  2. Attacker registers ``foo@x.com`` at an OAuth IdP (DNS hijack,
     domain re-purchase, IdP signup loophole, ...).
  3. Attacker clicks "Sign in with Google" — naive auto-link logic
     binds the IdP subject to the existing OmniSight user row.
  4. Attacker is now logged in as the victim.

The takeover-prevention rule (design doc §3.3) is: **OAuth email
matches an existing password user → require password verification
BEFORE link**.  The auth-methods set is the per-user record of
which login methods are *currently* accepted; the guard is enforced
before any new method is added.

This migration lands the schema half — a JSONB array column
``users.auth_methods`` whose contents drive the policy module
``backend.account_linking``.  The OAuth client itself lands in
AS.1 but it can already call into the policy guard once the
column exists.

Backfill semantics
──────────────────
Existing rows are explicitly backfilled to:

* ``["password"]``  — for any user whose ``password_hash`` is
  non-empty (the common case — every prod user up to 2026-04-27).
* ``[]``            — for invited-but-not-yet-completed users
  whose ``password_hash`` is the empty-string placeholder; once
  they set a password the application code path appends
  ``"password"``.

We do NOT seed any ``oauth_*`` method for existing users, even
when ``oidc_subject`` is non-empty — the existing OIDC route is a
501 stub (see ``backend/routers/auth.py::oidc_redirect``) so no
production user has actually federated via it.  AS.1 is the row
that will start writing OAuth method tags via the
``add_auth_method`` helper after the takeover guard passes.

Default for *new* rows is ``'[]'``.  The companion edits in
``backend/auth.py::_create_user_impl`` and
``backend/routers/tenant_invites.py`` make INSERT paths write
``["password"]`` when a password is supplied.  The
empty-array default keeps any future code path that bypasses
the helpers from getting silent, unexplained ``"password"``
membership.

Why an array (JSONB) and not, say, a comma-separated TEXT?
──────────────────────────────────────────────────────────
* PG ``jsonb`` lets the array be queried set-wise (``? operator``,
  ``jsonb_array_elements``) which the AS-OAuth router will need
  for "does this user already have OAuth bound?" lookups.
* Mirrors the AS.0.2 ``tenants.auth_features`` JSONB pattern —
  one cognitive model for "auth-shaped JSON column" across the
  AS roadmap.
* SQLite TEXT-of-JSON parses via ``json.loads`` exactly the same
  way the application reads ``tenants.auth_features``.

Module-global / cross-worker state audit
────────────────────────────────────────
Pure DDL + UPDATE migration.  No module-level singleton, no
in-memory cache.  Every worker reads the same column type / row
state from PG so the migration is visible atomically post-commit.
**Answer #1** — every worker reads the same DDL state from the
same DB.

Read-after-write timing audit
─────────────────────────────
The ALTER TABLE + UPDATE happen inside the alembic upgrade
transaction.  PG's ALTER takes AccessExclusive on ``users``;
``scripts/deploy.sh`` closes the asyncpg pool before alembic
upgrade and reopens it after, so runtime workers never see an
intermediate column shape.  No concurrent writer exists during
the migration window.

Production readiness gate
─────────────────────────
* No new Python / OS package — production image needs no rebuild.
* No new table — existing ``users`` modified in-place; the
  migrator's ``TABLES_IN_ORDER`` doesn't change.  Column-level
  parity with the SQLite CREATE TABLE in ``backend/db.py`` is
  maintained by the parallel edit landed in this same row.
* TODO.md predicted alembic 0058 for this row (0057 reserved for
  AS.2.2 ``oauth_tokens``).  This file uses 0058 as planned.
  The chain skips 0055 and 0057 — ``down_revision = "0056"``
  jumps over the reserved AS.2.2 slot which alembic accepts
  (revision strings carry no numeric ordering contract; the
  chain is a linked list, not an array).
* Production status of THIS commit: **dev-only**.  Next gate:
  ``deployed-inactive`` once the alembic chain (… → 0056 → 0058)
  is run against prod PG.

Revision ID: 0058
Revises: 0056
Create Date: 2026-04-27
"""
from __future__ import annotations

from alembic import op


revision = "0058"
down_revision = "0056"
branch_labels = None
depends_on = None


# ─── Constants ────────────────────────────────────────────────────────────


# Backfill payload for users that already carry a password.  Stored
# as a single-element JSON array of the literal string ``"password"``
# so the application can do a simple membership check
# (``"password" in arr``) without parsing nested structures.
_LEGACY_PASSWORD_USER_AUTH_METHODS_JSON = '["password"]'


# ─── PG branch ────────────────────────────────────────────────────────────


# ``IF NOT EXISTS`` is belt-and-braces against operators who manually
# added the column out of band.  Default ``'[]'::jsonb`` keeps any
# future INSERT path that bypasses the AS-aware helper from tripping
# a NOT NULL violation; the helper writes the explicit value.
_PG_ADD_COLUMN = (
    "ALTER TABLE users "
    "ADD COLUMN IF NOT EXISTS auth_methods jsonb "
    "NOT NULL DEFAULT '[]'::jsonb"
)

# Only rows still at the column DEFAULT (``'[]'``) AND carrying a
# non-empty ``password_hash`` get backfilled to ``["password"]``.
# Empty-password rows (invited-but-not-completed) stay at ``[]``;
# the application appends ``"password"`` when they set one.
# Operator hand-edits (``UPDATE users SET auth_methods = ...``) are
# preserved because the WHERE clause filters on the empty default.
_PG_BACKFILL = (
    "UPDATE users "
    f"SET auth_methods = '{_LEGACY_PASSWORD_USER_AUTH_METHODS_JSON}'::jsonb "
    "WHERE auth_methods = '[]'::jsonb "
    "AND password_hash <> ''"
)

_PG_DROP_COLUMN = (
    "ALTER TABLE users DROP COLUMN IF EXISTS auth_methods"
)


# ─── SQLite branch ────────────────────────────────────────────────────────


# SQLite has no native JSONB type; ``auth_methods`` is TEXT-of-JSON
# and parsed via ``json.loads`` at the application layer (same
# pattern as ``tenants.auth_features``).  The alembic-vs-SQLite
# column parity gate (``test_migrator_schema_coverage.py``) requires
# the column to exist on both sides.
_SQLITE_ADD_COLUMN = (
    "ALTER TABLE users "
    "ADD COLUMN auth_methods TEXT NOT NULL DEFAULT '[]'"
)

_SQLITE_BACKFILL = (
    "UPDATE users "
    f"SET auth_methods = '{_LEGACY_PASSWORD_USER_AUTH_METHODS_JSON}' "
    "WHERE auth_methods = '[]' "
    "AND password_hash <> ''"
)


# ─── upgrade / downgrade ──────────────────────────────────────────────────


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        conn.exec_driver_sql(_PG_ADD_COLUMN)
        conn.exec_driver_sql(_PG_BACKFILL)
        return

    # SQLite path — guard ADD COLUMN by table_info to keep upgrade()
    # idempotent under repeat invocation (the test suite re-binds the
    # migration against a hand-built schema and we want the second
    # call to be a no-op rather than a duplicate-column error).
    cols = {
        row[1]
        for row in conn.exec_driver_sql("PRAGMA table_info(users)").fetchall()
    }
    if "auth_methods" not in cols:
        conn.exec_driver_sql(_SQLITE_ADD_COLUMN)
    conn.exec_driver_sql(_SQLITE_BACKFILL)


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        conn.exec_driver_sql(_PG_DROP_COLUMN)
        return
    # SQLite < 3.35 cannot DROP COLUMN; even on 3.35+ the operation
    # rewrites the table.  The column is application-layer harmless
    # (readers tolerate it via ``json.loads`` over the TEXT-of-JSON
    # value), so we leave it in place rather than rewriting the table
    # for the rare downgrade path.  Same trade-off as 0056.
    return
