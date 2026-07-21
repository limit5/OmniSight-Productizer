"""β-F leg-2 — curator_merge_candidates ledger (DORMANT).

The durable (ticket ↔ merged Gerrit change) link the worker-loop curator
polls. Written at verified-merge time
(``webhooks._save_merged_solution_to_l3``) ONLY on a real, non-replay
insert of the episodic row — the ONE place the owning OP-key ↔
change-number link provably exists (the commit subject carries ``[OP-N]``;
the event carries the number). β-0's curator will SELECT
``WHERE NOT distilled AND merged_at > watermark`` — a cheap watermarked
scan — instead of re-scanning JIRA + Gerrit per tick.

Additive + quarantining: nothing reads this table until β-0 ships. PG only;
dev SQLite is a deliberate no-op subset (the curator runs on the
serving/PG path, like the U6 memory scheduler).

``revert_state`` exists so β-1's distiller can EXCLUDE reverted changes (a
revert is itself a merged, human-+2 change that passes verify); β-F seeds
it from the commit subject (``Revert "…"``), β-1 enriches it from the
ticket's ``runner-stoploss:*`` labels.

Revision ID: 0275
Revises: 0274
Create Date: 2026-07-20
"""
from __future__ import annotations

from alembic import op


revision = "0275"
down_revision = "0274"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        # Dev SQLite has no curator table — the curator is PG/serving only.
        return

    conn.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS curator_merge_candidates (
            id                TEXT PRIMARY KEY,
            ticket_key        TEXT,
            gerrit_change     INTEGER NOT NULL,
            change_id         TEXT NOT NULL,
            canonical_subject TEXT NOT NULL DEFAULT '',
            plus2_reviewer    TEXT,
            revert_state      TEXT NOT NULL DEFAULT 'none',
            tenant_id         TEXT NOT NULL DEFAULT 'omnisight-self',
            merged_at         TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            distilled         BOOLEAN NOT NULL DEFAULT FALSE,
            distilled_at      TIMESTAMPTZ
        )
        """
    )

    # revert_state domain — dropped-then-added so re-runs stay idempotent.
    conn.exec_driver_sql(
        "ALTER TABLE curator_merge_candidates DROP CONSTRAINT IF EXISTS "
        "curator_merge_candidates_revert_state_check"
    )
    conn.exec_driver_sql(
        "ALTER TABLE curator_merge_candidates ADD CONSTRAINT "
        "curator_merge_candidates_revert_state_check "
        "CHECK (revert_state IN ('none', 'reverted'))"
    )

    # Idempotent source key: one candidate per merged change. The ledger
    # write is already gated on the episodic insert returning a real row,
    # so this is belt-and-braces against a cross-replica double-write.
    conn.exec_driver_sql(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_curator_merge_candidate_change "
        "ON curator_merge_candidates (gerrit_change)"
    )
    # The curator's hot scan: undistilled candidates since a watermark.
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_curator_merge_candidates_undistilled "
        "ON curator_merge_candidates (distilled, merged_at)"
    )


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return
    conn.exec_driver_sql("DROP TABLE IF EXISTS curator_merge_candidates")
