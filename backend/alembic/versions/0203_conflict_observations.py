"""OP-746 -- ``conflict_observations`` table for runner-pipeline observability.

Records every conflict-equivalent event the bridge sees in the Gerrit
stream so we can answer "is the conflict rate climbing or falling?"
across fleet changes (R1/R3/R4, lessons-learned restructure, etc.).

The table is append-only; the daily report (``scripts/conflict_report.py``)
reads the trailing 24h window plus a 7d window for the operator dashboard
tile (``backend/routers/conflict_dashboard.py``). No mutation API.

Cause categories
----------------
* ``sibling_merged`` -- another change merged on develop and the
  patchset stopped being mergeable; emitted from the OP-733 auto-rebase
  sweeper when it returns ``conflict=True``.
* ``verified_minus_one`` -- a Gerrit ``Verified -1`` vote landed
  (CI/Mergeable check failed); emitted by the ``comment-added``
  handler in ``gerrit_jira_bridge``.
* ``manual_rebase`` -- an out-of-band rebase recorded by the runner
  worktree fix path (R3). Currently unused by the bridge but reserved
  so a future hook does not need a migration.

Module-global / cross-worker state audit
----------------------------------------
Pure DDL migration -- no module-level singleton, no in-memory cache.
Writers are the bridge daemon (one process) and the auto-rebase sweeper
(same process). The daily report job runs as its own process and only
reads.

Read-after-write timing audit
-----------------------------
Inserts use the asyncpg pool's per-statement autocommit semantics
(``COMMIT`` after each ``execute``); subsequent SELECTs from the report
script see the committed rows. SQLite uses the alembic env's standard
attached engine for tests.

Production readiness gate
-------------------------
No new Python / OS package. The ``files_in_conflict`` column uses
``TEXT[]`` on Postgres; on SQLite (test mode) it falls back to a JSON
text encoding because SQLite lacks native arrays. The reader normalises
both shapes.

Revision ID: 0203
Revises: 0202
Create Date: 2026-05-08
"""
from __future__ import annotations

from alembic import op


revision = "0203"
down_revision = "0202"
branch_labels = None
depends_on = None


_PG_DDL = """
CREATE TABLE IF NOT EXISTS conflict_observations (
    id                      BIGSERIAL PRIMARY KEY,
    ts                      TIMESTAMPTZ NOT NULL DEFAULT now(),
    ps_change_id            TEXT,
    ps_change_number        INTEGER,
    ticket                  TEXT,
    files_in_conflict       TEXT[] NOT NULL DEFAULT '{}',
    cause_category          TEXT NOT NULL,
    pre_existing_open_count INTEGER NOT NULL DEFAULT 0
)
"""


_PG_INDEX_TS = """
CREATE INDEX IF NOT EXISTS idx_conflict_observations_ts
    ON conflict_observations (ts DESC)
"""


_PG_INDEX_CAUSE = """
CREATE INDEX IF NOT EXISTS idx_conflict_observations_cause_ts
    ON conflict_observations (cause_category, ts DESC)
"""


_SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS conflict_observations (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ps_change_id            TEXT,
    ps_change_number        INTEGER,
    ticket                  TEXT,
    files_in_conflict       TEXT NOT NULL DEFAULT '[]',
    cause_category          TEXT NOT NULL,
    pre_existing_open_count INTEGER NOT NULL DEFAULT 0
)
"""


_SQLITE_INDEX_TS = """
CREATE INDEX IF NOT EXISTS idx_conflict_observations_ts
    ON conflict_observations (ts DESC)
"""


_SQLITE_INDEX_CAUSE = """
CREATE INDEX IF NOT EXISTS idx_conflict_observations_cause_ts
    ON conflict_observations (cause_category, ts DESC)
"""


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.exec_driver_sql(_PG_DDL)
        bind.exec_driver_sql(_PG_INDEX_TS)
        bind.exec_driver_sql(_PG_INDEX_CAUSE)
    else:
        bind.exec_driver_sql(_SQLITE_DDL)
        bind.exec_driver_sql(_SQLITE_INDEX_TS)
        bind.exec_driver_sql(_SQLITE_INDEX_CAUSE)


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(
        "DROP INDEX IF EXISTS idx_conflict_observations_cause_ts"
    )
    bind.exec_driver_sql(
        "DROP INDEX IF EXISTS idx_conflict_observations_ts"
    )
    bind.exec_driver_sql("DROP TABLE IF EXISTS conflict_observations")
