# Release Lifecycle States

This SOP defines the terminal release states used by the OP JIRA workflow.
It complements `docs/sop/jira-ticket-conventions.md` and the Gerrit/JIRA
bridge implementation in `backend/agents/gerrit_jira_bridge.py`.

## 公開済み / Published

`公開済み` means the ticket's change has merged to the release branch and
the Gerrit/JIRA bridge has completed the Deploy transition. It is still a
recent, inspectable release artifact:

- Operators may review final comments, AC verification, and linked Gerrit
  changes.
- The coordinator and release tooling may still include the ticket in
  freshness, deployment, and retrospective checks.
- The ticket is not considered abandoned or cancelled.

The bridge treats `Published` and `公開済み` as equivalent status names.

## Archived

`Archived` means the ticket is no longer part of the active release surface.
It remains available for audit and search, but normal runner/coordinator
automation should not reopen or force-publish it.

The automatic archive path is deliberately narrow:

- Only tickets already in `公開済み` / `Published` are eligible.
- `OMNISIGHT_ARCHIVE_AGE_DAYS` controls the retention window; unset means
  30 days.
- The age is measured from JIRA `statusCategoryChangedDate`, falling back
  to `updated` when JIRA omits the status-change timestamp.
- The `coord-keep-open` label blocks auto-archive for tickets that need
  extended observation.
- Every automatic archive writes a JIRA comment and an append-only
  coordinator decision-log entry with `action_type=auto_archive_published_ticket`.

## Operations

The event path runs inside `gerrit-jira-bridge.service`: when a
`change-merged` event maps to a ticket that is already `公開済み`, the bridge
checks the retention window and archives only if the ticket is old enough.

The sweep path runs daily via:

- `deploy/systemd/gerrit-jira-archive-sweep.service`
- `deploy/systemd/gerrit-jira-archive-sweep.timer`

Use `coord-keep-open` for the operator escape hatch rather than disabling
the timer. The label is visible on the ticket and keeps the exception local.
