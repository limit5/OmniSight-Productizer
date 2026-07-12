"""OP-2611 U6-0 T8-B1 — atomic idempotency for verified merge solutions.

A partial UNIQUE index over ``gerrit_change_id`` restricted to
``verified AND source='service_gerrit_merge'`` rows, so "one verified
row per merged change" is a DB invariant rather than a raceable
SELECT-then-INSERT. The verified-write path (T8-B2) inserts via
``ON CONFLICT (gerrit_change_id) WHERE verified AND
source='service_gerrit_merge' DO NOTHING RETURNING id`` and only does
post-insert work when a row is returned — so a duplicate/replayed
``change-merged`` webhook cannot create a second verified row.

The predicate is deliberately partial: quarantined rows (``verified``
FALSE) and other sources may still share a ``gerrit_change_id`` (e.g. a
``model_save_solution`` row that cites the same change).

SQLite branch: no-op here; ``backend/db.py::_SCHEMA``/``_migrate()`` add
the same partial unique index for dev/test.

Revision ID: 0261
Revises: 0260
Create Date: 2026-07-12
"""
from __future__ import annotations

from alembic import op


revision = "0261"
down_revision = "0260"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return
    conn.exec_driver_sql(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_episodic_verified_merge "
        "ON episodic_memory (gerrit_change_id) "
        "WHERE verified AND source = 'service_gerrit_merge'"
    )


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return
    conn.exec_driver_sql("DROP INDEX IF EXISTS uq_episodic_verified_merge")
