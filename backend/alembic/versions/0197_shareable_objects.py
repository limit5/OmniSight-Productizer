"""WP.9.1 -- ``shareable_objects`` table.

Schema-only landing for the generic share registry paired with the
WP.1 Block primitive.  Runtime permalink generation, ACL resolution,
expiry cleanup, and redaction enforcement remain owned by WP.9.2-WP.9.6.
This row gives those later steps one durable table and the lookup shape
needed to resolve a share id back to a tenant-scoped object.

* ``share_id`` -- app-generated stable share/permalink id and table
  primary key.
* ``object_kind`` / ``object_id`` -- generic target pointer for blocks,
  runbooks, notebooks, agent transcripts, and future shareable objects.
* ``tenant_id`` -- tenant scope for object isolation.
* ``owner_user_id`` -- user who owns the share row.
* ``visibility`` -- stored ACL tier for later private / team / tenant /
  public enforcement.
* ``expires_at`` -- optional expiry timestamp for later sweep jobs.
* ``redaction_applied`` -- JSON audit payload describing what masking
  was applied at share time.
* ``created_at`` -- share creation timestamp.

Module-global / cross-worker state audit
----------------------------------------
Pure DDL migration -- no module-level singleton, no in-memory cache.
Every worker reads the same DDL state from PG / SQLite so the table is
visible atomically post-commit.  Answer #1 of SOP Step 1 -- every
worker reads the same DDL state from the same DB.

Read-after-write timing audit
-----------------------------
The CREATE TABLE happens inside the alembic upgrade transaction.
Runtime writers are not introduced in this row, so no downstream
read-after-write timing expectation changes.

Production readiness gate
-------------------------
* No new Python / OS package -- production image needs no rebuild.
* New table added -- ``scripts/migrate_sqlite_to_pg.py::TABLES_IN_ORDER``
  is updated in this same row to include ``shareable_objects`` after
  ``users`` because ``owner_user_id`` references ``users(id)``.  TEXT
  PK, so the table is NOT in ``TABLES_WITH_IDENTITY_ID``.  The drift
  guard ``test_migrator_schema_coverage`` and the WP.9.1 contract test
  enforce this.
* SQLite dev parity -- :mod:`backend.db`'s ``_SCHEMA`` constant receives
  the same table/index shape with TIMESTAMPTZ/JSONB downgraded to TEXT so
  fresh dev SQLite DBs and the migrator drift guard see the table before
  runtime code starts writing.
* Production status of THIS commit: **dev-only**.  Next gate:
  ``deployed-inactive`` once the alembic chain (0196 -> 0197) is run
  against prod PG.  ``deployed-active`` requires WP.9.2-WP.9.5 runtime
  paths to create and enforce share rows.

Revision ID: 0197
Revises: 0196
Create Date: 2026-05-06
"""
from __future__ import annotations

from alembic import op


revision = "0197"
down_revision = "0196"
branch_labels = None
depends_on = None


_VISIBILITIES_SQL = "'private','team','tenant','public'"


_PG_CREATE_TABLE = (
    "CREATE TABLE IF NOT EXISTS shareable_objects (\n"
    "    share_id          TEXT PRIMARY KEY,\n"
    "    object_kind       TEXT NOT NULL,\n"
    "    object_id         TEXT NOT NULL,\n"
    "    tenant_id         TEXT NOT NULL\n"
    "                            REFERENCES tenants(id) ON DELETE CASCADE,\n"
    "    owner_user_id     TEXT NOT NULL\n"
    "                            REFERENCES users(id) ON DELETE CASCADE,\n"
    "    visibility        TEXT NOT NULL DEFAULT 'private'\n"
    f"                            CHECK (visibility IN ({_VISIBILITIES_SQL})),\n"
    "    expires_at        TIMESTAMPTZ,\n"
    "    redaction_applied JSONB NOT NULL DEFAULT '{}'::jsonb,\n"
    "    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()\n"
    ")"
)

_SQLITE_CREATE_TABLE = (
    "CREATE TABLE IF NOT EXISTS shareable_objects (\n"
    "    share_id          TEXT PRIMARY KEY,\n"
    "    object_kind       TEXT NOT NULL,\n"
    "    object_id         TEXT NOT NULL,\n"
    "    tenant_id         TEXT NOT NULL\n"
    "                            REFERENCES tenants(id) ON DELETE CASCADE,\n"
    "    owner_user_id     TEXT NOT NULL\n"
    "                            REFERENCES users(id) ON DELETE CASCADE,\n"
    "    visibility        TEXT NOT NULL DEFAULT 'private'\n"
    f"                            CHECK (visibility IN ({_VISIBILITIES_SQL})),\n"
    "    expires_at        TEXT,\n"
    "    redaction_applied TEXT NOT NULL DEFAULT '{}',\n"
    "    created_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP\n"
    ")"
)

_INDEX_TENANT_OBJECT = (
    "CREATE INDEX IF NOT EXISTS idx_shareable_objects_tenant_object "
    "ON shareable_objects(tenant_id, object_kind, object_id)"
)

_INDEX_OWNER_CREATED = (
    "CREATE INDEX IF NOT EXISTS idx_shareable_objects_owner_created "
    "ON shareable_objects(owner_user_id, created_at DESC)"
)

_INDEX_EXPIRES_AT = (
    "CREATE INDEX IF NOT EXISTS idx_shareable_objects_expires_at "
    "ON shareable_objects(expires_at) "
    "WHERE expires_at IS NOT NULL"
)


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        conn.exec_driver_sql(_PG_CREATE_TABLE)
    else:
        conn.exec_driver_sql(_SQLITE_CREATE_TABLE)
    conn.exec_driver_sql(_INDEX_TENANT_OBJECT)
    conn.exec_driver_sql(_INDEX_OWNER_CREATED)
    conn.exec_driver_sql(_INDEX_EXPIRES_AT)


def downgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql("DROP INDEX IF EXISTS idx_shareable_objects_expires_at")
    conn.exec_driver_sql("DROP INDEX IF EXISTS idx_shareable_objects_owner_created")
    conn.exec_driver_sql("DROP INDEX IF EXISTS idx_shareable_objects_tenant_object")
    conn.exec_driver_sql("DROP TABLE IF EXISTS shareable_objects")
