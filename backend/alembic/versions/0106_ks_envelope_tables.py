"""KS.1.10 -- envelope-encryption persistence tables.

Schema-only landing for KS Phase 1. Runtime rows KS.1.1-KS.1.9 already
define the KMS adapter, tenant DEK ref, decryption audit fan-out, spend
anomaly detector, log scrubber, backup DLP, and zeroization helpers.
This migration gives those helpers durable tables without changing the
call paths yet.

Tables
------
* ``kms_keys`` -- registry of master KEKs by provider/key id/version.
* ``tenant_deks`` -- persisted :class:`TenantDEKRef` rows from
  ``backend.security.envelope``. Stores wrapped DEKs only; plaintext
  DEKs never enter the database.
* ``decryption_audits`` -- normalized KS decrypt index keyed back to
  the N10 ``audit_log`` id. The tamper-evident source of truth remains
  ``audit_log``; this table is for tenant/key/request lookups.
* ``spend_thresholds`` -- durable per-tenant token-rate thresholds that
  match ``backend.security.spend_anomaly.SpendThreshold``.
* ``kek_rotations`` -- operator/audit metadata for quarterly KEK
  rotation batches and lazy re-encrypt progress.

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
  is updated in this same row. All five tables have TEXT or composite
  TEXT primary keys, so none are in ``TABLES_WITH_IDENTITY_ID``. The
  drift guard ``test_migrator_schema_coverage`` and the KS.1.10
  contract test enforce this.
* SQLite dev parity -- :mod:`backend.db`'s ``_SCHEMA`` constant
  receives the same table/index shape with JSONB -> TEXT,
  BOOLEAN -> INTEGER, and DOUBLE PRECISION -> REAL dialect shifts.
* Production status of THIS commit: **dev-only**. Next gate:
  ``deployed-inactive`` once the alembic chain (0065 -> 0106) is run
  against prod PG. ``deployed-active`` requires KS.1.11-KS.1.13 to
  start routing all secret paths through these tables.

Revision ID: 0106
Revises: 0065
Create Date: 2026-05-03
"""
from __future__ import annotations

from alembic import op


revision = "0106"
down_revision = "0065"
branch_labels = None
depends_on = None


# -- Enum whitelists --------------------------------------------------------


_KMS_PROVIDERS_SQL = "'aws-kms','gcp-kms','local-fernet','vault-transit'"
_KMS_KEY_STATUSES_SQL = "'active','disabled','destroyed','retiring'"
_KEK_ROTATION_STATUSES_SQL = "'cancelled','completed','failed','running','scheduled'"


# -- PG branch --------------------------------------------------------------


_PG_CREATE_KMS_KEYS = (
    "CREATE TABLE IF NOT EXISTS kms_keys (\n"
    "    key_id        TEXT PRIMARY KEY,\n"
    f"    provider      TEXT NOT NULL CHECK (provider IN ({_KMS_PROVIDERS_SQL})),\n"
    "    key_version   TEXT NOT NULL DEFAULT '1',\n"
    "    purpose       TEXT NOT NULL DEFAULT 'tenant-secret',\n"
    f"    status        TEXT NOT NULL DEFAULT 'active'\n"
    f"                      CHECK (status IN ({_KMS_KEY_STATUSES_SQL})),\n"
    "    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,\n"
    "    created_at    DOUBLE PRECISION NOT NULL,\n"
    "    rotated_at    DOUBLE PRECISION\n"
    ")"
)

_PG_CREATE_TENANT_DEKS = (
    "CREATE TABLE IF NOT EXISTS tenant_deks (\n"
    "    dek_id                  TEXT PRIMARY KEY,\n"
    "    tenant_id               TEXT NOT NULL\n"
    "                                  REFERENCES tenants(id) ON DELETE CASCADE,\n"
    "    key_id                  TEXT NOT NULL\n"
    "                                  REFERENCES kms_keys(key_id) ON DELETE RESTRICT,\n"
    f"    provider                TEXT NOT NULL\n"
    f"                                  CHECK (provider IN ({_KMS_PROVIDERS_SQL})),\n"
    "    wrapped_dek_b64         TEXT NOT NULL,\n"
    "    key_version             TEXT,\n"
    "    wrap_algorithm          TEXT NOT NULL DEFAULT '',\n"
    "    encryption_context_json JSONB NOT NULL DEFAULT '{}'::jsonb,\n"
    "    purpose                 TEXT NOT NULL DEFAULT 'tenant-secret',\n"
    "    schema_version          INTEGER NOT NULL DEFAULT 1,\n"
    "    created_at              DOUBLE PRECISION NOT NULL,\n"
    "    rotated_at              DOUBLE PRECISION,\n"
    "    revoked_at              DOUBLE PRECISION\n"
    ")"
)

