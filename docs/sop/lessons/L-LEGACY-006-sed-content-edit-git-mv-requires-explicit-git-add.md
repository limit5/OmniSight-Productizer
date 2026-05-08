---
id: L-LEGACY-006
ticket: LEGACY-006
title: `sed` content edit + `git mv` requires explicit `git add`
date: 2026-05-06
tags: [ci, git, legacy]
legacy_lesson: 6
---

# `sed` content edit + `git mv` requires explicit `git add`

**Situation**: BP.Q + WP.7 codex variants needed both file rename (`git mv 0186 0193`) AND internal `revision = "0186"` → `"0193"` content edit (via `sed -i`). Codex ran `git mv` then `sed -i` then `git commit`. The commit included the rename but not the sed content change — the file at HEAD had renamed name but stale internal revision literal. Required follow-up cleanup commit.

**Fix**: After `sed -i` on a file that's already in a `git mv`-staged state, the sed touches the working-tree file but doesn't auto-update the staged version. Explicit `git add <file>` required after sed:
```bash
git mv old new
sed -i 's/0186/0193/g' new   # touches working tree only
git add new                   # restage with content edit included
git commit
```

**Verification**: Post-commit `git show HEAD:new` shows the content edit. Recurrence avoided on subsequent migrations by following the SOP.

**Generalisation**: `git add -A` would also cover this case (`git status` after sed will flag the file as "modified, staged"). The trap is when operators rely on `git mv` having "staged the new file" and forget the post-edit re-add. Lint hint: any commit message containing "rename" should trigger a manual `git diff --cached` before commit.
