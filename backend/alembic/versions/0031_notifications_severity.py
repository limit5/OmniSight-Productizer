"""R9 row 2935 (#315) — notifications.severity tag column.

Adds the operational-priority tag column described in
:mod:`backend.severity` to the ``notifications`` table.

Why a NULLable column rather than a separate priority table
────────────────────────────────────────────────────────────
The TODO row 2935 design decision is "*do not* introduce a separate
P1/P2/P3 routing tier — make it a tag on top of L1-L4". Persisting
the tag as a sibling column on ``notifications`` keeps the canonical
join path identical for legacy callers (``SELECT * FROM
notifications WHERE level = ...`` still works), while severity-aware
callers gain a single nullable predicate (``AND severity = 'P1'``).
A separate ``notification_severity`` join table would split a 1:1
relationship across two rows for no semantic gain.

Why NULLable
────────────
Forty-plus existing call-sites of :func:`backend.notifications.notify`
across agents / watchdog / chatops do not pass severity. Forcing
``NOT NULL`` would either break those call-sites at runtime or force
a backfill choice (default to ``P3``? all → ``P2``?) that has no
semantically defensible answer — legacy notifications genuinely
*lack* a P-tag and the dispatcher must fall back to plain level
routing. NULL is the honest representation of "no severity
specified".

Dialect handling
────────────────
SQLite (dev) takes its notifications schema from
``backend.db._SCHEMA`` + ``_migrate()``; alembic is the *PG* canonical
path (per the comment in 0017_episodic_memory_tsvector). The
SQLite-side ``severity TEXT`` column is added via the migration
table in ``db.py`` (search for ``"R9 row 2935"``); this migration is
a no-op on SQLite to stay symmetric with the 0017 pattern.

Drift guard alignment
─────────────────────
``backend/db.py::_migrate`` adds the same column for the SQLite dev
path via ALTER TABLE ADD COLUMN; ``backend/db.py::_SCHEMA`` carries
``severity TEXT`` for fresh dev DBs. The three drift sources stay in
lockstep so any one going missing surfaces via SOP Step 4 drift-guard
tests (``test_migrator_schema_coverage``-style).

Revision ID: 0031
Revises: 0030
Create Date: 2026-04-25
"""
from __future__ import annotations

from alembic import op


revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    is_pg = conn.dialect.name == "postgresql"

    if not is_pg:
        # SQLite owns its notifications schema via backend.db._SCHEMA
        # + _migrate() (per the same convention 0017 follows for
        # SQLite-incompatible features). No-op here.
        return

    conn.exec_driver_sql(
        "ALTER TABLE notifications ADD COLUMN IF NOT EXISTS severity TEXT"
    )
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_notifications_severity "
        "ON notifications (severity) WHERE severity IS NOT NULL"
    )


def downgrade() -> None:
    conn = op.get_bind()
    is_pg = conn.dialect.name == "postgresql"

    if not is_pg:
        return

    op.execute("DROP INDEX IF EXISTS idx_notifications_severity")
    op.execute("ALTER TABLE notifications DROP COLUMN IF EXISTS severity")
