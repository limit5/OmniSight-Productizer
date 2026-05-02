"""FS.3.2 — ``provisioned_storage`` table.

Persistent registry for tenant-owned object storage buckets created by
the FS.3.1 provisioning adapters.  The table intentionally stores only
the handoff facts named by the TODO row:

* ``tenant_id`` — tenant that owns the provisioned bucket.  FK cascades
  on tenant delete so deleted tenants do not leave dangling bucket
  registry rows in the control-plane DB.
* ``provider`` — one of the three FS.3.1 providers:
  ``s3`` / ``r2`` / ``supabase-storage``.  The CHECK clause mirrors
  ``backend.storage_provisioning.list_providers`` and is locked by the
  migration contract test.
* ``bucket_name`` — provider bucket identifier returned by the FS.3.1
  adapters.  This is not a secret; credentials stay outside this table.
* ``created_at`` — epoch seconds when the bucket was first recorded.
  Matches the FS.3.1 result shape, which uses ``time.time()``.

Why composite PK ``(tenant_id, provider)``
─────────────────────────────────────────
The TODO row enumerates no synthetic ``id`` column.  Mirroring the
0061 ``provisioned_databases`` pattern, the natural pair becomes the
storage identity: a tenant can have at most one recorded bucket per
provider, while provider retries can upsert the same pair without
manufacturing a separate app id.  The table stays within the requested
four-column surface.

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
  is updated in this same row to include ``provisioned_storage``
  (replays AFTER ``tenants`` because of the FK).  PK is composite TEXT
  so the table is NOT in ``TABLES_WITH_IDENTITY_ID``.  The drift guard
  ``test_migrator_schema_coverage`` and the FS.3.2 contract test
  enforce this.
* Production status of THIS commit: **dev-only**.  Next gate:
  ``deployed-inactive`` once the alembic chain (… → 0061 → 0062) is run
  against prod PG.

Revision ID: 0062
Revises: 0061
Create Date: 2026-05-03
"""
from __future__ import annotations

from alembic import op


revision = "0062"
down_revision = "0061"
branch_labels = None
depends_on = None


# ─── Provider whitelist ──────────────────────────────────────────────────


_PROVIDERS_SQL = "'r2','s3','supabase-storage'"


# ─── PG branch ───────────────────────────────────────────────────────────


_PG_CREATE_TABLE = (
    "CREATE TABLE IF NOT EXISTS provisioned_storage (\n"
    "    tenant_id   TEXT NOT NULL\n"
    "                     REFERENCES tenants(id) ON DELETE CASCADE,\n"
    f"    provider    TEXT NOT NULL\n"
    f"                     CHECK (provider IN ({_PROVIDERS_SQL})),\n"
    "    bucket_name TEXT NOT NULL,\n"
    "    created_at  DOUBLE PRECISION NOT NULL,\n"
    "    PRIMARY KEY (tenant_id, provider)\n"
    ")"
)

# ─── SQLite branch ───────────────────────────────────────────────────────


_SQLITE_CREATE_TABLE = (
    "CREATE TABLE IF NOT EXISTS provisioned_storage (\n"
    "    tenant_id   TEXT NOT NULL\n"
    "                     REFERENCES tenants(id) ON DELETE CASCADE,\n"
    f"    provider    TEXT NOT NULL\n"
    f"                     CHECK (provider IN ({_PROVIDERS_SQL})),\n"
    "    bucket_name TEXT NOT NULL,\n"
    "    created_at  REAL NOT NULL,\n"
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
    op.execute("DROP TABLE IF EXISTS provisioned_storage")
