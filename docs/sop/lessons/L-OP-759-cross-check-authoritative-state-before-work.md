---
id: L-OP-759
ticket: OP-759
title: Cross-check authoritative state before doing work
date: 2026-05-08
tags: [gerrit, jira, runner]
---

# Cross-check authoritative state before doing work

**Situation**: OP-691 was reverted to To Do even though Gerrit had
already merged the implementation. The runner treated JIRA's queue state
as authoritative, picked the ticket up again, and repeated work that had
already landed.

**Fix**: OP-759 added an H12 runner gate that queries Gerrit for
`message:<ticket> status:merged` before pickup. If a merged change with
a subject starting `[TICKET]` exists, the runner force-walks JIRA to
Published, posts a `[runner-h12-self-heal]` comment, and skips the CLI.

**Verification**: `backend/tests/test_runner_h12.py` covers strict
subject matching, Gerrit-query fail-open behavior, the synthetic OP-691
self-heal path, and idempotent already-Published force-walk behavior.

**Generalisation**: Before starting expensive or mutating automation,
cross-check the system that owns the terminal truth. Queue state is a
pickup hint, not proof that work is still needed.
