"""KS.2.11 -- Tier 2 CMEK persistence tables.

Schema-only landing for KS Phase 2. Runtime rows KS.2.1-KS.2.10 already
define the wizard, live KMS adapters, revoke detection, graceful
degrade, SIEM specs, tier rewrap planning, and settings status UI. This
migration gives those helpers durable tables without changing call paths
yet.

Tables
------
* ``cmek_configs`` -- one customer CMK configuration record per saved
  wizard completion / KMS key target.
* ``tier_assignments`` -- current per-tenant security tier and its
  active CMEK config when the tenant is in Tier 2.
* ``cmek_revoke_events`` -- durable health / revoke event ledger keyed
  to the tenant and optional CMEK config.

Module-global / cross-worker state audit
----------------------------------------
Pure DDL migration -- no module-level singleton, no in-memory cache.
Every worker reads the same DDL state from PG / SQLite so the tables
are visible atomically post-commit. Answer #1 of SOP Step 1 -- every
worker reads the same DDL state from the same DB.

Read-after-write timing audit
-----------------------------
The CREATE TABLE statements happen inside the alembic upgrade
transaction. Runtime writers are not introduced in this row, so no
downstream read-after-write timing expectation changes.

Production readiness gate
-------------------------
* No new Python / OS package -- production image needs no rebuild.
* New tables added -- ``scripts/migrate_sqlite_to_pg.py::TABLES_IN_ORDER``
  is updated in this same row. All three tables have TEXT primary keys
  or tenant_id TEXT primary keys, so none are in
  ``TABLES_WITH_IDENTITY_ID``. The drift guard
  ``test_migrator_schema_coverage`` and the KS.2.11 contract test
  enforce this.
* SQLite dev parity -- :mod:`backend.db`'s ``_SCHEMA`` constant receives
  the same table/index shape with JSONB -> TEXT and DOUBLE PRECISION ->
  REAL dialect shifts.
* Production status of THIS commit: **dev-only**. Next gate:
  ``deployed-inactive`` once the alembic chain (0106 -> 0107) is run
  against prod PG. ``deployed-active`` requires KS.2.12-KS.2.13 to
  route CMEK runtime persistence and single-knob fallback through these
  tables.

Revision ID: 0107
Revises: 0106
Create Date: 2026-05-04
"""
from __future__ import annotations

from alembic import op


revision = "0107"
down_revision = "0106"
branch_labels = None
depends_on = None


_CMEK_PROVIDERS_SQL = "'aws-kms','gcp-kms','vault-transit'"
_CMEK_CONFIG_STATUSES_SQL = "'active','disabled','draft','revoked','verifying'"
_SECURITY_TIERS_SQL = "'tier-1','tier-2'"
_TIER_ASSIGNMENT_STATUSES_SQL = (
    "'active','downgrading','fallback_to_tier1','revoked','upgrading'"
)
_REVOKE_EVENT_REASONS_SQL = (
    "'describe_failed','key_disabled','permission_revoked','restored','unknown'"
)


# -- PG branch --------------------------------------------------------------


_PG_CREATE_CMEK_CONFIGS = (
    "CREATE TABLE IF NOT EXISTS cmek_configs (\n"
    "    config_id       TEXT PRIMARY KEY,\n"
    "    tenant_id       TEXT NOT NULL\n"
    "                         REFERENCES tenants(id) ON DELETE CASCADE,\n"
    f"    provider        TEXT NOT NULL CHECK (provider IN ({_CMEK_PROVIDERS_SQL})),\n"
    "    key_id          TEXT NOT NULL,\n"
    "    policy_principal TEXT NOT NULL DEFAULT '',\n"
    "    verification_id TEXT NOT NULL DEFAULT '',\n"
    f"    status          TEXT NOT NULL DEFAULT 'draft'\n"
    f"                         CHECK (status IN ({_CMEK_CONFIG_STATUSES_SQL})),\n"
    "    verified_at     DOUBLE PRECISION,\n"
    "    created_at      DOUBLE PRECISION NOT NULL,\n"
    "    updated_at      DOUBLE PRECISION NOT NULL,\n"
    "    disabled_at     DOUBLE PRECISION,\n"
    "    metadata_json   JSONB NOT NULL DEFAULT '{}'::jsonb\n"
    ")"
)

