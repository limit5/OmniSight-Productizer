---
id: ADR-0041
title: Merger-bot conflict triage — surface, resolve, abandon-requeue, or escalate
status: Proposed
date: 2026-05-22
---

# ADR-0041 — Merger-bot conflict triage (surface / resolve / abandon-requeue / escalate)

**Status**: Proposed (2026-05-22). Sequenced AFTER the release-train migration (ADR-0040) lands.

**Relates**: O6 Merger Agent (#269, CLAUDE.md L1 exception), `backend/agents/auto_rebase.py` (OP-733), `backend/agents/gerrit_jira_bridge.py` (OP-715/801 proactive merger), the `.gerrit/auto-resolve.yaml` registry, [[project_merger_bot_883_edit_edit_gap]].

## Context

Conflicts that surface **only after a sibling change merges** are the COMMON case in parallel development (N changes branch from one base; one merges; the rest may now conflict), not an edge case. Today the system handles only part of this:

- **Clean rebases** — `auto_rebase` (OP-733) fires on `change-merged`, sweeps open bot-owned changes, and re-uploads any that rebase cleanly. ✅
- **Derived-file conflicts** — if every conflicting file has an entry in `.gerrit/auto-resolve.yaml` (a *regenerate-the-file* resolver), auto_rebase regenerates it. ✅ but the registry currently holds **one** entry (`docs/sop/lessons-learned.md`).
- **Hand-written-code conflicts** — when a conflicting file has no registered resolver, auto_rebase **silently returns `conflict=True` with no JIRA notice**. The change rots un-mergeable until a human happens to notice.

The 2026-05-22 incident (#1129 RT-10b + #1130 RT-11, both editing `release_train.py`, made un-mergeable by RT-22 merging) hit the third case: both rotted silently at patchset 1 with no signal. #1129 was a mechanical take-both; #1130 was a genuine semantic collision (two independently-designed break-glass types + a duplicated `promote()` parameter).

Two gaps: (1) **silent rot** — unresolved sibling-merge conflicts produce no signal; (2) **the merger only ever tries to RESOLVE** — a hard synthesis task it correctly abstains on for semantic conflicts (#883), leaving them stuck.

## Decision

Reframe the merger from a **resolver** into a **triager**, and never let a sibling-merge conflict be silent.

### 1. Always SURFACE (the floor; cheap, high-value)
When `auto_rebase` cannot auto-resolve a sibling-merge conflict, it MUST emit a signal: a JIRA comment on the change's ticket + (if unresolved after the triage below) a tracking item. No conflict rots silently. This alone closes the "discover by luck" hazard.

### 2. TRIAGE (the merger LLM classifies; classification is easier than synthesis)
For each surfaced conflict, the merger classifies into one of three buckets and acts:

| Bucket | Signal | Action |
|---|---|---|
| **Mechanical / additive** (take-both: distinct `__all__` entries, imports, adjacent top-level defs) | non-overlapping additions on both sides | **Resolve** via a structural (AST/section-aware) additive-merge resolver; upload the rebased patchset. |
| **Stale-base duplication** (the change reinvented something the target branch now provides) | the change adds a type/function/concept that already exists post-merge | **Abandon + re-queue the ticket** to `To Do` with a **diagnosis note** ("`X` already exists on develop (added by `<sibling>`); REUSE it, do not re-introduce `Y`"). The runner redoes from current HEAD, adapting. |
| **Genuine bilateral fork** (both changes legitimately needed and truly incompatible) | overlapping intent that cannot be satisfied by adapting one to the other | **Escalate to a human** (abstain + open a decision ticket, per the existing #883 abstain flow). |

The classification is a JUDGMENT ("is the target branch already providing this? would a redo adapt cleanly?") — more tractable for an LLM than SYNTHESISING a correct merge. The middle bucket is the key new capability: most "semantic" conflicts are actually stale-base duplication, and a redo-from-current-HEAD with a precise diagnosis adapts cleanly.

### 3. GUARDRAILS (mandatory — without these the abandon-requeue path backfires)
- **Diagnosis-carrying re-queue.** The re-queued ticket MUST carry the merger's diagnosis ("reuse `X`; do not re-introduce `Y`"). Runners faithfully re-duplicate when not told ([[feedback_filing_existing_impl_check]]) — a bare re-queue would reproduce the conflict and thrash.
- **Retry cap.** Abandon-requeue at most ONCE per ticket; a second sibling-merge conflict on the same ticket → escalate to human. Prevents infinite abandon→redo→conflict loops on true bilateral forks (which burn runner compute). Wire into the existing runner stoploss.
- **Reserve abandon for the semantic class.** Mechanical conflicts use rebase/structural-resolve (cheap); never abandon+redo a take-both (a full re-run wastes compute when a rebase would do).
- **Deterministic loser selection.** When two OPEN changes mutually conflict, a fixed rule picks which to abandon (e.g., the one without a human +2, then the later-created, then the smaller). Never abandon a merged change; never abandon a change carrying a human +2 or human review comments (preserve human investment).
- **Audit every abandon.** Each abandon+requeue writes an audit row (change, diagnosis, sibling) so the action is traceable and loops are detectable.

## Consequences

**Positive**: sibling-merge conflicts move from "silently rot until noticed" to "mechanical auto-resolved + stale-base auto-redone + bilateral cleanly escalated". Auto-clears the largest, most common conflict classes without a human; humans only see genuine design forks.

**Costs / risks**:
- Abandon+redo spends a full CLI re-run — only worth it for the semantic class (hence guardrail #3).
- A misclassification that abandons a bilateral fork → one wasted redo, caught by the retry cap → escalate. Bounded.
- The structural additive-merge resolver edits hand-written code automatically; it MUST be AST/section-aware and conservative (only non-overlapping additions), with its own tests, or it is more dangerous than leaving the conflict. Ship behind a flag, start with `__all__`/import/adjacent-def shapes only.
- LLM classification is imperfect; the floor (surface everything) ensures a misclassification never silences a conflict.

**What this still does NOT solve**: genuine bilateral design forks always return to a human. That is correct, not a deficiency.

## Alternatives considered
- **Status quo (push-time merger only)** — misses the common (post-sibling-merge) case; rejected.
- **Always escalate every code conflict to a human** — safe but defeats the point; the mechanical + stale-base classes are auto-handleable.
- **Always try to RESOLVE (synthesise) every conflict** — the merger correctly abstains on semantic synthesis (#883); abandon-requeue is the more reliable action for that class.

## Rollout (when sequenced, post-ADR-0040)
1. **Surface** (§1) — smallest, highest-value; emit JIRA notice on any unresolved sweep conflict.
2. **Triage classifier + abandon-requeue** (§2 middle bucket + §3 guardrails).
3. **Structural additive-merge resolver** (§2 top bucket) — flagged, `__all__`/import shapes first, AST-safe, tested.
