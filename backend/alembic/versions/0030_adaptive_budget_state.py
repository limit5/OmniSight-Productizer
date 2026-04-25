"""H4a row 2582 — adaptive_budget_state (last-known-good AIMD budget).

Persists the host-level AIMD concurrency budget so a cold restart
does not lose calibration. Without this table every
``uvicorn`` boot seeds the AIMD controller with the static
``OMNISIGHT_AIMD_INIT_BUDGET=6`` regardless of what the host
tolerated on the previous run — operators on a 32c/128GB rig who
had legitimately grown to ``CAPACITY_MAX=25`` would be throttled
back to 6 on every deploy and spend the next ~10 min of green
ticks climbing back (30 s × 19 steps).

Design — singleton table, not per-tenant
─────────────────────────────────────────
The AIMD budget is a **host-level** knob (same CPU / mem envelope
every worker observes via psutil). A single row keyed by
``id='global'`` makes the semantics explicit and gives the writer
a stable ``ON CONFLICT`` target. The TEXT PK (rather than a
synthetic INTEGER IDENTITY) also keeps the row out of the
``TABLES_WITH_IDENTITY_ID`` sequence-reset path, matching
``bootstrap_state`` + ``user_mfa``.

Schema::

    id           TEXT PRIMARY KEY           -- always 'global'
    budget       INTEGER NOT NULL
    last_reason  TEXT NOT NULL DEFAULT 'init'
    updated_at   DOUBLE PRECISION NOT NULL  -- epoch seconds

Write policy: upserted on every budget-changing tick
(``AdjustReason.AI`` / ``AdjustReason.MD`` — not on ``HOLD`` /
``CAP`` / ``FLOOR`` which leave budget unchanged). Multi-worker
races write "last writer wins" semantics which is benign —
each worker observes the same host, so their candidate budgets
are within one AIMD step of each other; on next cold start any
of them is a valid "last known good".

Read policy: a single ``SELECT budget FROM adaptive_budget_state
WHERE id='global'`` at lifespan startup. If no row exists
(first-ever boot, table truncated, DB unavailable) the controller
falls back to the static ``INIT_BUDGET`` default — the row is
**load-bearing only for carry-over**, never required.

Module-global state audit (SOP Step 1):
``backend.adaptive_budget._state`` stays per-uvicorn-worker
(qualifying answer #1: every worker derives the same AIMD
decisions from the same host signals). The DB row is the
cross-worker coordination point **for cold-start bootstrap only**
(qualifying answer #2: PG-backed persistence). Live budget is
never read from the DB — workers diverge by at most one AIMD
step between ticks, which converges naturally.

Revision ID: 0030
Revises: 0029
Create Date: 2026-04-25
"""
from __future__ import annotations

from alembic import op


revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    dialect = conn.dialect.name

    if dialect == "postgresql":
        conn.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS adaptive_budget_state (
                id           TEXT PRIMARY KEY,
                budget       INTEGER NOT NULL,
                last_reason  TEXT NOT NULL DEFAULT 'init',
                updated_at   DOUBLE PRECISION NOT NULL
            )
            """
        )
    else:
        conn.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS adaptive_budget_state (
                id           TEXT PRIMARY KEY,
                budget       INTEGER NOT NULL,
                last_reason  TEXT NOT NULL DEFAULT 'init',
                updated_at   REAL NOT NULL
            )
            """
        )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS adaptive_budget_state")