_PG_CREATE_TIER_ASSIGNMENTS = (
    "CREATE TABLE IF NOT EXISTS tier_assignments (\n"
    "    tenant_id       TEXT PRIMARY KEY\n"
    "                         REFERENCES tenants(id) ON DELETE CASCADE,\n"
    f"    security_tier   TEXT NOT NULL DEFAULT 'tier-1'\n"
    f"                         CHECK (security_tier IN ({_SECURITY_TIERS_SQL})),\n"
    "    cmek_config_id  TEXT\n"
    "                         REFERENCES cmek_configs(config_id) ON DELETE SET NULL,\n"
    f"    status          TEXT NOT NULL DEFAULT 'active'\n"
    f"                         CHECK (status IN ({_TIER_ASSIGNMENT_STATUSES_SQL})),\n"
    "    assigned_by     TEXT NOT NULL DEFAULT '',\n"
    "    assigned_at     DOUBLE PRECISION NOT NULL,\n"
    "    updated_at      DOUBLE PRECISION NOT NULL,\n"
    "    metadata_json   JSONB NOT NULL DEFAULT '{}'::jsonb\n"
    ")"
)

_PG_CREATE_CMEK_REVOKE_EVENTS = (
    "CREATE TABLE IF NOT EXISTS cmek_revoke_events (\n"
    "    event_id       TEXT PRIMARY KEY,\n"
    "    tenant_id      TEXT NOT NULL\n"
    "                         REFERENCES tenants(id) ON DELETE CASCADE,\n"
    "    cmek_config_id TEXT\n"
    "                         REFERENCES cmek_configs(config_id) ON DELETE SET NULL,\n"
    f"    provider       TEXT NOT NULL CHECK (provider IN ({_CMEK_PROVIDERS_SQL})),\n"
    "    key_id         TEXT NOT NULL,\n"
    f"    reason         TEXT NOT NULL CHECK (reason IN ({_REVOKE_EVENT_REASONS_SQL})),\n"
    "    raw_state      TEXT NOT NULL DEFAULT '',\n"
    "    source         TEXT NOT NULL DEFAULT 'cmek_revoke_detector',\n"
    "    detected_at    DOUBLE PRECISION NOT NULL,\n"
    "    restored_at    DOUBLE PRECISION,\n"
    "    detail_json    JSONB NOT NULL DEFAULT '{}'::jsonb\n"
    ")"
)

_PG_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_cmek_configs_tenant_status "
    "ON cmek_configs(tenant_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_cmek_configs_provider_key "
    "ON cmek_configs(provider, key_id)",
    "CREATE INDEX IF NOT EXISTS idx_tier_assignments_security_tier "
    "ON tier_assignments(security_tier, status)",
    "CREATE INDEX IF NOT EXISTS idx_tier_assignments_cmek_config "
    "ON tier_assignments(cmek_config_id)",
    "CREATE INDEX IF NOT EXISTS idx_cmek_revoke_events_tenant_time "
    "ON cmek_revoke_events(tenant_id, detected_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_cmek_revoke_events_config_time "
    "ON cmek_revoke_events(cmek_config_id, detected_at DESC)",
)


# -- SQLite branch ----------------------------------------------------------


_SQLITE_CREATE_CMEK_CONFIGS = (
    "CREATE TABLE IF NOT EXISTS cmek_configs (\n"
    "    config_id       TEXT PRIMARY KEY,\n"
    "    tenant_id       TEXT NOT NULL\n"
    "                         REFERENCES tenants(id) ON DELETE CASCADE,\n"
    f"    provider        TEXT NOT NULL CHECK (provider IN ({_CMEK_PROVIDERS_SQL})),\n"
    "    key_id          TEXT NOT NULL,\n"
    "    policy_principal TEXT NOT NULL DEFAULT '',\n"
    "    verification_id TEXT NOT NULL DEFAULT '',\n"
    f"    status          TEXT NOT NULL DEFAULT 'draft'\n"
    f"                         CHECK (status IN ({_CMEK_CONFIG_STATUSES_SQL})),\n"
    "    verified_at     REAL,\n"
    "    created_at      REAL NOT NULL,\n"
    "    updated_at      REAL NOT NULL,\n"
    "    disabled_at     REAL,\n"
    "    metadata_json   TEXT NOT NULL DEFAULT '{}'\n"
    ")"
)

