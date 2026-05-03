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
    # Reverse-of-upgrade: drop the single column added by upgrade().
    # Going through alembic's op.drop_column keeps identifier quoting
    # on the dialect's IdentifierPreparer rather than f-string DDL —
    # same SQLAlchemy-ops track FX.1.10 / FX.1.11 / FX.1.12 pulled
    # 0106 / 0007 / 0008 onto. ALTER TABLE DROP COLUMN works natively
    # on PostgreSQL (since 7.3) and on SQLite >= 3.35 (2021-03); the
    # upgrade likewise relies on ALTER TABLE ADD COLUMN, so any DB
    # that ran upgrade() can run downgrade().
    #
    # Caveat — losing workflow_runs.version reopens the last-write-wins
    # race on live rows and breaks in-flight clients still sending
    # ``If-Match``. This downgrade exists for staging cutover abort
    # rollback (where the schema is being torn back to pre-J2 wholesale)
    # — production forward-only deployment never triggers it. Operators
    # invoking ``alembic downgrade 0008`` against a live workflow_runs
    # table should drain in-flight optimistic-lock writers first.
    op.drop_column("workflow_runs", "version")
