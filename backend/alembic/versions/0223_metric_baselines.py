"""OP-879 D7 -- ``metric_baselines`` table for smoke baseline comparator.

backwards-compat: safe

Stores the latency p50/p95/p99 baseline for the fixed set of
post-deploy smoke endpoints (``scripts/smoke_baseline_compare.py``).
One row per endpoint path. ``scripts/smoke_baseline_compare.py`` reads
the table to compute the warn/abort decision and re-snapshots rows on
operator-approved promote.

Column rationale
----------------
* ``endpoint`` -- the route path (e.g. ``/health``). Primary key so a
  re-snapshot of an existing endpoint is an UPSERT rather than a
  duplicate row.
* ``p50_ms`` / ``p95_ms`` / ``p99_ms`` -- captured latency percentiles
  in milliseconds. DOUBLE on PG, REAL on SQLite. The comparator only
  triggers warn/abort off ``p95_ms`` per the OP-879 AC, but p50/p99 are
  recorded so an operator investigating an abort has the surrounding
  distribution without having to re-run.
* ``sample_count`` -- how many requests fed the percentile computation.
  Drives a "too few samples to trust" warning at compare time.
* ``deployment_tag`` -- the image tag / SHA the baseline was captured
  against. Surfaces in the abort SSE so the operator can correlate.
* ``captured_at`` -- ISO timestamp of the snapshot.

Why no hash chain
-----------------
Baselines are mutable by design (operator-approved promote
re-snapshots) so an append-only chain would be the wrong primitive.
Tamper resistance for the *deploy* that wrote a given baseline lives
in ``deploy_audit`` (alembic 0204), which records the operator action
that triggered the re-snapshot.

Module-global / cross-worker state audit
----------------------------------------
Pure DDL migration -- no module-level singleton. The only writers are
``scripts/smoke_baseline_compare.py`` (bootstrap + operator-approved
re-snapshot). Readers are the comparator itself and any operator
diagnostic query.

Spec note on slot numbering
---------------------------
The OP-879 description gestures at ``0223`` as the migration slot.
The live ``versions/`` directory has two heads at the moment -- ``0207``
(release_audit) and ``0221`` (release_milestones), both descended from
``0206``. The next sequential slot is ``0223``; we revise from
``0221`` (the higher-numbered sibling) since this migration is
independent of both ``0207`` and ``0221``. A separate merge migration
is not needed because the comparator only touches ``metric_baselines``.

Revision ID: 0223
Revises: 0221
Create Date: 2026-05-11
"""
from __future__ import annotations

from alembic import op


revision = "0223"
down_revision = "0221"
branch_labels = None
depends_on = None


_PG_DDL = """
CREATE TABLE IF NOT EXISTS metric_baselines (
    endpoint        TEXT PRIMARY KEY,
    p50_ms          DOUBLE PRECISION NOT NULL,
    p95_ms          DOUBLE PRECISION NOT NULL,
    p99_ms          DOUBLE PRECISION NOT NULL,
    sample_count    INTEGER NOT NULL DEFAULT 0
                          CHECK (sample_count >= 0),
    deployment_tag  TEXT NOT NULL DEFAULT '',
    captured_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT metric_baselines_p50_chk CHECK (p50_ms >= 0),
    CONSTRAINT metric_baselines_p95_chk CHECK (p95_ms >= 0),
    CONSTRAINT metric_baselines_p99_chk CHECK (p99_ms >= 0)
)
"""


_PG_INDEX_CAPTURED = """
CREATE INDEX IF NOT EXISTS idx_metric_baselines_captured_at
    ON metric_baselines (captured_at DESC)
"""


_SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS metric_baselines (
    endpoint        TEXT PRIMARY KEY,
    p50_ms          REAL NOT NULL,
    p95_ms          REAL NOT NULL,
    p99_ms          REAL NOT NULL,
    sample_count    INTEGER NOT NULL DEFAULT 0
                          CHECK (sample_count >= 0),
    deployment_tag  TEXT NOT NULL DEFAULT '',
    captured_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT metric_baselines_p50_chk CHECK (p50_ms >= 0),
    CONSTRAINT metric_baselines_p95_chk CHECK (p95_ms >= 0),
    CONSTRAINT metric_baselines_p99_chk CHECK (p99_ms >= 0)
)
"""


_SQLITE_INDEX_CAPTURED = """
CREATE INDEX IF NOT EXISTS idx_metric_baselines_captured_at
    ON metric_baselines (captured_at DESC)
"""


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.exec_driver_sql(_PG_DDL)
        bind.exec_driver_sql(_PG_INDEX_CAPTURED)
    else:
        bind.exec_driver_sql(_SQLITE_DDL)
        bind.exec_driver_sql(_SQLITE_INDEX_CAPTURED)


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_metric_baselines_captured_at")
    bind.exec_driver_sql("DROP TABLE IF EXISTS metric_baselines")
