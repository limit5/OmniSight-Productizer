"""OP-218 -- ``agent_tool_proficiency`` table for RPG.W13 MCP/A2A tool proficiency.

ADR-0008 §"MCP/A2A tool proficiency (W13)" requires a durable, per-
``(agent_id, tool_id)`` row tracking the agent's invocation count,
success count, derived level (1-5), and last-used timestamp. The
W17.7 telemetry stream (OP-117) is the data source — every successful
or failed tool invocation increments the row's counters and may cross
a level threshold, at which point ``backend.agents.tool_proficiency``
emits ``tool:level_up`` on the SSE bus.

The table is intentionally narrow: counters + derived level + last-
used timestamp. The level is denormalised from ``(invocation_count,
success_count)`` via :func:`tool_proficiency.compute_tool_level` for
cheap reads on the Character Card "Tools" tab, and is recomputed at
write time so consumers never need to call the helper after a SELECT.

Schema notes
------------
* ``last_used_at`` is non-null because rows are created on first
  invocation. Until the first invocation no row exists and the gate
  helper treats the agent as Lv 1 (per ``ToolNotInProficiencyTable``
  error in W13 §"Error catalog" — bootstrap row with Lv 1, allow).
* ``invocation_count`` / ``success_count`` are monotonic; the
  ``scripts/rpg_rebuild_tool_proficiency.py`` replay walks the
  ``tool_invocation`` telemetry log to re-derive both, so the table
  can always be reconstructed from history.

Revision ID: 0227
Revises: 0226
Create Date: 2026-05-11
"""
from __future__ import annotations

from alembic import op


revision = "0227"
down_revision = "0226"
branch_labels = None
depends_on = None


_PG_DDL = """
CREATE TABLE IF NOT EXISTS agent_tool_proficiency (
    agent_id          TEXT NOT NULL,
    tool_id           TEXT NOT NULL,
    level             INTEGER NOT NULL DEFAULT 1,
    invocation_count  INTEGER NOT NULL DEFAULT 0,
    success_count     INTEGER NOT NULL DEFAULT 0,
    last_used_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (agent_id, tool_id),
    CONSTRAINT agent_tool_proficiency_level_chk
        CHECK (level BETWEEN 1 AND 5),
    CONSTRAINT agent_tool_proficiency_counts_chk
        CHECK (invocation_count >= 0 AND success_count >= 0
               AND success_count <= invocation_count)
)
"""


_SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS agent_tool_proficiency (
    agent_id          TEXT NOT NULL,
    tool_id           TEXT NOT NULL,
    level             INTEGER NOT NULL DEFAULT 1,
    invocation_count  INTEGER NOT NULL DEFAULT 0,
    success_count     INTEGER NOT NULL DEFAULT 0,
    last_used_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (agent_id, tool_id),
    CONSTRAINT agent_tool_proficiency_level_chk
        CHECK (level BETWEEN 1 AND 5),
    CONSTRAINT agent_tool_proficiency_counts_chk
        CHECK (invocation_count >= 0 AND success_count >= 0
               AND success_count <= invocation_count)
)
"""


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(_PG_DDL if bind.dialect.name == "postgresql" else _SQLITE_DDL)
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_agent_tool_proficiency_agent "
        "ON agent_tool_proficiency (agent_id)"
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_agent_tool_proficiency_last_used "
        "ON agent_tool_proficiency (last_used_at)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_agent_tool_proficiency_last_used")
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_agent_tool_proficiency_agent")
    bind.exec_driver_sql("DROP TABLE IF EXISTS agent_tool_proficiency")
