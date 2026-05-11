"""OP-868 -- release_milestones table.

Tracks JIRA fixVersion-backed release milestones for Sprint D release
acceptance. The row is created by ``scripts/milestone_define.py`` after
the corresponding JIRA fixVersion is created, then updated by the
nightly milestone checker when readiness status changes.

Revision ID: 0221
Revises: 0206
Create Date: 2026-05-11
"""
from __future__ import annotations

from alembic import op


revision = "0221"
down_revision = "0206"
branch_labels = None
depends_on = None


_PG_DDL = """
CREATE TABLE IF NOT EXISTS release_milestones (
    id                  BIGSERIAL PRIMARY KEY,
    version             TEXT NOT NULL UNIQUE,
    jira_version_id     TEXT NOT NULL,
    jira_project_key    TEXT NOT NULL DEFAULT 'OP',
    status              TEXT NOT NULL DEFAULT 'defined',
    last_report_json    JSONB,
    last_checked_at     TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT release_milestones_status_chk
        CHECK (status IN ('defined','ready','not_ready','error','force_accepted'))
)
"""


_SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS release_milestones (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    version             TEXT NOT NULL UNIQUE,
    jira_version_id     TEXT NOT NULL,
    jira_project_key    TEXT NOT NULL DEFAULT 'OP',
    status              TEXT NOT NULL DEFAULT 'defined',
    last_report_json    TEXT,
    last_checked_at     TEXT,
    created_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT release_milestones_status_chk
        CHECK (status IN ('defined','ready','not_ready','error','force_accepted'))
)
"""


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(_PG_DDL if bind.dialect.name == "postgresql" else _SQLITE_DDL)
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_release_milestones_status "
        "ON release_milestones (status, updated_at DESC)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_release_milestones_status")
    bind.exec_driver_sql("DROP TABLE IF EXISTS release_milestones")
