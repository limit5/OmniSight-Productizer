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
| 11 | 2026-05-06 | Repo topology drift — local `origin = GitHub` instead of GitLab per ADR 0002; live and tracked but no migration date | governance migration plan re-read |
| 12 | 2026-05-06 | Gerrit replication.config does NOT do env-var or `${name}` case substitution; lowercase target paths must be hardcoded per remote | OP-247 path validation |
| 13 | 2026-05-06 | Gerrit replication URL must NOT contain credentials inline; put username + password in `secure.config` `[remote "name"]` block instead | OP-247 path validation |
| 14 | 2026-05-06 | Git hooks for worktrees live in `--git-common-dir/hooks/`, NOT `--git-dir/hooks/`; the latter silently never fires | OP-17 first auto-push run |
| 15 | 2026-05-06 | Worktree must have bot identity in `git config user.email/name` before agent commits; otherwise Gerrit rejects "email not registered" | OP-17 first auto-push run |
| 16 | 2026-05-06 | Runner needs **per-ticket fresh-sync** from Gerrit develop — no carryover from prior tickets, no rebase onto local main | OP-17 worktree state confusion |
| 17 | 2026-05-06 | `pre_pickup_ok` must check live_state in **worktree cwd**, not main repo cwd; sync runs first so checks see fresh state | OP-18 first-launch failed pre-pickup on stale main repo |
| 18 | 2026-05-07 | Event consumers must combine stream handling with startup catchup and idempotent terminal-state checks | OP-689 Gerrit/JIRA bridge |

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

