"""KS.3.12 -- Tier 3 BYOG proxy persistence tables.

Schema-only landing for KS Phase 3. Runtime rows KS.3.1-KS.3.11 already
define the customer-side proxy image, mTLS / signed nonce auth,
heartbeat, zero-trust forwarding, metadata-only SaaS audit, settings UI,
and fail-fast behavior. This migration gives those helpers durable tables
without changing the call paths yet.

Tables
------
* ``proxy_registrations`` -- one SaaS-side registration per BYOG proxy.
* ``proxy_health_checks`` -- durable heartbeat / reachability ledger keyed
  to a registered proxy. Stores metadata only, never prompt/response body.
* ``proxy_mtls_certs`` -- certificate inventory and pinning metadata for
  proxy mTLS. Stores fingerprints / refs only, not private key material.

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
  is updated in this same row. All three tables have TEXT primary keys,
  so none are in ``TABLES_WITH_IDENTITY_ID``. The drift guard
  ``test_migrator_schema_coverage`` and the KS.3.12 contract test
  enforce this.
* SQLite dev parity -- :mod:`backend.db`'s ``_SCHEMA`` constant receives
  the same table/index shape with JSONB -> TEXT, BOOLEAN -> INTEGER, and
  DOUBLE PRECISION -> REAL dialect shifts.
* Production status of THIS commit: **dev-only**. Next gate:
  ``deployed-inactive`` once the alembic chain (0107 -> 0108) is run
  against prod PG. ``deployed-active`` requires KS.3.13-KS.3.14 to gate
  BYOG selection and exercise the mTLS / nonce / latency matrix against
  these tables.

Revision ID: 0108
Revises: 0107
Create Date: 2026-05-04
"""
from __future__ import annotations

from alembic import op


revision = "0108"
down_revision = "0107"
branch_labels = None
depends_on = None


_PROXY_REGISTRATION_STATUSES_SQL = "'active','disabled','pending','revoked'"
_PROXY_HEALTH_STATUSES_SQL = "'mtls_failed','ok','stale','unreachable'"
_PROXY_CERT_ROLES_SQL = "'ca','client','server'"
_PROXY_CERT_STATUSES_SQL = "'active','expired','revoked','rotating'"


# -- PG branch --------------------------------------------------------------


_PG_CREATE_PROXY_REGISTRATIONS = (
    "CREATE TABLE IF NOT EXISTS proxy_registrations (\n"
    "    proxy_id         TEXT PRIMARY KEY,\n"
    "    tenant_id        TEXT NOT NULL\n"
    "                           REFERENCES tenants(id) ON DELETE CASCADE,\n"
    "    display_name     TEXT NOT NULL DEFAULT '',\n"
    "    proxy_url        TEXT NOT NULL,\n"
    f"    status           TEXT NOT NULL DEFAULT 'pending'\n"
    f"                           CHECK (status IN ({_PROXY_REGISTRATION_STATUSES_SQL})),\n"
    "    service          TEXT NOT NULL DEFAULT 'omnisight-proxy',\n"
    "    provider_count   INTEGER NOT NULL DEFAULT 0 CHECK (provider_count >= 0),\n"
    "    heartbeat_interval_seconds INTEGER NOT NULL DEFAULT 30\n"
    "                           CHECK (heartbeat_interval_seconds > 0),\n"
    "    stale_threshold_seconds INTEGER NOT NULL DEFAULT 60\n"
    "                           CHECK (stale_threshold_seconds > 0),\n"
    "    nonce_key_ref    TEXT NOT NULL DEFAULT '',\n"
    "    client_cert_fingerprint_sha256 TEXT NOT NULL DEFAULT '',\n"
    "    created_at       DOUBLE PRECISION NOT NULL,\n"
    "    updated_at       DOUBLE PRECISION NOT NULL,\n"
    "    disabled_at      DOUBLE PRECISION,\n"
    "    metadata_json    JSONB NOT NULL DEFAULT '{}'::jsonb\n"
    ")"
)

