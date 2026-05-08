---
id: L-OP-750
ticket: OP-750
title: Orphan commit detection belongs at the runner tick boundary
date: 2026-05-08
tags: [runner, gerrit, jira, reliability]
---

# Orphan commit detection belongs at the runner tick boundary

**Situation**: A runner crash after the CLI committed but before the
Gerrit push left work stranded on a local `feature/OP-*-runner-fresh`
branch. The following tick force-created a fresh branch for the next
ticket, making the previous work easy to miss and requiring manual
salvage.

**Fix**: Scan runner feature branches at tick start, before ticket
pickup mutates the worktree. If a branch contains same-ticket `[OP-XXX]`
commits, has a non-empty diff, and has no matching open Gerrit change,
push it to `refs/for/develop` with the bot SSH identity and leave a JIRA
`[runner-orphan-salvage]` comment.

**Verification**: `backend/tests/test_orphan_salvage.py` covers the
dirty-worktree salvage path, existing Gerrit review skip, empty-diff
skip, mixed-ticket alert, and the more-than-five-orphans halt guard.

**Generalisation**: Destructive tick-start worktree operations need a
preflight scan for committed-but-unpublished work. Recovery must run at
the boundary before cleanup, branch replacement, or fresh-sync code can
discard the evidence.
