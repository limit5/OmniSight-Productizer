"""γ-2 leg-3 — claude_memory_exports: the SERVER side of the dual pin.

Integrity-audit DECIDED scheme: one pin cannot cover both threats. This
append-only ledger (each regeneration writes {export_sha256, manifest}) is
the server-side pin — it defends against an ``~/.claude`` attacker (a local
pin file is rewritable by whoever rewrites MEMORY.md; the SERVER copy is
not). The CLIENT side (regenerator re-hashing each row's body_sha256
against the LOCAL topic file) defends against a backend attacker. Honest
guarantee: index integrity holds iff at least one side is uncompromised.

Revision ID: 0280
Revises: 0279
Create Date: 2026-07-22
"""
from __future__ import annotations

from alembic import op


revision = "0280"
down_revision = "0279"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return
    conn.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS claude_memory_exports (
            id UUID PRIMARY KEY,
            project_id TEXT NOT NULL,
            export_sha256 TEXT,
            manifest JSONB NOT NULL,
            requested_by TEXT NOT NULL,
            at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    for trg, op_kind in (("no_update", "UPDATE"), ("no_delete", "DELETE")):
        conn.exec_driver_sql(
            f"DROP TRIGGER IF EXISTS trg_claude_memory_exports_{trg} "
            "ON claude_memory_exports"
        )
        conn.exec_driver_sql(
            f"CREATE TRIGGER trg_claude_memory_exports_{trg} "
            f"BEFORE {op_kind} ON claude_memory_exports "
            "FOR EACH ROW EXECUTE FUNCTION claude_memory_ledger_block_mutation()"
        )
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_claude_memory_exports_at "
        "ON claude_memory_exports (project_id, at DESC)"
    )


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return
    conn.exec_driver_sql("DROP TABLE IF EXISTS claude_memory_exports")
