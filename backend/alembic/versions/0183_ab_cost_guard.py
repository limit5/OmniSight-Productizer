"""AB.6.1 — cost guard tables.

Three tables that together drive AB.6 budget enforcement:

  * ``cost_estimates`` — one row per submitted call (real-time or
    batch). Records the *predicted* cost at submit time and the
    *actual* cost once usage data lands. Diff = estimator drift,
    surfaced via AB.10 cost regression test.
  * ``cost_budgets`` — operator-configured caps per scope. Scope is a
    composite of (kind, key) where kind ∈
    {workspace, priority, task_type, model, global} and key is the
    matching identifier (e.g., kind='priority', key='HD'). Each row
    has a daily and monthly limit in USD; nullable means "no cap"
    (still tracked, alerts not fired).
  * ``cost_alerts`` — event log of fired alerts (80% warn, 100% cap,
    120% over). Tracks the scope, period, threshold, observed spend,
    and the alert action taken (notification / throttle / block).

Indexes optimised for the hot reads: estimator look-up by call_id,
budget look-up by (kind, key), alerts list by recency.

Tenant-scoping deferred (R80 — multi-tenant gate is KS.1 envelope
+ Priority I); added in a follow-up migration when those land.

Revision ID: 0183
Revises: 0181
Create Date: 2026-05-02
"""
from __future__ import annotations

from alembic import op


revision = "0183"
down_revision = "0181"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "postgresql":
        bind.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS cost_estimates (
                estimate_id TEXT PRIMARY KEY,
                call_id TEXT NOT NULL,
                model TEXT NOT NULL,
                is_batch BOOLEAN NOT NULL,
                input_tokens_estimated INTEGER,
                output_tokens_estimated INTEGER,
                cost_usd_estimated DOUBLE PRECISION NOT NULL,
                input_tokens_actual INTEGER,
                output_tokens_actual INTEGER,
                cache_read_tokens_actual INTEGER,
                cache_creation_tokens_actual INTEGER,
                cost_usd_actual DOUBLE PRECISION,
                workspace TEXT,
                priority TEXT,
                task_type TEXT,
                metadata JSONB,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                completed_at TIMESTAMPTZ
            )
            """
        )
        bind.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_cost_estimates_call_id "
            "ON cost_estimates(call_id)"
        )
        bind.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_cost_estimates_created_at "
            "ON cost_estimates(created_at DESC)"
        )
        bind.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_cost_estimates_priority_created "
            "ON cost_estimates(priority, created_at DESC)"
        )

        bind.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS cost_budgets (
                budget_id TEXT PRIMARY KEY,
                scope_kind TEXT NOT NULL,
                scope_key TEXT NOT NULL,
                daily_limit_usd DOUBLE PRECISION,
                monthly_limit_usd DOUBLE PRECISION,
                per_batch_limit_usd DOUBLE PRECISION,
                enabled BOOLEAN NOT NULL DEFAULT TRUE,
                created_by TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (scope_kind, scope_key)
            )
            """
        )
        bind.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_cost_budgets_scope "
            "ON cost_budgets(scope_kind, scope_key) WHERE enabled = TRUE"
        )

        bind.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS cost_alerts (
                alert_id TEXT PRIMARY KEY,
                scope_kind TEXT NOT NULL,
                scope_key TEXT NOT NULL,
                period TEXT NOT NULL,
                level TEXT NOT NULL,
                threshold_usd DOUBLE PRECISION NOT NULL,
                observed_usd DOUBLE PRECISION NOT NULL,
                action_taken TEXT NOT NULL,
                fired_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        bind.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_cost_alerts_fired_at "
            "ON cost_alerts(fired_at DESC)"
        )
        bind.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_cost_alerts_scope "
            "ON cost_alerts(scope_kind, scope_key, fired_at DESC)"
        )
    else:
        bind.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS cost_estimates (
                estimate_id TEXT PRIMARY KEY,
                call_id TEXT NOT NULL,
                model TEXT NOT NULL,
                is_batch INTEGER NOT NULL,
                input_tokens_estimated INTEGER,
                output_tokens_estimated INTEGER,
                cost_usd_estimated REAL NOT NULL,
                input_tokens_actual INTEGER,
                output_tokens_actual INTEGER,
                cache_read_tokens_actual INTEGER,
                cache_creation_tokens_actual INTEGER,
                cost_usd_actual REAL,
                workspace TEXT,
                priority TEXT,
                task_type TEXT,
                metadata TEXT,
                created_at TEXT NOT NULL,
                completed_at TEXT
            )
            """
        )
        bind.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_cost_estimates_call_id "
            "ON cost_estimates(call_id)"
        )
        bind.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_cost_estimates_created_at "
            "ON cost_estimates(created_at DESC)"
        )
        bind.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_cost_estimates_priority_created "
            "ON cost_estimates(priority, created_at DESC)"
        )

        bind.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS cost_budgets (
                budget_id TEXT PRIMARY KEY,
                scope_kind TEXT NOT NULL,
                scope_key TEXT NOT NULL,
                daily_limit_usd REAL,
                monthly_limit_usd REAL,
                per_batch_limit_usd REAL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_by TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (scope_kind, scope_key)
            )
            """
        )

        bind.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS cost_alerts (
                alert_id TEXT PRIMARY KEY,
                scope_kind TEXT NOT NULL,
                scope_key TEXT NOT NULL,
                period TEXT NOT NULL,
                level TEXT NOT NULL,
                threshold_usd REAL NOT NULL,
                observed_usd REAL NOT NULL,
                action_taken TEXT NOT NULL,
                fired_at TEXT NOT NULL
            )
            """
        )
        bind.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_cost_alerts_fired_at "
            "ON cost_alerts(fired_at DESC)"
        )
        bind.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_cost_alerts_scope "
            "ON cost_alerts(scope_kind, scope_key, fired_at DESC)"
        )


def downgrade() -> None:
    # Defensive no-op: dropping these tables would lose budget config
    # and alert history. Hand-rolled rollback required.
    pass
