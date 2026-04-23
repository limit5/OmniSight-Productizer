"""Q.6 #300 (2026-04-24, checkbox 1) — user_drafts table.

Per-user, per-slot ephemeral composer text that survives a tab close
or device switch. The Q.6 cross-device audit
(``docs/design/multi-device-state-sync.md``) found two surfaces where
operators were losing in-flight typing on accidental refresh / device
switch / tunnel hiccup:

  1. ``components/omnisight/invoke-core.tsx`` — the bottom INVOKE
     command bar. Until now, the half-typed string lives only in
     React local state on one device.
  2. ``components/omnisight/workspace-chat.tsx`` — the workspace
     chat composer (web / mobile / software workspaces all share it).

Schema:
    user_id     TEXT NOT NULL    — owning user; FK-soft to users(id)
    slot_key    TEXT NOT NULL    — ``invoke:main`` / ``chat:main``
                                   (future extension: ``chat:<thread_id>``)
    content     TEXT NOT NULL DEFAULT ''
                                  — the in-flight draft text. May be
                                    empty (operator cleared the input);
                                    the row itself going away is left
                                    to the 24 h GC sweep (checkbox 3).
    updated_at  DOUBLE PRECISION NOT NULL
                                  — UNIX epoch seconds, same clock as
                                    sessions / chat_messages / etc. so
                                    the GC sweep just compares ``<``.
    tenant_id   TEXT NOT NULL DEFAULT 't-default'
                                  — RLS scope, mirrors every other Q.3
                                    per-user table.

Primary key: (user_id, slot_key) so the natural conflict on PUT is
``ON CONFLICT (user_id, slot_key) DO UPDATE``.

Why DOUBLE PRECISION ``updated_at``: matches the convention set by
``chat_messages.timestamp`` and ``sessions.last_seen_at`` — epoch
seconds flow straight from ``time.time()`` to PG without a tzinfo
dance, and the 24 h GC just compares against ``time.time() - 86400``.

Why no FK on ``user_id``: the rest of the per-user ephemeral tables
in this codebase (``user_preferences`` does FK; ``chat_messages``
deliberately does not) lean on tenant scoping + the periodic GC
rather than a hard FK so a user-row delete during a typing burst
does not cascade-blow the in-flight draft. Q.6 follows the
``chat_messages`` precedent.

Revision ID: 0022
Revises: 0021
Create Date: 2026-04-24
"""
from __future__ import annotations

from alembic import op


revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    dialect = conn.dialect.name

    if dialect == "postgresql":
        conn.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS user_drafts (
                user_id     TEXT NOT NULL,
                slot_key    TEXT NOT NULL,
                content     TEXT NOT NULL DEFAULT '',
                updated_at  DOUBLE PRECISION NOT NULL,
                tenant_id   TEXT NOT NULL DEFAULT 't-default',
                PRIMARY KEY (user_id, slot_key)
            )
            """
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_user_drafts_updated_at "
            "ON user_drafts(updated_at)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_user_drafts_tenant "
            "ON user_drafts(tenant_id)"
        )
    else:
        conn.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS user_drafts (
                user_id     TEXT NOT NULL,
                slot_key    TEXT NOT NULL,
                content     TEXT NOT NULL DEFAULT '',
                updated_at  REAL NOT NULL,
                tenant_id   TEXT NOT NULL DEFAULT 't-default',
                PRIMARY KEY (user_id, slot_key)
            )
            """
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_user_drafts_updated_at "
            "ON user_drafts(updated_at)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_user_drafts_tenant "
            "ON user_drafts(tenant_id)"
        )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS user_drafts")
