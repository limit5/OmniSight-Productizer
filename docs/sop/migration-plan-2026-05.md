# Governance Migration SOP — 2026-05

**Scope**: operational steps to migrate from single-`master` direct-push to the 5-branch Git Flow + GitLab primary + Gerrit review topology decided in 2026-05-04 strategic session.

**Authority**: this SOP implements [ADR 0001](../adr/0001-five-branch-gitflow.md) - [ADR 0005](../adr/0005-tier-authority-levels.md). If conflict, ADR wins; SOP is the operational steps not the decision record.

**Phases**:
- Phase 0 — design freeze + Track A/B/C baseline (in progress 2026-05-04)
- Phase 1 — `master → main` rename + Git Flow branch cut (~1 day)
- Phase 2 — GitLab connected + CI migration + GitHub mirror (~1-2 days)
- Phase 3 — Gerrit + MCP + JIRA workflow (~1 week)
- Phase 4 — Runner switchover to feature/* + old flow retired (~1 week)
- Phase 5 — WSL2 26.04 dev/prod env isolation (deferred ~2 weeks)

---

## Phase 0 — Design freeze + access baseline

### Status (2026-05-04)

| Item | Status |
|---|---|
| ADR 0001-0005 written | ✓ this commit |
| Phase 1 SOP written | ✓ this file |
| Track A — JIRA Atlassian baseline (Claude + Codex 4 verbs) | ✓ Track A green |
| Track B — GitLab self-hosted (claude + codex 6 verbs) | ✓ Track B 5/6 green (6c by-design 403) |
| Track C — Gerrit baseline | ⏳ deferred until Phase 1-2 stabilised |

### Phase 0 acceptance gate (must satisfy before Phase 1)

- [x] All 5 ADRs reviewed by operator (no objections)
- [x] Track A green: each AI agent can CREATE / TRANSITION / COMMENT / DELETE on JIRA OP project
- [x] Track B green (modulo by-design 6c): each AI agent can clone / commit / push / open MR / cross-comment
- [ ] GitLab `external_url` BLOCKER fixed (per `reference_gitlab_self_hosted.md`) — required before Phase 2 mirror push setup, but does NOT block Phase 1 branch operations
- [ ] Backup verified: `git bundle create /var/backups/omnisight-pre-rename.bundle --all` + offsite copy
- [ ] No active runner sessions during Phase 1 (`tmux ls` empty; orphan check)

---

## Phase 1 — master → main rename + Git Flow branch cut (~1 day)

### Pre-flight

- [ ] All in-flight feature branches merged or stashed
- [ ] All AI runners stopped (`tmux kill-server` or per-session kill)
- [ ] Backup bundle created (see Phase 0 gate)
- [ ] Operator on-call available (in case of revert needed)

### Step 1.1 — Rename master → main locally

```bash
cd /home/user/work/sora/OmniSight-Productizer
git branch -m master main
git push -u origin main
git remote set-head origin main
```

### Step 1.2 — Update remote default branch

GitHub: Settings → Branches → Default branch → main → confirm.
GitLab: Settings → Repository → Default branch → main → confirm.

### Step 1.3 — Cut develop + first release branch

```bash
git checkout main
git checkout -b develop
git push -u origin develop

# Tag current main tip as last pre-Git-Flow release
git tag -a v0.3.0 -m "Last pre-Git-Flow tip; first release tag of 5-branch model"
git push origin v0.3.0

# Set up release branch convention (no actual release/v0.4.0 branch yet)
echo "release/v* branches cut from develop tip when freezing for stabilisation" > docs/branching/RELEASE_CONVENTION.md
echo "hotfix/<id> branches cut from main tip for emergency fixes" > docs/branching/HOTFIX_CONVENTION.md
```

### Step 1.4 — Sweep `master` references in tooling

This is a 30+ file sweep. Use `grep` first to enumerate:

```bash
grep -rn 'master' --include='*.py' --include='*.sh' --include='*.txt' --include='*.yaml' --include='*.yml' --include='*.md' \
  --exclude-dir='.git' --exclude-dir='node_modules' --exclude-dir='.venv' \
  | grep -v 'test_assets' | grep -v 'CHANGELOG' | grep -iE 'master\b'
```

Known callsites (must update):

| File | Line region | Change |
|---|---|---|
| `deploy/prod-deploy-allowlist.txt` | full file | `branch:master` → `branch:main`; add `tag:v*` |
| `scripts/deploy-prod.sh` | `BRANCH=master` default | → `BRANCH=main` |
| `auto-runner.py` | string `'master'` | search + flip |
| `auto-runner-codex.py` | string `'master'` | search + flip |
| `auto-runner-sdk.py` | string `'master'` | search + flip |
| `coordination.md` | text references | search + flip; add note "renamed 2026-05-04" |
| `docs/operations/runner-strategy.md` | references | search + flip |
| `.github/workflows/*.yml` | branch triggers `branches: [master]` | → `[main]` |
| Docs / README / HANDOFF entries | `master` mentions | flip; add ADR cross-link |

Commit each sweep as one logical group (one commit for deploy infra, one for runner scripts, one for docs, one for CI).

### Step 1.5 — Branch protection setup

GitLab + GitHub both:

```
main:
  - require pull request (no direct push)
  - require status checks (CI green)
  - allow force push: NO
  - allow delete: NO
  - merge method: fast-forward only

develop:
  - require pull request
  - allow direct merge commit
  - allow force push: NO
  - allow delete: NO

release/v*:
  - require pull request
  - allow force push: NO
  - allow delete: only after release shipped + 30 days

feature/*:
  - allow direct push (owner only)
  - allow force push (owner only)
  - allow delete (after merge)

hotfix/*:
  - require pull request
  - allow force push: NO
  - allow delete: after merge
```

### Step 1.6 — Validation

- [ ] `git push origin master` should fail (branch deleted)
- [ ] `git push origin main` from non-protected user should fail (protection)
- [ ] CI runs on both `main` + `develop` push events
- [ ] Deploy script: `./scripts/deploy-prod.sh` defaults to `main` (dry-run + verify)
- [ ] Runner: start a test runner, confirm it pushes to `feature/*` not `master`
  - **Note**: Phase 1 only updates the *default*. Phase 4 enforces feature-branch-only. Phase 1 still allows runners to write to main as a transitional behaviour.
- [ ] Bundle backup re-verifies: `git bundle verify /var/backups/omnisight-pre-rename.bundle`

### Step 1.7 — Documentation + retrospective

- [ ] HANDOFF.md entry: `## [Operator] 2026-05-XX Phase 1 complete — master→main rename`
- [ ] Update governance plan memory: Phase 1 status → DONE
- [ ] First retrospective scheduled (1 week post-Phase-1): cover any tooling regressions, missed callsites

### Phase 1 rollback (if catastrophic)

```bash
# 1. Revert all commits since rename in reverse order
git reset --hard <pre-rename-commit-sha>

# 2. Restore master branch
git branch -m main master
git push -u origin master --force-with-lease

# 3. Restore default branch on remotes (manual UI step, both GitLab + GitHub)

# 4. From bundle if local repo trashed
git clone /var/backups/omnisight-pre-rename.bundle ../recovery
```

The 1-week observation window is the key signal — if no regressions surface in 7 days, rollback path becomes irrelevant.

---

## Phase 2 — GitLab connected + CI migration + GitHub mirror

### Phase 2 entry gate (must satisfy)

- [ ] Phase 1 1-week observation window passed without regression
- [ ] GitLab `external_url` BLOCKER fixed
- [ ] HTTPS enabled on GitLab (Cloudflare Tunnel or Let's Encrypt + reverse proxy)

### Step 2.1 — GitLab repo flip to primary

- [ ] All 5 branches mirrored from GitHub to GitLab (initial sync)
- [ ] GitLab repo settings: protected branches mirror Phase 1 config
- [ ] Operator + bots have correct access (Owner / Maintainer / Developer)

### Step 2.2 — CI migration to GitLab CI

- [ ] `.gitlab-ci.yml` written (lint / test / pip-audit / DLP / nightly tsc)
- [ ] GitHub Actions stripped to announce-only (release notes posting; no test gating)
- [ ] CI green on develop + main push events

### Step 2.3 — GitHub mirror push setup

- [ ] GitLab repo settings → Mirroring → Push to GitHub
- [ ] Schedule: every push (real-time mirror)
- [ ] Verify: `git push origin develop` triggers GitHub update within 60s
- [ ] PR auto-close webhook on GitHub mirror (any PR opened gets closed with link to GitLab equivalent)

### Step 2.4 — Validation

- [ ] CI gates merge (red CI → merge blocked)
- [ ] Mirror lag monitored (Prometheus alert if > 5 min lag)
- [ ] Backup bundle from GitLab (replaces GitHub-as-backup)

---

## Phase 3 — Gerrit + MCP + JIRA workflow

### Phase 3 entry gate

- [ ] Phase 2 1-week observation passed
- [ ] Gerrit infra stood up (separate dedicated server, not co-located with GitLab)

### Step 3.1 — Gerrit project setup

- [ ] Gerrit pulls main + develop + feature/* from GitLab (replication plugin)
- [ ] commit-msg hook installed for all contributors
- [ ] non-ai-reviewer + ai-reviewer + merger-agent + lint-bot + security-bot groups created
- [ ] Each AI agent SSH key uploaded

### Step 3.2 — Submit-rule + Tier config

- [ ] O7 submit-rule (Prolog) deployed
- [ ] `configs/governance/tier-paths.yaml` deployed
- [ ] 4-layer protection wired: path force-upgrade + S whitelist + reviewer monotonicity + audit log
- [ ] Trial run: submit a Tier S patchset, verify ai-self-+2 works
- [ ] Trial run: submit a Tier L patchset, verify human +2 required

### Step 3.3 — MCP servers

- [ ] mcp-atlassian (official) configured for Claude + Codex separately
- [ ] mcp-gitlab (official) configured per agent
- [ ] mcp-gerrit (FastMCP, custom-authored) — Phase 3 deliverable
- [ ] Each agent's `.claude.json` / equivalent updated

### Step 3.4 — JIRA workflow templates

- [ ] Issue templates: feature / bug / chore / spike
- [ ] Workflow: Backlog → Refining → Ready → In Progress → In Review → Done
- [ ] Status flow attached to OP project
- [ ] AI agent can transition issue via mcp-atlassian (verified per agent)

### Step 3.5 — Runner switchover

- [ ] Runners push to `feature/JIRA-XXX-<owner>-*` instead of master/main
- [ ] Runner integrates with Gerrit: post-commit `git push origin HEAD:refs/for/develop`
- [ ] Patchset ID + Change-Id embedded in commit message
- [ ] AI +1 review automation (lint-bot / security-bot run on every patchset)

---

## Phase 4 — Runner switchover + old flow retired

- [ ] Direct push to main forbidden (except hotfix/* via release manager)
- [ ] All in-flight runners migrated to feature/* flow
- [ ] First retrospective: misclassification rate / WIP overflow / review velocity

## Phase 5 — WSL2 dev/prod env isolation (deferred)

Out of Phase 1-4 scope. Plan in `docs/strategy/wsl-prod-env-isolation.md` (Phase 5 deliverable).

---

## Quarterly review

Tier rules + Phase plan + role definitions reviewed quarterly (first review: 2026-08). Adjust based on retrospective findings:
- Misclassification trend (audit log)
- Review velocity (Gerrit dashboard)
- WIP overflow (per-agent + per-tier work in progress)
- Cost (Atlassian + GitLab + Gerrit infra)
