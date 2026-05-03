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
    # Reverse-of-upgrade: drop locked_until first (added second), then
    # failed_login_count (added first). Going through alembic's
    # op.drop_column keeps identifier quoting on the dialect's
    # IdentifierPreparer rather than f-string DDL — same SQLAlchemy-ops
    # track FX.1.10 / FX.1.11 pulled 0106 / 0007 onto. ALTER TABLE DROP
    # COLUMN works natively on PostgreSQL (since 7.3) and on SQLite
    # >= 3.35 (2021-03); the upgrade likewise relies on ALTER TABLE
    # ADD COLUMN, so any DB that ran upgrade() can run downgrade().
    op.drop_column("users", "locked_until")
    op.drop_column("users", "failed_login_count")
