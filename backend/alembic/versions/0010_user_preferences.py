"""J4 — User preferences table for server-side wizard/tour state.

Stores per-user preferences that must survive device switches on
shared computers. The key/value design allows arbitrary preferences
without schema migrations for each new flag.

Revision ID: 0010
Revises: 0009
Create Date: 2026-04-16
"""
from __future__ import annotations

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS user_preferences (
            user_id   TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            pref_key  TEXT NOT NULL,
            value     TEXT NOT NULL DEFAULT '',
            updated_at REAL NOT NULL DEFAULT (strftime('%s', 'now')),
            PRIMARY KEY (user_id, pref_key)
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_prefs_user "
        "ON user_preferences(user_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS user_preferences")
