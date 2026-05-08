---
id: ADR-0010
title: Deployment Automation Gates
status: Accepted
date: 2026-05-08
---

# ADR 0010 — Deployment Automation Gates

**Status**: Accepted (2026-05-08)

**Context**

ADR 0001 defines `develop` as the integration trunk and `main` as the
production branch. The missing decision was how the system decides that a
release milestone is ready to promote from `develop` to `main` without
turning a human checklist into tribal memory.

JIRA already carries the release boundary through `fixVersion`, while
Gerrit and CI carry the implementation and validation evidence. OP-762
connects those signals without making any single ticket comment or TODO
marker authoritative.

**Decision**

Use JIRA `fixVersion` as the canonical milestone marker. A systemd timer
runs `scripts/release_milestone_checker.py` every 5 minutes and evaluates
each unreleased version.

The checker emits:

- `milestone_ready` when every gate is green.
- `milestone_blocked` with structured reason objects when any gate is red.

OP-766 adds `backend.agents.auto_promote_main`, a daemon-style consumer
for the checker log. On each `milestone_ready` event it verifies
`main..develop` is non-empty and `develop..main` is empty, then pushes
`develop:main` to Gerrit. If `main` has commits missing from `develop`,
the daemon aborts and alerts the operator with the main-only commit list.
Successful promotions emit `main_promoted` for the staging deploy
consumer.

The initial gates are:

- every ticket in the fixVersion has status `Published` / `公開済み`
- every ticket has a merged Gerrit change on `develop`
- no open Highest-priority ticket affects the same version
- latest canary status for the `develop` tip is green
- smoke status for the `develop` tip is green and newer than 4 hours

**Consequences**

Positive:
- D5 auto-promotion can consume one event instead of re-implementing
  release-readiness policy.
- D17 can surface blocked reasons directly from structured event fields.
- Human release decisions remain auditable because the gates cite JIRA,
  Gerrit, and CI state rather than a free-form checklist.

Negative:
- CI status producers must write machine-readable canary and smoke status
  records keyed by `branch=develop` and the develop tip revision.
- A missing status is treated as blocked, which may delay promotion after
  observability outages.

**Related**

- [ADR 0001 — Five-branch Git Flow](ADR-0001-five-branch-gitflow.md)
- [ADR 0003 — Gerrit Code Review](ADR-0003-gerrit-code-review.md)
- [OP-762 release milestone checker](../../scripts/release_milestone_checker.py)
