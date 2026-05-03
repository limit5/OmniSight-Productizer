"""FS.1.3 — ``provisioned_databases`` table.

Persistent registry for tenant-owned databases created by the FS.1.1
provisioning adapters.  The table intentionally stores only the
handoff facts that later FS rows need:

* ``tenant_id`` — tenant that owns the provisioned database.  FK
  cascades on tenant delete so a deleted tenant does not leave a
  dangling encrypted DSN reference in the control-plane DB.
* ``provider`` — one of the three FS.1.1 providers:
  ``supabase`` / ``neon`` / ``planetscale``.  The CHECK clause mirrors
  ``backend.db_provisioning.list_providers`` and is locked by the
  migration contract test.
* ``connection_url_enc`` — encrypted connection URL ciphertext.  The
  plaintext URL must never be stored in this table.
* ``created_at`` — epoch seconds when the provider database was first
  recorded.  Matches the existing FS.1.1 result shape, which is handed
  through Python code that naturally uses ``time.time()``.
* ``status`` — provider-normalized lifecycle string.  No CHECK
  constraint: FS.1.1 already preserves provider-native statuses
  (``ACTIVE``, ``ready``, ``idle`` and similar) and this table must not
  reject a legitimate provider state just because the catalog is stale.

Why composite PK ``(tenant_id, provider)``
─────────────────────────────────────────
The TODO row enumerates no synthetic ``id`` column.  Mirroring the
0057 ``oauth_tokens`` pattern, the natural pair becomes the database
identity: a tenant can have at most one recorded database per provider,
while provider migrations / retries can upsert the same pair without
manufacturing a separate app id.  The table stays within the requested
five-column surface.

Module-global / cross-worker state audit
────────────────────────────────────────
Pure DDL migration — no module-level singleton, no in-memory cache.
Every worker reads the same DDL state from PG / SQLite so the table is
visible atomically post-commit.  **Answer #1** of SOP §1 — every worker
reads the same DDL state from the same DB.

Read-after-write timing audit
─────────────────────────────
The CREATE TABLE happens inside the alembic upgrade transaction.
``scripts/deploy.sh`` closes the asyncpg pool before alembic upgrade
and reopens it after, so runtime workers never see a half-shaped
schema.  No concurrent writer exists during the migration window.

Production readiness gate
─────────────────────────
* No new Python / OS package — production image needs no rebuild.
* New table added — ``scripts/migrate_sqlite_to_pg.py::TABLES_IN_ORDER``
  is updated in this same row to include ``provisioned_databases``
  (replays AFTER ``tenants`` because of the FK).  PK is composite TEXT
  so the table is NOT in ``TABLES_WITH_IDENTITY_ID``.  The drift guard
  ``test_migrator_schema_coverage`` and the FS.1.3 contract test
  enforce this.
* Production status of THIS commit: **dev-only**.  Next gate:
  ``deployed-inactive`` once the alembic chain (… → 0057 → 0061) is run
  against prod PG.

Revision ID: 0061
Revises: 0057
Create Date: 2026-05-03
"""
from __future__ import annotations

from alembic import op


revision = "0061"
down_revision = "0057"
branch_labels = None
depends_on = None


# ─── Provider whitelist ──────────────────────────────────────────────────


_PROVIDERS_SQL = "'neon','planetscale','supabase'"


# ─── PG branch ───────────────────────────────────────────────────────────


_PG_CREATE_TABLE = (
    "CREATE TABLE IF NOT EXISTS provisioned_databases (\n"
    "    tenant_id          TEXT NOT NULL\n"
    "                            REFERENCES tenants(id) ON DELETE CASCADE,\n"
    f"    provider           TEXT NOT NULL\n"
    f"                            CHECK (provider IN ({_PROVIDERS_SQL})),\n"
    "    connection_url_enc TEXT NOT NULL,\n"
    "    created_at         DOUBLE PRECISION NOT NULL,\n"
    "    status             TEXT NOT NULL,\n"
    "    PRIMARY KEY (tenant_id, provider)\n"
    ")"
)

# ─── SQLite branch ───────────────────────────────────────────────────────


_SQLITE_CREATE_TABLE = (
    "CREATE TABLE IF NOT EXISTS provisioned_databases (\n"
    "    tenant_id          TEXT NOT NULL\n"
    "                            REFERENCES tenants(id) ON DELETE CASCADE,\n"
    f"    provider           TEXT NOT NULL\n"
    f"                            CHECK (provider IN ({_PROVIDERS_SQL})),\n"
    "    connection_url_enc TEXT NOT NULL,\n"
    "    created_at         REAL NOT NULL,\n"
    "    status             TEXT NOT NULL,\n"
    "    PRIMARY KEY (tenant_id, provider)\n"
    ")"
)

# ─── upgrade / downgrade ─────────────────────────────────────────────────


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        conn.exec_driver_sql(_PG_CREATE_TABLE)
        return

    conn.exec_driver_sql(_SQLITE_CREATE_TABLE)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS provisioned_databases")
