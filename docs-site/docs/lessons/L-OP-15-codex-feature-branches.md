---
id: L-OP-15
ticket: OP-15
title: Codex cuts feature branches per DoD; merger must scan multiple refs
date: 2026-05-06
tags: [ci, git, jira, runner]
legacy_lesson: 9
---

# Codex cuts feature branches per DoD; merger must scan multiple refs

**Situation**: OP-15 first attempt: codex committed to `codex-work` branch. OP-15 retry: codex saw the DoD checklist line `Branch feature/OP-15-mp-w1-2-quota-tracker cut from develop (per ADR-0001)` and cut a real feature branch, committing there. The operator's merge SOP (mental model: "merge codex-work") didn't anticipate this — `git merge --no-ff codex-work` returned "Already up to date" because codex-work hadn't moved.

**Fix (immediate)**: Manual fix — `git merge --no-ff feature/OP-15-mp-w1-2-quota-tracker` after diagnosing via `cat .git/worktrees/OmniSight-codex-worktree/HEAD`.

**Fix (future)**: Convention §10/§16 update + `scripts/jira_merge_helper.py` (deferred to META tooling ticket): scan worktree's HEAD ref AND `git for-each-ref refs/heads/feature/OP-*`; merge whichever branch has the OP-N tag in commit messages.

**Verification**: OP-15 retry merged successfully via `c41086f8` (later corrected to `df148f35`). Confirmed only after diagnosing the divergence — required ~10 min of manual investigation.

**Generalisation**: When the AC mentions a specific branch name, codex will cut that branch. The merger's branch discovery cannot assume a single canonical name; must enumerate. This is a healthy behavior (ADR 0001 5-branch flow) — the tooling needs to catch up.

---

*Source: `docs/sop/lessons/L-OP-15-codex-cuts-feature-branches-per-dod-merger-must-scan-multipl.md` (proof-of-concept copy under OP-786; full lessons corpus migrates in a follow-up ticket).*
