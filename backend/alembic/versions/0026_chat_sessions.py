"""ZZ.B2 #304-2 checkbox 1 — chat_sessions table.

Persists one row per chat-session hash so the left-sidebar workflow/chat
list can display an LLM-generated descriptive title instead of raw
``session_id`` hashes.

Schema:
    session_id  TEXT NOT NULL          — originator session hash
                                         (matches ``chat_messages.session_id``)
    user_id     TEXT NOT NULL          — owning user (RLS inside tenant)
    tenant_id   TEXT NOT NULL DEFAULT 't-default'
    metadata    JSONB/TEXT NOT NULL DEFAULT '{}'
                                        — ``{auto_title?, user_title?, ...}``.
                                          ``auto_title`` is LLM-generated from
                                          first 3 condensed turns; ``user_title``
                                          is operator-set and takes precedence.
    created_at  DOUBLE PRECISION NOT NULL
    updated_at  DOUBLE PRECISION NOT NULL

PK: (session_id, user_id, tenant_id) — tenants are first-class; two
tenants could in principle (though unlikely) share the same
``session_id`` hash. Scoping by (session_id, user_id, tenant_id) also
gives the ``ON CONFLICT`` target for upsert on every chat write.

Why JSONB on PG, TEXT on SQLite: keeps parity with ``bootstrap_state``
which stores its metadata as TEXT-of-JSON so the SQLite dev path
(still in compat-wrapper mode) doesn't need JSONB support. Readers
decode at the python layer either way.

Revision ID: 0026
Revises: 0025
Create Date: 2026-04-24
"""
from __future__ import annotations

from alembic import op


revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    dialect = conn.dialect.name

    if dialect == "postgresql":
        conn.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS chat_sessions (
                session_id  TEXT NOT NULL,
                user_id     TEXT NOT NULL,
                tenant_id   TEXT NOT NULL DEFAULT 't-default',
                metadata    JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at  DOUBLE PRECISION NOT NULL,
                updated_at  DOUBLE PRECISION NOT NULL,
                PRIMARY KEY (session_id, user_id, tenant_id)
            )
            """
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_chat_sessions_user_updated "
            "ON chat_sessions(user_id, updated_at DESC)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_chat_sessions_tenant "
            "ON chat_sessions(tenant_id)"
        )
    else:
        conn.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS chat_sessions (
                session_id  TEXT NOT NULL,
                user_id     TEXT NOT NULL,
                tenant_id   TEXT NOT NULL DEFAULT 't-default',
                metadata    TEXT NOT NULL DEFAULT '{}',
                created_at  REAL NOT NULL,
                updated_at  REAL NOT NULL,
                PRIMARY KEY (session_id, user_id, tenant_id)
            )
            """
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_chat_sessions_user_updated "
            "ON chat_sessions(user_id, updated_at DESC)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_chat_sessions_tenant "
            "ON chat_sessions(tenant_id)"
        )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS chat_sessions")
