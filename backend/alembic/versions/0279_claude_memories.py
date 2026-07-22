"""γ-0 leg-3 — claude_memories substrate: Claude's file-store, governed.

Audit-decided (wiring F1-F6): THREE tables — an APPEND-ONLY version ledger,
the ONE mutable head/state table (the ``learned_item_snapshots`` sanctioned-
mutable precedent; carries the MEMORY.md-harvested ``rank`` + hand-curated
``hook`` + the blocking/advisory ``lint`` split), and an append-only
transition-events ledger. OWN trigger function — NEVER 0258's
``learned_item_ledger_block_mutation`` (its own downgrade drops it; the 0259
lesson). No ``%`` in exec_driver_sql'd plpgsql except escaped (%% lesson).

Neither existing lane hosts this corpus: l3_facts is 64-char closed-value;
learned_item_versions has the FROZEN 8KB A2 cap + imperative-phrase bans (2
corpus files trip ``_TOOL_POLICY_RE`` today). Bodies ingest VERBATIM
(1MB abuse cap only; the 264KB handoff file is a top-starred READ-FIRST —
refusing it would be a payoff regression).

The publish-gate trigger is defense-in-depth per the 0259 doctrine: the REAL
gate is the human-only publish endpoint (γ-1). PG-only; dev SQLite exercises
the code gates.

Revision ID: 0279
Revises: 0278
Create Date: 2026-07-22
"""
from __future__ import annotations

from alembic import op


revision = "0279"
down_revision = "0278"
branch_labels = None
depends_on = None

_DDL = (
    """
    CREATE TABLE IF NOT EXISTS claude_memory_versions (
        id UUID PRIMARY KEY,
        project_id TEXT NOT NULL,
        slug TEXT NOT NULL,
        revision INT NOT NULL,
        title TEXT NOT NULL,
        description TEXT,
        mem_type TEXT NOT NULL
            CHECK (mem_type IN ('project','feedback','reference','user')),
        body TEXT NOT NULL CHECK (octet_length(body) <= 1048576),
        body_sha256 TEXT NOT NULL CHECK (body_sha256 ~ '^[0-9a-f]{64}$'),
        links JSONB NOT NULL DEFAULT '[]',
        origin_session_id TEXT,
        created_by TEXT NOT NULL,
        ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (project_id, slug, revision)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS claude_memory_state (
        project_id TEXT NOT NULL,
        slug TEXT NOT NULL,
        head_version_id UUID NOT NULL REFERENCES claude_memory_versions(id),
        published_version_id UUID REFERENCES claude_memory_versions(id),
        state TEXT NOT NULL DEFAULT 'quarantined'
            CHECK (state IN ('quarantined','published','revoked')),
        rank INT,
        hook TEXT,
        lint JSONB NOT NULL DEFAULT '{}',
        last_seen_at TIMESTAMPTZ,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (project_id, slug)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS claude_memory_transition_events (
        id UUID PRIMARY KEY,
        project_id TEXT NOT NULL,
        slug TEXT NOT NULL,
        version_id UUID,
        from_state TEXT,
        to_state TEXT NOT NULL,
        actor TEXT NOT NULL,
        at TIMESTAMPTZ NOT NULL DEFAULT now(),
        reason TEXT
    )
    """,
    # OWN append-only blocker (F1 — never reuse 0258's; %% escaped).
    """
    CREATE OR REPLACE FUNCTION claude_memory_ledger_block_mutation()
    RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION
            'claude-memory ledger rows are append-only (%%s blocked)', TG_OP;
    END;
    $$ LANGUAGE plpgsql
    """,
    "DROP TRIGGER IF EXISTS trg_claude_memory_versions_no_update "
    "ON claude_memory_versions",
    "CREATE TRIGGER trg_claude_memory_versions_no_update "
    "BEFORE UPDATE ON claude_memory_versions "
    "FOR EACH ROW EXECUTE FUNCTION claude_memory_ledger_block_mutation()",
    "DROP TRIGGER IF EXISTS trg_claude_memory_versions_no_delete "
    "ON claude_memory_versions",
    "CREATE TRIGGER trg_claude_memory_versions_no_delete "
    "BEFORE DELETE ON claude_memory_versions "
    "FOR EACH ROW EXECUTE FUNCTION claude_memory_ledger_block_mutation()",
    "DROP TRIGGER IF EXISTS trg_claude_memory_events_no_update "
    "ON claude_memory_transition_events",
    "CREATE TRIGGER trg_claude_memory_events_no_update "
    "BEFORE UPDATE ON claude_memory_transition_events "
    "FOR EACH ROW EXECUTE FUNCTION claude_memory_ledger_block_mutation()",
    "DROP TRIGGER IF EXISTS trg_claude_memory_events_no_delete "
    "ON claude_memory_transition_events",
    "CREATE TRIGGER trg_claude_memory_events_no_delete "
    "BEFORE DELETE ON claude_memory_transition_events "
    "FOR EACH ROW EXECUTE FUNCTION claude_memory_ledger_block_mutation()",
    # Publish-gate (0259 doctrine: defense-in-depth; the human endpoint is
    # the real gate): published requires a bound published_version_id and a
    # clean BLOCKING lint set.
    """
    CREATE OR REPLACE FUNCTION claude_memory_publish_gate()
    RETURNS trigger AS $$
    BEGIN
        IF NEW.state = 'published' THEN
            IF NEW.published_version_id IS NULL
               OR COALESCE(NEW.lint -> 'blocking', '[]'::jsonb) <> '[]'::jsonb
            THEN
                RAISE EXCEPTION
                    'ClaudeMemoryPublishGate: published requires a bound '
                    'version and empty blocking lint';
            END IF;
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """,
    "DROP TRIGGER IF EXISTS trg_claude_memory_state_publish_gate "
    "ON claude_memory_state",
    "CREATE TRIGGER trg_claude_memory_state_publish_gate "
    "BEFORE INSERT OR UPDATE ON claude_memory_state "
    "FOR EACH ROW EXECUTE FUNCTION claude_memory_publish_gate()",
    "CREATE INDEX IF NOT EXISTS idx_claude_memory_state_rank "
    "ON claude_memory_state (project_id, state, rank)",
)


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        # PG-only substrate (0275/0277 pattern); code gates carry on SQLite.
        return
    for stmt in _DDL:
        conn.exec_driver_sql(stmt)


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return
    for stmt in (
        "DROP TABLE IF EXISTS claude_memory_transition_events",
        "DROP TABLE IF EXISTS claude_memory_state",
        "DROP TABLE IF EXISTS claude_memory_versions",
        "DROP FUNCTION IF EXISTS claude_memory_publish_gate()",
        "DROP FUNCTION IF EXISTS claude_memory_ledger_block_mutation()",
    ):
        conn.exec_driver_sql(stmt)
