"""OP-249: guild_id on workflow/debug/audit trace tables.

BP.B.1 adds a nullable ``guild_id`` dimension to the durable execution
and operator-forensics tables that currently carry tenant scope but no
agent guild scope. The column is deliberately nullable so existing rows
and callers remain valid until writers are updated in a later BP task.

Revision ID: 0241
Revises: 0240
Create Date: 2026-05-17
backwards-compat: safe
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0241"
down_revision = "0240"
branch_labels = None
depends_on = None


_TABLES_NEEDING_GUILD_ID: tuple[str, ...] = (
    "workflow_runs",
    "debug_findings",
    "audit_log",
)


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        row = bind.exec_driver_sql(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_name = %s
              AND column_name = %s
            """,
            (table_name, column_name),
        ).fetchone()
        return row is not None
    rows = bind.exec_driver_sql(f"PRAGMA table_info({table_name})").fetchall()
    return column_name in {row[1] for row in rows}


def upgrade() -> None:
    for table_name in _TABLES_NEEDING_GUILD_ID:
        if not _has_column(table_name, "guild_id"):
            op.add_column(table_name, sa.Column("guild_id", sa.Text(), nullable=True))


def downgrade() -> None:
    for table_name in reversed(_TABLES_NEEDING_GUILD_ID):
        if _has_column(table_name, "guild_id"):
            op.drop_column(table_name, "guild_id")
