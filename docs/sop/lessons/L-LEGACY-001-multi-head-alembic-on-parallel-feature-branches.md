---
id: L-LEGACY-001
ticket: LEGACY-001
title: Multi-head alembic on parallel feature branches
date: 2026-05-04
tags: [ci, legacy]
legacy_lesson: 1
---

# Multi-head alembic on parallel feature branches

**Situation**: KS.2/3 codex-work branch authored alembic 0107/0108 chained off 0106. Concurrently, FX.9.4 work on main consumed 0106 into 0188_merge_heads. When merging codex-work → main, `alembic heads` reported two heads — 0108 (from codex) and 0188 (from main). Vanilla merge created an unmergeable chain.

**Fix**: Created a new merge migration (0190_merge_ks_envelope_byog) with both prior heads as `down_revision` tuple. Doc'd the pattern as the canonical resolution for diverging-feature-branch alembic conflicts.

**Verification**: `alembic heads` post-merge returns single head. Post-merge upgrade from clean DB applies both branch's migrations cleanly. Tested empirically on 2026-05-04 KS.2/3 merge.

**Generalisation**: When two feature branches both modify the alembic chain, the merging branch must add a merge migration referencing both prior heads — never silently delete one.
