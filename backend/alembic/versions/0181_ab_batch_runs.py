"""AB.3.5 — Anthropic batch run + result tables.

Persistence for batch dispatcher (AB.4): tracks Anthropic Messages Batch
API runs end-to-end so a worker restart / crash doesn't lose in-flight
batches and so completed results can be replayed by per-task callbacks.

Two tables:

  * ``batch_runs``    — one row per submitted batch. Tracks Anthropic's
                        ``batch_id``, processing status, request_count,
                        success/error/canceled/expired tallies, plus
                        timing (submitted_at, ended_at, expires_at).
                        ``metadata`` JSONB carries caller tag (which
                        OmniSight priority / phase / tenant the batch
                        serves) for cost attribution.
  * ``batch_results`` — one row per request inside a batch. Joins via
                        composite PK ``(batch_run_id, custom_id)``.
                        ``custom_id`` is what the caller passed to
                        Anthropic; ``task_id`` is the OmniSight task
                        identifier the result should route back to
                        (the AB.3.2 mapping). ``response`` JSONB stores
                        the full Anthropic message on success;
                        ``error`` JSONB the failure payload. Token
                        counts cached for cost attribution without
                        re-parsing response.

Both tables use composite indexes only where queries dictate:

  * ``ix_batch_results_task_id`` — AB.4 dispatcher resolves
                                    ``task_id → result`` on result
                                    delivery.
  * ``ix_batch_runs_status`` — pollers scan in_progress batches.

Tenant scoping intentionally NOT added at the table level; AB.3 ships
single-tenant. Multi-tenant scoping (R80) waits for KS.1 envelope to
land + Priority I tenant infra; at that point ``tenant_id`` columns
get appended via a follow-up migration.

Revision ID: 0181
Revises: 0058
Create Date: 2026-05-01
"""
from __future__ import annotations

from alembic import op


revision = "0181"
down_revision = "0058"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "postgresql":
        bind.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS batch_runs (
                batch_run_id TEXT PRIMARY KEY,
                anthropic_batch_id TEXT,
                status TEXT NOT NULL,
                request_count INTEGER NOT NULL,
                total_size_bytes BIGINT,
                submitted_at TIMESTAMPTZ,
                ended_at TIMESTAMPTZ,
                expires_at TIMESTAMPTZ,
                success_count INTEGER NOT NULL DEFAULT 0,
                error_count INTEGER NOT NULL DEFAULT 0,
                canceled_count INTEGER NOT NULL DEFAULT 0,
                expired_count INTEGER NOT NULL DEFAULT 0,
                metadata JSONB,
                created_by TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        bind.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_batch_runs_status "
            "ON batch_runs(status)"
        )
        bind.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS batch_results (
                batch_run_id TEXT NOT NULL
                    REFERENCES batch_runs(batch_run_id) ON DELETE CASCADE,
                custom_id TEXT NOT NULL,
                task_id TEXT,
                status TEXT NOT NULL,
                response JSONB,
                error JSONB,
                final_text TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                cache_read_tokens INTEGER,
                cache_creation_tokens INTEGER,
                completed_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (batch_run_id, custom_id)
            )
            """
        )
        bind.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_batch_results_task_id "
            "ON batch_results(task_id)"
        )
    else:
        # SQLite path for dev / test environments.
        bind.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS batch_runs (
                batch_run_id TEXT PRIMARY KEY,
                anthropic_batch_id TEXT,
                status TEXT NOT NULL,
                request_count INTEGER NOT NULL,
                total_size_bytes INTEGER,
                submitted_at TEXT,
                ended_at TEXT,
                expires_at TEXT,
                success_count INTEGER NOT NULL DEFAULT 0,
                error_count INTEGER NOT NULL DEFAULT 0,
                canceled_count INTEGER NOT NULL DEFAULT 0,
                expired_count INTEGER NOT NULL DEFAULT 0,
                metadata TEXT,
                created_by TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        bind.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_batch_runs_status "
            "ON batch_runs(status)"
        )
        bind.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS batch_results (
                batch_run_id TEXT NOT NULL,
                custom_id TEXT NOT NULL,
                task_id TEXT,
                status TEXT NOT NULL,
                response TEXT,
                error TEXT,
                final_text TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                cache_read_tokens INTEGER,
                cache_creation_tokens INTEGER,
                completed_at TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (batch_run_id, custom_id),
                FOREIGN KEY (batch_run_id) REFERENCES batch_runs(batch_run_id)
                    ON DELETE CASCADE
            )
            """
        )
        bind.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_batch_results_task_id "
            "ON batch_results(task_id)"
        )


def downgrade() -> None:
    # alembic-allow-noop-downgrade: dropping batch_runs / batch_results
    # would orphan in-flight Anthropic batch tasks and leak batch_id
    # references the dispatcher is still polling on. Hand-rolled
    # migration required for rollback (see FX.7.6 contract).
    pass
