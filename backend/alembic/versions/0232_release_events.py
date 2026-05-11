"""OP-947 H2 — ``release_events`` durable queue for the L3 event router.

Backwards-compat: safe (additive, no FK out)

The Sprint H L3 conductor (event-driven release scheduler) needs a
durable, at-least-once queue between the HTTP/internal-bus ingest
surface (`backend.release_conductor.event_router`) and the dispatch
worker (`backend.release_conductor.worker`). This migration creates the
``release_events`` table that the worker pulls FIFO.

Why a Postgres-backed queue and not Redis / NATS / `backend.events.bus`
================================================================
ADR-0018 §Idempotency calls out that the SQLite store at
``~/.config/omnisight/idem-keys.db`` is per-runner-process and does NOT
span the two backend instances (`backend-a` + `backend-b` behind the
Caddy LB). The L3 receiver needs a cross-backend-coherent store, and
the deploy already runs `pg-primary` (per
``reference_op693_deploy_outcomes.md``). A Postgres table is the lowest-
ops-cost answer that already meets the matrix's durability and
idempotency contracts. Redis / NATS would add a new infrastructure
dependency for marginal latency wins on a low-volume channel.

Schema rationale
----------------
* ``id`` — autoincrement primary key; the worker pulls by ``ORDER BY id``
  to preserve FIFO arrival order (AC #3).
* ``source`` — the event source (``gerrit`` / ``jira`` / ``slo_monitor``
  / ``canary`` / ``prod_orchestrator``), per the ADR-0018 matrix rows.
* ``event_type`` — the source-side wire name (``change-merged`` /
  ``jira:issue_updated`` / ``slo.breach`` / ``canary.stage.transitioned``
  / ...) so the dispatcher's lookup table can key on it.
* ``idempotency_key`` — ``sha256(source + ":" + event_id)`` per AC #5.
  ``UNIQUE`` so a duplicate POST that lands while the original is still
  queued collapses on INSERT — the HTTP handler catches the
  ``IntegrityError`` and returns the cached row's id instead of double-
  queuing.
* ``payload_json`` — the raw event body, TEXT (not JSONB) so the schema
  is portable across the test sqlite engine and the ``pg-primary``
  prod engine; the dispatcher parses on read.
* ``status`` — closed enum: ``pending`` (queued, never picked) /
  ``in_progress`` (worker leased it but hasn't finalised) / ``done``
  (handler returned cleanly) / ``failed`` (retried 3 times, exhausted —
  the dead-letter state from AC #4) / ``dead_letter`` (operator-
  triaged terminal state; reserved for H4 replay flow).
* ``attempt_count`` — incremented on each handler invocation; the
  worker stops retrying once ``attempt_count >= max_attempts``.
* ``max_attempts`` — per-row override of the default 3 (AC #4) so a
  future operator-replay path can re-queue with a fresh budget.
* ``next_retry_at`` — earliest wall-clock time the worker may pick up
  this row again after a transient handler failure. The dispatcher
  computes ``now + 2^attempt_count * base_backoff`` and writes it
  back on each retry; the worker's pull query filters on
  ``next_retry_at <= now``.
* ``last_error`` — the most recent exception's repr; surfaced in the
  H4 web UI for operator triage.
* ``received_at`` / ``updated_at`` — wall-clock breadcrumbs for the
  operator dashboard.
* ``handler_result_json`` — the dispatched handler's return value;
  cached so a duplicate delivery short-circuits to the same answer
  (AC #5, parallels ``backend/agents/idempotency.py::IdempotencyStore``).

Index on ``(status, next_retry_at, id)`` is the worker's hot path:
"give me the oldest ``pending`` or ready-to-retry row whose
``next_retry_at`` is in the past." A partial index on Postgres scopes
it tightly to terminal-rejecting rows (status NOT IN ('done',
'dead_letter')); sqlite gets the full index because partial indexes
are dialect-fiddly to write portably.

Module-global / cross-worker state audit
----------------------------------------
Pure DDL migration — no module-level singleton. Writers are
``backend.release_conductor.event_router.persist_event()`` (one row
per accepted event) and
``backend.release_conductor.worker._update_status()`` (worker leasing
+ retry bookkeeping). Readers are the worker's
``_claim_next_event()`` and the operator query API
(``backend.api.release_state_query``) plus the H4 web UI (not yet
built).

Revision ID: 0232
Revises: 0233
Create Date: 2026-05-12
"""
from __future__ import annotations

