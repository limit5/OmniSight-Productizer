"""β-4 leg-2 — lifecycle substrate: citations (append-only) + utility rollup.

Audit-decided: ``learned_item_citations`` is a LEDGER (append-only triggers
reusing 0258's ``learned_item_ledger_block_mutation()``; 0258's pattern is
per-table opt-in — confirmed it does NOT auto-apply) with
``UNIQUE(version_id, ticket_key)`` = structural anti-inflation (any hit
volume collapses to 1 row/version/ticket). ``learned_item_utility`` is an
UPSERT rollup by design (NO append-only triggers) — a HUMAN-ONLY display
signal (audit C3: no code path may gate on it). PG-only (0275 pattern; the
join source ``curator_merge_candidates`` is PG-only).

Revision ID: 0278
Revises: 0277
Create Date: 2026-07-22
"""
from __future__ import annotations

from alembic import op


revision = "0278"
down_revision = "0277"
branch_labels = None
depends_on = None

_DDL = (
    """
    CREATE TABLE IF NOT EXISTS learned_item_citations (
        id         UUID PRIMARY KEY,
        version_id UUID NOT NULL REFERENCES learned_item_versions(id),
        ticket_key TEXT NOT NULL,
        cited_by   TEXT NOT NULL DEFAULT '',
        cited_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT learned_item_citations_ticket_chk
            CHECK (ticket_key ~ '^[A-Z][A-Z0-9_]*-[0-9]+$')
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_learned_item_citation "
    "ON learned_item_citations (version_id, ticket_key)",
    "CREATE INDEX IF NOT EXISTS idx_learned_item_citations_cited_at "
    "ON learned_item_citations (cited_at)",
    "DROP TRIGGER IF EXISTS trg_learned_item_citations_no_update "
    "ON learned_item_citations",
    "CREATE TRIGGER trg_learned_item_citations_no_update "
    "BEFORE UPDATE ON learned_item_citations "
    "FOR EACH ROW EXECUTE FUNCTION learned_item_ledger_block_mutation()",
    "DROP TRIGGER IF EXISTS trg_learned_item_citations_no_delete "
    "ON learned_item_citations",
    "CREATE TRIGGER trg_learned_item_citations_no_delete "
    "BEFORE DELETE ON learned_item_citations "
    "FOR EACH ROW EXECUTE FUNCTION learned_item_ledger_block_mutation()",
    """
    CREATE TABLE IF NOT EXISTS learned_item_utility (
        version_id    UUID PRIMARY KEY REFERENCES learned_item_versions(id),
        hits          INTEGER NOT NULL DEFAULT 0,
        successes     INTEGER NOT NULL DEFAULT 0,
        window_days   INTEGER NOT NULL,
        baseline_rate DOUBLE PRECISION,
        updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
)


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return
    for stmt in _DDL:
        conn.exec_driver_sql(stmt)


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return
    conn.exec_driver_sql("DROP TABLE IF EXISTS learned_item_utility")
    conn.exec_driver_sql("DROP TABLE IF EXISTS learned_item_citations")
