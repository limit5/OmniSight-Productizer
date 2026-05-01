"""AB.7 — Dead Letter Queue persistent table.

Companion to ``rate_limiter.py``'s ``DeadLetterQueue`` Protocol — when
the in-memory impl gets swapped for ``PostgresDeadLetterQueue`` (which
``postgres_stores.py`` now provides), this is the table behind it.

DLQ entries:
  * ``entry_id``         — unique ID, returned to the dispatcher's
                           retry-count log + the operator's "manually
                           replay this batch" UI
  * ``workspace`` + ``model``  — which Anthropic workspace / which model
                           the failed call targeted
  * ``classification``  — retryable / rate_limited / non_retryable so
                           operators can filter retry-recoverable from
                           hard auth/4xx
  * ``attempts_made``   — how many times we tried before giving up
  * ``last_status_code`` — last HTTP status seen
  * ``last_exception_repr`` + ``last_reason`` — diagnostic text
  * ``request_metadata`` — JSONB caller-supplied tag
                          (typically ``{"task_id": "..."}``)

Two indexes:
  * ``(created_at DESC)`` — recent-first DLQ list (operator dashboard)
  * ``(workspace, classification, created_at DESC)`` — drill-down
    "show me all rate-limited failures in production this week"

Revision ID: 0185
Revises: 0184
Create Date: 2026-05-02
"""
from __future__ import annotations

from alembic import op


revision = "0185"
down_revision = "0184"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "postgresql":
        bind.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS dlq_entries (
                entry_id TEXT PRIMARY KEY,
                workspace TEXT NOT NULL,
                model TEXT NOT NULL,
                classification TEXT NOT NULL,
                attempts_made INTEGER NOT NULL,
                last_status_code INTEGER,
                last_exception_repr TEXT,
                last_reason TEXT NOT NULL,
                request_metadata JSONB,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        bind.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_dlq_entries_created_at "
            "ON dlq_entries(created_at DESC)"
        )
        bind.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_dlq_entries_workspace_class "
            "ON dlq_entries(workspace, classification, created_at DESC)"
        )
    else:
        bind.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS dlq_entries (
                entry_id TEXT PRIMARY KEY,
                workspace TEXT NOT NULL,
                model TEXT NOT NULL,
                classification TEXT NOT NULL,
                attempts_made INTEGER NOT NULL,
                last_status_code INTEGER,
                last_exception_repr TEXT,
                last_reason TEXT NOT NULL,
                request_metadata TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        bind.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_dlq_entries_created_at "
            "ON dlq_entries(created_at DESC)"
        )
        bind.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_dlq_entries_workspace_class "
            "ON dlq_entries(workspace, classification, created_at DESC)"
        )


def downgrade() -> None:
    # Defensive no-op: dropping the DLQ would lose forensic history of
    # exhausted retries. Hand-rolled rollback required.
    pass
