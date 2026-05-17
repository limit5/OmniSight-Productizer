"""OP-244: api_keys expires_at for bearer TTL enforcement.

Revision ID: 0238
Revises: m_2026_05_16_3head
Create Date: 2026-05-16
backwards-compat: safe
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers
revision = "0238"
down_revision = "m_2026_05_16_3head"
branch_labels = None
depends_on = None


def _has_column(column_name: str) -> bool:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        row = bind.exec_driver_sql(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_name = 'api_keys'
              AND column_name = %s
            """,
            (column_name,),
        ).fetchone()
        return row is not None
    rows = bind.exec_driver_sql("PRAGMA table_info(api_keys)").fetchall()
    return column_name in {row[1] for row in rows}


def upgrade() -> None:
    if not _has_column("expires_at"):
        op.add_column("api_keys", sa.Column("expires_at", sa.Float(), nullable=True))


def downgrade() -> None:
    if _has_column("expires_at"):
        op.drop_column("api_keys", "expires_at")
