"""OP-2637 U6-0 T9/T10 G4a-fix-B1 — bind challenge identity.

Add durable confirmer authentication evidence and database constraints for
decided challenges.  Challenge writers now copy the locked operation identity
from immutable prepared actions in the store helper rather than trusting
caller-supplied identity fields.

PostgreSQL is authoritative. SQLite gets its deliberate subset from
``backend/db.py::_SCHEMA`` because the dev SQLite path does not run alembic.

Additive and dormant: no production challenge writer is wired by this
revision. Rollback removes the decision constraint and composite key before
dropping the evidence column.

Revision ID: 0267
Revises: 0266
Create Date: 2026-07-13
backwards-compat: safe
"""
from __future__ import annotations

from alembic import op


revision = "0267"
down_revision = "0266"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return

    conn.exec_driver_sql(
        "ALTER TABLE challenges ADD COLUMN confirmer_auth_event_id TEXT"
    )
    conn.exec_driver_sql(
        "ALTER TABLE challenges "
        "ADD CONSTRAINT uq_challenges_tenant_challenge_instance "
        "UNIQUE (tenant_id, challenge_id, action_instance_id)"
    )
    conn.exec_driver_sql(
        "ALTER TABLE challenges "
        "ADD CONSTRAINT ck_challenges_decided_has_confirmer CHECK ("
        "state NOT IN ('confirmed', 'rejected') OR ("
        "confirmer_actor IS NOT NULL "
        "AND confirmer_principal_type IS NOT NULL "
        "AND confirmed_at IS NOT NULL "
        "AND confirmer_auth_event_id IS NOT NULL "
        "AND confirm_reason IS NOT NULL "
        "AND length(confirm_reason) > 0))"
    )


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return
    conn.exec_driver_sql(
        "ALTER TABLE challenges "
        "DROP CONSTRAINT IF EXISTS ck_challenges_decided_has_confirmer"
    )
    conn.exec_driver_sql(
        "ALTER TABLE challenges "
        "DROP CONSTRAINT IF EXISTS uq_challenges_tenant_challenge_instance"
    )
    conn.exec_driver_sql(
        "ALTER TABLE challenges DROP COLUMN IF EXISTS confirmer_auth_event_id"
    )
