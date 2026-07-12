"""OP-2622 U6-0 T9/T10 G0a — durable provenance snapshots (DORMANT).

Add the write-once ``provenance_snapshots`` store that later T9/T10
ActionGrant leaves bind to.  Only complete snapshots are admissible; the
database CHECK backs the repository's fail-closed write boundary.  The
globally unique snapshot id also has an explicit tenant-scoped UNIQUE key so
later grant tables can use a composite foreign key.

PostgreSQL is authoritative.  SQLite gets its deliberate subset from
``backend/db.py::_SCHEMA`` because the dev SQLite path does not run alembic.

Additive and dormant: no producer or consumer is wired by this revision.
Rollback drops the table and its dependent index.

Revision ID: 0262
Revises: 0261
Create Date: 2026-07-13
"""
from __future__ import annotations

from alembic import op


revision = "0262"
down_revision = "0261"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return

    conn.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS provenance_snapshots (
            snapshot_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL REFERENCES tenants(id),
            model_call_id TEXT NOT NULL,
            request_id TEXT NOT NULL DEFAULT '',
            records JSONB NOT NULL,
            omissions JSONB NOT NULL DEFAULT '[]'::jsonb,
            completeness TEXT NOT NULL
                CHECK (completeness = 'complete'),
            manifest_digest TEXT NOT NULL,
            schema_version TEXT NOT NULL DEFAULT 'v1',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_provenance_snapshots_tenant_snapshot
                UNIQUE (tenant_id, snapshot_id)
        )
        """
    )
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_provenance_snapshots_tenant_model_call "
        "ON provenance_snapshots (tenant_id, model_call_id)"
    )


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return
    conn.exec_driver_sql("DROP TABLE IF EXISTS provenance_snapshots")
