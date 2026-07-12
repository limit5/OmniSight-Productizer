"""OP-2636 U6-0 T9/T10 G4a-fix-A — immutable prepared actions.

Add the trusted recovery policy snapshot consumed by later confirmation work,
defaulting dormant and legacy callers to ``non_replayable``. PostgreSQL
UPDATE and DELETE triggers make the prepared row database-immutable so later
transactions can safely copy its locked identity and recovery policy.

PostgreSQL is authoritative. SQLite gets its deliberate subset from
``backend/db.py::_SCHEMA`` because the dev SQLite path does not run alembic.

Additive and dormant: no producer or consumer is wired by this revision.
Rollback removes the triggers and function before dropping the constraint and
column.

Revision ID: 0266
Revises: 0265
Create Date: 2026-07-13
backwards-compat: safe
"""
from __future__ import annotations

from alembic import op


revision = "0266"
down_revision = "0265"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return

    conn.exec_driver_sql(
        "ALTER TABLE prepared_actions ADD COLUMN recovery_mode "
        "TEXT NOT NULL DEFAULT 'non_replayable'"
    )
    conn.exec_driver_sql(
        "ALTER TABLE prepared_actions "
        "ADD CONSTRAINT ck_prepared_actions_recovery_mode "
        "CHECK (recovery_mode IN "
        "('non_replayable', 'sink_idempotency_key', 'read_after_write'))"
    )
    conn.exec_driver_sql(
        """
        CREATE OR REPLACE FUNCTION prepared_actions_block_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION
                'PreparedActionImmutable: prepared_actions is write-once';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    conn.exec_driver_sql(
        """
        DROP TRIGGER IF EXISTS trg_prepared_actions_no_update
            ON prepared_actions;
        CREATE TRIGGER trg_prepared_actions_no_update
            BEFORE UPDATE ON prepared_actions
            FOR EACH ROW EXECUTE FUNCTION prepared_actions_block_mutation()
        """
    )
    conn.exec_driver_sql(
        """
        DROP TRIGGER IF EXISTS trg_prepared_actions_no_delete
            ON prepared_actions;
        CREATE TRIGGER trg_prepared_actions_no_delete
            BEFORE DELETE ON prepared_actions
            FOR EACH ROW EXECUTE FUNCTION prepared_actions_block_mutation()
        """
    )


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return
    conn.exec_driver_sql(
        "DROP TRIGGER IF EXISTS trg_prepared_actions_no_delete "
        "ON prepared_actions"
    )
    conn.exec_driver_sql(
        "DROP TRIGGER IF EXISTS trg_prepared_actions_no_update "
        "ON prepared_actions"
    )
    conn.exec_driver_sql(
        "DROP FUNCTION IF EXISTS prepared_actions_block_mutation()"
    )
    conn.exec_driver_sql(
        "ALTER TABLE prepared_actions "
        "DROP CONSTRAINT IF EXISTS ck_prepared_actions_recovery_mode"
    )
    conn.exec_driver_sql(
        "ALTER TABLE prepared_actions DROP COLUMN IF EXISTS recovery_mode"
    )
