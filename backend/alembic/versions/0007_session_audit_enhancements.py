"""S0 — Session & audit enhancements.

Adds:
  audit_log.session_id   TEXT + index — links each audit row to the
                         originating session (NULL for system/anonymous)
  sessions.metadata      TEXT (JSON) — per-session mode / config
  sessions.mfa_verified  INTEGER DEFAULT 0 — future MFA gate
  sessions.rotated_from  TEXT — token rotation chain pointer

Revision ID: 0007
Revises: 0006
Create Date: 2026-04-16
"""
from __future__ import annotations

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    for stmt in [
        "ALTER TABLE audit_log ADD COLUMN session_id TEXT",
        "CREATE INDEX IF NOT EXISTS idx_audit_log_session ON audit_log(session_id)",
        "ALTER TABLE sessions ADD COLUMN metadata TEXT NOT NULL DEFAULT '{}'",
        "ALTER TABLE sessions ADD COLUMN mfa_verified INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE sessions ADD COLUMN rotated_from TEXT",
    ]:
        bind.exec_driver_sql(stmt)


def downgrade() -> None:
    pass
