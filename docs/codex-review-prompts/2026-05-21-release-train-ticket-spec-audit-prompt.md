# Codex Review Prompt — Release-train ticket spec completeness + filing-correctness audit

**Target output file**: `/tmp/release-train-ticket-spec-codex-audit-2026-05-21.txt`
**Reviewer role**: independent reviewer. You have audited this migration 4 times (`docs/audit/codex-reviews/*2026-05-21*`). THIS pass audits the **ticket spec** that will be filed to JIRA for AI runners. The operator's question: **is anything missing, and will these tickets file + execute cleanly?** Tickets are NOT yet on JIRA — your job is to catch gaps BEFORE filing.

## Read first
1. Ticket spec under review: `docs/design/2026-05-21-release-train-ticket-spec.md`.
2. Source of truth it must fully cover: `docs/design/2026-05-21-release-train-workplan.md` (v2) + `docs/design/2026-05-21-model-a-candidate-build-release-train.md` + `docs/adr/ADR-0040-single-trunk-release-train.md`.
3. Your 4 prior audits in `docs/audit/codex-reviews/*2026-05-21*` — every must-fix in them MUST map to a ticket.
4. Filing rules to enforce: `docs/sop/runner-ticket-filing-checklist.md`, `docs/sop/sprint-s12-ticket-decomposition-rules.md`, `docs/sop/jira-ticket-conventions.md`, `config/capability_matrix.yaml`, `auto-runner-jira.py` (RECOGNISED_AREAS ~line 143).

## Tasks
A. **Coverage completeness.** Map EVERY work-plan-v2 item + EVERY must-fix from the 4 prior audits to a ticket in the spec. List anything with NO ticket (gaps). Pay special attention to items that were dropped in earlier rounds (FE flags, webhooks.py, final-git-tag-no-rebuild semantics, release_audit hard-gating, bridge-image inventory, force/break-glass policy, prod-compose latest default).

B. **Dependency-graph correctness.** Verify the graph is acyclic; check each `blockedBy` is right (no item depends on something that should depend on it); confirm the v1 P0.6→P1.3 cycle is truly gone; flag any ordering that lets promote (RT-12) precede staging/migration gates (RT-05e/RT-11).

C. **Area-label correctness.** For each ticket, do the `area:*` labels span EVERY directory its ACs touch (the §11-revert trap)? Are any using `ci`/`gerrit` before RT-03 lands them in the whitelist (ordering hazard)? Are multi-area tickets that should be split flagged?

D. **Capability/tier/type correctness.** Does each ticket have the right issuetype (Story not Task), tier (M for push/deploy, X for human/ops), type (not meta unless it has children), and `capability:enable=gerrit_push` where needed? Will any trip the safe-default-leak / type:meta-routing / unknown-area / tier:S-no-push runner failure modes?

E. **4-AC + integration-ticket completeness.** Does every impl ticket have a real machine-checkable Exercised AC + Go-Live? Does every sibling group have a `[GATE]` integration ticket blockedBy all siblings (filed now, not "later")?

F. **Filing-time hazards.** Anything that will cause a §11 revert, a pickup loop, a stuck-In-Progress, or a TOCTOU/assignee issue on filing. Any ticket that is mislabeled 🤖 but is inherently human/ops.

## Output
Structure A–F with concrete ticket IDs + `path:line` to the SOP rule violated. End with: (1) the **gap list** (work-plan/audit items with no ticket), (2) the **per-ticket fix list** (label/tier/dep corrections), (3) a **GO / GO-WITH-FIXES / NO-GO** on filing this spec to JIRA. Write the FULL review to `/tmp/release-train-ticket-spec-codex-audit-2026-05-21.txt`; do not summarize to stdout.
