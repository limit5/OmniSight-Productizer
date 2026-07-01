"""orchestrator_tasks — user↔ticket links for chat-filed Stories (Gap C).

The orchestrator's ``create_task`` tool files a JIRA Story on the user's
behalf, but the platform kept no record of WHICH user/session asked for
WHICH ticket. So when the runner finished the work (ticket → Under Review,
Gerrit change pushed) nothing surfaced back to the user in the UI — the
delivery loop was open (dogfood 2026-07-01, OP-2495).

This table closes it: ``create_task`` records ``(tenant_id, user_id,
session_id, ticket_key, …)`` and a background delivery poller
(``backend.orchestrator_delivery``) watches the open rows, and when a
ticket reaches a review/done state it posts an orchestrator chat message
into that user's session (``chat_messages`` + ``chat.message`` SSE) so the
user learns "✅ your task is done" without leaving the UI.

Portability: TEXT + REAL (epoch seconds, matching the meetings/chat_messages
convention); app-set timestamps (no server default, so the DDL is identical
on PG (prod) and SQLite (dev/test)). ``CREATE TABLE/INDEX IF NOT EXISTS`` so
a re-run is a no-op.

Module-global / cross-worker audit: pure DDL, no singleton. The poller
claims each row atomically (UPDATE … WHERE status='open' RETURNING) so the
dual backend replicas never double-deliver.
"""
from __future__ import annotations

from alembic import op


revision = "0252"
down_revision = "0251"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS orchestrator_tasks (
            id               TEXT PRIMARY KEY,
            tenant_id        TEXT NOT NULL DEFAULT '',
            user_id          TEXT NOT NULL,
            session_id       TEXT NOT NULL DEFAULT '',
            ticket_key       TEXT NOT NULL,
            title            TEXT NOT NULL DEFAULT '',
            area             TEXT NOT NULL DEFAULT '',
            browse_url       TEXT NOT NULL DEFAULT '',
            status           TEXT NOT NULL DEFAULT 'open',
            last_jira_status TEXT NOT NULL DEFAULT '',
            filed_at         REAL NOT NULL DEFAULT 0,
            delivered_at     REAL
        )
        """
    )
    # One ticket → one orchestrator_tasks row (create_task is idempotent per
    # key; a re-file of the same ticket is an upsert, not a duplicate).
    bind.exec_driver_sql(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_orchestrator_tasks_ticket "
        "ON orchestrator_tasks(ticket_key)"
    )
    # The poller scans by status='open'; users' own lists scan by user_id.
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_orchestrator_tasks_status "
        "ON orchestrator_tasks(status)"
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_orchestrator_tasks_user "
        "ON orchestrator_tasks(user_id)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("DROP TABLE IF EXISTS orchestrator_tasks")
