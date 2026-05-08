---
id: L-OP-762
ticket: OP-762
title: Machine-checkable release milestones beat checklist milestones
date: 2026-05-08
tags: [release, jira, ci, gerrit]
---

# Machine-checkable release milestones beat checklist milestones

**Situation**: Sprint D needed a way to know when `develop` was ready to
promote to `main`. The tempting shortcut was a human-maintained "release
ready" note, but that would duplicate state already present in JIRA,
Gerrit, and CI, and it would drift as soon as one ticket or canary run
changed after the note was written.

**Fix**: OP-762 made JIRA `fixVersion` the milestone definition and
implemented `scripts/release_milestone_checker.py` as the acceptance
checker. The checker emits `milestone_ready` only when every gate is
machine-green; otherwise it emits `milestone_blocked` with structured
reasons that D17 can display and D5 can ignore safely.

**Verification**: `backend/tests/test_release_milestone_checker.py`
covers the synthetic five-ticket milestone, structured blocked reasons,
and the 5-minute systemd timer contract.

**Generalisation**: A milestone field should define the candidate set,
not the readiness verdict. Promotion readiness should be recomputed from
current source-of-truth systems and fail closed when evidence is missing.
