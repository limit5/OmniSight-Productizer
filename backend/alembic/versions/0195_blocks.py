"""WP.1.2 -- ``blocks`` table.

Schema-only landing for the Wave-1 addressable Block primitive.  Runtime
CRUD, React rendering, permalink sharing, and redaction-mask application
remain owned by WP.1.3-WP.1.8.  This row gives those later steps one
durable table and the two lookup paths named in WP.1.2.

* ``block_id`` -- app-generated stable block id and table primary key.
* ``parent_id`` -- optional self-reference for grouping multi-step work.
* ``tenant_id`` -- tenant scope for future runtime filters.
* ``user_id`` / ``project_id`` / ``session_id`` -- optional attribution
  and grouping fields mirrored from :class:`backend.models.Block`.
* ``kind`` / ``status`` -- caller-defined block classification and state.
* ``title`` -- display label, defaulting to the model's empty string.
* ``payload`` / ``metadata`` / ``redaction_mask`` -- JSON payloads used
  by follow-up UI, share, and masking rows.
* ``started_at`` / ``completed_at`` / ``created_at`` -- block timing.

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
  is updated in this same row to include ``blocks`` after ``tenants``.
  TEXT PK, so the table is NOT in ``TABLES_WITH_IDENTITY_ID``.  The
  drift guard ``test_migrator_schema_coverage`` and the WP.1.2 contract
  test enforce this.
* SQLite dev parity -- :mod:`backend.db`'s ``_SCHEMA`` constant receives
  the same table/index shape with TIMESTAMPTZ/JSONB downgraded to TEXT so
  fresh dev SQLite DBs and the migrator drift guard see the table before
  runtime code starts writing.
* Production status of THIS commit: **dev-only**.  Next gate:
  ``deployed-inactive`` once the alembic chain (0194 -> 0195) is run
  against prod PG.

Revision ID: 0195
Revises: 0194
Create Date: 2026-05-06
"""
from __future__ import annotations

from alembic import op


revision = "0195"
down_revision = "0194"
branch_labels = None
depends_on = None


_PG_CREATE_TABLE = (
    "CREATE TABLE IF NOT EXISTS blocks (\n"
    "    block_id       TEXT PRIMARY KEY,\n"
    "    parent_id      TEXT REFERENCES blocks(block_id),\n"
    "    tenant_id      TEXT NOT NULL,\n"
    "    user_id        TEXT,\n"
    "    project_id     TEXT,\n"
    "    session_id     TEXT,\n"
    "    kind           TEXT NOT NULL,\n"
    "    status         TEXT NOT NULL,\n"
    "    title          TEXT NOT NULL DEFAULT '',\n"
    "    payload        JSONB NOT NULL DEFAULT '{}'::jsonb,\n"
    "    metadata       JSONB NOT NULL DEFAULT '{}'::jsonb,\n"
    "    redaction_mask JSONB NOT NULL DEFAULT '{}'::jsonb,\n"
    "    started_at     TIMESTAMPTZ,\n"
    "    completed_at   TIMESTAMPTZ,\n"
    "    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()\n"
    ")"
)

_SQLITE_CREATE_TABLE = (
    "CREATE TABLE IF NOT EXISTS blocks (\n"
    "    block_id       TEXT PRIMARY KEY,\n"
    "    parent_id      TEXT REFERENCES blocks(block_id),\n"
    "    tenant_id      TEXT NOT NULL,\n"
    "    user_id        TEXT,\n"
    "    project_id     TEXT,\n"
    "    session_id     TEXT,\n"
    "    kind           TEXT NOT NULL,\n"
    "    status         TEXT NOT NULL,\n"
    "    title          TEXT NOT NULL DEFAULT '',\n"
    "    payload        TEXT NOT NULL DEFAULT '{}',\n"
    "    metadata       TEXT NOT NULL DEFAULT '{}',\n"
    "    redaction_mask TEXT NOT NULL DEFAULT '{}',\n"
    "    started_at     TEXT,\n"
    "    completed_at   TEXT,\n"
    "    created_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP\n"
    ")"
)

_INDEX_TENANT_SESSION = (
    "CREATE INDEX IF NOT EXISTS idx_blocks_tenant_session "
    "ON blocks(tenant_id, session_id, started_at DESC)"
)

_INDEX_PARENT = (
    "CREATE INDEX IF NOT EXISTS idx_blocks_parent "
    "ON blocks(parent_id)"
)


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        conn.exec_driver_sql(_PG_CREATE_TABLE)
    else:
        conn.exec_driver_sql(_SQLITE_CREATE_TABLE)
    conn.exec_driver_sql(_INDEX_TENANT_SESSION)
    conn.exec_driver_sql(_INDEX_PARENT)


def downgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql("DROP INDEX IF EXISTS idx_blocks_parent")
    conn.exec_driver_sql("DROP INDEX IF EXISTS idx_blocks_tenant_session")
    conn.exec_driver_sql("DROP TABLE IF EXISTS blocks")
