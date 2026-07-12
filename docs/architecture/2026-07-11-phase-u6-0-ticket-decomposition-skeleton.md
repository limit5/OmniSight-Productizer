# Phase U6-0 — ticket decomposition skeleton (pre-freeze)

Input for the EPIC-SOP Stage-4 draft. Source design: `docs/design/2026-07-11-phase-u6-0-action-capability-
guard-design-v2.md` (v3 freeze-candidate). **Do not file until the v3 re-audit confirms GO-WITH-CHANGES and
the contracts freeze.** The increment list is stable (codex's 8-step + sub-splits); the re-audit tightens
contract wording, not this skeleton. Every ticket: issuetype=Story, 4-AC (Code/Deploy/Integration/
Exercised), `capability:enable=gerrit_push`, `class:subscription-*`, area labels spanning all AC areas.

**EPIC / META anchor**: `U6-0 Action-Capability Authorization Kernel` (label `meta:*`), links every ticket.

| Ticket | Increment | Scope (1-liner) | tier | areas | blockedBy |
|---|---|---|---|---|---|
| OP-2591 ✅ | H0 | unbind model `save_solution` from `SORA_ACTION_TOOLS` (MERGED #2062) | S | backend,tests | — |
| T1 | H0.5a | unbind guild model-authored episodic writes (`EPISODIC_TOOLS`/guild loadouts; keep webhook write) | S | backend,tests | — |
| T2 | H0.5b | governed **read-only Figma MCP proxy** (allowlisted read methods as local in-process tools; stop wholesale forward) | L | backend,tests | — |
| T3 | H1a | tool-metadata **registry + operation resolver** + full 6-adapter/background-sink inventory + CI metadata-parity test (unknown⇒deny) | L | backend,tests | T2 |
| T4 | H2a | **ExecutionContext**: thread server-derived identity across chat+runner+A2A (preserve auth principals; msg_id before model exec; central machine-principal factory) | L | backend,tests | — |
| T5 | H2b | **ProvenanceSnapshot**: immutable per-model-turn snapshots + structured provenance from every assembler/tool + integrity fields | L | backend,tests | T4 |
| T6 | H1b-kernel | the pure `authorize_action(execution_context, operation_descriptor)` kernel (reads registry + context) | M | backend,tests | T3, T4 |
| T7 | H1b-adapters | thin wrapper per adapter (×6) calling the kernel before the handler + fix the 2 fail-opens (`nodes.py:1162`, `tool_dispatcher.py:220`) + fault-injection tests | L | backend,tests | T6 |
| T8 | H4a | source-aware **episodic schema** (alembic ~0260) + quarantine backfill + verified-only auto-read + query scoping | L | backend,tests,devops | T1 |
| T9 | H3a | request-grant issuance + **RequiresGrant challenge flow** (challenge_id → UI confirm → request-bound grant → resume) | L | backend,frontend,tests | T7 |
| T10 | H3b | **ActionGrant** lifecycle + harden `proposed_actions` (tenant-scope ALL transitions, args_hash, atomic consume, recheck-at-execution) | L | backend,tests | T9 |
| T11 | H3c | **enforce family-by-family** (flip shadow→enforce per family; gated on legit-action + adversarial-false-allow corpora = 0) | M | backend,tests | T10, T8, T5 |
| T12 | H4b | residual admin/report/decay query scoping + any leftover guild/SDK memory-write migration | S | backend,tests | T8 |

**Critical chains** (all links direction-checked at file time — Blocks: inwardIssue=blocker):
- containment lane: T1 → T8 → {T11, T12}
- kernel lane: T2 → T3 → T6 ; T4 → T6 ; T6 → T7 → T9 → T10 → T11
- provenance lane: T4 → T5 → T11
- T6 also blockedBy T4 (context before kernel — the v2-audit reorder); T11 gates on T8(schema)+T5(provenance)+T10(grants).

**Parallelism**: T1, T2, T4 are independent leaves (file first). T8 unblocks early off T1. Keep T6 single
(kernel) then T7 fans the adapters. **Same-file-sibling caution**: T1/T2 both touch `agents/tools.py`
registries + T7 touches `agents/nodes.py` (both dispatch sites) + `tool_dispatcher.py` — sequence T7's
node edits after T1/T2 land, or file T7 as one ticket owning all adapter wrappers (avoid the file-mutex
trap — `[[feedback_sequence_same_file_siblings]]`).

**Deploy-safe/rollback note per ticket** (the audit's correction — NOT "dormant"): T1/T2 remove
tools/close MCP writes (rollback = re-add); T3 unknown⇒deny (gated); T7 flips fail-open→deny (gated); T8
quarantines legacy → auto-prefetch goes EMPTY (compat/rollback story mandatory); T11 is the intended
behavior flip. Each Deploy-AC states the compatibility + rollback path.

**Hand-driven vs runner**: T4/T6/T7 (kernel + identity, security-critical, cross-cutting) are candidates
for hand-driven or close review; T1/T8/T12 are cleaner runner Stories. Decide per-ticket at file time. All
hand-driven tracking tickets file `--tier X` ([[feedback_assignee_self_not_durable_pickup_block]]).
