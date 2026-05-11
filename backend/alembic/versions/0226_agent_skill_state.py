"""OP-217 -- ``agent_skill_state`` table for RPG.W12 skill leveling.

ADR-0008 §"Skill leveling (W12)" requires a durable, per-``(agent_id,
skill_id)`` row tracking the agent's accumulated skill XP, derived
level, immutable branch_choice (locked at Lv 3), and last-active
timestamp the decay cron consumes. This migration adds the table
plus indexes for the two hot read paths:

* Character Card "Skills" tab — list rows by ``agent_id``.
* Weekly decay cron — scan rows with ``last_active_at`` older than 30 days.

Schema notes
------------
* ``branch_choice`` is nullable until the agent crosses Lv 3 and the
  operator picks a fork via the Character Card UI. Once written, the
  application-level helper ``lock_branch_choice`` refuses re-writes
  (immutability is enforced by ``SkillBranchAlreadyLocked``, not a
  database trigger — operators can still hand-correct with SQL during
  recovery).
* ``level`` is denormalised from ``xp`` for cheap reads. The
  application keeps it in sync via :func:`backend.agents.skill_leveling.compute_level`;
  the migration does not add a CHECK because the decay path
  intentionally lets ``xp`` regress while leaving ``level`` pinned.

Revision ID: 0226
Revises: 0221
Create Date: 2026-05-11
"""
from __future__ import annotations

from alembic import op


revision = "0226"
down_revision = "0221"
branch_labels = None
depends_on = None


_PG_DDL = """
CREATE TABLE IF NOT EXISTS agent_skill_state (
    agent_id        TEXT NOT NULL,
    skill_id        TEXT NOT NULL,
    level           INTEGER NOT NULL DEFAULT 1,
    xp              INTEGER NOT NULL DEFAULT 0,
    branch_choice   TEXT,
    last_active_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_taught_at  TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (agent_id, skill_id),
    CONSTRAINT agent_skill_state_level_chk CHECK (level BETWEEN 1 AND 5),
    CONSTRAINT agent_skill_state_xp_chk    CHECK (xp >= 0)
)
"""


_SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS agent_skill_state (
    agent_id        TEXT NOT NULL,
    skill_id        TEXT NOT NULL,
    level           INTEGER NOT NULL DEFAULT 1,
    xp              INTEGER NOT NULL DEFAULT 0,
    branch_choice   TEXT,
    last_active_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_taught_at  TEXT,
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (agent_id, skill_id),
    CONSTRAINT agent_skill_state_level_chk CHECK (level BETWEEN 1 AND 5),
    CONSTRAINT agent_skill_state_xp_chk    CHECK (xp >= 0)
)
"""


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(_PG_DDL if bind.dialect.name == "postgresql" else _SQLITE_DDL)
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_agent_skill_state_agent "
        "ON agent_skill_state (agent_id)"
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_agent_skill_state_last_active "
        "ON agent_skill_state (last_active_at)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_agent_skill_state_last_active")
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_agent_skill_state_agent")
    bind.exec_driver_sql("DROP TABLE IF EXISTS agent_skill_state")
