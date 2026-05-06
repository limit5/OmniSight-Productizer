"""MP.W1.2a -- ``provider_usage_event`` table.

Per-usage event log for provider quota rolling-window calculations.  Later
MP quota tracker rows can compute SQL-driven windows from this source of
truth, for example ``WHERE ts > now() - interval '5 hours'`` and 7-day
variants, while ``provider_quota_state`` keeps cached sums and circuit state
for routing decisions.

Events older than 30 days can be pruned by a separate operator cron row; this
migration only creates the event log schema and lookup indexes.

No RLS policy is created here: this table stores global per-provider
control-plane state, mirroring ``provider_quota_state``.  Tenant-scoped usage
records must use their own tenant-bound tables and policies.

Module-global / cross-worker state audit
----------------------------------------
Pure DDL migration -- no module-level singleton, no in-memory cache.
Every worker reads the same DDL state from PG / SQLite once the
migration commits.  Answer #1 of SOP Step 1.

Read-after-write timing audit
-----------------------------
The CREATE TABLE and indexes happen inside the alembic transaction.  Runtime
writers are introduced by later MP rows, so this migration does not add a new
read-after-write timing assumption.

Production readiness gate
-------------------------
No new Python / OS package.  New table added for later deployed-inactive
MP quota tracker work.  Production status of this commit: dev-only; next
gate is deployed-inactive once alembic 0200 is applied to production.

Revision ID: 0200
Revises: 0199
Create Date: 2026-05-06
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0200"
down_revision = "0199"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "provider_usage_event",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("tokens", sa.BigInteger(), nullable=False),
        sa.Column(
            "ts",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("agent_id", sa.Text(), nullable=True),
        sa.Column("correlation_id", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "tokens >= 0",
            name="provider_usage_event_tokens_nonneg",
        ),
    )
    op.create_index(
        "idx_provider_usage_event_provider_ts",
        "provider_usage_event",
        ["provider", sa.text("ts DESC")],
    )
    op.create_index(
        "idx_provider_usage_event_ts",
        "provider_usage_event",
        [sa.text("ts DESC")],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_provider_usage_event_ts",
        table_name="provider_usage_event",
    )
    op.drop_index(
        "idx_provider_usage_event_provider_ts",
        table_name="provider_usage_event",
    )
    op.drop_table("provider_usage_event")
