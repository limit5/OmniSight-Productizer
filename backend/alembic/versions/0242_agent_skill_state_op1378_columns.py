"""OP-1378 -- align ``agent_skill_state`` columns with RPG.W12.1.

The original W12 bundle created ``agent_skill_state`` with ``xp`` and
``last_active_at``. The OP-1378 row names the durable stat-sheet columns
as ``skill_xp`` and ``last_used_at`` and includes a materialized
``mastery_effects`` array for cheap Character Card reads. This migration
adds those columns, backfills them from the existing row state, and keeps
the earlier columns in place for older readers during rollout.

Revision ID: 0242
Revises: 0241
Create Date: 2026-05-17
backwards-compat: safe
"""
from __future__ import annotations

from alembic import op


revision = "0242"
down_revision = "0241"
branch_labels = None
depends_on = None


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
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        if not _has_column("agent_skill_state", "skill_xp"):
            bind.exec_driver_sql(
                "ALTER TABLE agent_skill_state "
                "ADD COLUMN skill_xp INTEGER NOT NULL DEFAULT 0"
            )
        if not _has_column("agent_skill_state", "last_used_at"):
            bind.exec_driver_sql(
                "ALTER TABLE agent_skill_state "
                "ADD COLUMN last_used_at TIMESTAMPTZ NOT NULL DEFAULT NOW()"
            )
        if not _has_column("agent_skill_state", "mastery_effects"):
            bind.exec_driver_sql(
                "ALTER TABLE agent_skill_state "
                "ADD COLUMN mastery_effects TEXT[] NOT NULL DEFAULT '{}'"
            )
    else:
        if not _has_column("agent_skill_state", "skill_xp"):
            bind.exec_driver_sql(
                "ALTER TABLE agent_skill_state "
                "ADD COLUMN skill_xp INTEGER NOT NULL DEFAULT 0"
            )
        if not _has_column("agent_skill_state", "last_used_at"):
            bind.exec_driver_sql(
                "ALTER TABLE agent_skill_state "
                "ADD COLUMN last_used_at TEXT NOT NULL DEFAULT '1970-01-01 00:00:00'"
            )
        if not _has_column("agent_skill_state", "mastery_effects"):
            bind.exec_driver_sql(
                "ALTER TABLE agent_skill_state "
                "ADD COLUMN mastery_effects TEXT NOT NULL DEFAULT '[]'"
            )

    if _has_column("agent_skill_state", "xp"):
        bind.exec_driver_sql("UPDATE agent_skill_state SET skill_xp = xp")
    if _has_column("agent_skill_state", "last_active_at"):
        bind.exec_driver_sql(
            "UPDATE agent_skill_state SET last_used_at = last_active_at"
        )
    if bind.dialect.name == "postgresql":
        bind.exec_driver_sql(
            """
            UPDATE agent_skill_state
            SET mastery_effects = CASE
                WHEN level >= 5 THEN ARRAY[
                    'extended_thinking',
                    'parallel_subtask',
                    'prompt_overhead',
                    'teach_other_agent'
                ]::TEXT[]
                WHEN level = 4 THEN ARRAY[
                    'extended_thinking',
                    'parallel_subtask',
                    'prompt_overhead'
                ]::TEXT[]
                WHEN level = 3 THEN ARRAY[
                    'extended_thinking',
                    'parallel_subtask'
                ]::TEXT[]
                WHEN level = 2 THEN ARRAY['extended_thinking']::TEXT[]
                ELSE ARRAY[]::TEXT[]
            END
            """
        )
    else:
        bind.exec_driver_sql(
            """
            UPDATE agent_skill_state
            SET mastery_effects = CASE
                WHEN level >= 5 THEN
                    '["extended_thinking","parallel_subtask","prompt_overhead","teach_other_agent"]'
                WHEN level = 4 THEN
                    '["extended_thinking","parallel_subtask","prompt_overhead"]'
                WHEN level = 3 THEN
                    '["extended_thinking","parallel_subtask"]'
                WHEN level = 2 THEN '["extended_thinking"]'
                ELSE '[]'
            END
            """
        )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_agent_skill_state_last_used "
        "ON agent_skill_state (last_used_at)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_agent_skill_state_last_used")
    if _has_column("agent_skill_state", "mastery_effects"):
        op.drop_column("agent_skill_state", "mastery_effects")
    if _has_column("agent_skill_state", "last_used_at"):
        op.drop_column("agent_skill_state", "last_used_at")
    if _has_column("agent_skill_state", "skill_xp"):
        op.drop_column("agent_skill_state", "skill_xp")
