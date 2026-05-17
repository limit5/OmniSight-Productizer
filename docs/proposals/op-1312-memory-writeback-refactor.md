# OP-1312 memory writeback refactor proposal

## Context

`backend/agents/memory_writeback.py` owns the fail-open runner completion
write-back path. The public surface is intentionally small: callers build a
`WritebackRequest`, call `MemoryWriteback.write()`, and receive a
`WritebackResult` that records per-store outcomes for Memory Tool,
`runner_incidents`, and Cognee.

Two small refactors would reduce coupling inside the orchestrator without
changing the write-back protocol or backing-store behavior:

1. Extract the per-store thread launch, budget join, and failure aggregation
   logic from `MemoryWriteback.write()` into a private fan-out helper. The
   current method repeats the same wrapper shape for Memory Tool, incidents,
   and Cognee, then repeats the same timeout/error checks. A helper such as
   `_run_store_fanout(request)` could keep the public method focused on
   idempotency, retry queueing, result construction, and logging while
   preserving the current daemon-thread and per-store-budget behavior.
2. Extract incident row payload selection from `_do_incident()` into a private
   pure helper. Today `_do_incident()` decides whether a row should be written,
   classifies failures, formats success-outcome summaries, and calls the
   injected writer. A helper such as `_incident_payload_for(request)` returning
   `None` or keyword arguments would separate policy from I/O and make the
   success-vs-failure branches easier to review.

## Non-goals

- No behavior change to idempotency, fail-open semantics, per-store budgets,
  daemon-thread usage, incident retry queueing, lesson classification, or Cognee
  tickle behavior.
- No public API change to `WritebackRequest`, `WritebackResult`,
  `MemoryWriteback`, `classify_lesson()`, or exported store constants.
- No change to backing stores, database schema, Memory Tool storage format,
  Cognee ingest behavior, runner JIRA comments, or dashboard-facing results.
- No database, devops, embedded, frontend, security, tooling, or production
  dependency changes.
- No code changes in this proposal patch set.

## Follow-up implementation ticket draft

Summary: Refactor memory writeback fan-out and incident payload helpers

Areas: backend, tests

Tier: S

Description:

Implement the two OP-1312 proposal refactors in
`backend/agents/memory_writeback.py`:

- Add a private helper for Memory Tool / runner_incidents / Cognee fan-out that
  preserves the existing daemon-thread launch, per-store timeout budgets, error
  capture, and `stores_failed` names.
- Keep `MemoryWriteback.write()` responsible for idempotency lookup,
  incident-retry queueing, `WritebackResult` construction, result caching, and
  partial-write logging.
- Add a private incident payload helper that returns `None` when no incident row
  should be written, or keyword arguments for the injected incident writer when
  the request should produce a row.
- Preserve failure classification, `SUCCESS_OUTCOME` summary tagging, optional
  success-outcome rows, mutex label propagation, area propagation, and runner
  class propagation.
- Add focused tests under `backend/tests/test_memory_writeback.py` only if the
  helper extraction needs direct coverage for preserved fan-out or incident
  payload behavior.

Acceptance criteria:

1. Existing public imports from `backend.agents.memory_writeback` remain valid.
2. Existing Memory Tool, runner_incidents, and Cognee success/failure behavior is
   unchanged for the cases covered by `backend/tests/test_memory_writeback.py`.
3. Store timeout and store exception paths still populate the same
   `WritebackResult.stores_failed` values and still queue incident retry files
   when the incident store fails.
4. `backend/.venv/bin/pytest backend/tests/test_memory_writeback.py` passes.

Filed follow-up: blocked by OP-1312 pickup write restriction. This pickup
allows programmatic JIRA writes only through
`jira_dispatch.add_comment()` / `transition_back_to_todo()`, and the repository
does not expose a general follow-up ticket creation helper in that allowed set.
