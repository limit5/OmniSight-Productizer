---
id: L-OP-247
ticket: OP-247
title: Gerrit replication.config templating: no env vars, no case transform
date: 2026-05-06
tags: [ci, gerrit, git, runner]
legacy_lesson: 12
---

# Gerrit replication.config templating: no env vars, no case transform

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
