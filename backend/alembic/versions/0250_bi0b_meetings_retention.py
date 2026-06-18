"""OP-2239 BI0b -- ``meetings.retention_until`` retention deadline column.

Revision ID: 0250
Revises: 0249
Create Date: 2026-06-18
backwards-compat: safe

Adds a nullable ``retention_until`` column to the ``meetings`` table laid
down by 0249 (OP-2238). The column is the wall-clock epoch (REAL) at or
after which a meeting -- and its dependent ``transcript_segments`` -- is
eligible for deletion by the BI0b retention sweep
(``backend.privacy_retention.sweep_expired_meetings``).

Why a column and not a separate ``meetings_retention`` table:
* meetings are 1:1 with their retention deadline (no historical multi-row
  shape worth modelling); and
* the sweep is a single indexed range DELETE, so the column-on-the-row
  shape gives PG the best execution plan with zero join cost.

Why nullable + a per-tenant default applied at write time (router /
sweep) rather than ``NOT NULL DEFAULT $WINDOW`` here:
* the retention window is a runtime config knob (``OMNISIGHT_BI0_TRANSCRIPT_
  RETENTION_S``), so embedding it in a server-default would freeze the
  value on the row even after the operator rotates the env knob;
* a NULL ``retention_until`` is the explicit "no deadline" sentinel --
  the sweep DELETE filter is ``retention_until IS NOT NULL AND
  retention_until < $now`` so a NULL row is never swept.

Idempotency: the ``add_column`` calls are guarded by an existence probe
(mirrors 0248 ``api_keys.expires_at`` precedent) so re-running the
migration on a partially-applied DB is a no-op rather than an error. The
secondary index uses ``CREATE INDEX IF NOT EXISTS`` for the same reason.

Module-global / cross-worker state audit
----------------------------------------
Pure DDL migration -- no module-level singleton, no in-memory cache.
Every uvicorn worker reads the same DDL state from PG once the migration
commits.

Production readiness gate
-------------------------
No new Python / OS package. Single nullable column + single secondary
index on the existing tenant table. TEXT-only contract (OP-2238)
unchanged.
"""
from __future__ import annotations

from alembic import op


revision = "0250"
down_revision = "0249"
branch_labels = None
depends_on = None


_RETENTION_COLUMN = "retention_until"


def _meetings_has_column(bind, column: str) -> bool:
    if bind.dialect.name == "postgresql":
        rows = bind.exec_driver_sql(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'meetings'"
        ).fetchall()
        return column in {row[0] for row in rows}
    rows = bind.exec_driver_sql("PRAGMA table_info(meetings)").fetchall()
    return column in {row[1] for row in rows}


def upgrade() -> None:
    bind = op.get_bind()
    if not _meetings_has_column(bind, _RETENTION_COLUMN):
        # REAL to match the existing ``created_at`` / ``updated_at`` column
        # convention on ``meetings`` (epoch seconds as float).
        bind.exec_driver_sql(
            "ALTER TABLE meetings ADD COLUMN retention_until REAL"
        )
    # The sweep query shape is
    #   DELETE FROM meetings
    #   WHERE retention_until IS NOT NULL AND retention_until < $now
    # so a single-column index on retention_until gives the planner an
    # indexed range scan rather than a tenant-wide seq scan. The sweep
    # additionally filters by tenant_id, but the discriminating predicate
    # is the deadline range -- index the discriminator.
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_meetings_retention_until "
        "ON meetings(retention_until)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(
        "DROP INDEX IF EXISTS idx_meetings_retention_until"
    )
    if _meetings_has_column(bind, _RETENTION_COLUMN):
        # SQLite < 3.35 cannot drop columns; we run on 3.35+ in CI but
        # guard the path so the migration is safe even on older test
        # boxes (just leaves the column behind on downgrade -- harmless
        # nullable column).
        try:
            op.drop_column("meetings", _RETENTION_COLUMN)
        except Exception:
            pass
