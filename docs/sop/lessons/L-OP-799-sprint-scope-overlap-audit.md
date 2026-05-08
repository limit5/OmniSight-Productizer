---
id: L-OP-799
ticket: OP-799
title: Audit Sprint META child file scopes before pickup
date: 2026-05-09
tags: [jira, meta, runner, scope]
---

# Audit Sprint META child file scopes before pickup

**Situation**: Sprint E split framework decision work (OP-785) and
canonical docs-site scaffold work (OP-786) into separate children, but both
declared and wrote the same five docs-site files. E1 merged first, then E2
hit add/add conflicts on `docs-site/mkdocs.yml`,
`docs-site/requirements.txt`, `docs-site/docs/index.md`,
`docs-site/docs/lessons/index.md`, and
`docs-site/docs/stylesheets/extra.css`, requiring operator rebase recovery.

**Fix**: Add `scripts/audit_sprint_overlap.py` and require Sprint META
filing to run it before pickup. The audit compares child `Files / Paths`
sections and fails on exact path reuse, matching glob/file declarations,
and overlapping added-directory claims.

**Verification**: `backend/tests/test_audit_sprint_overlap.py` replays the
Sprint E five-file conflict, a clean Sprint D-style distinct-file set, and
a synthetic overlap-then-clean case.

**Generalisation**: Planning tickets must make file ownership explicit
before runner concurrency begins. If two children need the same canonical
path, combine that path under one child or make one child a spike that writes
only non-canonical output.
