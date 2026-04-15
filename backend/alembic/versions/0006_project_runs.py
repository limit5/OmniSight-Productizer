"""B7 (#207) — project_runs table for run aggregation.

Groups workflow_runs into logical project runs so the UI can
show a collapsed parent row with summary stats.

Revision ID: 0006
Revises: 0005
Create Date: 2026-04-15
"""
from __future__ import annotations

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


_SQL = """
CREATE TABLE IF NOT EXISTS project_runs (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL,
    label           TEXT NOT NULL DEFAULT '',
    created_at      REAL NOT NULL,
    workflow_run_ids TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_project_runs_project ON project_runs(project_id);
CREATE INDEX IF NOT EXISTS idx_project_runs_created ON project_runs(created_at);
"""


def upgrade() -> None:
    bind = op.get_bind()
    for stmt in [s.strip() for s in _SQL.split(";") if s.strip()]:
        bind.exec_driver_sql(stmt)


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("DROP TABLE IF EXISTS project_runs")
