"""Q.1 UI follow-up (2026-04-24) — session_revocations log table.

Adds ``session_revocations``: a small append-only log that records
*why* a given session token was revoked (password change, MFA
enrolled / disabled, backup codes regenerated, webauthn register /
remove, admin role_change / account_disabled). The table lives
independently of ``sessions`` so that even after the session row
itself is evicted (``_get_session_impl`` deletes expired rows on
lookup), the next request from that peer device can still look up
the revocation reason and get a tailored 401 ("please re-login
because your password was changed on another device") instead of
the generic "authentication required" banner.

The lookup is keyed by ``token`` — the same token the evicted
session was addressed by. We keep the table small with a
retention window (``revoked_at > now - 7 days``) and a single
index. No FK to ``sessions`` because the referenced row is
deliberately deleted before the probe fires.

Revision ID: 0019
Revises: 0018
Create Date: 2026-04-24
"""
from __future__ import annotations

from alembic import op


revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    dialect = conn.dialect.name

    if dialect == "postgresql":
        conn.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS session_revocations (
                token       TEXT PRIMARY KEY,
                user_id     TEXT NOT NULL,
                reason      TEXT NOT NULL,
                trigger     TEXT NOT NULL DEFAULT '',
                revoked_at  DOUBLE PRECISION NOT NULL
            )
            """
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_session_revocations_revoked_at "
            "ON session_revocations(revoked_at)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_session_revocations_user "
            "ON session_revocations(user_id)"
        )
    else:
        conn.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS session_revocations (
                token       TEXT PRIMARY KEY,
                user_id     TEXT NOT NULL,
                reason      TEXT NOT NULL,
                trigger     TEXT NOT NULL DEFAULT '',
                revoked_at  REAL NOT NULL
            )
            """
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_session_revocations_revoked_at "
            "ON session_revocations(revoked_at)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_session_revocations_user "
            "ON session_revocations(user_id)"
        )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS session_revocations")
