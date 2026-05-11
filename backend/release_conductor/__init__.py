"""Release conductor package — Sprint H L3 event-driven scheduler.

Layers:

* ``state_machine.py`` (OP-948 H3) — per-version state projection +
  append-only audit trail. JIRA graph remains the canonical source of
  truth per ``docs/operations/release-conductor-runbook.md`` §0; this
  module is the local Postgres mirror that the L3 dispatchers write
  to and the operator query API (``backend.api.release_state_query``)
  reads from.
* ``event_router.py`` (OP-947 H2) — HTTP ingest endpoint
  (``POST /api/v1/release-conductor/events``) + persistence layer
  over the ``release_events`` durable queue (alembic 0232). Owns the
  ``EventInsertFailed`` / dead-letter / idempotency contract for the
  ADR-0018 webhook subscription matrix.
* ``event_handlers/`` (OP-947 H2) — dispatch table mapping
  ``(source, event_type)`` to handler callables, one row per
  ADR-0018 matrix entry.
* ``worker.py`` (OP-947 H2) — FIFO puller + retry/backoff loop that
  drains the ``release_events`` queue through the dispatch table.
"""
