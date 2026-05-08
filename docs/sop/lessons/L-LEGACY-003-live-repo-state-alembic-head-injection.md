---
id: L-LEGACY-003
ticket: LEGACY-003
title: LIVE REPO STATE alembic head injection
date: 2026-05-05
tags: [jira, legacy]
legacy_lesson: 3
---

# LIVE REPO STATE alembic head injection

**Situation**: Codex bigbatch (78 items) repeatedly produced alembic migrations with stale `down_revision` literals copied from TODO.md text. The TODO described intent at write time (e.g. "alembic 0186"), but by the time codex executed, the live chain head had advanced. Result: file naming and revision string mismatch, broken chain integrity.

**Fix**: `auto-runner-codex.py::_current_alembic_head()` queries live `alembic heads` at prompt-build time and injects a `LIVE REPO STATE` block into every codex prompt:

```
## LIVE REPO STATE (refreshed at <timestamp>)
- alembic head: 0198
- branch: codex-work
- last commit: <sha> <subject>
```

Codex then uses this fresh value rather than TODO's stale literal.

**Verification**: 78-item bigbatch + later WP.1 single-item run both produced correctly-numbered migrations. Generalised to the live-state check engine in `docs/sop/jira-ticket-conventions.md` §13.

**Generalisation**: Any prompt that references repo state must inject *fresh* state, not state-as-described-when-the-task-was-authored. TODO / ticket text describes intent; live values describe ground truth. The two diverge over time.
