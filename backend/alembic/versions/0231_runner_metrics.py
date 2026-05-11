"""OP-909 -- runner_metrics table for agent drift instrumentation.

Revision ID: 0231
Revises: 0230
Create Date: 2026-05-11
"""
from __future__ import annotations

from alembic import op


revision = "0231"
down_revision = "0230"
branch_labels = None
depends_on = None


_PG_DDL = """
CREATE TABLE IF NOT EXISTS runner_metrics (
    id                         BIGSERIAL PRIMARY KEY,
    agent_class                TEXT NOT NULL,
    instance_id                TEXT NOT NULL,
    ticket_key                 TEXT NOT NULL,
    ticket_type                TEXT NOT NULL,
    tier                       TEXT NOT NULL,
    area                       TEXT NOT NULL,
    time_to_complete_seconds   DOUBLE PRECISION,
    outcome                    TEXT,
    lessons_used_count         INTEGER NOT NULL DEFAULT 0,
    mcp_calls_count            INTEGER NOT NULL DEFAULT 0,
    claude_model_used          TEXT,
    ts                         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at               TIMESTAMPTZ,
    CONSTRAINT runner_metrics_outcome_check
        CHECK (outcome IS NULL OR outcome IN ('success', 'failure', 'skipped', 'timeout'))
)
"""

_SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS runner_metrics (
    id                         INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_class                TEXT NOT NULL,
    instance_id                TEXT NOT NULL,
    ticket_key                 TEXT NOT NULL,
    ticket_type                TEXT NOT NULL,
    tier                       TEXT NOT NULL,
    area                       TEXT NOT NULL,
    time_to_complete_seconds   REAL,
    outcome                    TEXT,
    lessons_used_count         INTEGER NOT NULL DEFAULT 0,
    mcp_calls_count            INTEGER NOT NULL DEFAULT 0,
    claude_model_used          TEXT,
    ts                         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at                 TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at               TEXT,
    CONSTRAINT runner_metrics_outcome_check
        CHECK (outcome IS NULL OR outcome IN ('success', 'failure', 'skipped', 'timeout'))
)
"""


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(_PG_DDL if bind.dialect.name == "postgresql" else _SQLITE_DDL)
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_runner_metrics_task_window "
        "ON runner_metrics (agent_class, ticket_type, ts)"
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_runner_metrics_ticket_open "
        "ON runner_metrics (ticket_key, agent_class, instance_id) "
        "WHERE outcome IS NULL"
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_runner_metrics_ticket_open")
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_runner_metrics_task_window")
    bind.exec_driver_sql("DROP TABLE IF EXISTS runner_metrics")
