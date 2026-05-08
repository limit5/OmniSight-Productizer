---
id: L-OP-247
ticket: OP-247
title: Repo topology drift: `origin = GitHub` vs ADR 0002 plan
date: 2026-05-06
tags: [ci, gerrit, git, runner]
legacy_lesson: 11
---

# Repo topology drift: `origin = GitHub` vs ADR 0002 plan

**Situation**: ADR 0002 (2026-05-04) declared the target topology as **GitLab self-hosted primary, GitHub one-way mirror**. By 2026-05-06 the repo's `origin` remote was still `https://github.com/limit5/OmniSight-Productizer.git` — every dev push goes direct-to-GitHub, GitLab is unused for dev work, and ADR 0002's "do not gate any merge on GitHub" is implicitly violated. Discovered while diagnosing OP-247 prerequisites: I assumed GitLab was the dev target and was wrong.

**Fix (partial, 2026-05-06)**:
- Validated full `local → Gerrit → GitLab → GitHub` path (Steps 1-9 in this session)
- All three remotes now have `develop` + `main` aligned at the same SHA
- Origin remote not yet flipped — stays as GitHub for now (no migration date set per operator)
- L11 documents the drift so it's tracked, not silent

**Fix (target state)**: When governance Phase 2 cutover happens (no date yet — operator decides), local `origin` re-points to GitLab; GitHub becomes a `mirror` remote (read-only OSS visibility). Tracked indirectly by [OP-247](https://soraapp.atlassian.net/browse/OP-247) (runner Gerrit integration) which assumes the cutover is done; if cutover lags, OP-247 needs an explicit dependency note.

**Verification**: `git remote -v` returns `origin = https://github.com/...` — confirms drift. After cutover, `origin = https://oauth2:...@sora.services:49156/omnisight/omnisight-productizer.git`.

**Generalisation**: ADR records intent; reality may lag silently. Drift-scan periodically (e.g. as part of META audit cycles) — checked-in `git remote -v` output vs ADR 0002 should be a CI-runnable invariant once Phase 2 ships.
