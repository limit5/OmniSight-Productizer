"""proposed_actions: decision_reason + executed_at — durable P5 decision/exec audit.

Audit r3: (codex) the operator's required approve/reject reason was enforced but
not PERSISTED — only in the HTTP response; and (workflow RS-2) the execution-result
write clobbered ``decided_at`` with the completion time, losing WHEN the human
actually decided. This adds two nullable columns:
  * ``decision_reason`` — written by decide_proposed_action in the same atomic CAS.
  * ``executed_at``     — written by set_proposed_action_result, leaving decided_at
    as the durable operator-decision timestamp.
Pure additive DDL (nullable, no backfill) → low-risk deploy; guarded so a re-run
is a no-op on PG and SQLite alike.
"""
from __future__ import annotations

from alembic import op


revision = "0257"
down_revision = "0256"
branch_labels = None
depends_on = None

_COLUMNS = (("decision_reason", "TEXT"), ("executed_at", "REAL"))


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        cols = [r[1] for r in bind.exec_driver_sql("PRAGMA table_info(proposed_actions)").fetchall()]
        for name, typ in _COLUMNS:
            if name not in cols:
                bind.exec_driver_sql(f"ALTER TABLE proposed_actions ADD COLUMN {name} {typ}")
    else:
        for name, typ in _COLUMNS:
            bind.exec_driver_sql(f"ALTER TABLE proposed_actions ADD COLUMN IF NOT EXISTS {name} {typ}")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":  # SQLite can't DROP COLUMN on old versions
        for name, _ in _COLUMNS:
            bind.exec_driver_sql(f"ALTER TABLE proposed_actions DROP COLUMN IF EXISTS {name}")
