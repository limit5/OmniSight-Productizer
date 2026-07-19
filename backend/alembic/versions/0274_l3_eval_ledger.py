"""OP-2701 U6-5b — per-user L3 eval/approval ledger (DORMANT).

Persist the U6-5a memory-safety eval decision for an L3 candidate fact. This is a
NEW per-user ledger (design §2.D: "L3 is NOT the U4 append-only global ledger"), so
it does NOT reuse the U4 ``memory_eval_runs`` (which FKs the global
``learned_item_versions``); it MIRRORS its ``eval_kind`` / ``decision`` vocabulary,
keyed on ``l3_facts`` + ``(tenant_id, user_id)``. ``reasons`` is content-free (the
diverging probe names), never fact content — the eval run is auditable metadata that
may outlive a crypto-shredded fact.

REAL FORCED row-level security keyed on ``app.tenant_id`` / ``app.user_id`` (same as
0273; effective under a non-superuser app role). ``fact_id`` is a SOFT reference (no
FK) so an eval run survives its fact's hard-erase as content-free audit.

The publish / promote / revoke path + the ``/memories`` confirm UX is U6-6; this
increment persists the eval + the user approval only. Additive + dormant: no
producer/consumer wired (U6-5a produces the decision, U6-6 confirms/publishes).
Rollback drops policies + tables.

Revision ID: 0274
Revises: 0273
Create Date: 2026-07-19
backwards-compat: safe
"""
from __future__ import annotations

from alembic import op

revision = "0274"
down_revision = "0273"
branch_labels = None
depends_on = None

_DECISIONS = "('promote', 'reject', 'insufficient_evidence', 'infra_invalid')"


def _rls(table: str) -> None:
    conn = op.get_bind()
    conn.exec_driver_sql(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    conn.exec_driver_sql(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    conn.exec_driver_sql(f"DROP POLICY IF EXISTS {table}_scope ON {table}")
    conn.exec_driver_sql(
        f"""
        CREATE POLICY {table}_scope ON {table}
            USING (tenant_id = current_setting('app.tenant_id', true)
                   AND user_id = current_setting('app.user_id', true))
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true)
                       AND user_id = current_setting('app.user_id', true))
        """
    )


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return

    conn.exec_driver_sql(
        f"""
        CREATE TABLE IF NOT EXISTS l3_eval_runs (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL REFERENCES tenants(id),
            user_id TEXT NOT NULL,
            fact_id TEXT NOT NULL,
            eval_kind TEXT NOT NULL CHECK (eval_kind IN ('memory_safety')),
            decision TEXT NOT NULL CHECK (decision IN {_DECISIONS}),
            reasons JSONB NOT NULL DEFAULT '[]'::jsonb,
            ran_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_l3_eval_runs_fact "
        "ON l3_eval_runs (tenant_id, user_id, fact_id)"
    )
    conn.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS l3_approvals (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL REFERENCES tenants(id),
            user_id TEXT NOT NULL,
            eval_run_id TEXT NOT NULL REFERENCES l3_eval_runs(id),
            fact_id TEXT NOT NULL,
            approved_by TEXT NOT NULL,
            approved_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_l3_approvals_fact "
        "ON l3_approvals (tenant_id, user_id, fact_id)"
    )
    _rls("l3_eval_runs")
    _rls("l3_approvals")


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return
    for table in ("l3_approvals", "l3_eval_runs"):
        conn.exec_driver_sql(f"DROP POLICY IF EXISTS {table}_scope ON {table}")
    conn.exec_driver_sql("DROP INDEX IF EXISTS idx_l3_approvals_fact")
    conn.exec_driver_sql("DROP INDEX IF EXISTS idx_l3_eval_runs_fact")
    conn.exec_driver_sql("DROP TABLE IF EXISTS l3_approvals")
    conn.exec_driver_sql("DROP TABLE IF EXISTS l3_eval_runs")
