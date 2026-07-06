"""proposed_actions — Sora P5 propose-and-approve gate for dangerous actions.

P5 lets the Sora orchestrator handle "dangerous" operations (deploy / promote /
restart / rollback) WITHOUT ever executing them autonomously. Instead Sora files
a PROPOSAL here (status='pending') with a human-readable preview + blast-radius;
a human must APPROVE it before anything runs, and execution always goes through
the existing reversible machinery (release-train scripts / systemctl), never raw
shell. This table is the durable backbone of that gate — proposals MUST survive a
backend restart / power loss (a human may approve minutes later), so it is
Postgres-durable like orchestrator_tasks, not in-memory.

Safety: this migration is pure additive DDL (a new table, no data change), so the
deploy is low-risk. The propose side (Sora) and the approve side (operator) are
decoupled through the ``status`` state machine:
  pending → approved → executing → executed | failed
  pending → rejected
  pending/approved → canceled

Portability: TEXT + REAL (epoch seconds), app-set timestamps (no server default)
so the DDL is identical on PG (prod) and SQLite (dev/test). ``IF NOT EXISTS`` so a
re-run is a no-op.
"""
from __future__ import annotations

from alembic import op


revision = "0256"
down_revision = "0255"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS proposed_actions (
            id            TEXT PRIMARY KEY,
            tenant_id     TEXT NOT NULL DEFAULT '',
            user_id       TEXT NOT NULL DEFAULT '',
            session_id    TEXT NOT NULL DEFAULT '',
            action_kind   TEXT NOT NULL,
            params        TEXT NOT NULL DEFAULT '{}',
            title         TEXT NOT NULL DEFAULT '',
            preview       TEXT NOT NULL DEFAULT '',
            blast_radius  TEXT NOT NULL DEFAULT '',
            status        TEXT NOT NULL DEFAULT 'pending',
            proposed_by   TEXT NOT NULL DEFAULT '',
            proposed_at   REAL NOT NULL DEFAULT 0,
            decided_by    TEXT,
            decided_at    REAL,
            result        TEXT
        )
        """
    )
    # The operator UI + Sora's list tool scan by status (pending first).
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_proposed_actions_status "
        "ON proposed_actions(status, proposed_at)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("DROP TABLE IF EXISTS proposed_actions")
