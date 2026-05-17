# OP-1332 milestone query refactor proposal

## Context

`backend/agents/milestone_query.py` is the shared release-milestone helper used
by `scripts/milestone_define.py` and `scripts/milestone_check.py`. The module is
already small and keeps external JIRA/Gerrit I/O in the scripts, but it still
mixes three responsibilities in one file: pure milestone readiness policy,
SQLite dry-run table access, and report-file serialization.

Two small refactors would reduce coupling without changing the public dataclasses,
status/error strings, SQL table shape, or script behavior:

1. Extract the Verified-label evaluation branch from `evaluate_milestone()` into
   a private pure helper. Today the function owns ticket publication checks,
   blocker checks, Verified-label missing/red-run checks, force-accept warnings,
   final readiness, and status selection. A helper such as
   `_verified_label_findings(verified_rows, *, verified_required, force_accept)`
   could return `(errors, warnings)` for only the Verified-label policy, keeping
   the top-level evaluator focused on ordering and final result assembly.
2. Move release-milestone row serialization into a private store helper.
   `ReleaseMilestoneStore.update_status()` currently owns JSON encoding,
   timestamp generation, previous-status lookup, and the update statement. A
   helper such as `_status_update_payload(status, report)` could centralize the
   `last_report_json` and `checked_at` values while preserving the existing
   `ensure_ascii=False`, sorted-key report encoding and SQLite-compatible text
   timestamps.

## Non-goals

- No behavior change to accepted JIRA statuses, force-accept semantics,
  Verified-label missing/red-run errors, blocker errors, readiness calculation,
  exit codes, or report shape.
- No database schema, migration, Alembic, production persistence, or SQL dialect
  changes.
- No public API rename for `MilestoneTicket`, `VerifiedRun`,
  `MilestoneEvaluation`, `evaluate_milestone()`, `ReleaseMilestoneStore`,
  `create_engine()`, `ensure_release_milestones_table_for_dry_run()`, or
  `write_json_report()`.
- No script behavior change in `scripts/milestone_define.py` or
  `scripts/milestone_check.py`.
- No production code changes in this proposal patch set.
- No test changes in this proposal patch set.
- No db, devops, embedded, frontend, security, tests, tooling, or out-of-scope
  domain changes.

## Follow-up implementation ticket draft

Summary: Refactor milestone query evaluation and store payload helpers

Areas: backend, tests

Tier: S

Description:

Implement the two OP-1332 proposal refactors in
`backend/agents/milestone_query.py`:

- Add a private pure helper for Verified-label findings and call it from
  `evaluate_milestone()`.
- Preserve current error/warning code strings, error/warning ordering,
  force-accept behavior, ready/status/exit-code calculation, and `to_report()`
  output.
- Add a private helper for `ReleaseMilestoneStore.update_status()` payload
  construction, including report JSON encoding and one shared UTC timestamp
  value.
- Preserve the existing SQL statements, table columns, `ensure_ascii=False`,
  sorted report keys, and previous-status return value.
- Add focused tests under `backend/tests/test_milestone.py` only if the helper
  extraction needs extra coverage beyond the existing milestone evaluation and
  store cases.

Acceptance criteria:

1. Existing imports from `backend.agents.milestone_query` used by
   `scripts/milestone_define.py`, `scripts/milestone_check.py`, and
   `backend/tests/test_milestone.py` remain valid.
2. Milestone readiness outcomes are unchanged for published, unpublished,
   force-accepted, missing-Verified-label, red-Verified-label, and open-blocker
   inputs.
3. `ReleaseMilestoneStore.update_status()` still returns the previous status and
   persists the same report JSON fields and status timestamp fields.
4. `backend/.venv/bin/pytest backend/tests/test_milestone.py` passes.

Filed follow-up: blocked by OP-1332 pickup write restriction. This pickup allows
programmatic JIRA writes only through `jira_dispatch.add_comment()` /
`transition_back_to_todo()`, and the repository does not expose a general
follow-up ticket creation helper in that allowed set.