**Fix (target state)**: [OP-247](https://soraapp.atlassian.net/browse/OP-247) META `meta:tooling` ticket — wire codex push to `refs/for/develop` (Gerrit), Gerrit submit hook → JIRA transition, automatic merge on +2 vote. ADR 0003 separation enforced by tooling, not by operator discipline. Tier L, blocks: nothing; soft prereq: governance migration plan Phase 2 items.

**Verification (today)**: Operator manually validates each merge cognitively before instructing Claude to push. Claude does NOT vote +2 on its own work. Soft-enforce until hard-enforce lands.

**Generalisation**: When the SOP defines roles (Author / Reviewer / Merger) but the tooling collapses them, it's the *tooling* that needs to mature, not the SOP that should be relaxed. Document the gap explicitly so future contributors don't think the relaxed practice is canonical.

---

## Lesson 11 — Repo topology drift: `origin = GitHub` vs ADR 0002 plan (2026-05-06)

**Situation**: ADR 0002 (2026-05-04) declared the target topology as **GitLab self-hosted primary, GitHub one-way mirror**. By 2026-05-06 the repo's `origin` remote was still `https://github.com/limit5/OmniSight-Productizer.git` — every dev push goes direct-to-GitHub, GitLab is unused for dev work, and ADR 0002's "do not gate any merge on GitHub" is implicitly violated. Discovered while diagnosing OP-247 prerequisites: I assumed GitLab was the dev target and was wrong.

**Fix (partial, 2026-05-06)**: 
- Validated full `local → Gerrit → GitLab → GitHub` path (Steps 1-9 in this session)
- All three remotes now have `develop` + `main` aligned at the same SHA
- Origin remote not yet flipped — stays as GitHub for now (no migration date set per operator)
- L11 documents the drift so it's tracked, not silent

**Fix (target state)**: When governance Phase 2 cutover happens (no date yet — operator decides), local `origin` re-points to GitLab; GitHub becomes a `mirror` remote (read-only OSS visibility). Tracked indirectly by [OP-247](https://soraapp.atlassian.net/browse/OP-247) (runner Gerrit integration) which assumes the cutover is done; if cutover lags, OP-247 needs an explicit dependency note.

**Verification**: `git remote -v` returns `origin = https://github.com/...` — confirms drift. After cutover, `origin = https://oauth2:...@sora.services:49156/omnisight/omnisight-productizer.git`.

**Generalisation**: ADR records intent; reality may lag silently. Drift-scan periodically (e.g. as part of META audit cycles) — checked-in `git remote -v` output vs ADR 0002 should be a CI-runnable invariant once Phase 2 ships.

---

## Lesson 12 — Gerrit replication.config templating: no env vars, no case transform (2026-05-06)

**Situation**: When wiring Gerrit → GitLab replication for OP-247 path validation, my first replication.config draft was:
```ini
url = https://oauth2:${GITLAB_TOKEN}@sora.services:49156/omnisight/${name}.git
```
Two bugs:
1. **`${GITLAB_TOKEN}` is not substituted** — Gerrit's replication.config only recognises `${name}` (project name) as a template variable; arbitrary env-var-like names are kept literal. `replication list --detail` revealed the URL was stored with `${GITLAB_TOKEN}` as a literal string.
2. **`${name}` preserves case** — Gerrit project name `omnisight/OmniSight-Productizer` substituted into URL gave `.../omnisight/OmniSight-Productizer.git` (mixed case). GitLab path is forcibly lowercase (`omnisight-productizer`), so the URL 404'd.
3. **Plus a duplicate-prefix bug** — using `omnisight/${name}.git` doubled the `omnisight/` because `${name}` already contained it.

**Fix**: Hardcode the URL per remote (no template variables). For single-project setups this is fine; for multi-project, write one `[remote "..."]` block per project. Final working config:
```ini
[remote "gitlab-mirror"]
  url = https://sora.services:49156/omnisight/omnisight-productizer.git
  push = +refs/heads/*:refs/heads/*
  push = +refs/tags/*:refs/tags/*
  projects = omnisight/OmniSight-Productizer
  replicateOnStartup = true
```

**Verification**: After fix + `gerrit plugin reload replication`, `replication start --all --wait` returned `Replicate omnisight/OmniSight-Productizer refs ..all.. to sora.services:49156, Succeeded! (OK)`. GitLab's `develop` branch tip then byte-equal'd Gerrit's.

**Generalisation**: When using template strings in config files, verify what *exactly* gets substituted by checking the runtime view (`replication list --detail` here) — never trust the source file as ground truth for what's loaded. Per-project hardcoding is more verbose but unambiguous.

---

## Lesson 13 — Gerrit credentials in URL is wrong; use `secure.config` (2026-05-06)

**Situation**: Same OP-247 wire-up. My first replication.config put credentials inline in the URL: `https://oauth2:${GITLAB_TOKEN}@sora.services:...`. Even if `${GITLAB_TOKEN}` HAD substituted, this is **the wrong place**:
- URLs in `replication.config` are stored in the world-readable section
- `replication list --detail` (which any project member can run) shows the URL — exposing the token
- The "blessed" path is `etc/secure.config` which Gerrit reads with restricted permissions

**Fix**: Plain URL in `replication.config`, credentials in `etc/secure.config`:
```ini
# secure.config (chmod 600 typical)
[remote "gitlab-mirror"]
  username = oauth2
  password = glpat-xxxxxxxxxxxxxxxx
```
Gerrit auto-pairs the `[remote "gitlab-mirror"]` blocks across the two files.

**Verification**: After moving creds to secure.config, `replication list --detail` shows the URL without any password leak. Replication still works because Gerrit uses secure.config username + password as HTTP basic auth.

**Generalisation**: Credentials live in **dedicated secrets files** (per the tool's documented contract), never in URLs or general config. URL-inline auth is convenient for quick scripts but a leak vector in any config that gets `cat`'d or queried via API.

---

## Lesson 14 — Git hooks for worktrees live at `--git-common-dir`, not `--git-dir` (2026-05-06)

**Situation**: OP-17 first auto-push run via `auto-runner-jira.py`'s OP-247 Phase 1 logic. `install_commit_msg_hook` resolved the hook path via `git rev-parse --git-dir` and wrote the script there. But codex's commits had no Change-Id; Gerrit rejected the push with `missing Change-Id in message footer`. Manual debug: ran the hook script directly with a test message → it worked. So the script was correct; git just wasn't invoking it.

**Fix**: For Git worktrees, `--git-dir` returns the worktree-specific path (`.git/worktrees/<name>`), but git executes hooks from `--git-common-dir/hooks` (the parent's `.git/hooks`). Hooks installed at the worktree-specific path silently never fire. `install_commit_msg_hook` now uses `_git_common_dir()` which calls `git rev-parse --git-common-dir`. Commit `83e89baa` (Change #28).

**Verification**: Post-fix, `git commit --amend --no-edit` triggers the hook and adds Change-Id. Confirmed by manual hook-trigger test before-after the path correction.

**Generalisation**: Any tool that resolves git paths for worktree-shared resources (hooks, refs, config) needs to use `--git-common-dir`, not `--git-dir`. The two are interchangeable only for non-worktree repos. When integrating with worktrees, audit every git path lookup.

---

## Lesson 15 — Worktree needs bot identity set before agent commits (2026-05-06)

**Situation**: OP-17 first auto-push run. After commit-msg hook fix (L14), the runner pushed to Gerrit `refs/for/develop` and Gerrit responded `email address row7-self-agent@omnisight.local is not registered in your account, and you lack 'forge committer' permission`. The codex commit's committer was the worktree's default git config user (the operator's env user `Agent-row7-self-agent`), not codex-bot's registered Gerrit email.

**Fix**: `set_bot_identity_in_worktree(worktree, agent_class)` runs `git config user.email` + `user.name` in the worktree before invoking the CLI. Identity is derived from the agent_class via `_GERRIT_AUTH_BY_CLASS` (claude-bot for `subscription-claude` / `api-anthropic`; codex-bot for `subscription-codex` / `api-openai`). Email convention: `rt3628+<bot-username>@gmail.com`. Idempotent — setting same value is a no-op. Commit `83e89baa`.

**Verification**: Post-fix, codex commits have `committer: codex-bot <rt3628+codex-bot@gmail.com>` which matches Gerrit's account. Push succeeds without manual `--reset-author` workaround.

**Generalisation**: Whenever an automated agent commits in a shared workspace, the workspace's identity must match the agent's principal in downstream gates. Setting identity on every agent invocation is cheaper (and idempotent) than relying on workspace-state being correct from prior setup.

---

## Lesson 16 — Per-ticket fresh-sync to Gerrit develop (2026-05-06)

**Situation**: OP-17 launch. The codex worktree was on `feature/OP-11-mp-w1-1-orchestrator` from the previous ticket; pre-pickup live_state checks in main repo cwd failed because main repo lacked the OP-11 module (chain divergence between local `main` and Gerrit `develop`). Manual fix: `git checkout -B develop FETCH_HEAD` in worktree, then `git merge --no-ff origin/develop` in main repo. After codex completed work, `ensure_change_ids` rebased onto local `main` which now contained a merge commit with row7-self-agent committer → Gerrit rejected (separate from L15's same-error-different-cause).

**Fix**: `sync_to_gerrit_develop(worktree, agent_class, ticket_key)` runs at the start of every ticket pickup in `auto-runner-jira.py`:

```python
1. git fetch <gerrit-ssh-url> develop      # canonical source
2. develop_sha = git rev-parse FETCH_HEAD  # capture explicitly
3. git switch -C feature/<TICKET>-runner-fresh <develop_sha>
4. git clean -fdx                          # discard any partial state
```

Returns `WorktreeSyncResult(branch_name, develop_sha, detail)`. Caller passes `develop_sha` to `ensure_change_ids` so rebase base is the Gerrit develop tip (not local main). Commit `83e89baa`.

**Verification**: After Phase 1.5 ships, OP-18 (next codex run) is the first ticket expected to need zero manual recovery. Verification deferred to that run.

**Generalisation**: Local refs (especially `main`) drift from canonical refs (Gerrit develop) over time. Rather than tracking + repairing drift, automate a fresh-sync at every ticket boundary. The workflow's source of truth is Gerrit; any local repo state is throwaway. Aligns with ADR 0001 5-branch flow ("feature/* are throwaway, develop is integration trunk").

---

## Lesson 17 — `pre_pickup_ok` must check live_state in worktree cwd (2026-05-06)

**Situation**: After Phase 1.5 shipped (commit `83e89baa`, Change #28), launching codex on OP-18 failed at pre-pickup with `file_exists: backend/agents/provider_adapters/__init__.py: MISSING`. The file existed on Gerrit develop (shipped via OP-17 = Change #27) but main repo's local `main` branch hadn't been merged yet. `live_state_check.evaluate` ran in `REPO_ROOT` cwd (the runner host's main repo), saw stale state, blocked pickup.

The `sync_to_gerrit_develop` shipped in Phase 1.5 fresh-syncs the *worktree* but not the runner-host repo. They serve different roles: worktree is where codex commits land; main repo is where `auto-runner-jira.py` runs from. Pre-pickup checks were running against the wrong cwd.

**Fix**: `live_state_check.evaluate(requirements, cwd=Path | None)` — handlers (`alembic_head`, `file_exists`, `command_succeeds`) accept a `cwd` parameter. Backward-compatible default (`cwd=None`) falls back to `REPO_ROOT`.

`jira_dispatch.pre_pickup_ok(client, snapshot, worktree_path=None)` — optional `worktree_path` propagates to `evaluate(cwd=worktree_path)`.

`auto-runner-jira.py` reorders main loop:
```
1. select ticket
2. resolve worktree_path
3. sync_to_gerrit_develop(worktree_path)  ← MOVED UP from after pre_pickup
4. pre_pickup_ok(client, snapshot, worktree_path=worktree_path)
5. transition + invoke CLI
```

Sync now runs BEFORE pre-pickup, so when pre-pickup queries the live state it sees the worktree just-fresh-synced from Gerrit develop tip.

**Verification**: 7 new tests in `test_live_state_check.py` + `test_jira_dispatch.py`. Pinned that `evaluate(cwd=tmp)` resolves `file_exists` against tmp; `evaluate(cwd=None)` keeps REPO_ROOT default. Pinned `pre_pickup_ok` signature. Empirical: OP-18 retry after the merge fix (before this refactor) succeeded — but ONLY because operator manually merged `origin/develop` into local main. After this refactor, that manual step is unnecessary.

**Generalisation**: When tooling has multiple file-system "vantage points" (runner host, agent worktree, Gerrit develop, GitHub mirror, GitLab mirror), every check needs to specify which vantage it queries. Defaulting to "the runner's cwd" is correct for runner self-introspection but wrong for "what's the agent's actual workspace state". The cwd parameter makes this explicit, bypasses the silent ambiguity.

---

## Lesson 18 — Stream consumers need catchup plus idempotency (2026-05-07)

**Situation**: OP-19 stayed in `Approved` for hours after operator +2 because the interactive session that previously handled ad-hoc JIRA advancement had ended. Gerrit merge state existed, but there was no long-running consumer to translate `change-merged` into JIRA `Published`.

**Fix**: OP-689 added a stateless Gerrit `stream-events` consumer with a startup catchup pass and per-ticket status gate. The bridge only uses transition id=7 (`Deploy`) and first checks JIRA state, so duplicate stream/catchup races become harmless and the daemon cannot perform the ADR 0003 `Approve` hop.

**Verification**: `backend/tests/test_gerrit_jira_bridge.py` covers startup catchup, stream merge handling, malformed events, duplicate/multi-ticket protection, reconnect, auth failure, JIRA retry classes, and transition id drift guard.

**Generalisation**: Event-driven automation that represents terminal workflow state should never rely on the stream alone. Pair stream consumption with catchup from source-of-truth state, make the terminal transition idempotent, and make authority boundaries explicit in tests.

---

## Amendment block (corrections / additions to past entries)

(none yet)

<!-- e2e smoke 2026-05-06: validate Gerrit→GitLab→GitHub replication chain. To be reverted post-verify. -->


## Lesson 19 — Production Readiness Gate Q1 must check DB image, not just app image (2026-05-07)

**Situation**: OP-693 deploy attempt (16:25) hit alembic 0193 (BP.Q.4 embedding_chunks) failing with `extension "vector" is not available`. The migration's docstring (line 36-38) says "PostgreSQL deployments must enable the existing vector extension" but the running pg-primary container used `postgres:16-alpine` which doesn't ship pgvector. SOP §1 Production Readiness Gate Q1 ("這條 code path 在 production image 真的跑得起來嗎") had been checked against the backend image (correctly verified `import backend.merger_agent` works), but never against the DB image — pgvector is a DB-side dependency that the alembic apply touches.

**Fix**: SOP §1 Production Readiness Gate Q1 needs to be answered for EACH artifact the migration / deploy / runtime touches: backend image, DB image, sidecar images (caddy, cloudflared), external services (Gerrit, JIRA). For DB migrations specifically, run `docker compose run backend-a python -m alembic upgrade --sql heads` against fresh DB locally (or staging) to surface CREATE EXTENSION / CREATE INDEX / CHECK constraint issues that only fail at apply time.

The operator-side fix landed: deploy/postgres-ha/Dockerfile.pgvector builds `omnisight-postgres-pgvector:16-alpine` from `postgres:16-alpine` + `apk build-base + git + clone pgvector v0.8.0 + make install`. Compose updated to use this image. Replication preserved (alpine uid 70 stays consistent across primary + standby).

**Verification**: After image swap, alembic upgrade heads applied all 13 migrations cleanly. CREATE EXTENSION vector + CREATE INDEX hnsw both succeeded. Subsequently exposed a separate bug (HNSW on bare-dim vector — fixed via OP-707 #72) but that's downstream of the pgvector availability issue.

**Generalisation**: "Static lists / catalogs aligned with live" (SOP § Production Readiness Gate Q2) needs to extend beyond the app's TABLES_IN_ORDER / drift-guard tests — DB-level extension requirements (`CREATE EXTENSION`), DB-level CHECK constraints, sidecar version pins, and runtime feature flags all need their own pre-flight verification against the live image they'll run on.


## Lesson 20 — `echo >>` to .env without trailing-newline guard concatenates with last token (2026-05-07)

**Situation**: OP-693 SP-D added `OMNISIGHT_GERRIT_ENABLED=true` to .env via `echo "..." >> .env`. The .env's last line (`OMNISIGHT_CLOUDFLARE_TUNNEL_TOKEN=eyJ...`) lacked a trailing newline, so `echo` appended directly to that line: the resulting line read `...OMNISIGHT_CLOUDFLARE_TUNNEL_TOKEN=eyJ...gAzOMNISIGHT_GERRIT_ENABLED=true`. Both vars corrupted: cloudflared's running container kept a copy of the pre-edit token in memory (didn't re-read .env) and stayed alive, but a subsequent restart would've broken cloudflared. Backend rolling restart picked up the corrupted .env on the env_file load and silently misread BOTH values.

**Fix**: Always check + ensure trailing newline before appending to .env / config files via `echo >>`. Pattern:
```bash
[ -z "$(tail -c 1 .env)" ] || echo "" >> .env  # add newline if last char isn't already
printf '%s=%s\n' "$KEY" "$VALUE" >> .env
```

Or use Python's pathlib for safer text editing: `text = p.read_text(); if not text.endswith('\n'): text += '\n'; text += new_line + '\n'; p.write_text(text)`.

**Verification**: Repaired with regex split — `re.compile(r'^(OMNISIGHT_CLOUDFLARE_TUNNEL_TOKEN=[A-Za-z0-9+/=]+)(OMNISIGHT_GERRIT_ENABLED=true)$', re.M).sub(r'\1\n\2\n', text)`. Backend restart re-read corrected .env; cloudflared restart_count stayed 0 (its token was loaded at startup before .env was touched, and the in-memory copy is not affected by re-reads); subsequent restart picked up the fixed token.

**Generalisation**: Any tool that appends to a config file via shell redirection MUST guard against missing trailing newlines. Production secrets in .env are particularly risky because a corrupted long token (like a JWT or OAuth token) can produce VERY hard-to-debug failures (the whole token becomes one giant invalid string, which fails parsing in different layers depending on consumer).


## Lesson 21 — Webhook endpoints with `require_operator + signature` are dual-gate, not redundant (2026-05-07)

**Situation**: OP-708 surfaced that backend's `/api/v1/orchestrator/merge-conflict` endpoint requires BOTH a valid Bearer api_key (passes `require_operator`) AND a separate `X-Jira-Webhook-Secret` header (passes `_verify_jira_signature`). Gerrit's webhooks plugin natively supports `secret` for HMAC body signing into `X-Hub-Signature`, but the backend doesn't verify HMAC of the body — it does a constant-time string compare against `settings.jira_webhook_secret`. So Gerrit needs to set TWO custom headers, not one HMAC signature.

**Fix**: Gerrit webhooks plugin supports `header = Name: Value` lines in `[remote "..."]` blocks (Gerrit 3.13 confirmed via project.config push). Set:
```
[remote "merge-conflict-webhook"]
  url = ...
  event = change-merge-failed
  header = Authorization: Bearer <api_key>          # require_operator
  header = X-Jira-Webhook-Secret: <jira_webhook_secret>  # _verify_jira_signature
```

Both go in refs/meta/config (admin-only readable), so secret-in-config is acceptable for this deployment topology.

**Verification**: External curl test (`curl -X POST -H Authorization -H X-Jira-Webhook-Secret https://ai.sora-dev.app/api/v1/orchestrator/merge-conflict`) returned HTTP 200 — endpoint accepted both headers, validated payload, dispatched to merge_arbiter. Direct in-container invocation of `merge_arbiter.on_merge_conflict_webhook` also confirmed the merger code path runs (with downstream LLM bug found, separate ticket OP-709).

**Generalisation**: When an endpoint name suggests "webhook" but auth requires user/operator credentials, the design intent is dual-gate (defense-in-depth: signature isolates abuse from external internet + user auth provides identity for audit). Don't simplify to "just signature" without team agreement — the operator identity is sometimes load-bearing for audit_log entries / per-user rate limits / etc. For Gerrit-side, configure custom headers; don't try to bend Gerrit's HMAC `secret` into the user-auth shape.
