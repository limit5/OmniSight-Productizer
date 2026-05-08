---
id: L-OP-766
ticket: OP-766
title: Release auto-promotion must stay fast-forward-only
date: 2026-05-08
tags: [release, gerrit, git, audit]
---

# Release auto-promotion must stay fast-forward-only

**Situation**: Sprint D needed `develop` to promote to `main` after the
machine milestone gates turn green. Letting the daemon merge would hide
hotfix divergence and make the release branch policy depend on automation
judgement.

**Fix**: `backend.agents.auto_promote_main` performs two explicit git
pre-checks: `main..develop` must contain work to promote, and
`develop..main` must be empty. Only that shape runs
`git push gerrit develop:main`; any `main`-only commit aborts and alerts
the operator with the divergent commit list.

**Verification**: `backend/tests/test_auto_promote_main.py` covers the
clean fast-forward promotion, the non-fast-forward alert path, ignored
non-ready events, and cursor-based consumption of the OP-762 checker log.

**Generalisation**: Branch promotion daemons should move refs only when
the desired topology is already true. They may detect and report
divergence, but they should not invent a merge policy.
