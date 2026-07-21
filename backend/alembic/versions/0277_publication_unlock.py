"""β-3b leg-2 — publication UNLOCK: 0259 relaxation + approvals bind gate + index.

Audit-decided (GO-WITH-CONDITIONS, all folded): BOTH branches (promote AND
insufficient_evidence) require a clean bound IN-REPO run PLUS a clean
HOLDOUT-marked sibling (F7: the answer-key attack rides promote); the
approvals bind gate is the DB-invariant backstop for the β-3a bearer-token
code checks (holdout rows exempt from latest, NOT from reject); the
(version_id, ran_at) index serves every gate query. No holdout configured ⇒
no holdout rows ⇒ nothing approvable/publishable — fail-closed.

Revision ID: 0277
Revises: 0276
Create Date: 2026-07-21
"""
from __future__ import annotations

from alembic import op


revision = "0277"
down_revision = "0276"
branch_labels = None
depends_on = None

_CLEAN = (
    "(x.stat_summary -> 'neg_control_catches' IS NULL "
    "OR x.stat_summary -> 'neg_control_catches' = '[]'::jsonb)"
)

_PG_GATE = """
CREATE OR REPLACE FUNCTION learned_item_publication_gate()
RETURNS trigger AS $$
BEGIN
    IF NEW.state IN ('publishing', 'published') THEN
        IF NEW.approval_id IS NULL OR NOT EXISTS (
            SELECT 1
            FROM memory_approvals a
            JOIN memory_eval_runs e ON e.id = a.eval_run_id
            WHERE a.id = NEW.approval_id
              AND a.version_id = NEW.version_id
              AND COALESCE(e.stat_summary ->> 'holdout', '') <> 'true'
              AND e.decision IN ('promote', 'insufficient_evidence')
              AND (e.stat_summary -> 'neg_control_catches' IS NULL
                   OR e.stat_summary -> 'neg_control_catches' = '[]'::jsonb)
              AND EXISTS (
                  SELECT 1 FROM memory_eval_runs h
                  WHERE h.version_id = NEW.version_id
                    AND h.stat_summary ->> 'holdout' = 'true'
                    AND h.decision IN ('promote', 'insufficient_evidence')
                    AND h.ran_at >= e.ran_at
                    AND (h.stat_summary -> 'neg_control_catches' IS NULL
                         OR h.stat_summary -> 'neg_control_catches' = '[]'::jsonb)
              )
        ) THEN
            RAISE EXCEPTION
                'PublicationGate: %% requires a clean bound run plus a clean holdout run',
                NEW.state;
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

_PG_BIND = """
CREATE OR REPLACE FUNCTION memory_approvals_bind_gate()
RETURNS trigger AS $$
DECLARE
    bound_ran_at  TIMESTAMPTZ;
    bound_holdout TEXT;
BEGIN
    SELECT e.ran_at, COALESCE(e.stat_summary ->> 'holdout', '')
      INTO bound_ran_at, bound_holdout
      FROM memory_eval_runs e
     WHERE e.id = NEW.eval_run_id AND e.version_id = NEW.version_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ApprovalBindGate: eval run does not belong to version';
    END IF;
    IF bound_holdout = 'true' THEN
        RAISE EXCEPTION 'ApprovalBindGate: cannot bind the holdout leg';
    END IF;
    IF EXISTS (
        SELECT 1 FROM memory_eval_runs r
         WHERE r.version_id = NEW.version_id
           AND r.id <> NEW.eval_run_id
           AND r.ran_at >= bound_ran_at
           AND COALESCE(r.decision, '') <> 'infra_invalid'
           AND COALESCE(r.stat_summary ->> 'holdout', '') <> 'true'
    ) THEN
        RAISE EXCEPTION 'ApprovalBindGate: bound run is not the latest non-infra run';
    END IF;
    IF EXISTS (
        SELECT 1 FROM memory_eval_runs r
         WHERE r.version_id = NEW.version_id
           AND r.id <> NEW.eval_run_id
           AND r.ran_at >= bound_ran_at
           AND r.decision = 'reject'
    ) THEN
        RAISE EXCEPTION 'ApprovalBindGate: a later or tied reject exists';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

# Restores the 0259 promote-only predicate (pre-relaxation).
_PG_GATE_0259 = """
CREATE OR REPLACE FUNCTION learned_item_publication_gate()
RETURNS trigger AS $$
BEGIN
    IF NEW.state IN ('publishing', 'published') THEN
        IF NEW.approval_id IS NULL OR NOT EXISTS (
            SELECT 1
            FROM memory_approvals a
            JOIN memory_eval_runs e ON e.id = a.eval_run_id
            WHERE a.id = NEW.approval_id
              AND a.version_id = NEW.version_id
              AND e.decision = 'promote'
        ) THEN
            RAISE EXCEPTION
                'PublicationGate: %% requires an approval bound to a promote eval run',
                NEW.state;
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        # Dev SQLite subset: the code-gate checks in learned_item_approval
        # carry the same predicate; the sqlite 0259 trigger stays (stricter,
        # promote-only) — publication tests on sqlite exercise the code
        # path, prod PG gets the full trigger pair.
        return
    conn.exec_driver_sql(_PG_GATE)
    conn.exec_driver_sql(_PG_BIND)
    conn.exec_driver_sql(
        "DROP TRIGGER IF EXISTS trg_memory_approvals_bind_gate ON memory_approvals"
    )
    conn.exec_driver_sql(
        "CREATE TRIGGER trg_memory_approvals_bind_gate "
        "BEFORE INSERT ON memory_approvals "
        "FOR EACH ROW EXECUTE FUNCTION memory_approvals_bind_gate()"
    )
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_memory_eval_runs_version_ran_at "
        "ON memory_eval_runs (version_id, ran_at DESC)"
    )


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return
    conn.exec_driver_sql(
        "DROP INDEX IF EXISTS ix_memory_eval_runs_version_ran_at"
    )
    conn.exec_driver_sql(
        "DROP TRIGGER IF EXISTS trg_memory_approvals_bind_gate ON memory_approvals"
    )
    conn.exec_driver_sql("DROP FUNCTION IF EXISTS memory_approvals_bind_gate()")
    conn.exec_driver_sql(_PG_GATE_0259)
