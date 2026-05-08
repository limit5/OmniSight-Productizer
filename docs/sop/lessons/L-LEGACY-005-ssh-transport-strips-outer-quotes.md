---
id: L-LEGACY-005
ticket: LEGACY-005
title: SSH transport strips outer quotes
date: 2026-05-05
tags: [gerrit, legacy]
legacy_lesson: 5
---

# SSH transport strips outer quotes

**Situation**: Tried `gerrit create-group --description "Human reviewers"` over SSH. Failed with "Too many arguments: reviewers". The outer double-quotes were stripped by SSH transport before the remote shell parsed args; remote shell saw `--description Human reviewers` as 3 separate args.

**Fix**: Use double-quote-wrap-single-quote pattern for SSH arg values containing spaces:
```bash
gerrit create-group ai-reviewer --description "'AI bot reviewers (max +1)'"
```
The outer `"` is stripped by SSH transport; the inner `'` survives to the remote shell, which sees `--description 'AI bot reviewers (max +1)'` as one arg.

**Verification**: `gerrit ls-groups -v | grep ai-reviewer` shows full description string preserved.

**Generalisation**: SSH transport quote-stripping is asymmetric (outer quotes consumed). Any tool invoked via SSH with multi-word arg values needs the double-wrap pattern. Document as gotcha #5 in `reference_gerrit_self_hosted.md`.
