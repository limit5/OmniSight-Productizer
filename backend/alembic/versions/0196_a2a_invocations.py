"""BP.A2A.4 -- ``a2a_invocations`` table.

Persistent audit/replay index for inbound Agent-to-Agent calls.  This
migration lands only the durable table; runtime writers remain owned by
the follow-up A2A invocation persistence work.

* ``invocation_id`` -- app-generated stable id from the A2A route.
* ``tenant_id`` -- tenant scope for replay isolation.  Tenant deletion
  cascades invocation rows so teardown cannot leave orphaned metadata.
* ``agent_name`` -- AgentCard capability name that handled the call.
* ``caller_identity`` -- authenticated caller email / API-key identity.
* ``payload_hash`` / ``response_hash`` -- hashes only; request and
  response plaintext are deliberately absent from this table.
* ``latency_ms`` -- wall-clock invocation latency recorded by runtime.
* ``status`` -- terminal invocation status from the A2A response shape.
* ``created_at`` -- invocation creation time for recent-first audit
  and replay views.

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
  is updated in this same row to include ``a2a_invocations`` after
  ``tenants``.  TEXT PK, so the table is NOT in
  ``TABLES_WITH_IDENTITY_ID``.  The drift guard
  ``test_migrator_schema_coverage`` and the BP.A2A.4 contract test
  enforce this.
* SQLite dev parity -- :mod:`backend.db`'s ``_SCHEMA`` constant receives
  the same table/index shape with TIMESTAMPTZ downgraded to TEXT so
  fresh dev SQLite DBs and the migrator drift guard see the table before
  runtime code starts writing.
* Production status of THIS commit: **dev-only**.  Next gate:
  ``deployed-inactive`` once the alembic chain (0195 -> 0196) is run
  against prod PG.  ``deployed-active`` requires the A2A inbound runtime
  to write invocation rows.

Revision ID: 0196
Revises: 0195
Create Date: 2026-05-06
"""
from __future__ import annotations

from alembic import op


revision = "0196"
down_revision = "0195"
branch_labels = None
depends_on = None


_STATUSES_SQL = "'completed','failed'"


_PG_CREATE_TABLE = (
    "CREATE TABLE IF NOT EXISTS a2a_invocations (\n"
    "    invocation_id   TEXT PRIMARY KEY,\n"
    "    tenant_id       TEXT NOT NULL\n"
    "                          REFERENCES tenants(id) ON DELETE CASCADE,\n"
    "    agent_name      TEXT NOT NULL,\n"
    "    caller_identity TEXT NOT NULL,\n"
    "    payload_hash    TEXT NOT NULL,\n"
    "    response_hash   TEXT NOT NULL,\n"
    "    latency_ms      INTEGER NOT NULL CHECK (latency_ms >= 0),\n"
    "    status          TEXT NOT NULL\n"
    f"                          CHECK (status IN ({_STATUSES_SQL})),\n"
    "    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()\n"
    ")"
)

_SQLITE_CREATE_TABLE = (
    "CREATE TABLE IF NOT EXISTS a2a_invocations (\n"
    "    invocation_id   TEXT PRIMARY KEY,\n"
    "    tenant_id       TEXT NOT NULL\n"
    "                          REFERENCES tenants(id) ON DELETE CASCADE,\n"
    "    agent_name      TEXT NOT NULL,\n"
    "    caller_identity TEXT NOT NULL,\n"
    "    payload_hash    TEXT NOT NULL,\n"
    "    response_hash   TEXT NOT NULL,\n"
    "    latency_ms      INTEGER NOT NULL CHECK (latency_ms >= 0),\n"
    "    status          TEXT NOT NULL\n"
    f"                          CHECK (status IN ({_STATUSES_SQL})),\n"
    "    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP\n"
    ")"
)

_INDEX_TENANT_AGENT_TIME = (
    "CREATE INDEX IF NOT EXISTS idx_a2a_invocations_tenant_agent_time "
    "ON a2a_invocations(tenant_id, agent_name, created_at DESC)"
)

_INDEX_PAYLOAD_HASH = (
    "CREATE INDEX IF NOT EXISTS idx_a2a_invocations_payload_hash "
    "ON a2a_invocations(payload_hash)"
)


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        conn.exec_driver_sql(_PG_CREATE_TABLE)
    else:
        conn.exec_driver_sql(_SQLITE_CREATE_TABLE)
    conn.exec_driver_sql(_INDEX_TENANT_AGENT_TIME)
    conn.exec_driver_sql(_INDEX_PAYLOAD_HASH)


def downgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql("DROP INDEX IF EXISTS idx_a2a_invocations_payload_hash")
    conn.exec_driver_sql("DROP INDEX IF EXISTS idx_a2a_invocations_tenant_agent_time")
    conn.exec_driver_sql("DROP TABLE IF EXISTS a2a_invocations")
