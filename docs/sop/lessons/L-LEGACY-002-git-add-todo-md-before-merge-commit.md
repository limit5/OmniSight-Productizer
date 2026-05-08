---
id: L-LEGACY-002
ticket: LEGACY-002
title: `git add TODO.md` before merge commit
date: 2026-05-04
tags: [git, legacy]
legacy_lesson: 2
---

# `git add TODO.md` before merge commit

**Situation**: During W1A (FX2 cheap BLOCKERs) merge, the runner-side TODO.md had been updated with `[x][G]` markers as codex completed items. Operator (Claude) ran `git merge --no-ff codex-work` while TODO.md was still unstaged on main. The merge commit didn't include the marker updates → next operator wakeup found "uncommitted TODO.md modifications" and asked why.

**Fix**: Codified merge SOP — before `git merge --no-ff <codex-branch>`:
1. Check `git status` for unstaged TODO.md (runner-written markers)
2. If yes, `git add TODO.md` first (so merge commit consolidates marker updates with codex's deliverables)
3. Then `git merge --no-ff codex-work`

**Verification**: Post-merge `git status` shows clean working tree. Recurrence on the OAuth D9.7 merge avoided by following the SOP.

**Generalisation**: When the runner manages a file (writes markers / state) and an external merge happens, treat the runner's unstaged writes as part of the merge's logical unit-of-work. Otherwise the merge commit becomes a half-truth.
