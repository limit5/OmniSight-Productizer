"""OP-2697 U6-2b — L2 chat_session_summaries store (DORMANT).

Add the write-once ``chat_session_summaries`` table the L2 session-end writer
(also this revision, dormant) will write. Each row is a structured, allowlisted
session outcome (U6-2a ``summary_outcome``) + provenance that survives the 30-day
``chat_messages`` prune (``source_message_hashes``). The exactly-once job key is
``UNIQUE (tenant_id, user_id, session_id, source_watermark)``; late turns produce
a NEW revision (a new watermark = a new row), never an in-place mutate — enforced
by a BEFORE UPDATE trigger. L2 is write-only + UI-only in v1 (NOT injected, §2.C).

PostgreSQL is authoritative. SQLite gets its deliberate subset from
``backend/db.py::_SCHEMA`` because the dev SQLite path does not run alembic.

Additive and dormant: no producer or consumer is wired by this revision (the
U6-8 scheduler wires the writer later). Rollback drops the trigger, index, table.

Revision ID: 0272
Revises: 0271
Create Date: 2026-07-19
backwards-compat: safe
"""
from __future__ import annotations

from alembic import op

revision = "0272"
down_revision = "0271"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return

    conn.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS chat_session_summaries (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL REFERENCES tenants(id),
            user_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            source_watermark TEXT NOT NULL,
            source_message_hashes JSONB NOT NULL DEFAULT '[]'::jsonb,
            summary_outcome JSONB NOT NULL DEFAULT '{}'::jsonb,
            token_count INTEGER NOT NULL DEFAULT 0,
            model_fingerprint TEXT NOT NULL DEFAULT '',
            classifier_version INTEGER NOT NULL DEFAULT 0,
            renderer_version INTEGER NOT NULL DEFAULT 0,
            revision INTEGER NOT NULL DEFAULT 0,
            session_end_reason TEXT NOT NULL
                CHECK (session_end_reason IN ('inactivity_timeout', 'explicit_close')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_chat_session_summaries_exactly_once
                UNIQUE (tenant_id, user_id, session_id, source_watermark)
        )
        """
    )
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_chat_session_summaries_user "
        "ON chat_session_summaries (tenant_id, user_id, created_at DESC)"
    )
    # write-once: late turns get a NEW revision (new row), never an in-place mutate.
    conn.exec_driver_sql(
        """
        CREATE OR REPLACE FUNCTION trg_chat_session_summaries_no_update_fn()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION
                'ChatSessionSummaryImmutable: write-once; late turns get a new revision';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    conn.exec_driver_sql(
        "DROP TRIGGER IF EXISTS trg_chat_session_summaries_no_update "
        "ON chat_session_summaries"
    )
    conn.exec_driver_sql(
        """
        CREATE TRIGGER trg_chat_session_summaries_no_update
            BEFORE UPDATE ON chat_session_summaries
            FOR EACH ROW EXECUTE FUNCTION trg_chat_session_summaries_no_update_fn()
        """
    )


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return
    conn.exec_driver_sql(
        "DROP TRIGGER IF EXISTS trg_chat_session_summaries_no_update "
        "ON chat_session_summaries"
    )
    conn.exec_driver_sql("DROP FUNCTION IF EXISTS trg_chat_session_summaries_no_update_fn()")
    conn.exec_driver_sql("DROP INDEX IF EXISTS idx_chat_session_summaries_user")
    conn.exec_driver_sql("DROP TABLE IF EXISTS chat_session_summaries")
