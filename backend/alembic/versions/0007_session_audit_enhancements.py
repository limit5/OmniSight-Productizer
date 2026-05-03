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
    # Reverse-of-upgrade order: index first, then columns from sessions
    # (rotated_from / mfa_verified / metadata in reverse-of-add order),
    # finally audit_log.session_id. Going through alembic schema ops
    # (op.drop_index / op.drop_column) keeps identifier quoting on the
    # dialect's IdentifierPreparer rather than f-string DDL — same
    # SQLAlchemy-ops track FX.1.10 pulled 0106 onto. ALTER TABLE DROP
    # COLUMN works natively on PostgreSQL and on SQLite >= 3.35; the
    # upgrade likewise relies on ALTER TABLE ADD COLUMN, so any DB that
    # ran upgrade() can run downgrade().
    op.drop_index(
        "idx_audit_log_session",
        table_name="audit_log",
        if_exists=True,
    )
    op.drop_column("sessions", "rotated_from")
    op.drop_column("sessions", "mfa_verified")
    op.drop_column("sessions", "metadata")
    op.drop_column("audit_log", "session_id")