_SQLITE_CREATE_TIER_ASSIGNMENTS = (
    "CREATE TABLE IF NOT EXISTS tier_assignments (\n"
    "    tenant_id       TEXT PRIMARY KEY\n"
    "                         REFERENCES tenants(id) ON DELETE CASCADE,\n"
    f"    security_tier   TEXT NOT NULL DEFAULT 'tier-1'\n"
    f"                         CHECK (security_tier IN ({_SECURITY_TIERS_SQL})),\n"
    "    cmek_config_id  TEXT\n"
    "                         REFERENCES cmek_configs(config_id) ON DELETE SET NULL,\n"
    f"    status          TEXT NOT NULL DEFAULT 'active'\n"
    f"                         CHECK (status IN ({_TIER_ASSIGNMENT_STATUSES_SQL})),\n"
    "    assigned_by     TEXT NOT NULL DEFAULT '',\n"
    "    assigned_at     REAL NOT NULL,\n"
    "    updated_at      REAL NOT NULL,\n"
    "    metadata_json   TEXT NOT NULL DEFAULT '{}'\n"
    ")"
)

_SQLITE_CREATE_CMEK_REVOKE_EVENTS = (
    "CREATE TABLE IF NOT EXISTS cmek_revoke_events (\n"
    "    event_id       TEXT PRIMARY KEY,\n"
    "    tenant_id      TEXT NOT NULL\n"
    "                         REFERENCES tenants(id) ON DELETE CASCADE,\n"
    "    cmek_config_id TEXT\n"
    "                         REFERENCES cmek_configs(config_id) ON DELETE SET NULL,\n"
    f"    provider       TEXT NOT NULL CHECK (provider IN ({_CMEK_PROVIDERS_SQL})),\n"
    "    key_id         TEXT NOT NULL,\n"
    f"    reason         TEXT NOT NULL CHECK (reason IN ({_REVOKE_EVENT_REASONS_SQL})),\n"
    "    raw_state      TEXT NOT NULL DEFAULT '',\n"
    "    source         TEXT NOT NULL DEFAULT 'cmek_revoke_detector',\n"
    "    detected_at    REAL NOT NULL,\n"
    "    restored_at    REAL,\n"
    "    detail_json    TEXT NOT NULL DEFAULT '{}'\n"
    ")"
)


_TABLES_PG = (
    _PG_CREATE_CMEK_CONFIGS,
    _PG_CREATE_TIER_ASSIGNMENTS,
    _PG_CREATE_CMEK_REVOKE_EVENTS,
)

_TABLES_SQLITE = (
    _SQLITE_CREATE_CMEK_CONFIGS,
    _SQLITE_CREATE_TIER_ASSIGNMENTS,
    _SQLITE_CREATE_CMEK_REVOKE_EVENTS,
)

_INDEXES = _PG_INDEXES

_DROP_INDEXES = (
    "idx_cmek_revoke_events_config_time",
    "idx_cmek_revoke_events_tenant_time",
    "idx_tier_assignments_cmek_config",
    "idx_tier_assignments_security_tier",
    "idx_cmek_configs_provider_key",
    "idx_cmek_configs_tenant_status",
)

_DROP_TABLES = (
    "cmek_revoke_events",
    "tier_assignments",
    "cmek_configs",
)


def upgrade() -> None:
    conn = op.get_bind()
    tables = _TABLES_PG if conn.dialect.name == "postgresql" else _TABLES_SQLITE
    for sql in tables:
        conn.exec_driver_sql(sql)
    for sql in _INDEXES:
        conn.exec_driver_sql(sql)


def downgrade() -> None:
    for idx in _DROP_INDEXES:
        op.drop_index(idx, if_exists=True)
    for table in _DROP_TABLES:
        op.drop_table(table, if_exists=True)
