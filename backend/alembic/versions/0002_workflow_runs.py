"""Phase 56 — durable workflow checkpointing.

Adds two tables so an in-flight LangGraph invoke / pipeline phase
survives a backend crash. Per design (HANDOFF Phase 56):

  workflow_runs   — one row per logical workflow execution
  workflow_steps  — append-only checkpoint log; (run_id, idempotency_key)
                    UNIQUE so a step that already ran returns the
                    cached output rather than re-executing.

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-14
"""
from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


_SQL = """
CREATE TABLE IF NOT EXISTS workflow_runs (
    id              TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,
    started_at      REAL NOT NULL,
    completed_at    REAL,
    status          TEXT NOT NULL DEFAULT 'running',
    last_step_id    TEXT,
    metadata        TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_workflow_runs_status
    ON workflow_runs(status);

CREATE TABLE IF NOT EXISTS workflow_steps (
    id              TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    started_at      REAL NOT NULL,
    completed_at    REAL,
    output_json     TEXT,
    error           TEXT,
    UNIQUE (run_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_workflow_steps_run
    ON workflow_steps(run_id);
"""


def upgrade() -> None:
    bind = op.get_bind()
    for stmt in [s.strip() for s in _SQL.split(";") if s.strip()]:
        bind.exec_driver_sql(stmt)


def downgrade() -> None:
    bind = op.get_bind()
    for stmt in [
        "DROP INDEX IF EXISTS idx_workflow_steps_run",
        "DROP INDEX IF EXISTS idx_workflow_runs_status",
        "DROP TABLE IF EXISTS workflow_steps",
        "DROP TABLE IF EXISTS workflow_runs",
    ]:
        bind.exec_driver_sql(stmt)