_PG_CREATE_PROXY_HEALTH_CHECKS = (
    "CREATE TABLE IF NOT EXISTS proxy_health_checks (\n"
    "    check_id        TEXT PRIMARY KEY,\n"
    "    proxy_id        TEXT NOT NULL\n"
    "                          REFERENCES proxy_registrations(proxy_id) "
    "ON DELETE CASCADE,\n"
    "    tenant_id       TEXT NOT NULL\n"
    "                          REFERENCES tenants(id) ON DELETE CASCADE,\n"
    f"    status          TEXT NOT NULL CHECK (status IN ({_PROXY_HEALTH_STATUSES_SQL})),\n"
    "    service         TEXT NOT NULL DEFAULT 'omnisight-proxy',\n"
    "    provider_count  INTEGER NOT NULL DEFAULT 0 CHECK (provider_count >= 0),\n"
    "    heartbeat_interval_seconds INTEGER NOT NULL DEFAULT 30\n"
    "                          CHECK (heartbeat_interval_seconds > 0),\n"
    "    latency_ms      DOUBLE PRECISION,\n"
    "    error           TEXT NOT NULL DEFAULT '',\n"
    "    checked_at      DOUBLE PRECISION NOT NULL,\n"
    "    detail_json     JSONB NOT NULL DEFAULT '{}'::jsonb\n"
    ")"
)

_PG_CREATE_PROXY_MTLS_CERTS = (
    "CREATE TABLE IF NOT EXISTS proxy_mtls_certs (\n"
    "    cert_id        TEXT PRIMARY KEY,\n"
    "    proxy_id       TEXT NOT NULL\n"
    "                         REFERENCES proxy_registrations(proxy_id) "
    "ON DELETE CASCADE,\n"
    "    tenant_id      TEXT NOT NULL\n"
    "                         REFERENCES tenants(id) ON DELETE CASCADE,\n"
    f"    cert_role      TEXT NOT NULL CHECK (cert_role IN ({_PROXY_CERT_ROLES_SQL})),\n"
    "    fingerprint_sha256 TEXT NOT NULL,\n"
    "    subject        TEXT NOT NULL DEFAULT '',\n"
    "    issuer         TEXT NOT NULL DEFAULT '',\n"
    "    serial_number  TEXT NOT NULL DEFAULT '',\n"
    "    not_before     DOUBLE PRECISION,\n"
    "    not_after      DOUBLE PRECISION,\n"
    f"    status         TEXT NOT NULL DEFAULT 'active'\n"
    f"                         CHECK (status IN ({_PROXY_CERT_STATUSES_SQL})),\n"
    "    pinned         BOOLEAN NOT NULL DEFAULT FALSE,\n"
    "    material_ref   TEXT NOT NULL DEFAULT '',\n"
    "    created_at     DOUBLE PRECISION NOT NULL,\n"
    "    rotated_at     DOUBLE PRECISION,\n"
    "    revoked_at     DOUBLE PRECISION,\n"
    "    metadata_json  JSONB NOT NULL DEFAULT '{}'::jsonb\n"
    ")"
)

_PG_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_proxy_registrations_tenant_status "
    "ON proxy_registrations(tenant_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_proxy_registrations_url "
    "ON proxy_registrations(proxy_url)",
    "CREATE INDEX IF NOT EXISTS idx_proxy_health_checks_proxy_time "
    "ON proxy_health_checks(proxy_id, checked_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_proxy_health_checks_tenant_status "
    "ON proxy_health_checks(tenant_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_proxy_mtls_certs_proxy_status "
    "ON proxy_mtls_certs(proxy_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_proxy_mtls_certs_fingerprint "
    "ON proxy_mtls_certs(fingerprint_sha256)",
)


# -- SQLite branch ----------------------------------------------------------


_SQLITE_CREATE_PROXY_REGISTRATIONS = (
    "CREATE TABLE IF NOT EXISTS proxy_registrations (\n"
    "    proxy_id         TEXT PRIMARY KEY,\n"
    "    tenant_id        TEXT NOT NULL\n"
    "                           REFERENCES tenants(id) ON DELETE CASCADE,\n"
    "    display_name     TEXT NOT NULL DEFAULT '',\n"
    "    proxy_url        TEXT NOT NULL,\n"
    f"    status           TEXT NOT NULL DEFAULT 'pending'\n"
    f"                           CHECK (status IN ({_PROXY_REGISTRATION_STATUSES_SQL})),\n"
    "    service          TEXT NOT NULL DEFAULT 'omnisight-proxy',\n"
    "    provider_count   INTEGER NOT NULL DEFAULT 0 CHECK (provider_count >= 0),\n"
    "    heartbeat_interval_seconds INTEGER NOT NULL DEFAULT 30\n"
    "                           CHECK (heartbeat_interval_seconds > 0),\n"
    "    stale_threshold_seconds INTEGER NOT NULL DEFAULT 60\n"
    "                           CHECK (stale_threshold_seconds > 0),\n"
    "    nonce_key_ref    TEXT NOT NULL DEFAULT '',\n"
    "    client_cert_fingerprint_sha256 TEXT NOT NULL DEFAULT '',\n"
    "    created_at       REAL NOT NULL,\n"
    "    updated_at       REAL NOT NULL,\n"
    "    disabled_at      REAL,\n"
    "    metadata_json    TEXT NOT NULL DEFAULT '{}'\n"
    ")"
)

