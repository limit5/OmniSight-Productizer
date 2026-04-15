"""J2 — Optimistic locking version column on workflow_runs.

Adds:
  workflow_runs.version  INTEGER DEFAULT 0 — incremented on every
  state-changing operation; clients pass If-Match: <version> to
  guard against concurrent modifications.

Revision ID: 0009
Revises: 0008
Create Date: 2026-04-16
"""
from __future__ import annotations

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(
        "ALTER TABLE workflow_runs ADD COLUMN version INTEGER NOT NULL DEFAULT 0"
    )


def downgrade() -> None:
    pass