_PG_CREATE_DECRYPTION_AUDITS = (
    "CREATE TABLE IF NOT EXISTS decryption_audits (\n"
    "    audit_id      TEXT PRIMARY KEY,\n"
    "    tenant_id     TEXT NOT NULL\n"
    "                         REFERENCES tenants(id) ON DELETE CASCADE,\n"
    "    user_id       TEXT NOT NULL,\n"
    "    key_id        TEXT NOT NULL,\n"
    "    dek_id        TEXT,\n"
    "    request_id    TEXT NOT NULL,\n"
    "    purpose       TEXT NOT NULL DEFAULT '',\n"
    f"    provider      TEXT NOT NULL CHECK (provider IN ({_KMS_PROVIDERS_SQL})),\n"
    "    audit_log_id  INTEGER,\n"
    "    decrypted_at  DOUBLE PRECISION NOT NULL,\n"
    "    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb\n"
    ")"
)

_PG_CREATE_SPEND_THRESHOLDS = (
    "CREATE TABLE IF NOT EXISTS spend_thresholds (\n"
    "    tenant_id           TEXT PRIMARY KEY\n"
    "                              REFERENCES tenants(id) ON DELETE CASCADE,\n"
    "    token_rate_limit    INTEGER NOT NULL CHECK (token_rate_limit > 0),\n"
    "    window_seconds      DOUBLE PRECISION NOT NULL CHECK (window_seconds > 0),\n"
    "    throttle_seconds    DOUBLE PRECISION NOT NULL CHECK (throttle_seconds > 0),\n"
    "    enabled             BOOLEAN NOT NULL DEFAULT TRUE,\n"
    "    alert_channels_json JSONB NOT NULL DEFAULT '[]'::jsonb,\n"
    "    created_at          DOUBLE PRECISION NOT NULL,\n"
    "    updated_at          DOUBLE PRECISION NOT NULL\n"
    ")"
)

_PG_CREATE_KEK_ROTATIONS = (
    "CREATE TABLE IF NOT EXISTS kek_rotations (\n"
    "    rotation_id      TEXT PRIMARY KEY,\n"
    "    key_id           TEXT NOT NULL\n"
    "                           REFERENCES kms_keys(key_id) ON DELETE RESTRICT,\n"
    f"    provider         TEXT NOT NULL CHECK (provider IN ({_KMS_PROVIDERS_SQL})),\n"
    "    from_key_version TEXT NOT NULL,\n"
    "    to_key_version   TEXT NOT NULL,\n"
    f"    status           TEXT NOT NULL DEFAULT 'scheduled'\n"
    f"                           CHECK (status IN ({_KEK_ROTATION_STATUSES_SQL})),\n"
    "    scheduled_for    DOUBLE PRECISION,\n"
    "    started_at       DOUBLE PRECISION,\n"
    "    completed_at     DOUBLE PRECISION,\n"
    "    rotated_rows     INTEGER NOT NULL DEFAULT 0,\n"
    "    error            TEXT NOT NULL DEFAULT '',\n"
    "    metadata_json    JSONB NOT NULL DEFAULT '{}'::jsonb\n"
    ")"
)

_PG_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_kms_keys_provider_status "
    "ON kms_keys(provider, status)",
    "CREATE INDEX IF NOT EXISTS idx_tenant_deks_tenant_purpose "
    "ON tenant_deks(tenant_id, purpose)",
    "CREATE INDEX IF NOT EXISTS idx_tenant_deks_key_version "
    "ON tenant_deks(key_id, key_version)",
    "CREATE INDEX IF NOT EXISTS idx_decryption_audits_tenant_time "
    "ON decryption_audits(tenant_id, decrypted_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_decryption_audits_request "
    "ON decryption_audits(request_id)",
    "CREATE INDEX IF NOT EXISTS idx_decryption_audits_key_time "
    "ON decryption_audits(key_id, decrypted_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_kek_rotations_status_schedule "
    "ON kek_rotations(status, scheduled_for)",
    "CREATE INDEX IF NOT EXISTS idx_kek_rotations_key "
    "ON kek_rotations(key_id, started_at DESC)",
)


# -- SQLite branch ----------------------------------------------------------


_SQLITE_CREATE_KMS_KEYS = (
    "CREATE TABLE IF NOT EXISTS kms_keys (\n"
    "    key_id        TEXT PRIMARY KEY,\n"
    f"    provider      TEXT NOT NULL CHECK (provider IN ({_KMS_PROVIDERS_SQL})),\n"
    "    key_version   TEXT NOT NULL DEFAULT '1',\n"
    "    purpose       TEXT NOT NULL DEFAULT 'tenant-secret',\n"
    f"    status        TEXT NOT NULL DEFAULT 'active'\n"
    f"                      CHECK (status IN ({_KMS_KEY_STATUSES_SQL})),\n"
    "    metadata_json TEXT NOT NULL DEFAULT '{}',\n"
    "    created_at    REAL NOT NULL,\n"
    "    rotated_at    REAL\n"
    ")"
)

