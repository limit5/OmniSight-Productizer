"""OP-948 H3 — ``release_state`` table for per-version conductor state.

Backwards-compat: safe (additive, no FK out)

The Sprint H conductor (event-driven release scheduler) needs durable,
per-``RELEASE-vX.Y.Z`` state so the L3 layer can answer "what stage is
``v0.5.1-rc1`` in right now?" without reconstructing the answer from
the JIRA child graph on every webhook tick. The G1 runbook still
treats the JIRA graph as the canonical state machine
(`docs/operations/release-conductor-runbook.md` §0); ``release_state``
is the *cached projection* + append-only audit trail of every
transition the H2 dispatchers apply.

Schema rationale
----------------
* ``release_id`` — the JIRA META key (e.g. ``OP-1234``). Kept text so
  the table doesn't take a hard dependency on a JIRA-ticket lookup
  table that doesn't exist; the (JIRA, conductor) coupling is one-way
  (conductor reads JIRA), so a plain TEXT column is enough.
* ``version`` — the SemVer (``vX.Y.Z`` / ``vX.Y.Z-rcN``) the META
  represents. ``UNIQUE`` so the conductor cannot accidentally fork two
  rows for the same release; if a duplicate-instantiation slip
  happens upstream (per G1 §6 ``--force`` semantics) the INSERT fails
  loudly rather than silently bifurcating state.
* ``state`` — closed enum enforced by ``CHECK`` (AC #2):
  ``pending, building, staging, canary_5, canary_25, canary_100,
  done, failed, rolled_back``. A bad string never lands in the DB; a
  typo in ``state_machine.transition()`` fails the constraint, not
  the wire format.
* ``row_version`` — optimistic-locking counter for
  ``RaceConditionDoubleTransition`` (AC error catalog). ``transition``
  bumps it with ``WHERE state = :from AND row_version = :prev``; a
  concurrent writer racing for the same transition lands 0 rows
  updated and surfaces the race.
* ``last_transition_at`` — UTC timestamp of the most recent transition
  (mirrors the tail entry of ``transition_log_json``). Bumped on every
  transition; the bare ``created_at`` stays pinned to row birth.
* ``transition_log_json`` — append-only **JSON Lines** column (one
  ``{from, to, reason, at}`` object per ``\n``-separated line) per
  AC #4. The state machine reads the existing text, appends
  ``"\n" + json(new_entry)``, writes the whole blob back in the same
  UPDATE that bumps the row state. SQLite/Postgres TEXT-typed so the
  schema is portable across the test sqlite engine and the
  ``pg-primary`` prod engine; JSONL (rather than a single JSON array)
  means each line is independently parseable and the format degrades
  gracefully — a truncated tail still leaves the prefix recoverable.

No FK to a JIRA-side table exists by design — the JIRA graph is the
authoritative state machine per G1, ``release_state`` is the local
cache. A foreign key would require a synchronous JIRA-lookup at write
time and would deadlock the H4 cron-failover when JIRA is the
unreachable upstream.

Module-global / cross-worker state audit
----------------------------------------
Pure DDL migration — no module-level singleton. Writers are
``backend.release_conductor.state_machine.transition()`` (one row
per release per transition); readers are
``backend.api.release_state_query`` and the
``backend/tests/test_release_state_machine.py`` suite.

Revision ID: 0233
Revises: 0231
Create Date: 2026-05-12
"""
from __future__ import annotations

from alembic import op


revision = "0233"
down_revision = "0231"
branch_labels = None
depends_on = None


# Closed enum — must stay in lock-step with
# ``backend.release_conductor.state_machine.STATES`` (the state machine
# imports its enum from the same source-of-truth string list).
_STATE_LITERAL = (
    "'pending','building','staging',"
    "'canary_5','canary_25','canary_100',"
    "'done','failed','rolled_back'"
)


_PG_DDL = f"""
CREATE TABLE IF NOT EXISTS release_state (
    id                  BIGSERIAL PRIMARY KEY,
    release_id          TEXT NOT NULL,
    version             TEXT NOT NULL,
    state               TEXT NOT NULL,
    row_version         INTEGER NOT NULL DEFAULT 0,
    last_transition_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    transition_log_json TEXT NOT NULL DEFAULT '[]',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT release_state_state_chk
        CHECK (state IN ({_STATE_LITERAL})),
    CONSTRAINT release_state_version_uniq UNIQUE (version)
)
"""


_PG_INDEX_RELEASE_ID = """
CREATE INDEX IF NOT EXISTS idx_release_state_release_id
    ON release_state (release_id)
"""


_PG_INDEX_STATE = """
CREATE INDEX IF NOT EXISTS idx_release_state_state_last_transition
    ON release_state (state, last_transition_at DESC)
"""


_SQLITE_DDL = f"""
CREATE TABLE IF NOT EXISTS release_state (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    release_id          TEXT NOT NULL,
    version             TEXT NOT NULL,
    state               TEXT NOT NULL,
    row_version         INTEGER NOT NULL DEFAULT 0,
    last_transition_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    transition_log_json TEXT NOT NULL DEFAULT '[]',
    created_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT release_state_state_chk
        CHECK (state IN ({_STATE_LITERAL})),
    CONSTRAINT release_state_version_uniq UNIQUE (version)
)
"""


_SQLITE_INDEX_RELEASE_ID = """
CREATE INDEX IF NOT EXISTS idx_release_state_release_id
    ON release_state (release_id)
"""


_SQLITE_INDEX_STATE = """
CREATE INDEX IF NOT EXISTS idx_release_state_state_last_transition
    ON release_state (state, last_transition_at DESC)
"""


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.exec_driver_sql(_PG_DDL)
        bind.exec_driver_sql(_PG_INDEX_RELEASE_ID)
        bind.exec_driver_sql(_PG_INDEX_STATE)
    else:
        bind.exec_driver_sql(_SQLITE_DDL)
        bind.exec_driver_sql(_SQLITE_INDEX_RELEASE_ID)
        bind.exec_driver_sql(_SQLITE_INDEX_STATE)


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_release_state_state_last_transition")
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_release_state_release_id")
    bind.exec_driver_sql("DROP TABLE IF EXISTS release_state")
