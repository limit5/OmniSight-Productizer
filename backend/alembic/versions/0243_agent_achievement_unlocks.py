"""OP-1467 -- durable ``agent_achievement_unlocks`` table.

RPG.W16 achievement definitions stay in the in-process registry. This table
stores the per-agent unlock state keyed by ``(agent_id, achievement_id)`` so
the daemon can persist milestone hits and the REST API can list earned badges.

Revision ID: 0243
Revises: 0242
Create Date: 2026-05-18
backwards-compat: safe
"""
from __future__ import annotations

from alembic import op


revision = "0243"
down_revision = "0242"
branch_labels = None
depends_on = None


_PG_DDL = """
CREATE TABLE IF NOT EXISTS agent_achievement_unlocks (
    id             SERIAL PRIMARY KEY,
    agent_id       TEXT NOT NULL,
    achievement_id TEXT NOT NULL,
    earned_at      TIMESTAMPTZ NOT NULL,
    progress_label TEXT NULL,
    rarity         TEXT NOT NULL,
    CONSTRAINT agent_achievement_unlocks_agent_chk
        CHECK (length(trim(agent_id)) > 0),
    CONSTRAINT agent_achievement_unlocks_achievement_chk
        CHECK (length(trim(achievement_id)) > 0),
    CONSTRAINT agent_achievement_unlocks_rarity_chk
        CHECK (rarity IN ('bronze', 'silver', 'gold')),
    CONSTRAINT uq_agent_achievement_unlocks_agent_achievement
        UNIQUE (agent_id, achievement_id)
)
"""


_SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS agent_achievement_unlocks (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id       TEXT NOT NULL,
    achievement_id TEXT NOT NULL,
    earned_at      TEXT NOT NULL,
    progress_label TEXT NULL,
    rarity         TEXT NOT NULL,
    CONSTRAINT agent_achievement_unlocks_agent_chk
        CHECK (length(trim(agent_id)) > 0),
    CONSTRAINT agent_achievement_unlocks_achievement_chk
        CHECK (length(trim(achievement_id)) > 0),
    CONSTRAINT agent_achievement_unlocks_rarity_chk
        CHECK (rarity IN ('bronze', 'silver', 'gold')),
    CONSTRAINT uq_agent_achievement_unlocks_agent_achievement
        UNIQUE (agent_id, achievement_id)
)
"""


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(_PG_DDL if bind.dialect.name == "postgresql" else _SQLITE_DDL)
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_agent_achievement_unlocks_agent_id "
        "ON agent_achievement_unlocks (agent_id)"
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_agent_achievement_unlocks_achievement_id "
        "ON agent_achievement_unlocks (achievement_id)"
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_agent_achievement_unlocks_earned_at "
        "ON agent_achievement_unlocks (earned_at)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_agent_achievement_unlocks_earned_at")
    bind.exec_driver_sql(
        "DROP INDEX IF EXISTS idx_agent_achievement_unlocks_achievement_id"
    )
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_agent_achievement_unlocks_agent_id")
    bind.exec_driver_sql("DROP TABLE IF EXISTS agent_achievement_unlocks")
