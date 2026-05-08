"""OP-735 R5 -- per-(bot, file_class) AI Reviewer trust score table.

Adds the ``trust_scores`` table backing
``backend.agents.trust_scoring``. The table stores running
``successes`` / ``failures`` counters per (bot, file_class) tuple so
the auto-+1 gate can decide whether to keep delegating spot-checks to
that bot for that class of files.

Module-global / cross-worker state audit
----------------------------------------
Pure DDL migration -- no module-level singleton, no in-memory cache.
The ``PgTrustScoreStore`` instance held in ``trust_scoring`` reads
from this table on every call (no read-through cache); cross-worker
visibility is whatever the PG transaction commit ordering gives, which
is sufficient because the gate is advisory (worst case: one extra
auto-+1 fires before a freshly-recorded failure propagates).

Read-after-write timing audit
-----------------------------
``record_outcome`` upserts inside the caller's pool transaction; the
next ``get`` from the same worker sees the updated row immediately.
For cross-worker visibility, the in-place UPDATE commits and is
visible to every subsequent SELECT (PG ReadCommitted default).

Production readiness gate
-------------------------
No new Python / OS package. New table only -- existing rows are
untouched. The composite primary key (bot, file_class) is the natural
identity for the ON CONFLICT path used by the upsert; no separate
sequence column is needed.

Revision ID: 0202
Revises: 0201
Create Date: 2026-05-08
"""
from __future__ import annotations

from alembic import op


revision = "0202"
down_revision = "0201"
branch_labels = None
depends_on = None


_PG_DDL = """
CREATE TABLE IF NOT EXISTS trust_scores (
    bot           TEXT NOT NULL,
    file_class    TEXT NOT NULL,
    successes     INTEGER NOT NULL DEFAULT 0,
    failures      INTEGER NOT NULL DEFAULT 0,
    last_updated  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (bot, file_class)
)
"""


_SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS trust_scores (
    bot           TEXT NOT NULL,
    file_class    TEXT NOT NULL,
    successes     INTEGER NOT NULL DEFAULT 0,
    failures      INTEGER NOT NULL DEFAULT 0,
    last_updated  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (bot, file_class)
)
"""


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.exec_driver_sql(_PG_DDL)
    else:
        bind.exec_driver_sql(_SQLITE_DDL)


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("DROP TABLE IF EXISTS trust_scores")