_SQLITE_CREATE_TENANT_DEKS = (
    "CREATE TABLE IF NOT EXISTS tenant_deks (\n"
    "    dek_id                  TEXT PRIMARY KEY,\n"
    "    tenant_id               TEXT NOT NULL\n"
    "                                  REFERENCES tenants(id) ON DELETE CASCADE,\n"
    "    key_id                  TEXT NOT NULL\n"
    "                                  REFERENCES kms_keys(key_id) ON DELETE RESTRICT,\n"
    f"    provider                TEXT NOT NULL\n"
    f"                                  CHECK (provider IN ({_KMS_PROVIDERS_SQL})),\n"
    "    wrapped_dek_b64         TEXT NOT NULL,\n"
    "    key_version             TEXT,\n"
    "    wrap_algorithm          TEXT NOT NULL DEFAULT '',\n"
    "    encryption_context_json TEXT NOT NULL DEFAULT '{}',\n"
    "    purpose                 TEXT NOT NULL DEFAULT 'tenant-secret',\n"
    "    schema_version          INTEGER NOT NULL DEFAULT 1,\n"
    "    created_at              REAL NOT NULL,\n"
    "    rotated_at              REAL,\n"
    "    revoked_at              REAL\n"
    ")"
)

_SQLITE_CREATE_DECRYPTION_AUDITS = (
    "CREATE TABLE IF NOT EXISTS decryption_audits (\n"
    "    audit_id      TEXT PRIMARY KEY,\n"
    "    tenant_id     TEXT NOT NULL\n"
    "                         REFERENCES tenants(id) ON DELETE CASCADE,\n"
    "    user_id       TEXT NOT NULL,\n"
    "    key_id        TEXT NOT NULL,\n"
    "    dek_id        TEXT,\n"
    "    request_id    TEXT NOT NULL,\n"
    "    purpose       TEXT NOT NULL DEFAULT '',\n"
    f"    provider      TEXT NOT NULL CHECK (provider IN ({_KMS_PROVIDERS_SQL})),\n"
    "    audit_log_id  INTEGER,\n"
    "    decrypted_at  REAL NOT NULL,\n"
    "    metadata_json TEXT NOT NULL DEFAULT '{}'\n"
    ")"
)

_SQLITE_CREATE_SPEND_THRESHOLDS = (
    "CREATE TABLE IF NOT EXISTS spend_thresholds (\n"
    "    tenant_id           TEXT PRIMARY KEY\n"
    "                              REFERENCES tenants(id) ON DELETE CASCADE,\n"
    "    token_rate_limit    INTEGER NOT NULL CHECK (token_rate_limit > 0),\n"
    "    window_seconds      REAL NOT NULL CHECK (window_seconds > 0),\n"
    "    throttle_seconds    REAL NOT NULL CHECK (throttle_seconds > 0),\n"
    "    enabled             INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),\n"
    "    alert_channels_json TEXT NOT NULL DEFAULT '[]',\n"
    "    created_at          REAL NOT NULL,\n"
    "    updated_at          REAL NOT NULL\n"
    ")"
)

_SQLITE_CREATE_KEK_ROTATIONS = (
    "CREATE TABLE IF NOT EXISTS kek_rotations (\n"
    "    rotation_id      TEXT PRIMARY KEY,\n"
    "    key_id           TEXT NOT NULL\n"
    "                           REFERENCES kms_keys(key_id) ON DELETE RESTRICT,\n"
    f"    provider         TEXT NOT NULL CHECK (provider IN ({_KMS_PROVIDERS_SQL})),\n"
    "    from_key_version TEXT NOT NULL,\n"
    "    to_key_version   TEXT NOT NULL,\n"
    f"    status           TEXT NOT NULL DEFAULT 'scheduled'\n"
    f"                           CHECK (status IN ({_KEK_ROTATION_STATUSES_SQL})),\n"
    "    scheduled_for    REAL,\n"
    "    started_at       REAL,\n"
    "    completed_at     REAL,\n"
    "    rotated_rows     INTEGER NOT NULL DEFAULT 0,\n"
    "    error            TEXT NOT NULL DEFAULT '',\n"
    "    metadata_json    TEXT NOT NULL DEFAULT '{}'\n"
    ")"
)


_TABLES_PG = (
    _PG_CREATE_KMS_KEYS,
    _PG_CREATE_TENANT_DEKS,
    _PG_CREATE_DECRYPTION_AUDITS,
    _PG_CREATE_SPEND_THRESHOLDS,
    _PG_CREATE_KEK_ROTATIONS,
)

_TABLES_SQLITE = (
    _SQLITE_CREATE_KMS_KEYS,
    _SQLITE_CREATE_TENANT_DEKS,
    _SQLITE_CREATE_DECRYPTION_AUDITS,
    _SQLITE_CREATE_SPEND_THRESHOLDS,
    _SQLITE_CREATE_KEK_ROTATIONS,
)

_INDEXES = _PG_INDEXES

_DROP_INDEXES = (
    "idx_kek_rotations_key",
    "idx_kek_rotations_status_schedule",
    "idx_decryption_audits_key_time",
    "idx_decryption_audits_request",
    "idx_decryption_audits_tenant_time",
    "idx_tenant_deks_key_version",
    "idx_tenant_deks_tenant_purpose",
    "idx_kms_keys_provider_status",
)

_DROP_TABLES = (
    "kek_rotations",
    "spend_thresholds",
    "decryption_audits",
    "tenant_deks",
    "kms_keys",
)


# -- upgrade / downgrade ----------------------------------------------------


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