_SQLITE_CREATE_PROXY_HEALTH_CHECKS = (
    "CREATE TABLE IF NOT EXISTS proxy_health_checks (\n"
    "    check_id        TEXT PRIMARY KEY,\n"
    "    proxy_id        TEXT NOT NULL\n"
    "                          REFERENCES proxy_registrations(proxy_id) "
    "ON DELETE CASCADE,\n"
    "    tenant_id       TEXT NOT NULL\n"
    "                          REFERENCES tenants(id) ON DELETE CASCADE,\n"
    f"    status          TEXT NOT NULL CHECK (status IN ({_PROXY_HEALTH_STATUSES_SQL})),\n"
    "    service         TEXT NOT NULL DEFAULT 'omnisight-proxy',\n"
    "    provider_count  INTEGER NOT NULL DEFAULT 0 CHECK (provider_count >= 0),\n"
    "    heartbeat_interval_seconds INTEGER NOT NULL DEFAULT 30\n"
    "                          CHECK (heartbeat_interval_seconds > 0),\n"
    "    latency_ms      REAL,\n"
    "    error           TEXT NOT NULL DEFAULT '',\n"
    "    checked_at      REAL NOT NULL,\n"
    "    detail_json     TEXT NOT NULL DEFAULT '{}'\n"
    ")"
)

_SQLITE_CREATE_PROXY_MTLS_CERTS = (
    "CREATE TABLE IF NOT EXISTS proxy_mtls_certs (\n"
    "    cert_id        TEXT PRIMARY KEY,\n"
    "    proxy_id       TEXT NOT NULL\n"
    "                         REFERENCES proxy_registrations(proxy_id) "
    "ON DELETE CASCADE,\n"
    "    tenant_id      TEXT NOT NULL\n"
    "                         REFERENCES tenants(id) ON DELETE CASCADE,\n"
    f"    cert_role      TEXT NOT NULL CHECK (cert_role IN ({_PROXY_CERT_ROLES_SQL})),\n"
    "    fingerprint_sha256 TEXT NOT NULL,\n"
    "    subject        TEXT NOT NULL DEFAULT '',\n"
    "    issuer         TEXT NOT NULL DEFAULT '',\n"
    "    serial_number  TEXT NOT NULL DEFAULT '',\n"
    "    not_before     REAL,\n"
    "    not_after      REAL,\n"
    f"    status         TEXT NOT NULL DEFAULT 'active'\n"
    f"                         CHECK (status IN ({_PROXY_CERT_STATUSES_SQL})),\n"
    "    pinned         INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0, 1)),\n"
    "    material_ref   TEXT NOT NULL DEFAULT '',\n"
    "    created_at     REAL NOT NULL,\n"
    "    rotated_at     REAL,\n"
    "    revoked_at     REAL,\n"
    "    metadata_json  TEXT NOT NULL DEFAULT '{}'\n"
    ")"
)


_TABLES_PG = (
    _PG_CREATE_PROXY_REGISTRATIONS,
    _PG_CREATE_PROXY_HEALTH_CHECKS,
    _PG_CREATE_PROXY_MTLS_CERTS,
)

_TABLES_SQLITE = (
    _SQLITE_CREATE_PROXY_REGISTRATIONS,
    _SQLITE_CREATE_PROXY_HEALTH_CHECKS,
    _SQLITE_CREATE_PROXY_MTLS_CERTS,
)

_INDEXES = _PG_INDEXES

_DROP_INDEXES = (
    "idx_proxy_mtls_certs_fingerprint",
    "idx_proxy_mtls_certs_proxy_status",
    "idx_proxy_health_checks_tenant_status",
    "idx_proxy_health_checks_proxy_time",
    "idx_proxy_registrations_url",
    "idx_proxy_registrations_tenant_status",
)

_DROP_TABLES = (
    "proxy_mtls_certs",
    "proxy_health_checks",
    "proxy_registrations",
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
