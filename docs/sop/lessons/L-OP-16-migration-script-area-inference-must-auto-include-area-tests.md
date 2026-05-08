---
id: L-OP-16
ticket: OP-16
title: Migration script area inference must auto-include `area:tests`
date: 2026-05-06
tags: [ci, jira, runner]
legacy_lesson: 8
---

# Migration script area inference must auto-include `area:tests`

**Situation**: OP-16 ticket had labels `area:backend, area:db` but not `area:tests`. The migration script's `_infer_areas` only added `db + backend` for alembic keywords. When the runner built the §5 prompt-injection, `area:tests` was on the forbidden list — codex would have refused to write the AC-required test file. Manual label patch needed before launch.

**Fix**: `scripts/jira_migrate_active_tickets.py::_infer_areas` adds: any ticket with `alembic` / `schema` / `migration` keyword in title or `alembic` in any file path → auto-add `area:tests`. Migration ALWAYS needs contract tests in this project; convention. Commit `a6e6cd9f`.

**Verification**: OP-246 (later created via the patched migration logic) had `area:tests` in labels from the start; codex worked tests without manual operator intervention.

**Generalisation**: Area inference rules should reflect *project conventions about co-required artifacts*, not just file paths. If "X always needs Y" is true at the project level, the inference should encode it.
