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
| 7 | 2026-05-06 | `HANDOFF.md` is FROZEN — runner prompt MUST tell codex explicitly, CLAUDE.md amendment alone insufficient | OP-16 first run |
| 8 | 2026-05-06 | Migration script `_infer_areas` must auto-add `area:tests` for alembic-keyword tickets | OP-16 prompt blocked test work |
| 9 | 2026-05-06 | Codex cuts `feature/<key>-<slug>` per DoD; merger must scan multiple branch refs (`codex-work` + `feature/OP-*`) | OP-15 retry |
| 10 | 2026-05-06 | Without Gerrit wired, operator does Author/Reviewer/Merger simultaneously — violates ADR 0003 in spirit | OP-15/16/246 first runs |

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

## Lesson 7 — `HANDOFF.md` freeze rule needs prompt-level enforcement (2026-05-06)

**Situation**: After CLAUDE.md L1 was amended 2026-05-06 to mark `HANDOFF.md` FROZEN (per `docs/sop/jira-ticket-conventions.md` §7), OP-16 codex run still appended a 12-line resolution entry to `HANDOFF.md`. Codex reads CLAUDE.md but the legacy "always generate HANDOFF.md" SOP from `auto-runner-codex.py` training history overrode the new convention.

**Fix**: `auto-runner-jira.py::_build_prompt` adds explicit "DO NOT append to HANDOFF.md — that file is FROZEN" directive. Prompt-level instruction is deterministic; CLAUDE.md alone is too easy to skip when the model has prior training to the contrary. Commit `a6e6cd9f`.

**Verification**: OP-246 + OP-15 (after fix) committed without touching HANDOFF.md. `git log main..HEAD -- HANDOFF.md` returns empty for both runs.

**Generalisation**: Any rule change in CLAUDE.md / convention docs that overrides existing AI training MUST be mirrored as an explicit prompt directive when AI is invoked autonomously. The CLAUDE.md amendment is the *intent*; the prompt directive is the *enforcement*.

---

## Lesson 8 — Migration script area inference must auto-include `area:tests` (2026-05-06)

**Situation**: OP-16 ticket had labels `area:backend, area:db` but not `area:tests`. The migration script's `_infer_areas` only added `db + backend` for alembic keywords. When the runner built the §5 prompt-injection, `area:tests` was on the forbidden list — codex would have refused to write the AC-required test file. Manual label patch needed before launch.

**Fix**: `scripts/jira_migrate_active_tickets.py::_infer_areas` adds: any ticket with `alembic` / `schema` / `migration` keyword in title or `alembic` in any file path → auto-add `area:tests`. Migration ALWAYS needs contract tests in this project; convention. Commit `a6e6cd9f`.

**Verification**: OP-246 (later created via the patched migration logic) had `area:tests` in labels from the start; codex worked tests without manual operator intervention.

**Generalisation**: Area inference rules should reflect *project conventions about co-required artifacts*, not just file paths. If "X always needs Y" is true at the project level, the inference should encode it.

---

## Lesson 9 — Codex cuts feature branches per DoD; merger must scan multiple refs (2026-05-06)

**Situation**: OP-15 first attempt: codex committed to `codex-work` branch. OP-15 retry: codex saw the DoD checklist line `Branch feature/OP-15-mp-w1-2-quota-tracker cut from develop (per ADR-0001)` and cut a real feature branch, committing there. The operator's merge SOP (mental model: "merge codex-work") didn't anticipate this — `git merge --no-ff codex-work` returned "Already up to date" because codex-work hadn't moved.

**Fix (immediate)**: Manual fix — `git merge --no-ff feature/OP-15-mp-w1-2-quota-tracker` after diagnosing via `cat .git/worktrees/OmniSight-codex-worktree/HEAD`.

**Fix (future)**: Convention §10/§16 update + `scripts/jira_merge_helper.py` (deferred to META tooling ticket): scan worktree's HEAD ref AND `git for-each-ref refs/heads/feature/OP-*`; merge whichever branch has the OP-N tag in commit messages.

**Verification**: OP-15 retry merged successfully via `c41086f8` (later corrected to `df148f35`). Confirmed only after diagnosing the divergence — required ~10 min of manual investigation.

**Generalisation**: When the AC mentions a specific branch name, codex will cut that branch. The merger's branch discovery cannot assume a single canonical name; must enumerate. This is a healthy behavior (ADR 0001 5-branch flow) — the tooling needs to catch up.

---

## Lesson 10 — Operator currently does Author/Reviewer/Merger simultaneously (2026-05-06)

**Situation**: First step-3 cycle (OP-16 / OP-246 / OP-15). Runner posts `[runner-cli-success]` comment after codex exits 0; ticket sits in `In Progress` with codex-bot assignee. Operator (you, with Claude assist) then: (a) reads commit + tests + AC, (b) merges codex-work / feature branch into main, (c) pushes origin, (d) transitions through Under Review → Approved → Published manually. ADR 0003 mandates *human +2 review distinct from author* + *automatic merge after +2*; in practice all three roles are collapsed into one operator pass.

**Fix (transitional acknowledgment)**: Convention §10/§16 update — explicit "transition period" disclaimer that operator currently combines roles; flag as ADR-0003-violating-in-practice; META tooling ticket tracks the Gerrit wire-up that will separate them.

**Fix (target state)**: META `meta:tooling` ticket — wire codex push to `refs/for/develop` (Gerrit), Gerrit submit hook → JIRA transition, automatic merge on +2 vote. ADR 0003 separation enforced by tooling, not by operator discipline.

**Verification (today)**: Operator manually validates each merge cognitively before instructing Claude to push. Claude does NOT vote +2 on its own work. Soft-enforce until hard-enforce lands.

**Generalisation**: When the SOP defines roles (Author / Reviewer / Merger) but the tooling collapses them, it's the *tooling* that needs to mature, not the SOP that should be relaxed. Document the gap explicitly so future contributors don't think the relaxed practice is canonical.

---

## Amendment block (corrections / additions to past entries)

(none yet)
