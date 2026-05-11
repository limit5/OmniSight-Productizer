"""OP-219 -- ``agent_capstone_ability`` table for RPG.W14 Lv-80 capstone.

ADR-0008 §"Talent tree (W14)" gives every Guild a single capstone
ability at Lv 80 (e.g. backend Guild = ``code_archaeologist`` / 1M
context legacy-code read). The migration stores the operator's lock of
that ability separately from the per-milestone ``agent_talent_choice``
rows so the capstone read path is a constant-time lookup.

Schema notes
------------
* One row per agent (``PRIMARY KEY (agent_id)``). Locking is gated by
  the Lv-80 milestone talent already being chosen; the helper
  ``lock_capstone_ability`` raises :class:`CapstoneRequiresLv80` before
  reaching SQL.
* ``ability_id`` is the slug from ``config/talent_tree.yaml`` under the
  Guild's ``capstone:`` block. We do not constrain it server-side
  because a YAML rename should not orphan an existing row.

Revision ID: 0229
Revises: 0228
Create Date: 2026-05-11
"""
from __future__ import annotations

from alembic import op


revision = "0229"
down_revision = "0228"
branch_labels = None
depends_on = None


_PG_DDL = """
CREATE TABLE IF NOT EXISTS agent_capstone_ability (
    agent_id     TEXT PRIMARY KEY,
    ability_id   TEXT NOT NULL,
    locked_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""


_SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS agent_capstone_ability (
    agent_id     TEXT PRIMARY KEY,
    ability_id   TEXT NOT NULL,
    locked_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(_PG_DDL if bind.dialect.name == "postgresql" else _SQLITE_DDL)


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("DROP TABLE IF EXISTS agent_capstone_ability")
