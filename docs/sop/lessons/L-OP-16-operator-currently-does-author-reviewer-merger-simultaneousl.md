---
id: L-OP-16
ticket: OP-16
title: Operator currently does Author/Reviewer/Merger simultaneously
date: 2026-05-06
tags: [ci, gerrit, jira, runner]
legacy_lesson: 10
---

# Operator currently does Author/Reviewer/Merger simultaneously

**Situation**: First step-3 cycle (OP-16 / OP-246 / OP-15). Runner posts `[runner-cli-success]` comment after codex exits 0; ticket sits in `In Progress` with codex-bot assignee. Operator (you, with Claude assist) then: (a) reads commit + tests + AC, (b) merges codex-work / feature branch into main, (c) pushes origin, (d) transitions through Under Review → Approved → Published manually. ADR 0003 mandates *human +2 review distinct from author* + *automatic merge after +2*; in practice all three roles are collapsed into one operator pass.

**Fix (transitional acknowledgment)**: Convention §10/§16 update — explicit "transition period" disclaimer that operator currently combines roles; flag as ADR-0003-violating-in-practice; META tooling ticket tracks the Gerrit wire-up that will separate them.

**Fix (target state)**: [OP-247](https://soraapp.atlassian.net/browse/OP-247) META `meta:tooling` ticket — wire codex push to `refs/for/develop` (Gerrit), Gerrit submit hook → JIRA transition, automatic merge on +2 vote. ADR 0003 separation enforced by tooling, not by operator discipline. Tier L, blocks: nothing; soft prereq: governance migration plan Phase 2 items.

**Verification (today)**: Operator manually validates each merge cognitively before instructing Claude to push. Claude does NOT vote +2 on its own work. Soft-enforce until hard-enforce lands.

**Generalisation**: When the SOP defines roles (Author / Reviewer / Merger) but the tooling collapses them, it's the *tooling* that needs to mature, not the SOP that should be relaxed. Document the gap explicitly so future contributors don't think the relaxed practice is canonical.
