---
id: L-OP-917
ticket: OP-917
title: Audit claims of "X is blocked by Y" must cite the JIRA `Blocks` edge, not infer gating from `Relates`
date: 2026-05-11
tags: [jira, audit, planning, governance, runner, blockedby, dependency-graph]
related_tickets: [OP-876, OP-874, OP-718, OP-875]
---

# Audit claims of "X is blocked by Y" must cite the JIRA `Blocks` edge, not infer gating from `Relates`

**Situation**: AUDIT-3 (OP-917) under the 2026-05-11 deep-system-audit
follow-up cycle was filed against OP-876 (Merger Agent proactive-trigger
spike) on the premise that the spike was "currently blocked by Sprint F
F1-F5 children" and needed to be decoupled. The AC asked the runner to
remove two outward `Relates` links (→ OP-718, → OP-875) "if they imply
Sprint F gating".

When the runner verified the live JIRA state, the premise did not hold:

* `jira_get_blocked_by(OP-876)` → `[]` (zero `Blocks` inward edges).
* `has_unresolved_blockedby(OP-876)` → `(False, "all blockers resolved")`.
* The two outward links were `Relates`, which does not feed the runner's
  pre-pickup gate.
* Neither link target carried a `sprint:F*` label (OP-875 was
  `sprint:retrospective`; OP-718 had no sprint label at all), so neither
  could be read as a Sprint F gating dependency in any sense.
* OP-876 itself was already at status `公開済み` (Published) — the spike
  had executed in parallel with Sprint F work and shipped its report at
  `docs/research/merger-proactive-trigger-spike-2026-05.md` before the
  audit was even written.

The audit's "must decouple OP-876 from Sprint F" framing was therefore a
phantom dependency: there was nothing to remove, the runner JQL was
already clear, and the cleanup task it generated was a no-op.

**Root cause**: the auditor inferred gating from the *visible* `Relates`
links on the OP-876 issue page without checking which edge type drives
the scheduler. Atlassian's UI renders both `Blocks` and `Relates` in the
same Issue Links panel; only the verb on each row distinguishes them.
Skimming the panel is enough to produce a plausible-sounding "this is
blocked by …" finding even when the scheduler-relevant edge is absent.

**Fix** (already in this ticket's resolution):

1. **Audit ticket runs as verification, not mutation**: the OP-917
   resolution comment on OP-876 records the empty `Blocks` set + the
   `has_unresolved_blockedby` return value as proof the spike was never
   gated, and retains both `Relates` links for traceability rather than
   deleting them speculatively.

2. **No state change required on OP-876**: status stays at Published,
   `Relates → OP-875` and `Relates → OP-718` are preserved as the
   provenance trail (retrospective that motivated the spike, follow-up
   that consumes its output).

**Verification** (this ticket):

* OP-876 issue-link inspection via JIRA REST
  (`GET /issue/OP-876?fields=issuelinks`) shows zero `Blocks` edges in
  either direction, both before and after this ticket's resolution.
* `has_unresolved_blockedby(snapshot(OP-876)) → (False, "all blockers resolved")`
  re-runs identically pre- and post-resolution.

**Generalisation**:

1. **An audit finding that names a "blockage" must cite the `Blocks`
   edge it observed.** Operator code that gates on dependency state
   (`has_unresolved_blockedby`, runner pickup JQL, Gerrit submit rules)
   reads exactly one edge type. An audit claim of "X is blocked by Y"
   that does not name the edge type is unfalsifiable until someone goes
   to the live JIRA and looks — which is the whole point of capturing
   audit findings as actionable tickets rather than re-derived state.
   Acceptance criterion for any audit finding of this shape: include
   the issue-link JSON snippet or the JQL expression that surfaces the
   edge, not just the issue-page screenshot.

2. **`Relates` is provenance, not gating; default-keep.** The runner's
   pickup gate ignores `Relates` entirely. Removing a `Relates` link
   costs traceability (the next operator triaging OP-876 has to
   reconstruct that OP-875 was the retrospective that motivated it)
   and gains nothing because the link had no scheduler effect to begin
   with. Default: keep `Relates` links unless they are *misleading*
   (point at a deprecated ticket, suggest a dependency that does not
   exist) — not merely because an audit author mistook them for gating.

3. **Audit tickets must verify their own premise before mutation.** If
   an audit ticket says "remove X to unblock Y", the resolution should
   run the live check that Y was actually gated by X *before* deleting
   anything, and surrender to a comment-only resolution when the
   premise is stale. This shape repeats whenever planning artefacts
   (audits, retrospectives, ADRs) refer to JIRA state captured at
   time-of-writing — the state may have moved by time-of-execution.
   The CLAUDE.md L1 "anti-bulldozer" / hypothesis-driven debugging
   discipline applies to audit follow-ups too: verify, then act.

4. **Cross-reference**: L-OP-874 codifies the *direction* trap on the
   same `Blocks` edge (REST parameter names that look semantic are
   not). This lesson is the *type* trap on the adjacent surface — both
   ask the operator to read the actual contract (which edge type? in
   which direction?) before acting on the surface reading. Combined,
   they pin the discipline: when JIRA `issuelinks` enter operator
   reasoning, the edge type AND direction both need to come from a
   verified source, not from the rendered issue panel.

Per CLAUDE.md L1, this lesson lands as a per-file entry under
``docs/sop/lessons/`` because the discipline it codifies — *audit
findings about scheduler-level gating must cite the scheduler-relevant
edge type, and audit resolution must verify the premise before
mutating JIRA* — is reusable across every future audit-followup cycle.
