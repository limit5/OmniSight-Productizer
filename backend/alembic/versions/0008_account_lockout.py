"""K2 — Account lockout columns on users table.

Adds:
  users.failed_login_count  INTEGER DEFAULT 0 — consecutive failed logins
  users.locked_until        REAL (epoch) — NULL when not locked

Revision ID: 0008
Revises: 0007
Create Date: 2026-04-16
"""
from __future__ import annotations

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    for stmt in [
        "ALTER TABLE users ADD COLUMN failed_login_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN locked_until REAL",
    ]:
        bind.exec_driver_sql(stmt)


def downgrade() -> None:
    pass
