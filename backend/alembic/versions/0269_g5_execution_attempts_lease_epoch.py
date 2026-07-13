"""OP-2641 U6-0 T9/T10 G5a-2 — execution attempt substrate (DORMANT).

Widen action-grant terminal states, add a fencing epoch to resume leases, and
record execution attempts separately from reconciled execution results. The
separate attempt log permits multiple observations per grant without consuming
the write-once execution-result key.

PostgreSQL is authoritative. SQLite gets its deliberate subset from
``backend/db.py::_SCHEMA`` because the dev SQLite path does not run alembic.

Altering and dormant: no producer, consumer, lease worker, or recovery hook is
wired by this revision. Rollback drops the attempt log and fencing column, then
restores the original four-state grant check.

Revision ID: 0269
Revises: 0268
Create Date: 2026-07-13
backwards-compat: safe
"""
from __future__ import annotations

from alembic import op


revision = "0269"
down_revision = "0268"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return

    conn.exec_driver_sql(
        "ALTER TABLE action_grants "
        "DROP CONSTRAINT IF EXISTS action_grants_state_check"
    )
    conn.exec_driver_sql(
        "ALTER TABLE action_grants "
        "ADD CONSTRAINT ck_grants_state CHECK (state IN ("
        "'pending', 'executing', 'consumed', 'expired', 'failed', 'manual'))"
    )
    conn.exec_driver_sql(
        "ALTER TABLE resume_jobs "
        "ADD COLUMN lease_epoch BIGINT NOT NULL DEFAULT 0"
    )
    conn.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS execution_attempts (
            attempt_id TEXT PRIMARY KEY,
            grant_id TEXT NOT NULL,
            tenant_id TEXT NOT NULL REFERENCES tenants(id),
            outcome TEXT NOT NULL CHECK (outcome IN (
                'applied', 'definitely_not_applied', 'unknown'
            )),
            evidence TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            lease_owner TEXT NOT NULL DEFAULT '',
            lease_epoch BIGINT NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_execution_attempts_tenant_attempt
                UNIQUE (tenant_id, attempt_id),
            CONSTRAINT fk_execution_attempts_grant
                FOREIGN KEY (tenant_id, grant_id)
                REFERENCES action_grants (tenant_id, grant_id)
        )
        """
    )
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_execution_attempts_tenant_grant "
        "ON execution_attempts (tenant_id, grant_id)"
    )


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return

    conn.exec_driver_sql(
        "DROP INDEX IF EXISTS idx_execution_attempts_tenant_grant"
    )
    conn.exec_driver_sql("DROP TABLE IF EXISTS execution_attempts")
    conn.exec_driver_sql(
        "ALTER TABLE resume_jobs DROP COLUMN IF EXISTS lease_epoch"
    )
    conn.exec_driver_sql(
        "ALTER TABLE action_grants DROP CONSTRAINT IF EXISTS ck_grants_state"
    )
    conn.exec_driver_sql(
        "ALTER TABLE action_grants ADD CHECK (state IN ("
        "'pending', 'executing', 'consumed', 'expired'))"
    )
