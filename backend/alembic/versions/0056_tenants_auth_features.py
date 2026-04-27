"""AS.0.2 — tenants.auth_features JSONB column.

Why
───
Phase AS (Auth & Security shared library) introduces three new
behavior knobs that need per-tenant gating so existing prod
tenants can opt out (or, more accurately, **stay** opted out)
without a single global flag flip:

* ``oauth_login``        — tenant-scope toggle for the AS.1 OAuth
                           client core.  Existing tenants default
                           ``false`` (zero behavior change).
* ``turnstile_required`` — Cloudflare Turnstile challenge gate
                           (AS.4 fail-open phase).  Existing
                           tenants default ``false``.
* ``honeypot_active``    — invisible honeypot field on signup form
                           (AS.0.7 design).  Existing tenants
                           default ``false``.

A fourth knob ``auth_layer`` (one of
``cf_access | app_oauth | password_only``) is reserved for the
AS.6 K-rest CF Access SSO landing — its absence in legacy rows is
interpreted by the application as ``password_only`` so this
migration does NOT seed it for existing tenants.  AS.6 will
backfill ``auth_layer`` once K-rest landing is on the rails.

Default value semantics
───────────────────────
Column-level DEFAULT is the empty JSONB object ``'{}'`` — that's
what keeps brand-new tenant rows that don't go through the AS-aware
INSERT path from tripping a NOT NULL violation while still
signalling "no AS opinion".

Existing rows are then **explicitly backfilled** with
``{"oauth_login": false, "turnstile_required": false,
"honeypot_active": false}``.  We do NOT rely on
"absent key → falsy" interpretation; every existing tenant gets
an explicit ``false`` so a future code-path bug that
defaults-to-true on a missing key cannot silently flip a 50-tenant
prod estate.  Only rows that still equal the column DEFAULT
(``'{}'``) are touched — operator hand-edits to ``auth_features``
are preserved.

The companion edits in ``backend/routers/admin_tenants.py`` and
``backend/routers/bootstrap.py`` make new-tenant INSERTs write
``{"oauth_login": true, "turnstile_required": true,
"honeypot_active": true}`` so 新 tenant 預設全開 per the AS.0.2
TODO row (no consumer reads these flags yet — AS.1+ wires them
up — so the all-on seed is dormant until then).

Module-global / cross-worker state audit
────────────────────────────────────────
Pure DDL + UPDATE migration.  No module-level singleton, no
in-memory cache.  Every worker reads the same column type and row
state from PG so the migration is visible atomically post-commit.
Answer #1 for the cross-worker question — every worker reads the
same DDL state from the same DB.

Read-after-write timing audit
─────────────────────────────
The ALTER TABLE + UPDATE happen inside the alembic upgrade
transaction.  Writers and readers see consistent column types
before and after, with no concurrent writes during the migration
window.  ALTER TABLE acquires AccessExclusive on PG; the asyncpg
pool is closed before alembic upgrade per ``scripts/deploy.sh``
and reopened after.

Production readiness gate
─────────────────────────
* No new Python / OS package — production image needs no rebuild.
* No new table — existing ``tenants`` modified in-place; the
  migrator's ``TABLES_IN_ORDER`` doesn't change since the table
  is still listed by name.  Column-level parity with the SQLite
  CREATE TABLE in ``backend/db.py`` is maintained by the parallel
  edit landed in this same row.
* TODO.md predicted alembic 0056 for this row.  The migration
  table also reserves 0057 for AS.2.2 ``oauth_tokens`` and 0058
  as buffer.  This file uses 0056 as planned.  The chain skips
  0055 (no AS row was scoped for that slot); ``down_revision``
  jumps from ``0054`` directly to ``0056`` which alembic accepts.
* Production status of THIS commit: **dev-only**.  Next gate:
  ``deployed-inactive`` once the alembic chain (… → 0054 → 0056)
  is run against prod PG.

Revision ID: 0056
Revises: 0054
Create Date: 2026-04-27
"""
from __future__ import annotations

from alembic import op


revision = "0056"
down_revision = "0054"
branch_labels = None
depends_on = None


# ─── Constants ────────────────────────────────────────────────────────────


# Explicit-false JSON object — the shape every existing tenant row gets
# backfilled to.  Keys are sorted alphabetically so the round-trip
# string is deterministic across PG (jsonb_set canonicalises) and
# SQLite (TEXT-of-JSON is byte-stable).
_LEGACY_TENANT_AUTH_FEATURES_JSON = (
    '{"honeypot_active": false, '
    '"oauth_login": false, '
    '"turnstile_required": false}'
)


# ─── PG branch ────────────────────────────────────────────────────────────


# ``IF NOT EXISTS`` is belt-and-braces against operators who manually
# added the column out of band — the migration stays idempotent.
_PG_ADD_COLUMN = (
    "ALTER TABLE tenants "
    "ADD COLUMN IF NOT EXISTS auth_features jsonb "
    "NOT NULL DEFAULT '{}'::jsonb"
)

# Only rows still at the column DEFAULT (``'{}'``) get backfilled.
# Operator hand-edits via ``UPDATE tenants SET auth_features = ...``
# (or a future migration that landed before this one runs) are
# preserved.
_PG_BACKFILL = (
    "UPDATE tenants "
    f"SET auth_features = '{_LEGACY_TENANT_AUTH_FEATURES_JSON}'::jsonb "
    "WHERE auth_features = '{}'::jsonb"
)

_PG_DROP_COLUMN = (
    "ALTER TABLE tenants DROP COLUMN IF EXISTS auth_features"
)


# ─── SQLite branch ────────────────────────────────────────────────────────


# SQLite has no native JSONB type; ``auth_features`` is TEXT-of-JSON
# and parsed via ``json.loads`` at the application layer (same pattern
# as ``bootstrap_state.metadata`` pre-0054).  The alembic-vs-SQLite
# column parity test (``test_migrator_schema_coverage.py``) requires
# the column to exist on both sides.
_SQLITE_ADD_COLUMN = (
    "ALTER TABLE tenants "
    "ADD COLUMN auth_features TEXT NOT NULL DEFAULT '{}'"
)

_SQLITE_BACKFILL = (
    "UPDATE tenants "
    f"SET auth_features = '{_LEGACY_TENANT_AUTH_FEATURES_JSON}' "
    "WHERE auth_features = '{}'"
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
        for row in conn.exec_driver_sql("PRAGMA table_info(tenants)").fetchall()
    }
    if "auth_features" not in cols:
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
    # for the rare downgrade path.
    return
