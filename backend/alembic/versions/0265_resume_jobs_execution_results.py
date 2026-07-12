"""OP-2635 U6-0 T9/T10 G4a-2 — resume jobs and results (DORMANT).

Add the durable ``resume_jobs`` queue and write-once ``execution_results``
store that later confirmation and ExecutionService leaves will enqueue,
lease, and consume. Both rows bind to an action grant through a tenant-scoped
composite foreign key.

PostgreSQL is authoritative. SQLite gets its deliberate subset from
``backend/db.py::_SCHEMA`` because the dev SQLite path does not run alembic.

Additive and dormant: no enqueue, lease, execution, or result transition is
wired by this revision. Rollback drops the two independent child tables.

Revision ID: 0265
Revises: 0264
Create Date: 2026-07-13
backwards-compat: safe
"""
from __future__ import annotations

from alembic import op


revision = "0265"
down_revision = "0264"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return

    conn.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS resume_jobs (
            resume_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL REFERENCES tenants(id),
            grant_id TEXT NOT NULL,
            action_instance_id TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'queued'
                CHECK (state IN ('queued', 'claimed', 'done', 'manual', 'failed')),
            lease_owner TEXT,
            lease_expires_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_resume_jobs_tenant_grant
                UNIQUE (tenant_id, grant_id),
            CONSTRAINT uq_resume_jobs_tenant_resume
                UNIQUE (tenant_id, resume_id),
            CONSTRAINT fk_resume_jobs_grant
                FOREIGN KEY (tenant_id, grant_id)
                REFERENCES action_grants (tenant_id, grant_id)
        )
        """
    )
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_resume_jobs_tenant_state "
        "ON resume_jobs (tenant_id, state)"
    )
    conn.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS execution_results (
            grant_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL REFERENCES tenants(id),
            result JSONB NOT NULL,
            ambiguous BOOLEAN NOT NULL DEFAULT FALSE,
            completed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_execution_results_tenant_grant
                UNIQUE (tenant_id, grant_id),
            CONSTRAINT fk_execution_results_grant
                FOREIGN KEY (tenant_id, grant_id)
                REFERENCES action_grants (tenant_id, grant_id)
        )
        """
    )


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return
    conn.exec_driver_sql("DROP TABLE IF EXISTS execution_results")
    conn.exec_driver_sql("DROP INDEX IF EXISTS idx_resume_jobs_tenant_state")
    conn.exec_driver_sql("DROP TABLE IF EXISTS resume_jobs")
