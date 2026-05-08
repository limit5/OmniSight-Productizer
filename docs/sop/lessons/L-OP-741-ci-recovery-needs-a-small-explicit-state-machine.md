---
id: L-OP-741
ticket: OP-741
title: CI recovery needs a small explicit state machine
date: 2026-05-08
tags: [ci, gerrit, git, runner]
legacy_lesson: 30
---

# CI recovery needs a small explicit state machine

**Situation**: OP-739's parallel CI gate makes Gerrit ``Verified -1`` a normal runner input instead of an operator-only event. Without a recovery state machine, a single flaky timeout, stale base, or simple test failure leaves the patchset stalled until a human notices.

**Fix**: OP-741 models ``Verified -1`` as ``categorize -> loop guard -> strategy dispatch``. The failure category selects one of retry, R3 rebase, bot-patch, or escalation; the attempt labels fire a medium notification on attempt 3 and hard-stop on attempt 6 with ``runner-loop-paused-pending-review``.

**Verification**: `backend/tests/test_ci_recovery.py` exercises the five categories, all strategy branches, timeout retry/escalation, the four operator escapes, and a synthetic 50-PS soak with audit trail coverage.

**Generalisation**: CI automation should treat logs as typed events before taking action. Keep the categorizer small, make the loop guard run before any retry/rebase/patch action, and ensure every operator escape writes an audit trail so bypasses are reviewable later.
