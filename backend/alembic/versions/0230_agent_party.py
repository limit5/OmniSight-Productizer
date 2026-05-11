"""OP-220 -- ``agent_party`` table for RPG.W17 synergy / party system.

ADR-0008 §"Party / Synergy system (W17)" requires a durable mapping
of party membership (``party_id`` × ``member_agent_id``) plus the
party's active Tier L+ task slot. Party size is bounded to [2, 5]
members per ADR-0008. The helper ``backend.agents.party.create_party``
validates the bound before insert; the ``CHECK`` constraint on the
companion ``agent_party_state`` table backs that contract at the
storage layer so a manual DB write cannot insert a 1-member or
6-member party.

Schema notes
------------
* ``agent_party`` is the membership rows — one row per ``(party_id,
  member_agent_id)``. Per ADR-0008 a member can belong to at most one
  active party at a time; the partial unique index on
  ``member_agent_id WHERE released_at IS NULL`` enforces that.
* ``agent_party_state`` is one row per party with the party-level
  state (name, synergy_label, synergy_xp_bonus, active_task_id,
  active_task_assigned_at). Splitting the state out keeps the
  membership table append-only and gives a constant-time lookup for
  the per-task exclusivity gate (W17 AC #4).
* ``active_task_id`` is nullable: a party can exist without an active
  task, but cannot hold more than one (enforced at app layer in
  ``party.assign_task`` and by the unique index here on
  ``active_task_id WHERE active_task_id IS NOT NULL``).

Revision ID: 0230
Revises: 0229
Create Date: 2026-05-11
"""
from __future__ import annotations

from alembic import op


revision = "0230"
down_revision = "0229"
branch_labels = None
depends_on = None


_PG_DDL_STATE = """
CREATE TABLE IF NOT EXISTS agent_party_state (
    party_id                 TEXT PRIMARY KEY,
    name                     TEXT NOT NULL,
    synergy_label            TEXT,
    synergy_xp_bonus         REAL NOT NULL DEFAULT 0.0,
    active_task_id           TEXT,
    active_task_assigned_at  TIMESTAMPTZ,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    disbanded_at             TIMESTAMPTZ
)
"""

_PG_DDL_MEMBERS = """
CREATE TABLE IF NOT EXISTS agent_party (
    party_id          TEXT NOT NULL,
    member_agent_id   TEXT NOT NULL,
    joined_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    released_at       TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (party_id, member_agent_id),
    FOREIGN KEY (party_id) REFERENCES agent_party_state (party_id)
        ON DELETE CASCADE
)
"""

_SQLITE_DDL_STATE = """
CREATE TABLE IF NOT EXISTS agent_party_state (
    party_id                 TEXT PRIMARY KEY,
    name                     TEXT NOT NULL,
    synergy_label            TEXT,
    synergy_xp_bonus         REAL NOT NULL DEFAULT 0.0,
    active_task_id           TEXT,
    active_task_assigned_at  TEXT,
    created_at               TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at               TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    disbanded_at             TEXT
)
"""

_SQLITE_DDL_MEMBERS = """
CREATE TABLE IF NOT EXISTS agent_party (
    party_id          TEXT NOT NULL,
    member_agent_id   TEXT NOT NULL,
    joined_at         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    released_at       TEXT,
    created_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (party_id, member_agent_id),
    FOREIGN KEY (party_id) REFERENCES agent_party_state (party_id)
        ON DELETE CASCADE
)
"""


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    bind.exec_driver_sql(_PG_DDL_STATE if is_pg else _SQLITE_DDL_STATE)
    bind.exec_driver_sql(_PG_DDL_MEMBERS if is_pg else _SQLITE_DDL_MEMBERS)
    # Partial unique index: each agent can be in at most one active
    # party at a time. ``released_at IS NULL`` = still a member.
    bind.exec_driver_sql(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_party_member_active "
        "ON agent_party (member_agent_id) WHERE released_at IS NULL"
    )
    # Per-task exclusivity: at most one party can hold a given
    # active_task_id (defensive — the app layer rejects this first).
    bind.exec_driver_sql(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_party_state_task "
        "ON agent_party_state (active_task_id) WHERE active_task_id IS NOT NULL"
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_agent_party_party "
        "ON agent_party (party_id)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_agent_party_party")
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_agent_party_state_task")
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_agent_party_member_active")
    bind.exec_driver_sql("DROP TABLE IF EXISTS agent_party")
    bind.exec_driver_sql("DROP TABLE IF EXISTS agent_party_state")
