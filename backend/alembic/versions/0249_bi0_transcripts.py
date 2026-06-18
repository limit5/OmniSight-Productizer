"""OP-2238 BI0 -- ``meetings`` + ``transcript_segments`` tables.

Revision ID: 0249
Revises: 0248
Create Date: 2026-06-18
backwards-compat: safe

Greenfield tables for the on-board MI lane's TEXT-only transcript
ingest API. Designed for the ingest contract documented in OP-2231:

* ``meetings`` -- one row per conference meeting, tenant-scoped. The
  envelope GET projects this row plus an aggregate over the segments
  table. Rows are auto-created on first segment ingest if no explicit
  POST /meetings preceded it (router picks auto-create -- see
  ``backend/routers/transcripts.py``).
* ``transcript_segments`` -- append-only ASR segment store keyed on
  ``(tenant_id, meeting_id, session_id, segment_seq)`` so a reconnect
  replay from the on-board ASR (same session) deduplicates against the
  UNIQUE constraint. ``segment_seq`` is monotonic per
  ``(tenant_id, meeting_id)`` -- the router enforces ordering on read
  via ``ORDER BY segment_seq`` so an out-of-order arrival is still
  safe.

Idempotency / replay semantics live in the router (INSERT ... ON
CONFLICT ... DO UPDATE WHERE NOT is_final, plus a final-then-partial
reject rule). The migration just lays down the constraints and indexes
the router needs to make those primitives O(1).

Why a plain ``op.execute(CREATE TABLE IF NOT EXISTS ...)`` rather than
``op.create_table``: matches the house pattern used by 0036 / 0035 /
0033 etc. (raw DDL through the ``alembic_pg_compat`` shim), keeps
this migration idempotent on re-run, and keeps the CHECK / UNIQUE
constraints inline with the DDL.

Module-global / cross-worker state audit
----------------------------------------
Pure DDL migration -- no module-level singleton, no in-memory cache.
Every uvicorn worker reads the same DDL state from PG once the
migration commits.

Production readiness gate
-------------------------
No new Python / OS package. New tables for the BI0 ingest API; the
router and tests are added in the same commit but stay TEXT-only --
NO audio handling, NO summary/translation/extraction (those are BI1-4).
"""
from __future__ import annotations

from alembic import op


revision = "0249"
down_revision = "0248"
branch_labels = None
depends_on = None


_CREATE_MEETINGS = """
CREATE TABLE IF NOT EXISTS meetings (
    id            TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    title         TEXT,
    status        TEXT NOT NULL DEFAULT 'open',
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL,
    CHECK (status IN ('open', 'closed'))
)
"""


_CREATE_TRANSCRIPT_SEGMENTS = """
CREATE TABLE IF NOT EXISTS transcript_segments (
    id              TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    meeting_id      TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    segment_seq     BIGINT NOT NULL,
    start_ms        BIGINT,
    end_ms          BIGINT,
    text            TEXT NOT NULL,
    language        TEXT,
    confidence      REAL,
    is_final        BOOLEAN NOT NULL,
    source          TEXT NOT NULL,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    UNIQUE (tenant_id, meeting_id, session_id, segment_seq)
)
"""


_INDEXES = (
    # Tenant-scoped scan for the meetings list endpoint + DSAR sweep.
    "CREATE INDEX IF NOT EXISTS idx_meetings_tenant "
    "ON meetings(tenant_id)",
    # Tenant-scoped scan over transcript_segments (BI0b DSAR / retention).
    "CREATE INDEX IF NOT EXISTS idx_transcript_segments_tenant "
    "ON transcript_segments(tenant_id)",
    # The hot ordered read path: GET /meetings/{id}/segments?since_seq=...
    # The UNIQUE composite (tenant_id, meeting_id, session_id,
    # segment_seq) cannot serve this query shape efficiently because
    # session_id sits between meeting_id and segment_seq -- a query
    # filtering only on (tenant_id, meeting_id) and ordering by
    # segment_seq would skip the leading-prefix optimisation. This
    # index covers it.
    "CREATE INDEX IF NOT EXISTS idx_transcript_segments_tenant_meeting_seq "
    "ON transcript_segments(tenant_id, meeting_id, segment_seq)",
)


def upgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql(_CREATE_MEETINGS)
    conn.exec_driver_sql(_CREATE_TRANSCRIPT_SEGMENTS)
    for stmt in _INDEXES:
        conn.exec_driver_sql(stmt)


def downgrade() -> None:
    conn = op.get_bind()
    for idx in (
        "idx_transcript_segments_tenant_meeting_seq",
        "idx_transcript_segments_tenant",
        "idx_meetings_tenant",
    ):
        conn.exec_driver_sql(f"DROP INDEX IF EXISTS {idx}")
    op.execute("DROP TABLE IF EXISTS transcript_segments")
    op.execute("DROP TABLE IF EXISTS meetings")
