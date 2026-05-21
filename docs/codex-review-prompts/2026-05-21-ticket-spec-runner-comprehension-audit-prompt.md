# Codex Review Prompt — Ticket spec from the RUNNER-EXECUTION angle (comprehension + goal-drift)

**Target output file**: `/tmp/ticket-spec-runner-comprehension-codex-audit-2026-05-21.txt`
**Reviewer role**: simulate an AI runner (claude-bot / codex-bot) that PICKS UP each ticket cold. You have NOT seen the design discussion — you only get the JIRA ticket: its title (`[TAG][RT-NN] ...`), labels, and (at file time) its description/4-AC. The operator's question: **when a runner receives each ticket, will it understand what we want — or will the goal DRIFT** (runner builds the wrong thing, over-scopes, under-scopes, misreads the boundary, or "fixes" something we didn't ask)?

## Read
1. Ticket spec v2 (just tag-corrected): `docs/design/2026-05-21-release-train-ticket-spec.md`.
2. Source of intent: `docs/design/2026-05-21-release-train-workplan.md`, `docs/design/2026-05-21-model-a-candidate-build-release-train.md`, `docs/adr/ADR-0040-single-trunk-release-train.md`.
3. Title-tag convention: the `[BOT/OP/META/REF/GATE/HOLD][scope]` rules (in the spec's "Title action-tag convention" section).
4. Runner reality: `backend/agents/pipeline_coordinator*.py`, `scripts/runner-wrapper/`, the prompt-builder / scope-anchor handling, and prior runner failure-mode lessons in `docs/sop/` (capability safe-default, goal-drift, boundary enforcement, scope-anchor = Goal+Files+Spec-ref).

## Tasks — judge each ticket as a runner would

A. **Title comprehension.** For each RT-NN, is the title alone specific enough that a runner forms the RIGHT intent? Flag titles that are vague, overloaded (multiple actions in one), or that a runner could misread as a different task. Confirm the `[TAG]` matches what the ticket actually is (e.g., is any `[BOT]` actually operator-only work, or any `[OP]` actually runner-doable?).

B. **Goal-drift risk per ticket.** Where is a runner most likely to drift — e.g., a ticket says "fix X" but the runner rewrites adjacent code, over-engineers, touches files outside scope, or re-implements something that already exists? Identify the top goal-drift hazards and what scope-anchor / boundary / "do NOT touch" guardrail each needs in its description.

C. **Scope-anchor sufficiency.** The spec claims Goal+Files+Spec-ref is enough for runner-authored 4-AC. Per ticket, are the Files concrete enough, and is the Spec-ref pointer precise (which doc section)? Where will a runner not know which files to edit?

D. **Cross-ticket confusion.** With ~30 RT tickets in one scope, will a runner picking up RT-NN understand its boundary vs siblings (e.g., RT-05a vs RT-05b, RT-10a vs RT-10b)? Where could two runners collide or duplicate work? Are mutex/file-path locks needed?

E. **Ambiguous decisions embedded in impl tickets.** Flag any `[BOT]` ticket that secretly contains an unresolved DECISION (e.g., RT-09/RT-12 depend on RT-20 final-git-tag + RT-21 bridge decisions) — a runner could pick an interpretation and drift. Confirm those are blockedBy the decision tickets, not buried in prose.

F. **Tag/label classifier correctness.** Verify the corrected tags: are the integration tickets correctly `[BOT]` (not `[GATE]`)? Is anything still mis-tagged such that the runner JQL picks up something it shouldn't (or skips something it should)?

## Output
Per task A–F with concrete RT-NN + the exact drift risk + the guardrail/wording fix the ticket needs. End with: (1) the **top goal-drift hazards** ranked, (2) per-ticket **title/scope-anchor rewrites** where needed so a cold runner won't drift, (3) a **GO / GO-WITH-FIXES / NO-GO** on whether these tickets are runner-comprehensible enough to file. Write the FULL review to `/tmp/ticket-spec-runner-comprehension-codex-audit-2026-05-21.txt`; do not summarize to stdout.
