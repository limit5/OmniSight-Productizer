"""BP.H.3 -- notifications.is_red_card marker.

Adds a persistent bool marker so red-card notifications can be routed
to the existing L3 Jira + L4 PagerDuty legs without inventing a second
notification table or a new severity value.

Module-global / cross-worker state audit
----------------------------------------
No module-global state. The marker is stored on the notification row;
all workers derive the same routing decision by reading the same DB/SSE
payload value.

Read-after-write timing audit
-----------------------------
This migration adds one nullable-safe column with a default. It does
not parallelise a formerly serialised workflow or add a new immediate
read-after-write dependency.

Production readiness gate
-------------------------
* No new Python / OS package -- production image needs no dependency
  rebuild.
* No new table -- SQLite/PG migration-table drift guards are
  unaffected; the column is mirrored in ``backend.db`` for dev DBs.

Revision ID: 0191
Revises: 0190
Create Date: 2026-05-04
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0191"
down_revision = "0190"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    is_pg = conn.dialect.name == "postgresql"

    if is_pg:
        conn.exec_driver_sql(
            "ALTER TABLE notifications "
            "ADD COLUMN IF NOT EXISTS is_red_card BOOLEAN NOT NULL DEFAULT FALSE"
        )
        return

    with op.batch_alter_table("notifications") as batch_op:
        batch_op.add_column(sa.Column(
            "is_red_card", sa.Boolean(), nullable=False,
            server_default=sa.false(),
        ))


def downgrade() -> None:
    conn = op.get_bind()
    is_pg = conn.dialect.name == "postgresql"

    if is_pg:
        op.execute("ALTER TABLE notifications DROP COLUMN IF EXISTS is_red_card")
        return

    with op.batch_alter_table("notifications") as batch_op:
        batch_op.drop_column("is_red_card")
