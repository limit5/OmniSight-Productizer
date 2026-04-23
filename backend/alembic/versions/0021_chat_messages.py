"""Q.3-SUB-6 (#297, 2026-04-24) — chat_messages table.

Before this migration, ``backend/routers/chat.py`` stored chat
history in a module-global ``_history: list[OrchestratorMessage]``
(single per-worker Python list, cleared on process restart, invisible
across ``uvicorn --workers N``). Three consequences, covered by the
Q.3 cross-device audit (``docs/design/multi-device-state-sync.md``
Path 5):

1. A device on worker A never sees messages typed on worker B.
2. Any restart, rolling deploy, or replica recycle blanks the log.
3. ``lib/api.ts::getChatHistory()`` was an orphan consumer — the
   endpoint existed but no frontend surface mounted it because the
   list it returned was per-worker and thus misleading.

This migration adds a durable, per-user, tenant-scoped chat log:

Schema:
    id         TEXT PRIMARY KEY      — uuid generated in handler
    user_id    TEXT NOT NULL         — owning user; FK-soft to users(id)
    session_id TEXT NOT NULL DEFAULT ''
                                     — originator session; empty for
                                       bearer / api-key submissions
    role       TEXT NOT NULL         — 'user' | 'orchestrator' | 'system'
    content    TEXT NOT NULL
    timestamp  DOUBLE PRECISION NOT NULL
                                     — UNIX epoch seconds, same clock
                                       as sessions.created_at
    tenant_id  TEXT NOT NULL DEFAULT 't-default'
                                     — RLS scope, mirrors every other
                                       Q.3 per-tenant business table

Indexes:
    idx_chat_messages_user_ts        — the hot-path
                                       ``ORDER BY timestamp DESC``
                                       read after login + on SSE-push
                                       append.
    idx_chat_messages_timestamp      — supports the 30-day retention
                                       prune sweep.

Why DOUBLE PRECISION ``timestamp`` and not ``TIMESTAMPTZ``: the rest
of the Q.2 / Q.3 per-user tables (``sessions``, ``session_revocations``,
``session_fingerprints``) standardised on epoch-seconds so
``time.time()`` can flow straight from Python to PG without a tzinfo
dance; the retention sweep just compares ``<`` against
``time.time() - 30 * 86400``.

Retention: 30 days per user (hard-coded in
``backend/db.py::prune_chat_messages``). See SOP "列表 vs 資料源
drift-guard" — the prune keeps the PG row count bounded.

Revision ID: 0021
Revises: 0020
Create Date: 2026-04-24
"""
from __future__ import annotations

from alembic import op


revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    dialect = conn.dialect.name

    if dialect == "postgresql":
        conn.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS chat_messages (
                id          TEXT PRIMARY KEY,
                user_id     TEXT NOT NULL,
                session_id  TEXT NOT NULL DEFAULT '',
                role        TEXT NOT NULL,
                content     TEXT NOT NULL,
                timestamp   DOUBLE PRECISION NOT NULL,
                tenant_id   TEXT NOT NULL DEFAULT 't-default'
            )
            """
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_chat_messages_user_ts "
            "ON chat_messages(user_id, timestamp)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_chat_messages_timestamp "
            "ON chat_messages(timestamp)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_chat_messages_tenant "
            "ON chat_messages(tenant_id)"
        )
    else:
        conn.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS chat_messages (
                id          TEXT PRIMARY KEY,
                user_id     TEXT NOT NULL,
                session_id  TEXT NOT NULL DEFAULT '',
                role        TEXT NOT NULL,
                content     TEXT NOT NULL,
                timestamp   REAL NOT NULL,
                tenant_id   TEXT NOT NULL DEFAULT 't-default'
            )
            """
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_chat_messages_user_ts "
            "ON chat_messages(user_id, timestamp)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_chat_messages_timestamp "
            "ON chat_messages(timestamp)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_chat_messages_tenant "
            "ON chat_messages(tenant_id)"
        )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS chat_messages")
