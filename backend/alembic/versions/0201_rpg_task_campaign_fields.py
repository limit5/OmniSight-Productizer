"""RPG.W20.1 -- task-level RPG campaign grouping fields.

Adds nullable campaign metadata to ``tasks`` so an operator can group
multiple tasks under one narrative operation, for example
``Operation: Phase 2 Migration``.  The campaign is deliberately stored as
task metadata in this row: W20.1 only needs a definable multi-task campaign
label, while later W20 rows can add richer campaign lifecycle state if the
operator workflow proves it needs a first-class table.

Module-global / cross-worker state audit
----------------------------------------
Pure DDL migration -- no module-level singleton, no in-memory cache.
Every worker reads the same ``tasks`` columns from PG / SQLite after the
upgrade commits.

Read-after-write timing audit
-----------------------------
No background writer is introduced.  Existing task create/update paths write
the new nullable fields in the same statement as the task row, so readers see
campaign metadata atomically with the rest of the task.

Production readiness gate
-------------------------
No new Python / OS package.  No new tables, so SQLite -> PG migrator table
ordering is unchanged; the existing task row replay carries the added columns.

Revision ID: 0201
Revises: 0200
Create Date: 2026-05-08
"""
from __future__ import annotations

from alembic import op


revision = "0201"
down_revision = "0200"
branch_labels = None
depends_on = None


_COLUMNS = (
    "rpg_campaign_id",
    "rpg_campaign_title",
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for column in _COLUMNS:
            bind.exec_driver_sql(
                f"ALTER TABLE tasks ADD COLUMN IF NOT EXISTS {column} TEXT"
            )
        return

    cols = {
        row[1]
        for row in bind.exec_driver_sql("PRAGMA table_info(tasks)").fetchall()
    }
    for column in _COLUMNS:
        if column not in cols:
            bind.exec_driver_sql(f"ALTER TABLE tasks ADD COLUMN {column} TEXT")


def downgrade() -> None:
    # alembic-allow-noop-downgrade: SQLite cannot drop columns safely across
    # supported versions, and removing campaign metadata from live task rows
    # would be data-lossy. Roll back with a hand-authored table rebuild if
    # OP-211 is reverted.
    pass
