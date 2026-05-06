"""MP.W1.3 -- ``provider_quota_state`` table.

Persistent global quota state for provider-level routing decisions.  The
MP quota tracker and orchestrator rows write one record per provider id
(``anthropic-subscription``, ``openai-subscription`` and future
providers), keeping the rolling token windows and circuit breaker state
outside process memory.

No RLS policy is created here: this table stores global per-provider
control-plane state, not tenant-owned data.  Tenant-scoped quota or usage
records must use their own tenant-bound tables and policies.

Module-global / cross-worker state audit
----------------------------------------
Pure DDL migration -- no module-level singleton, no in-memory cache.
Every worker reads the same DDL state from PG / SQLite once the
migration commits.  Answer #1 of SOP Step 1.

Read-after-write timing audit
-----------------------------
The CREATE TABLE happens inside the alembic transaction.  Runtime writers
are introduced by later MP rows, so this migration does not add a new
read-after-write timing assumption.

Production readiness gate
-------------------------
No new Python / OS package.  New table added for later deployed-inactive
MP quota tracker work.  Production status of this commit: dev-only; next
gate is deployed-inactive once alembic 0199 is applied to production.

Revision ID: 0199
Revises: 0198
Create Date: 2026-05-06
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0199"
down_revision = "0198"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "provider_quota_state",
        sa.Column("provider", sa.Text(), primary_key=True),
        sa.Column(
            "rolling_5h_tokens",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "weekly_tokens",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("last_reset_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_cap_hit_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "circuit_state",
            sa.Text(),
            nullable=False,
            server_default="closed",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "circuit_state IN ('closed', 'open', 'half_open')",
            name="provider_quota_state_circuit_state_check",
        ),
    )


def downgrade() -> None:
    op.drop_table("provider_quota_state")
