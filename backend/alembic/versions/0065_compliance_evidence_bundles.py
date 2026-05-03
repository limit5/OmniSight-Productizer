"""SC.11.1 -- ``compliance_evidence_bundles`` table.

Persistent queue/catalog for SOC 2 and ISO 27001 evidence bundle
generation.  This row deliberately lands only the schema; SC.11.2 and
SC.11.3 own the control mappings and evidence collection, while SC.11.4
owns zip export and signatures.

* ``id`` -- app-generated bundle id (``ceb-*`` convention expected by
  later SC.11 routes).  TEXT PK follows ``dsar_requests`` and avoids
  sequence-reset work during SQLite -> PG cutover.
* ``tenant_id`` -- tenant scope for the bundle.  FK cascades on tenant
  delete so tenant teardown does not leave compliance work rows that
  cannot be inspected in the tenant context.
* ``requested_by`` -- user that requested the bundle.  Nullable FK with
  ``ON DELETE SET NULL`` mirrors install-job style audit ownership:
  losing the user row should not delete compliance evidence metadata.
* ``standard`` -- one of ``soc2`` / ``iso27001``.  These are exactly
  the two mapping rows under SC.11.2 and SC.11.3.
* ``status`` -- coarse lifecycle for the later worker:
  ``pending`` / ``collecting`` / ``completed`` / ``failed`` /
  ``cancelled``.  State transitions remain application-owned.
* ``requested_at`` / ``completed_at`` -- epoch seconds.  ``completed_at``
  is nullable until a terminal outcome.
* ``control_mapping_json`` -- resolved control map for the requested
  standard; default empty object until SC.11.2 / SC.11.3 populate it.
* ``evidence_manifest_json`` -- collected log/policy pointers and
  per-control evidence metadata; default empty object until collection.
* ``artifact_uri`` -- storage URI for the eventual zip export; empty
  string until SC.11.4 writes an artifact.
* ``signature_json`` -- structured signature metadata for SC.11.4;
  default empty object until the export path signs the bundle.
* ``error`` -- short failure detail for operator triage; empty string
  on non-failed rows.
* ``version`` -- optimistic-lock counter, matching the J2/Q.7 convention
  used by other mutable workflow tables.

Indexes
-------
* ``idx_compliance_evidence_bundles_tenant_status`` supports tenant
  bundle listing and worker scans by lifecycle.
* ``idx_compliance_evidence_bundles_requested_by`` supports "show my
  requested bundles" UI reads without scanning the tenant's full bundle
  history.

Module-global / cross-worker state audit
----------------------------------------
Pure DDL migration -- no module-level singleton, no in-memory cache.
Every worker reads the same DDL state from PG / SQLite so the table is
visible atomically post-commit.  Answer #1 of SOP Step 1 -- every
worker reads the same DDL state from the same DB.

Read-after-write timing audit
-----------------------------
The CREATE TABLE happens inside the alembic upgrade transaction.
``scripts/deploy.sh`` closes the asyncpg pool before alembic upgrade
and reopens it after, so runtime workers never see a half-shaped
schema.  No concurrent writer exists during the migration window.

Production readiness gate
-------------------------
* No new Python / OS package -- production image needs no rebuild.
* New table added -- ``scripts/migrate_sqlite_to_pg.py::TABLES_IN_ORDER``
  is updated in this same row to include
  ``compliance_evidence_bundles`` (replays after ``tenants`` and
  ``users`` because of the FKs).  TEXT PK, so the table is NOT in
  ``TABLES_WITH_IDENTITY_ID``.  The drift guard
  ``test_migrator_schema_coverage`` and the SC.11.1 contract test
  enforce this.
* SQLite dev parity -- :mod:`backend.db`'s ``_SCHEMA`` constant
  receives the same CREATE TABLE / index shape so fresh dev SQLite DBs
  and the migrator drift guard see the new table before runtime code
  starts writing rows.
* Production status of THIS commit: **dev-only**.  Next gate:
  ``deployed-inactive`` once the alembic chain (0064 -> 0065) is run
  against prod PG.  ``deployed-active`` requires SC.11.2-SC.11.4 to
  start collecting evidence and writing export rows.

Revision ID: 0065
Revises: 0064
Create Date: 2026-05-03
"""
from __future__ import annotations

from alembic import op


revision = "0065"
down_revision = "0064"
branch_labels = None
depends_on = None


# -- Enum whitelists --------------------------------------------------------


_STANDARDS_SQL = "'iso27001','soc2'"
_STATUSES_SQL = "'cancelled','collecting','completed','failed','pending'"


# -- PG branch --------------------------------------------------------------


