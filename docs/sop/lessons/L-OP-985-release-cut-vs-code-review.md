---
id: L-OP-985
ticket: OP-985
title: Code-review and release-cut are different operations — don't force one through the other
date: 2026-05-12
tags: [release, gerrit, git, architecture, audit]
---

# Code-review and release-cut are different operations — don't force one through the other

**Situation**: Sprint D's "advance `refs/heads/main` to the accepted release
content" step (D5 / the `RELEASE-vX.Y.Z` R3 child) was implemented three times
on top of the *code-review* channel — push `develop` to the `refs/for/main`
magic ref. AUDIT-13 (OP-960) shipped it for the single-commit case (mocked
Gerrit). AUDIT-13a (OP-961) made the cron script delegate to it. AUDIT-13b
(OP-962) added a conditional merger-bot `+2` submit-requirement
(`MainFastForwardMergerPlus2`) to ungate it. Every layer inherited the same
defect: pushing the *commit range* `main..develop` to `refs/for/main` creates
**one Gerrit change per intervening commit** — a chain an operator must `+2` and
submit in order. For the first live release (OP-925, RELEASE-v0.5.0-rc1) that
chain was 61 commits long; Gerrit rejected the push wholesale with `no new
changes` (the develop commits already carried Change-Ids from when they were
reviewed on the way *in*) and `receive.maxBatchChanges` (default 10) would have
killed it anyway. The mechanism could not promote *any* release after the first.

**Fix**: Recognise the category error and stop forcing it. A release cut is
**one event** — "this is the snapshot we're shipping" — not N code reviews; the
content of `develop` was *already* reviewed commit-by-commit on the way in.
ADR-0020 (AUDIT-26c / OP-982) redesigns it as: `auto_promote_main` does
`git merge --no-ff develop` from `main`'s tip and pushes **that single merge
commit** to `refs/for/main` → one Gerrit change → one `+2` set → one submit;
`submit-type: MERGE_ALWAYS` on `refs/heads/main` records the merge on submit; a
new **quad-keyed** (`branch:main` ∧ `topic:^release-v…$` ∧
`hashtag:"milestone:R3-fastforward"` ∧ `author:` ∈ allow-list) `release-cut-promote`
submit-requirement requires `merger-agent-bot` `+2` **AND** `non-ai-reviewer`
`+2` (co-exist, not substitute — the human `+2` and CLAUDE.md L1 stay
un-amended). AUDIT-13/13a/13b are marked superseded; ADR-0016 → Superseded by
ADR-0020 (this is OP-985's cleanup pass). The cross-cutting lesson is recorded
here and the architectural anchor in `project_release_cut_mechanism.md`.

**Verification**: `pytest backend/tests/test_merger_bot_main_promote.py`
(OP-962's policy-contract suite, extended in OP-982 with `release-cut-promote`
quad-key positive/negative cases + the `project.config` ↔ `.example` sync
assertion); `pytest backend/tests/test_auto_promote_main.py` (OP-983 — the
single-merge-commit push side, `noop` short-circuit, non-FF refusal);
`pytest backend/tests/test_release_cut_e2e.py` (OP-984 — end-to-end). ADR-0020
§"Verification" carries the operator pre-/post-deploy Gerrit checklist
(sandbox push to `refs/for/main` with/without the four keys → `release-cut-promote`
appears `UNSATISFIED` / `NOT_APPLICABLE`).

**Generalisation**: **When "a single commit works but a multi-commit batch
fails," stop and ask whether you're performing the wrong *type* of operation —
not whether you need a bigger batch limit.** A code review is per-commit and
about *new, unreviewed* content; a release cut / branch promotion / snapshot
publish is one event about *already-accepted* content. They have different
natural shapes (N changes vs. 1 change), different review semantics (per-commit
`+2` vs. one authorisation), and different correct Gerrit primitives
(`refs/for/X` magic ref vs. a merge change under `MERGE_ALWAYS`). Forcing a
release-cut through the code-review channel cost three tickets and a stalled
production release before the category error was named. Other smells of the same
mistake: bumping `receive.maxBatchChanges`; "promote the range as a chain";
needing the operator to submit a chain "in order"; assuming `main` must stay a
linear superset of `develop`. If you see those, you're probably solving the
wrong problem one size up — re-derive what *operation* you're actually doing.
