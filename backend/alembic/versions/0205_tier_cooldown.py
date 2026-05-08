"""OP-807 (G5) -- ``tier_cooldown_observation`` + ``tier_cooldown_state``.

ADR-0005 §4 layer 4 cooldown enforcement schema. Two tables:

1. ``tier_cooldown_observation`` -- append-only event log. One row per
   ``(agent_id, classification_date, computed_tier, override_tier)``
   tuple emitted by the Gerrit ``patchset-created`` hook (OP-805) once
   it has both the agent's computed tier and the reviewer-set
   override. Daily cron (``scripts/cron/tier_cooldown_daily.py``)
   reads the trailing 30-day window per agent.

2. ``tier_cooldown_state`` -- one row per agent, mutated by the cron.
   Carries ``cooldown_level`` (``none|30d|90d|revoked``),
   ``cooldown_started_at`` / ``cooldown_expires_at`` window markers,
   and a ``cooldown_history`` JSON blob the sweep uses for the
   "2 cooldowns in 90 days → 90d" and "3 cooldowns in 365 days →
   revoked" escalation rules.

Why date (not timestamp) on observation rows
--------------------------------------------
The cron operates at daily granularity. Storing
``classification_date`` as a ``DATE`` instead of ``TIMESTAMP`` makes
the trailing-30d window scan index-friendly without a ``date_trunc``
on the hot path, and it sidesteps timezone ambiguity for cron-sliced
windows. The Gerrit hook timestamp is already in the
``gerrit-tier-hook.log`` audit file if a forensic ms-precision lookup
is ever needed.

Module-global / cross-worker state audit
----------------------------------------
Pure DDL migration -- no module-level singleton. Writers are the
Gerrit hook (one event per PS upload) and the daily cron (one state
row per evaluated agent). The dashboard router is read-only.

Production readiness gate
-------------------------
No new Python / OS package. Two new tables only -- existing rows
untouched. The ``cooldown_history`` column is ``JSONB`` on Postgres
and ``TEXT`` (JSON-encoded) on SQLite; the readers in
``backend.governance.tier_cooldown`` normalise both shapes.

Revision ID: 0205
Revises: 0204
Create Date: 2026-05-09
"""
from __future__ import annotations

from alembic import op


revision = "0205"
down_revision = "0204"
branch_labels = None
depends_on = None


# ─── Postgres DDL ────────────────────────────────────────────────────


_PG_OBSERVATION_DDL = """
CREATE TABLE IF NOT EXISTS tier_cooldown_observation (
    id                  BIGSERIAL PRIMARY KEY,
    agent_id            TEXT NOT NULL,
    classification_date DATE NOT NULL,
    computed_tier       TEXT NOT NULL,
    override_tier       TEXT NOT NULL,
    ticket              TEXT,
    change_id           TEXT,
    patchset            INTEGER,
    CONSTRAINT tier_cooldown_obs_computed_chk
        CHECK (computed_tier IN ('s','m','l','x')),
    CONSTRAINT tier_cooldown_obs_override_chk
        CHECK (override_tier IN ('s','m','l','x'))
)
"""


_PG_OBSERVATION_INDEX_AGENT_DATE = """
CREATE INDEX IF NOT EXISTS idx_tier_cooldown_obs_agent_date
    ON tier_cooldown_observation (agent_id, classification_date DESC)
"""


_PG_OBSERVATION_INDEX_DATE = """
CREATE INDEX IF NOT EXISTS idx_tier_cooldown_obs_date
    ON tier_cooldown_observation (classification_date DESC)
"""


_PG_STATE_DDL = """
CREATE TABLE IF NOT EXISTS tier_cooldown_state (
    agent_id            TEXT PRIMARY KEY,
    cooldown_level      TEXT NOT NULL DEFAULT 'none',
    cooldown_started_at TIMESTAMPTZ,
    cooldown_expires_at TIMESTAMPTZ,
    last_evaluation_at  TIMESTAMPTZ,
    cooldown_history    JSONB NOT NULL DEFAULT '[]'::jsonb,
    CONSTRAINT tier_cooldown_state_level_chk
        CHECK (cooldown_level IN ('none','30d','90d','revoked'))
)
"""


_PG_STATE_INDEX_LEVEL = """
CREATE INDEX IF NOT EXISTS idx_tier_cooldown_state_level
    ON tier_cooldown_state (cooldown_level)
    WHERE cooldown_level <> 'none'
"""


# ─── SQLite DDL ──────────────────────────────────────────────────────


_SQLITE_OBSERVATION_DDL = """
CREATE TABLE IF NOT EXISTS tier_cooldown_observation (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id            TEXT NOT NULL,
    classification_date TEXT NOT NULL,
    computed_tier       TEXT NOT NULL,
    override_tier       TEXT NOT NULL,
    ticket              TEXT,
    change_id           TEXT,
    patchset            INTEGER,
    CONSTRAINT tier_cooldown_obs_computed_chk
        CHECK (computed_tier IN ('s','m','l','x')),
    CONSTRAINT tier_cooldown_obs_override_chk
        CHECK (override_tier IN ('s','m','l','x'))
)
"""


_SQLITE_OBSERVATION_INDEX_AGENT_DATE = """
CREATE INDEX IF NOT EXISTS idx_tier_cooldown_obs_agent_date
    ON tier_cooldown_observation (agent_id, classification_date DESC)
"""


_SQLITE_OBSERVATION_INDEX_DATE = """
CREATE INDEX IF NOT EXISTS idx_tier_cooldown_obs_date
    ON tier_cooldown_observation (classification_date DESC)
"""


_SQLITE_STATE_DDL = """
CREATE TABLE IF NOT EXISTS tier_cooldown_state (
    agent_id            TEXT PRIMARY KEY,
    cooldown_level      TEXT NOT NULL DEFAULT 'none',
    cooldown_started_at TEXT,
    cooldown_expires_at TEXT,
    last_evaluation_at  TEXT,
    cooldown_history    TEXT NOT NULL DEFAULT '[]',
    CONSTRAINT tier_cooldown_state_level_chk
        CHECK (cooldown_level IN ('none','30d','90d','revoked'))
)
"""


_SQLITE_STATE_INDEX_LEVEL = """
CREATE INDEX IF NOT EXISTS idx_tier_cooldown_state_level
    ON tier_cooldown_state (cooldown_level)
"""


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.exec_driver_sql(_PG_OBSERVATION_DDL)
        bind.exec_driver_sql(_PG_OBSERVATION_INDEX_AGENT_DATE)
        bind.exec_driver_sql(_PG_OBSERVATION_INDEX_DATE)
        bind.exec_driver_sql(_PG_STATE_DDL)
        bind.exec_driver_sql(_PG_STATE_INDEX_LEVEL)
    else:
        bind.exec_driver_sql(_SQLITE_OBSERVATION_DDL)
        bind.exec_driver_sql(_SQLITE_OBSERVATION_INDEX_AGENT_DATE)
        bind.exec_driver_sql(_SQLITE_OBSERVATION_INDEX_DATE)
        bind.exec_driver_sql(_SQLITE_STATE_DDL)
        bind.exec_driver_sql(_SQLITE_STATE_INDEX_LEVEL)


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_tier_cooldown_state_level")
    bind.exec_driver_sql("DROP TABLE IF EXISTS tier_cooldown_state")
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_tier_cooldown_obs_date")
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_tier_cooldown_obs_agent_date")
    bind.exec_driver_sql("DROP TABLE IF EXISTS tier_cooldown_observation")