_PG_CREATE_TABLE = (
    "CREATE TABLE IF NOT EXISTS compliance_evidence_bundles (\n"
    "    id                     TEXT PRIMARY KEY,\n"
    "    tenant_id              TEXT NOT NULL\n"
    "                                  REFERENCES tenants(id) ON DELETE CASCADE,\n"
    "    requested_by           TEXT\n"
    "                                  REFERENCES users(id) ON DELETE SET NULL,\n"
    f"    standard               TEXT NOT NULL\n"
    f"                                  CHECK (standard IN ({_STANDARDS_SQL})),\n"
    f"    status                 TEXT NOT NULL DEFAULT 'pending'\n"
    f"                                  CHECK (status IN ({_STATUSES_SQL})),\n"
    "    requested_at           DOUBLE PRECISION NOT NULL,\n"
    "    completed_at           DOUBLE PRECISION,\n"
    "    control_mapping_json   JSONB NOT NULL DEFAULT '{}'::jsonb,\n"
    "    evidence_manifest_json JSONB NOT NULL DEFAULT '{}'::jsonb,\n"
    "    artifact_uri           TEXT NOT NULL DEFAULT '',\n"
    "    signature_json         JSONB NOT NULL DEFAULT '{}'::jsonb,\n"
    "    error                  TEXT NOT NULL DEFAULT '',\n"
    "    version                INTEGER NOT NULL DEFAULT 0\n"
    ")"
)

_PG_INDEX_TENANT_STATUS = (
    "CREATE INDEX IF NOT EXISTS idx_compliance_evidence_bundles_tenant_status "
    "ON compliance_evidence_bundles(tenant_id, status)"
)

_PG_INDEX_REQUESTED_BY = (
    "CREATE INDEX IF NOT EXISTS idx_compliance_evidence_bundles_requested_by "
    "ON compliance_evidence_bundles(requested_by)"
)


# -- SQLite branch ----------------------------------------------------------


_SQLITE_CREATE_TABLE = (
    "CREATE TABLE IF NOT EXISTS compliance_evidence_bundles (\n"
    "    id                     TEXT PRIMARY KEY,\n"
    "    tenant_id              TEXT NOT NULL\n"
    "                                  REFERENCES tenants(id) ON DELETE CASCADE,\n"
    "    requested_by           TEXT\n"
    "                                  REFERENCES users(id) ON DELETE SET NULL,\n"
    f"    standard               TEXT NOT NULL\n"
    f"                                  CHECK (standard IN ({_STANDARDS_SQL})),\n"
    f"    status                 TEXT NOT NULL DEFAULT 'pending'\n"
    f"                                  CHECK (status IN ({_STATUSES_SQL})),\n"
    "    requested_at           REAL NOT NULL,\n"
    "    completed_at           REAL,\n"
    "    control_mapping_json   TEXT NOT NULL DEFAULT '{}',\n"
    "    evidence_manifest_json TEXT NOT NULL DEFAULT '{}',\n"
    "    artifact_uri           TEXT NOT NULL DEFAULT '',\n"
    "    signature_json         TEXT NOT NULL DEFAULT '{}',\n"
    "    error                  TEXT NOT NULL DEFAULT '',\n"
    "    version                INTEGER NOT NULL DEFAULT 0\n"
    ")"
)

_SQLITE_INDEX_TENANT_STATUS = (
    "CREATE INDEX IF NOT EXISTS idx_compliance_evidence_bundles_tenant_status "
    "ON compliance_evidence_bundles(tenant_id, status)"
)

_SQLITE_INDEX_REQUESTED_BY = (
    "CREATE INDEX IF NOT EXISTS idx_compliance_evidence_bundles_requested_by "
    "ON compliance_evidence_bundles(requested_by)"
)


# -- upgrade / downgrade ----------------------------------------------------


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        conn.exec_driver_sql(_PG_CREATE_TABLE)
        conn.exec_driver_sql(_PG_INDEX_TENANT_STATUS)
        conn.exec_driver_sql(_PG_INDEX_REQUESTED_BY)
        return

    conn.exec_driver_sql(_SQLITE_CREATE_TABLE)
    conn.exec_driver_sql(_SQLITE_INDEX_TENANT_STATUS)
    conn.exec_driver_sql(_SQLITE_INDEX_REQUESTED_BY)


def downgrade() -> None:
    conn = op.get_bind()
    for idx in (
        "idx_compliance_evidence_bundles_requested_by",
        "idx_compliance_evidence_bundles_tenant_status",
    ):
        conn.exec_driver_sql(f"DROP INDEX IF EXISTS {idx}")
    op.execute("DROP TABLE IF EXISTS compliance_evidence_bundles")
