"""OP-1340 -- ``agent_character_card`` table for RPG.W1 stat sheets.

ADR-0008 §"Layer-1 stat sheet / character card" requires a durable row
per concrete agent instance. The backend character-card store already
reads and writes this shape: ``agent_id`` is the stable lookup key, while
``class`` + ``instance_suffix`` describe the runtime identity and ``guild``
+ level/xp fields drive RPG roster and progression views.

Schema notes
------------
* ``class`` is quoted because it is a SQL keyword in common dialects.
  The application aliases it to ``agent_class`` at read time.
* ``level`` and ``xp`` mirror the in-process ``CharacterCard`` guards:
  level starts at 1 and xp cannot be negative.
* ``specialization_label`` and ``style_fingerprint`` default to empty
  strings so first-task bootstrap can insert a complete card without
  waiting for later RPG enrichment workers.

Revision ID: 0239
Revises: 0238
Create Date: 2026-05-17
backwards-compat: safe
"""
from __future__ import annotations

from alembic import op


revision = "0239"
down_revision = "0238"
branch_labels = None
depends_on = None


_PG_DDL = """
CREATE TABLE IF NOT EXISTS agent_character_card (
    agent_id              TEXT PRIMARY KEY,
    "class"               TEXT NOT NULL,
    instance_suffix       TEXT NOT NULL DEFAULT 'alpha',
    guild                 TEXT NOT NULL DEFAULT 'backend',
    level                 INTEGER NOT NULL DEFAULT 1,
    xp                    INTEGER NOT NULL DEFAULT 0,
    specialization_label  TEXT NOT NULL DEFAULT '',
    style_fingerprint     TEXT NOT NULL DEFAULT '',
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT agent_character_card_level_chk CHECK (level >= 1),
    CONSTRAINT agent_character_card_xp_chk CHECK (xp >= 0)
)
"""


_SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS agent_character_card (
    agent_id              TEXT PRIMARY KEY,
    "class"               TEXT NOT NULL,
    instance_suffix       TEXT NOT NULL DEFAULT 'alpha',
    guild                 TEXT NOT NULL DEFAULT 'backend',
    level                 INTEGER NOT NULL DEFAULT 1,
    xp                    INTEGER NOT NULL DEFAULT 0,
    specialization_label  TEXT NOT NULL DEFAULT '',
    style_fingerprint     TEXT NOT NULL DEFAULT '',
    created_at            TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at            TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT agent_character_card_level_chk CHECK (level >= 1),
    CONSTRAINT agent_character_card_xp_chk CHECK (xp >= 0)
)
"""


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(_PG_DDL if bind.dialect.name == "postgresql" else _SQLITE_DDL)
    bind.exec_driver_sql(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_character_card_identity "
        'ON agent_character_card ("class", instance_suffix)'
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_agent_character_card_guild_level "
        "ON agent_character_card (guild, level DESC, xp DESC)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_agent_character_card_guild_level")
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_agent_character_card_identity")
    bind.exec_driver_sql("DROP TABLE IF EXISTS agent_character_card")
