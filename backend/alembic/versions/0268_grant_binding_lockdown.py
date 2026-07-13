"""OP-2638 U6-0 T9/T10 G4a-fix-B2 — lock down grant bindings.

Bind each action grant to its challenge on the same action instance, require
sink-idempotency grants to carry a non-empty key, and bind each resume job to
the action instance authorized by its grant.

PostgreSQL is authoritative. SQLite gets its deliberate subset from
``backend/db.py::_SCHEMA`` because the dev SQLite path does not run alembic.

Altering and dormant: no production grant writer is wired by this revision.
Rollback first swaps resume jobs off the grant composite key before dropping
that key and the remaining grant constraints.

Revision ID: 0268
Revises: 0267
Create Date: 2026-07-13
backwards-compat: safe
"""
from __future__ import annotations

from alembic import op


revision = "0268"
down_revision = "0267"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return

    conn.exec_driver_sql(
        "ALTER TABLE action_grants "
        "ADD CONSTRAINT uq_grants_tenant_grant_instance "
        "UNIQUE (tenant_id, grant_id, action_instance_id)"
    )
    conn.exec_driver_sql(
        "ALTER TABLE action_grants "
        "ADD CONSTRAINT fk_grants_challenge_instance "
        "FOREIGN KEY (tenant_id, challenge_id, action_instance_id) "
        "REFERENCES challenges (tenant_id, challenge_id, action_instance_id)"
    )
    conn.exec_driver_sql(
        "ALTER TABLE action_grants "
        "ADD CONSTRAINT ck_grants_sink_idempotency CHECK ("
        "recovery_mode <> 'sink_idempotency_key' OR ("
        "idempotency_key IS NOT NULL AND length(idempotency_key) > 0))"
    )
    conn.exec_driver_sql(
        "ALTER TABLE resume_jobs DROP CONSTRAINT fk_resume_jobs_grant"
    )
    conn.exec_driver_sql(
        "ALTER TABLE resume_jobs "
        "ADD CONSTRAINT fk_resume_jobs_grant_instance "
        "FOREIGN KEY (tenant_id, grant_id, action_instance_id) "
        "REFERENCES action_grants (tenant_id, grant_id, action_instance_id)"
    )


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return

    conn.exec_driver_sql(
        "ALTER TABLE resume_jobs "
        "DROP CONSTRAINT IF EXISTS fk_resume_jobs_grant_instance"
    )
    conn.exec_driver_sql(
        "ALTER TABLE resume_jobs "
        "ADD CONSTRAINT fk_resume_jobs_grant "
        "FOREIGN KEY (tenant_id, grant_id) "
        "REFERENCES action_grants (tenant_id, grant_id)"
    )
    conn.exec_driver_sql(
        "ALTER TABLE action_grants "
        "DROP CONSTRAINT IF EXISTS fk_grants_challenge_instance"
    )
    conn.exec_driver_sql(
        "ALTER TABLE action_grants "
        "DROP CONSTRAINT IF EXISTS ck_grants_sink_idempotency"
    )
    conn.exec_driver_sql(
        "ALTER TABLE action_grants "
        "DROP CONSTRAINT IF EXISTS uq_grants_tenant_grant_instance"
    )
