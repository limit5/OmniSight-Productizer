"""KS.4.13 -- ``firewall_events`` table.

Persistent review queue for KS.4.10-KS.4.12 LLM firewall decisions.
This migration lands only the schema; runtime persistence is owned by
the follow-up firewall integration rows.

* ``event_id`` -- app-generated event id for review/audit correlation.
* ``tenant_id`` -- tenant scope for review isolation. Tenant deletion
  cascades review rows so teardown cannot leave orphaned firewall data.
* ``classification`` -- persisted review class. Only ``suspicious`` and
  ``blocked`` are valid here; safe inputs are not stored.
* ``input_hash`` -- SHA-256 or equivalent stable hash of the input.
  Plaintext input is deliberately absent from the schema.
* ``blocked_reason`` -- normalized reason label/detail from the firewall.
* ``created_at`` -- event creation time for recent-first review queues.

Module-global / cross-worker state audit
----------------------------------------
Pure DDL migration -- no module-level singleton, no in-memory cache.
Every worker reads the same DDL state from PG / SQLite so the table is
visible atomically post-commit. Answer #1 of SOP Step 1 -- every worker
reads the same DDL state from the same DB.

Read-after-write timing audit
-----------------------------
The CREATE TABLE happens inside the alembic upgrade transaction. Runtime
writers are not introduced in this row, so no downstream read-after-write
timing expectation changes.

Production readiness gate
-------------------------
* No new Python / OS package -- production image needs no rebuild.
* New table added -- ``scripts/migrate_sqlite_to_pg.py::TABLES_IN_ORDER``
  is updated in this same row to include ``firewall_events`` after
  ``tenants``. TEXT PK, so the table is NOT in
  ``TABLES_WITH_IDENTITY_ID``. The drift guard
  ``test_migrator_schema_coverage`` and the KS.4.13 contract test
  enforce this.
* SQLite dev parity -- :mod:`backend.db`'s ``_SCHEMA`` constant receives
  the same CREATE TABLE / index shape so fresh dev SQLite DBs and the
  migrator drift guard see the table before runtime code starts writing.
* Production status of THIS commit: **dev-only**. Next gate:
  ``deployed-inactive`` once the alembic chain (0185 -> 0187) is run
  against prod PG. ``deployed-active`` requires KS.4.11-KS.4.12 runtime
  entries to write suspicious / blocked decisions into this table.

Revision ID: 0187
Revises: 0185
Create Date: 2026-05-03
"""
from __future__ import annotations

from alembic import op


revision = "0187"
down_revision = "0185"
branch_labels = None
depends_on = None


_CLASSIFICATIONS_SQL = "'blocked','suspicious'"


_PG_CREATE_TABLE = (
    "CREATE TABLE IF NOT EXISTS firewall_events (\n"
    "    event_id       TEXT PRIMARY KEY,\n"
    "    tenant_id      TEXT NOT NULL\n"
    "                         REFERENCES tenants(id) ON DELETE CASCADE,\n"
    f"    classification TEXT NOT NULL CHECK (classification IN ({_CLASSIFICATIONS_SQL})),\n"
    "    input_hash     TEXT NOT NULL,\n"
    "    blocked_reason TEXT NOT NULL DEFAULT '',\n"
    "    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()\n"
    ")"
)

_SQLITE_CREATE_TABLE = (
    "CREATE TABLE IF NOT EXISTS firewall_events (\n"
    "    event_id       TEXT PRIMARY KEY,\n"
    "    tenant_id      TEXT NOT NULL\n"
    "                         REFERENCES tenants(id) ON DELETE CASCADE,\n"
    f"    classification TEXT NOT NULL CHECK (classification IN ({_CLASSIFICATIONS_SQL})),\n"
    "    input_hash     TEXT NOT NULL,\n"
    "    blocked_reason TEXT NOT NULL DEFAULT '',\n"
    "    created_at     TEXT NOT NULL\n"
    ")"
)

_INDEX_TENANT_CLASS_TIME = (
    "CREATE INDEX IF NOT EXISTS idx_firewall_events_tenant_class_time "
    "ON firewall_events(tenant_id, classification, created_at DESC)"
)

_INDEX_INPUT_HASH = (
    "CREATE INDEX IF NOT EXISTS idx_firewall_events_input_hash "
    "ON firewall_events(input_hash)"
)


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        conn.exec_driver_sql(_PG_CREATE_TABLE)
    else:
        conn.exec_driver_sql(_SQLITE_CREATE_TABLE)
    conn.exec_driver_sql(_INDEX_TENANT_CLASS_TIME)
    conn.exec_driver_sql(_INDEX_INPUT_HASH)


def downgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql("DROP INDEX IF EXISTS idx_firewall_events_input_hash")
    conn.exec_driver_sql("DROP INDEX IF EXISTS idx_firewall_events_tenant_class_time")
    op.execute("DROP TABLE IF EXISTS firewall_events")
