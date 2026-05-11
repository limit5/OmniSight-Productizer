"""OP-219 -- ``agent_talent_choice`` table for RPG.W14 talent tree milestones.

ADR-0008 §"Talent tree (W14)" requires a durable, per-``(agent_id,
milestone_level)`` row tracking the talent the operator locked in at
each Lv-{10,30,50,80} milestone. The choice is immutable per the spec
— the application-level helper ``backend.agents.talent_tree.lock_talent``
refuses to overwrite a different talent_id with
``TalentAlreadyLocked``. The migration does not add a DB trigger for
that invariant because operators can still hand-correct with SQL
during recovery (mirrors W12's branch_choice rationale on 0226).

Schema notes
------------
* ``talent_id`` is the canonical id from ``config/talent_tree.yaml``.
  The drift guard in ``backend/agents/talent_tree.py`` validates the
  value at lock time; no CHECK constraint here so a YAML rename never
  invalidates a stored row.
* ``milestone_level`` is the agent's character level at the milestone
  trigger (one of 10/30/50/80 per ADR-0008). The CHECK enforces the
  closed enum so a malformed call site can't insert ``Lv 25``.

Revision ID: 0228
Revises: 0226
Create Date: 2026-05-11
"""
from __future__ import annotations

from alembic import op


revision = "0228"
down_revision = "0226"
branch_labels = None
depends_on = None


_PG_DDL = """
CREATE TABLE IF NOT EXISTS agent_talent_choice (
    agent_id         TEXT NOT NULL,
    milestone_level  INTEGER NOT NULL,
    talent_id        TEXT NOT NULL,
    chosen_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (agent_id, milestone_level),
    CONSTRAINT agent_talent_choice_milestone_chk
        CHECK (milestone_level IN (10, 30, 50, 80))
)
"""


_SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS agent_talent_choice (
    agent_id         TEXT NOT NULL,
    milestone_level  INTEGER NOT NULL,
    talent_id        TEXT NOT NULL,
    chosen_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (agent_id, milestone_level),
    CONSTRAINT agent_talent_choice_milestone_chk
        CHECK (milestone_level IN (10, 30, 50, 80))
)
"""


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(_PG_DDL if bind.dialect.name == "postgresql" else _SQLITE_DDL)
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_agent_talent_choice_agent "
        "ON agent_talent_choice (agent_id)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_agent_talent_choice_agent")
    bind.exec_driver_sql("DROP TABLE IF EXISTS agent_talent_choice")