from alembic import op


revision = "0232"
# 0233 (OP-948 H3 release_state) merged first in the Sprint H wave even
# though its number is later — the H2 / H3 work was scheduled in
# parallel and H3 landed first. Chain 0232 *after* 0233 so the alembic
# DAG stays linear (a real branch would force a merge migration and
# OP-948 is already in develop). The revision string is opaque to
# alembic; the filename keeps the ticket-named ``0232`` per H2's
# files-touched manifest.
down_revision = "0233"
branch_labels = None
depends_on = None


# Closed enum — must stay in lock-step with the status constants in
# ``backend.release_conductor.event_router`` (the module imports its
# enum from the same source-of-truth string list and we assert the two
# match in ``test_event_router.py``).
_STATUS_LITERAL = (
    "'pending','in_progress','done','failed','dead_letter'"
)


_PG_DDL = f"""
CREATE TABLE IF NOT EXISTS release_events (
    id                   BIGSERIAL PRIMARY KEY,
    source               TEXT NOT NULL,
    event_type           TEXT NOT NULL,
    idempotency_key      TEXT NOT NULL,
    payload_json         TEXT NOT NULL,
    status               TEXT NOT NULL DEFAULT 'pending',
    attempt_count        INTEGER NOT NULL DEFAULT 0,
    max_attempts         INTEGER NOT NULL DEFAULT 3,
    next_retry_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_error           TEXT,
    handler_result_json  TEXT,
    received_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT release_events_status_chk
        CHECK (status IN ({_STATUS_LITERAL})),
    CONSTRAINT release_events_idempotency_uniq UNIQUE (idempotency_key)
)
"""


_PG_INDEX_WORKER = """
CREATE INDEX IF NOT EXISTS idx_release_events_status_ready
    ON release_events (status, next_retry_at, id)
"""


_PG_INDEX_SOURCE = """
CREATE INDEX IF NOT EXISTS idx_release_events_source_received
    ON release_events (source, received_at DESC)
"""


_SQLITE_DDL = f"""
CREATE TABLE IF NOT EXISTS release_events (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    source               TEXT NOT NULL,
    event_type           TEXT NOT NULL,
    idempotency_key      TEXT NOT NULL,
    payload_json         TEXT NOT NULL,
    status               TEXT NOT NULL DEFAULT 'pending',
    attempt_count        INTEGER NOT NULL DEFAULT 0,
    max_attempts         INTEGER NOT NULL DEFAULT 3,
    next_retry_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_error           TEXT,
    handler_result_json  TEXT,
    received_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT release_events_status_chk
        CHECK (status IN ({_STATUS_LITERAL})),
    CONSTRAINT release_events_idempotency_uniq UNIQUE (idempotency_key)
)
"""


_SQLITE_INDEX_WORKER = """
CREATE INDEX IF NOT EXISTS idx_release_events_status_ready
    ON release_events (status, next_retry_at, id)
"""


_SQLITE_INDEX_SOURCE = """
CREATE INDEX IF NOT EXISTS idx_release_events_source_received
    ON release_events (source, received_at DESC)
"""


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.exec_driver_sql(_PG_DDL)
        bind.exec_driver_sql(_PG_INDEX_WORKER)
        bind.exec_driver_sql(_PG_INDEX_SOURCE)
    else:
        bind.exec_driver_sql(_SQLITE_DDL)
        bind.exec_driver_sql(_SQLITE_INDEX_WORKER)
        bind.exec_driver_sql(_SQLITE_INDEX_SOURCE)


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_release_events_source_received")
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_release_events_status_ready")
    bind.exec_driver_sql("DROP TABLE IF EXISTS release_events")
