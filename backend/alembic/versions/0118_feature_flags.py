"""WP.7.1 -- ``feature_flags`` registry table.

Schema-only landing for the tiered feature-flag registry.  Runtime
resolution priority, in-memory / Redis invalidation, expiry enforcement,
and operator UI toggles remain owned by WP.7.2-WP.7.9.  This row gives
those later steps one durable source of truth and reserves the generic
``audit_log`` namespace they must write when flag rows change.

* ``flag_name`` -- stable app-generated flag key and table primary key.
* ``tier`` -- deployment tier label.  WP.7.2 defines and constrains the
  exact five legal values: debug / dogfood / preview / release / runtime.
* ``state`` -- global state seed used by later resolution code.
* ``expires_at`` -- optional expiry timestamp for later CI enforcement.
* ``owner`` -- accountable team / operator for review and cleanup.
* ``created_at`` -- registry insertion timestamp.

Audit log contract
------------------
Runtime writers must emit ``audit.log(..., entity_kind="feature_flag",
entity_id=flag_name, ...)`` for create/update/delete.  The existing
``audit_log`` table and ``idx_audit_log_entity`` index already provide
the durable hash-chain and lookup path, so this migration does not add a
parallel audit table or seed rows.

Module-global / cross-worker state audit
----------------------------------------
Pure DDL migration -- no module-level singleton, no in-memory cache.
Every worker reads the same DDL state from PG / SQLite so the table is
visible atomically post-commit.  Answer #1 of SOP Step 1 -- every
worker reads the same DDL state from the same DB.

Read-after-write timing audit
-----------------------------
The CREATE TABLE happens inside the alembic upgrade transaction.
Runtime writers and hot-path caches are not introduced in this row, so
no downstream read-after-write timing expectation changes.

Production readiness gate
-------------------------
* No new Python / OS package -- production image needs no rebuild.
* New table added -- ``scripts/migrate_sqlite_to_pg.py::TABLES_IN_ORDER``
  is updated in this same row to include ``feature_flags`` after
  ``tenants``.  TEXT PK, so the table is NOT in
  ``TABLES_WITH_IDENTITY_ID``.  The drift guard
  ``test_migrator_schema_coverage`` and the WP.7.1 contract test
  enforce this.
* SQLite dev parity -- :mod:`backend.db`'s ``_SCHEMA`` constant receives
  the same table/index shape with TIMESTAMPTZ downgraded to TEXT so
  fresh dev SQLite DBs and the migrator drift guard see the table before
  runtime code starts writing.
* Production status of THIS commit: **dev-only**.  Next gate:
  ``deployed-inactive`` once the alembic chain (0186 -> 0118) is run
  against prod PG.  ``deployed-active`` requires WP.7.2-WP.7.8 runtime
  rows to resolve and audit flag changes.

Revision ID: 0118
Revises: 0186
Create Date: 2026-05-05
"""
from __future__ import annotations

from alembic import op


revision = "0118"
down_revision = "0186"
branch_labels = None
depends_on = None


_STATES_SQL = "'disabled','enabled'"
_TIERS_SQL = "'debug','dogfood','preview','release','runtime'"


_PG_CREATE_TABLE = (
    "CREATE TABLE IF NOT EXISTS feature_flags (\n"
    "    flag_name  TEXT PRIMARY KEY,\n"
    "    tier       TEXT NOT NULL\n"
    f"               CHECK (tier IN ({_TIERS_SQL})),\n"
    "    state      TEXT NOT NULL DEFAULT 'disabled'\n"
    f"               CHECK (state IN ({_STATES_SQL})),\n"
    "    expires_at TIMESTAMPTZ,\n"
    "    owner      TEXT NOT NULL DEFAULT '',\n"
    "    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()\n"
    ")"
)

_SQLITE_CREATE_TABLE = (
    "CREATE TABLE IF NOT EXISTS feature_flags (\n"
    "    flag_name  TEXT PRIMARY KEY,\n"
    "    tier       TEXT NOT NULL\n"
    f"               CHECK (tier IN ({_TIERS_SQL})),\n"
    "    state      TEXT NOT NULL DEFAULT 'disabled'\n"
    f"               CHECK (state IN ({_STATES_SQL})),\n"
    "    expires_at TEXT,\n"
    "    owner      TEXT NOT NULL DEFAULT '',\n"
    "    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP\n"
    ")"
)

_INDEX_TIER_STATE = (
    "CREATE INDEX IF NOT EXISTS idx_feature_flags_tier_state "
    "ON feature_flags(tier, state)"
)

_INDEX_EXPIRES_AT = (
    "CREATE INDEX IF NOT EXISTS idx_feature_flags_expires_at "
    "ON feature_flags(expires_at)"
)


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        conn.exec_driver_sql(_PG_CREATE_TABLE)
    else:
        conn.exec_driver_sql(_SQLITE_CREATE_TABLE)
    conn.exec_driver_sql(_INDEX_TIER_STATE)
    conn.exec_driver_sql(_INDEX_EXPIRES_AT)


def downgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql("DROP INDEX IF EXISTS idx_feature_flags_expires_at")
    conn.exec_driver_sql("DROP INDEX IF EXISTS idx_feature_flags_tier_state")
    conn.exec_driver_sql("DROP TABLE IF EXISTS feature_flags")
