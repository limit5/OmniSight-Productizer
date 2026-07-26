---
id: L-OP-2747
ticket: OP-2747
title: Splitting a ticket to make it reviewable can introduce defects the unsplit ticket did not have
date: 2026-07-26
tags: [process, sop, review, migrations, safety]
---

# Splitting a ticket to make it reviewable can introduce defects the unsplit ticket did not have

**Situation**: Stage 4 of the DSAR subject-ownership epic (OP-2747) decomposed five design stories
into fileable tickets. Review round 1 returned NOT-READY on the tickets themselves. One finding was
that story D was too large to review or revert as a unit — registry schema, FK cutover, tenant
lifecycle, state machine, triggers on shared tables, retry, lock-order repair and benchmarking — so
it was split into D-a / D-b / D-c, and G into G1 / G2.

**Round 2 then returned four CRITICALs, three of which existed only because of the split.** The
unsplit ticket had none of them.

1. **A seam that was not revertible.** D-a backfilled a deliberately non-cascading registry; D-b
   dropped the cascading foreign keys. Individually reasonable. But if D-b never landed, D-a alone
   had manufactured orphaned personal data with no retention enforcement — and once D-b *had*
   landed, the FKs could never be restored, because surviving regulatory records by then referenced
   deleted parents. The split created a state that neither the original ticket nor the finished
   chain contained.

2. **An intermediate ticket that left the system worse than before it.** Erasure rolls back
   atomically on failure today (`backend/routers/privacy.py:411-428`), so a crash mid-erasure is
   harmless. D-b committed `subject = erasing` before child DML — correct for the finished design,
   which has a durable worker resuming nonterminal requests. But the worker lived in E. Between D-b
   and E, a crash after the claim would strand a subject in `erasing` **forever**, blocked by the
   barrier, with nothing to resume it. Each ticket was right; the order was not.

3. **Two tickets requiring contradictory orderings.** E deleted the local OAuth row atomically with
   writing an encrypted retry snapshot. G2 required the IdP contacted *before* that row was deleted.
   Both sentences were written by the same author on the same day and could not both hold. In one
   ticket this would have been a single paragraph that contradicted itself and been obvious.

A fourth, in round 3: **D-b's `MUST NOT add triggers` (meaning "leave the barrier to D-c")
contradicted D-b's own requirement** for a `BEFORE INSERT` integrity check and immutable columns,
which PostgreSQL expresses only as triggers. A fence drawn to protect the *next* ticket cut through
the current one.

**Fix**: the seams were repaired individually — D-a ships empty tables with backfill moved to the
ticket that owns the cutover and its rollback window; E gained the durable worker and a
process-death-after-phase-1 restart test; the OAuth ordering was settled as E-deletes /
G2-revokes-from-the-snapshot, which is also the better design because revocation becomes a
retryable obligation after erasure rather than a precondition of it; and D-b's fence was narrowed to
erasure-barrier triggers so it no longer forbids its own goal. But the durable fix is the review
rule below, not these four edits.

**Verification**: four Stage-4 review rounds against an independent adversarial reviewer, rounds 1-3
NOT-READY, round 4 confirming 6 of 7 blockers fixed. Filed as OP-2750 (C), OP-2751/2752/2753
(D-a/D-b/D-c), OP-2754 (G1), OP-2755 (E), OP-2756 (G2), OP-2757 (F), OP-2758 (ACT), with all 13
dependency links verified by read-back rather than assumed. Draft and design in Gerrit 2216/2217.

**Root cause**: splitting moves work across a boundary but does not move the *invariants* with it.
Each ticket was internally coherent; what broke was the space between them — the deployed states
that only exist because the work now lands in pieces. A reviewer reading one ticket cannot see them,
and the author who just split the work is the least likely to, having spent their attention on
making each piece self-contained.

**Generalisation**: when splitting a ticket, review the **seams**, not just the pieces. For every adjacent
pair, ask three questions explicitly and write the answers into the tickets:

- **If the next ticket never lands, is the system worse than before this one?** If yes, the split is
  wrong, or the earlier ticket must ship inert. (D-a now ships empty tables; backfill moved to the
  ticket that owns the cutover *and* its rollback.)
- **Is this ticket independently revertible after the next one has landed?** An irreversible step
  (dropping a constraint, deleting a parent) must not be separated from the guard that makes it
  safe. D-b's tenant-delete interlock therefore ships **enabled**, not default-OFF, because it is
  the counterpart of an irreversible FK drop — gating it would create exactly the uncoordinated
  window it exists to close.
- **Do any two tickets state opposite orderings or opposite constraints?** Grep the pair for the
  same nouns and read those sentences side by side. Contradictions that would be obvious within one
  paragraph become invisible across two documents.

**Corollary — "everything ships default-OFF" is not a safety rule, it is a slogan.** Applied
blanket-wise it contradicted a ticket whose entire purpose was to stop writing a false completion
status: shipping *that* disabled means shipping code that knowingly keeps lying. Decide OFF/ON per
ticket by failure mode. Gate what fails by blocking legitimate work; ship enabled what fails by
doing more of the correct thing.

**Related**: `docs/sop/epic-decomposition-and-ticket-filing-sop.md` (Stage 4);
`docs/sop/architecture-anti-patterns.md`; [[L-OP-2728]] on artefacts proven only by induced failure —
the same underlying habit of checking that a mechanism can actually fail, applied to ticket seams
instead of alerts.
