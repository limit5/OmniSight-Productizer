"""β-1 leg-2 — curator_merge_candidates.patchset_count (nullable, additive).

The hard-ticket gate's struggle signal: the merged change's current patchset
NUMBER, captured at verified-merge time by β-F's writer (SSH
``currentPatchSet.number`` / REST ``revisions[current]._number``). Nullable:
historical rows (and payloads lacking the field) stay NULL, which the β-1
gate treats as UNKNOWN (not ≥N) — the incidents arm still catches those.

Revision ID: 0276
Revises: 0275
Create Date: 2026-07-21
"""
from __future__ import annotations

from alembic import op


revision = "0276"
down_revision = "0275"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        # PG-only substrate (0275 pattern) — the curator runs on serving/PG.
        return
    conn.exec_driver_sql(
        "ALTER TABLE curator_merge_candidates "
        "ADD COLUMN IF NOT EXISTS patchset_count INTEGER"
    )


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return
    conn.exec_driver_sql(
        "ALTER TABLE curator_merge_candidates "
        "DROP COLUMN IF EXISTS patchset_count"
    )
