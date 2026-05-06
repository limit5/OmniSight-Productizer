# Lessons Learned — OmniSight

**Status**: Living document. Append-only by convention; corrections via amendment block, not delete-rewrite.

**Authority**: Owned by operator + AI fleet collectively. Each entry must include `Situation` (what happened), `Fix` (specific change), and `Verification` (how we'd know it worked). Vague entries (`be more careful`, `pay attention`) are auto-rejected by the META retrospective workflow (per `docs/sop/jira-ticket-conventions.md` §14).

**How entries land here**:
1. From META retrospective tickets (label `meta:lessons-learned`) once Approved
2. From cross-Phase retrospectives (`docs/retrospectives/YYYY-MM-DD-<slug>.md`) that distil into a generalisable lesson
3. Direct operator append when codifying tribal knowledge

---

## Index

| # | Date | Lesson | Origin |
|---|---|---|---|
| 1 | 2026-05-04 | KS.2/KS.3 + FX.9.4 collision → multi-head alembic merge migration pattern | governance Phase 0 |
| 2 | 2026-05-04 | git add TODO.md before merge commit (runner-managed `[x][G]` markers) | W1A merge fix |
| 3 | 2026-05-05 | LIVE REPO STATE alembic head injection at runner prompt build time | codex bigbatch alembic numbering bug |
| 4 | 2026-05-05 | Gerrit `gerrit create-group` auto-adds creator (sora) to created group → cleanup required | Gerrit setup |
| 5 | 2026-05-05 | SSH transport strips outer quotes — use `"'multi word'"` for values containing spaces | Gerrit `create-group --description` |
| 6 | 2026-05-06 | Sed content edit + `git mv` → must `git add` after sed before commit | BP.Q + WP.7 codex variants |

---

## Lesson 1 — Multi-head alembic on parallel feature branches (2026-05-04)

**Situation**: KS.2/3 codex-work branch authored alembic 0107/0108 chained off 0106. Concurrently, FX.9.4 work on main consumed 0106 into 0188_merge_heads. When merging codex-work → main, `alembic heads` reported two heads — 0108 (from codex) and 0188 (from main). Vanilla merge created an unmergeable chain.

**Fix**: Created a new merge migration (0190_merge_ks_envelope_byog) with both prior heads as `down_revision` tuple. Doc'd the pattern as the canonical resolution for diverging-feature-branch alembic conflicts.

**Verification**: `alembic heads` post-merge returns single head. Post-merge upgrade from clean DB applies both branch's migrations cleanly. Tested empirically on 2026-05-04 KS.2/3 merge.

**Generalisation**: When two feature branches both modify the alembic chain, the merging branch must add a merge migration referencing both prior heads — never silently delete one.

---

## Lesson 2 — `git add TODO.md` before merge commit (2026-05-04)

**Situation**: During W1A (FX2 cheap BLOCKERs) merge, the runner-side TODO.md had been updated with `[x][G]` markers as codex completed items. Operator (Claude) ran `git merge --no-ff codex-work` while TODO.md was still unstaged on main. The merge commit didn't include the marker updates → next operator wakeup found "uncommitted TODO.md modifications" and asked why.

**Fix**: Codified merge SOP — before `git merge --no-ff <codex-branch>`:
1. Check `git status` for unstaged TODO.md (runner-written markers)
2. If yes, `git add TODO.md` first (so merge commit consolidates marker updates with codex's deliverables)
3. Then `git merge --no-ff codex-work`

**Verification**: Post-merge `git status` shows clean working tree. Recurrence on the OAuth D9.7 merge avoided by following the SOP.

**Generalisation**: When the runner manages a file (writes markers / state) and an external merge happens, treat the runner's unstaged writes as part of the merge's logical unit-of-work. Otherwise the merge commit becomes a half-truth.

---

## Lesson 3 — LIVE REPO STATE alembic head injection (2026-05-05)

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

---

## Lesson 4 — `gerrit create-group` auto-adds creator (2026-05-05)

**Situation**: Setting up Gerrit groups (`non-ai-reviewer`, `ai-reviewer`, `merger-agent-bot` per ADR 0003), the `gerrit create-group` SSH command auto-added the creator (sora) to every group it created. ADR 0003 explicitly forbids sora from being in `ai-reviewer` or `merger-agent-bot` (separation of concerns).

**Fix**: After `create-group`, immediately run `gerrit set-members <group> --remove sora` for any group sora shouldn't be in. Codified as Gerrit SOP cleanup step.

**Verification**: Post-cleanup `gerrit ls-members <group>` returns expected member list. ADR 0003 separation of concerns preserved.

**Generalisation**: Admin tools that "helpfully" auto-include the actor often violate intended permission boundaries. Always verify membership / ACL after creation, never trust create-time defaults.

---

## Lesson 5 — SSH transport strips outer quotes (2026-05-05)

**Situation**: Tried `gerrit create-group --description "Human reviewers"` over SSH. Failed with "Too many arguments: reviewers". The outer double-quotes were stripped by SSH transport before the remote shell parsed args; remote shell saw `--description Human reviewers` as 3 separate args.

**Fix**: Use double-quote-wrap-single-quote pattern for SSH arg values containing spaces:
```bash
gerrit create-group ai-reviewer --description "'AI bot reviewers (max +1)'"
```
The outer `"` is stripped by SSH transport; the inner `'` survives to the remote shell, which sees `--description 'AI bot reviewers (max +1)'` as one arg.

**Verification**: `gerrit ls-groups -v | grep ai-reviewer` shows full description string preserved.

**Generalisation**: SSH transport quote-stripping is asymmetric (outer quotes consumed). Any tool invoked via SSH with multi-word arg values needs the double-wrap pattern. Document as gotcha #5 in `reference_gerrit_self_hosted.md`.

---

## Lesson 6 — `sed` content edit + `git mv` requires explicit `git add` (2026-05-06)

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

---

## Amendment block (corrections / additions to past entries)

(none yet)
