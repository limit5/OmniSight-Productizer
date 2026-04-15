"""K6: Per-key bearer tokens (api_keys table).

Revision ID: 0011
Revises: 0010
"""

revision = "0011"
down_revision = "0010"

from alembic import op
import sqlalchemy as sa


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("key_hash", sa.Text(), nullable=False),
        sa.Column("key_prefix", sa.Text(), nullable=False, server_default=""),
        sa.Column("scopes", sa.Text(), nullable=False, server_default='["*"]'),
        sa.Column("created_by", sa.Text(), nullable=False, server_default=""),
        sa.Column("last_used_ip", sa.Text(), nullable=True),
        sa.Column("last_used_at", sa.Float(), nullable=True),
        sa.Column("enabled", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.Text(), nullable=False,
                  server_default=sa.text("(datetime('now'))")),
    )
    op.create_index("idx_api_keys_enabled", "api_keys", ["enabled"])
    op.create_index("idx_api_keys_prefix", "api_keys", ["key_prefix"])


def downgrade() -> None:
    op.drop_index("idx_api_keys_prefix")
    op.drop_index("idx_api_keys_enabled")
    op.drop_table("api_keys")
