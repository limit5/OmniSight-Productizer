"""OP-854 (C2) — ``runner_incidents`` table.

Persists runner-side failure incidents tagged with a closed
``failure_class`` taxonomy (see ``backend/agents/failure_class.py``).
C6 (OP-856) and C8 (OP-858) already reference this table — the C6
memory-recall audit shim writes rows to it, and the C8 failure-graph
projects rows into in-memory dataclasses for cross-incident causality.
This migration finally backs both with a durable surface.

Column layout (per OP-854 AC #2):

* ``incident_id`` — UUID hex PK so writers do not need to coordinate
  with the database for primary-key assignment.
* ``ticket_key`` — JIRA ticket the incident is attached to. Always
  populated (the runner has a ticket context when it raises).
* ``failure_class`` — closed enum from ``failure_class.FailureClass``;
  the CHECK constraint mirrors the Python enum so a typo in the writer
  surfaces as a constraint violation rather than a silent ``OTHER``
  coercion at recall time.
* ``summary`` — operator-visible one-liner.
* ``raw_traceback`` — full stderr / traceback; rebuild source for
  re-classification if the enum ever gains new members.
* ``runner_class`` — which runner emitted it (``subscription-claude``,
  ``subscription-codex``, etc.) — used by the C8 graph for fleet-burst
  detection and by C6 cross-fleet recall.
* ``mutex_label`` — file-coordinator mutex held at incident time, if
  any. Drives the C8 ``same_mutex_window`` edge.
* ``area`` — recognised JIRA area label (``backend`` / ``frontend`` /
  ...). Nullable for ``UNKNOWN_AREA_LABEL`` rows. The C2 recall path
  joins on (failure_class, area) so this column needs an index.
* ``created_at`` — write-time timestamp.

Why date is forward-only (per spec recovery/rollback section)
-------------------------------------------------------------
``runner_incidents`` is the source of truth for the C8 failure graph;
dropping the table truncates the operator's incident-review window.
The downgrade therefore exists for symmetry but operators are expected
to migrate forward only — see ``docs/operations/replay-deprecation.md``
for the recovery procedure (idempotent re-classification from
``raw_traceback``).

Module-global / cross-worker state audit
----------------------------------------
Pure DDL migration — no module-level singleton. Writers are the runner
(one row per failure incident) and the memory-recall policy gate (one
row per recall decision, ``failure_class='MEMORY_RECALL_AUDIT'``).
Readers are the C8 failure-graph builder and the C2 recall path; both
are read-only.

Spec note: the OP-854 description gestures at ``0220`` as the migration
slot but the live ``versions/`` directory is at ``0205`` head, so the
next sequential slot is ``0206``. The slot number does not affect
behaviour — it is purely the alembic linkage ID.

Revision ID: 0206
Revises: 0205
Create Date: 2026-05-11
"""
from __future__ import annotations

from alembic import op


revision = "0206"
down_revision = "0205"
branch_labels = None
depends_on = None


# Mirrors ``backend.agents.failure_class.FailureClass`` enum members.
# Co-update both when the taxonomy changes (the unit-test suite asserts
# the two stay in sync).
_FAILURE_CLASS_LITERAL = (
    "'LINT_FAILURE','TEST_FAILURE','MERGE_CONFLICT','WORKTREE_DIRTY',"
    "'LLM_LOOP_DETECTED','UNKNOWN_AREA_LABEL','BRIDGE_DESYNC','RUNNER_TIMEOUT',"
    "'MUTEX_CONTENTION','OUTCOMES_GRADER_REFUSED','OTHER','MEMORY_RECALL_AUDIT'"
)


# ── Postgres DDL ────────────────────────────────────────────────────


_PG_TABLE_DDL = f"""
CREATE TABLE IF NOT EXISTS runner_incidents (
    incident_id     TEXT PRIMARY KEY,
    ticket_key      TEXT NOT NULL,
    failure_class   TEXT NOT NULL,
    summary         TEXT NOT NULL DEFAULT '',
    raw_traceback   TEXT NOT NULL DEFAULT '',
    runner_class    TEXT NOT NULL DEFAULT 'unknown',
    mutex_label     TEXT,
    area            TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT runner_incidents_failure_class_chk
        CHECK (failure_class IN ({_FAILURE_CLASS_LITERAL}))
)
"""


_PG_INDEX_TICKET = """
CREATE INDEX IF NOT EXISTS idx_runner_incidents_ticket_key
    ON runner_incidents (ticket_key, created_at DESC)
"""


_PG_INDEX_CLASS_AREA = """
CREATE INDEX IF NOT EXISTS idx_runner_incidents_class_area
    ON runner_incidents (failure_class, area, created_at DESC)
"""


_PG_INDEX_MUTEX = """
CREATE INDEX IF NOT EXISTS idx_runner_incidents_mutex
    ON runner_incidents (mutex_label, created_at DESC)
    WHERE mutex_label IS NOT NULL
"""


# ── SQLite DDL ──────────────────────────────────────────────────────


_SQLITE_TABLE_DDL = f"""
CREATE TABLE IF NOT EXISTS runner_incidents (
    incident_id     TEXT PRIMARY KEY,
    ticket_key      TEXT NOT NULL,
    failure_class   TEXT NOT NULL,
    summary         TEXT NOT NULL DEFAULT '',
    raw_traceback   TEXT NOT NULL DEFAULT '',
    runner_class    TEXT NOT NULL DEFAULT 'unknown',
    mutex_label     TEXT,
    area            TEXT,
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT runner_incidents_failure_class_chk
        CHECK (failure_class IN ({_FAILURE_CLASS_LITERAL}))
)
"""


_SQLITE_INDEX_TICKET = """
CREATE INDEX IF NOT EXISTS idx_runner_incidents_ticket_key
    ON runner_incidents (ticket_key, created_at DESC)
"""


_SQLITE_INDEX_CLASS_AREA = """
CREATE INDEX IF NOT EXISTS idx_runner_incidents_class_area
    ON runner_incidents (failure_class, area, created_at DESC)
"""


_SQLITE_INDEX_MUTEX = """
CREATE INDEX IF NOT EXISTS idx_runner_incidents_mutex
    ON runner_incidents (mutex_label, created_at DESC)
"""


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.exec_driver_sql(_PG_TABLE_DDL)
        bind.exec_driver_sql(_PG_INDEX_TICKET)
        bind.exec_driver_sql(_PG_INDEX_CLASS_AREA)
        bind.exec_driver_sql(_PG_INDEX_MUTEX)
    else:
        bind.exec_driver_sql(_SQLITE_TABLE_DDL)
        bind.exec_driver_sql(_SQLITE_INDEX_TICKET)
        bind.exec_driver_sql(_SQLITE_INDEX_CLASS_AREA)
        bind.exec_driver_sql(_SQLITE_INDEX_MUTEX)


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_runner_incidents_mutex")
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_runner_incidents_class_area")
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_runner_incidents_ticket_key")
    bind.exec_driver_sql("DROP TABLE IF EXISTS runner_incidents")
