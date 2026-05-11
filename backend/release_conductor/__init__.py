"""OP-948 H3 — release conductor package.

Houses the per-version state machine (``state_machine.py``) that the
Sprint H L3 event-driven conductor uses to record + serialise release
stage transitions. The JIRA graph remains the canonical source of truth
per ``docs/operations/release-conductor-runbook.md`` §0; this package is
the local Postgres projection + append-only audit trail that the
webhook dispatchers (H2) write to and the operator query API
(``backend.api.release_state_query``) reads from.
"""
