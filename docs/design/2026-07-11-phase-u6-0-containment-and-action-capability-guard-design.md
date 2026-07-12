# Phase U6-0 — Containment + Action-Capability Guard (memory-safety runtime foundation)

- **Date**: 2026-07-11
- **Status**: DRAFT-v1 (RE-AUDITED → **NO-GO; v2 required, scope expanded**). **H0 SHIPPED + MERGED**
  (Gerrit #2062, OP-2591). H1–H4 got a 3-way audit (`audits/2026-07-11-phase-u6-0-audit.md`): codex
  **NO-GO** (8 BLOCKER), safety GO-WITH-CHANGES, correctness SOUND-WITH-CHANGES — all converging that v1
  understated the scope. The "guard" is a **global authorization KERNEL** (`authorize_action(execution_
  context, tool_descriptor, canonical_args)` + thin adapters), not a 2-site seam: there are ~6 live model→
  tool execution adapters (chat, LangGraph executor, **ToolDispatcher/runner**, **Memory Tool**, **A2A**,
  **remote MCP** which executes provider-side before any local guard). Plus: a tool-metadata registry
  (unknown⇒deny), server-issued intent GRANTS (not model text), an opaque `ActionGrant` composing with
  `proposed_actions`, a structured `ContentProvenance` (string-scraping can't meet INV-3), a source-aware
  episodic schema (verified-only read, quarantine backfill), and an `ExecutionContext`. codex's 8-step
  re-decomposition + two natural milestones (**M-close** containment-complete = H0.5+H4a; **M-kernel** the
  full authorization subsystem) are in the audit report. v2 written after the phase-scope decision.
- **Reads-with**: the U6 v2 design + its re-audit (`audits/2026-07-11-phase-u6-v2-reaudit.md`, blockers RB4
  = "INV-1..5 not wired + the loop still open"), the U4-A0 contract freeze (the governed-substrate
  discipline), the P5 supervised-execution model (`release_approval.py`, the human-approve substrate this
  guard composes with).

## 0. The one rule this phase serves
**Persistent memory — and any model-authored content — can NEVER authorize a side effect. Enforcement is
in code (the tool-dispatch guard), not in the prompt.** Prompt fencing is defense-in-depth only. This is
the reusable foundation: once the guard exists, L2/L3 memory can be injected knowing that a validator
false-negative cannot become a durable action channel. Without it, every memory-safety claim rests on the
model obeying a fence — which the U6 re-audit proved is not a boundary.

## 1. Current state (code-grounded — recon 2026-07-11)
| Fact | Where | Consequence |
|---|---|---|
| **No single dispatch chokepoint.** The Sora chat path `_run_tool_rounds` dispatches `fn.ainvoke(args)` with **NO PEP at all** (only per-turn write-dedup + a write budget). | `agents/nodes.py:1888` (invoke), `:1764` (`_run_tool_rounds`), `:1947` (`conversation_node`), write-set `:63-65` | The path where `save_solution`/`propose_action`/`create_task`/`supervisor_*` are bound **default-ON** has zero authorization enforcement. |
| **The other path's PEP FAILS OPEN.** `tool_executor_node` calls `pep_gateway.evaluate` but on a raised exception logs `"…proceeding"` and falls through to invoke. | `nodes.py:1148` (evaluate), `:1162-1166` (except→log), `:1169` (invoke anyway) | Only the explicit `PepAction.deny` branch blocks; any guard error = the action runs. |
| **GraphState carries no principal.** No `tenant_id`/`user_id`/`session_id`/`request_id`/`message_id`, no injected-memory IDs, no capability set — only a coarse `user_role="operator"` string. | `agents/state.py` (field list); identity available but dropped at `routers/chat.py:505` (`User`) → `_bind_chat_context` sets only user_id+session_id into a **contextvar**, `tenant_id` defaulted to `""`; `run_graph` has no identity param (`agents/graph.py:254`). | The guard has no server-derived identity or provenance to key off; tools reach identity only via a fragile ambient contextvar. |
| **No capability-token substrate.** Only a decision propose/approve queue + sandbox concurrency-budget "tokens". No nonce / args-hash / single-use / capability concept. | `decision_engine.py` (budget tokens), `release_approval.py` (human-only `require_admin` + bot-principal refusal) | The capability token is greenfield; it must COMPOSE with the existing `propose_action → release_approval` human-approve gate, not duplicate it. |
| **The episodic loop is model-forgeable + global** (H0 closed only the Sora write surface). | `save_solution` `agents/tools.py:1940` (`quality_score=1.0 if gerrit_change_id else 0.5`, no server +2 check); `episodic_memory` `db.py:865` (no user_id/tenant_id); read-back `rag_prefetch.py:186/308` + `search_past_solutions` (global, unscoped) | Guild `EPISODIC_TOOLS` write path + the unscoped read-back remain — H4's job. |
| **P5 dangerous execution is already model-unreachable.** | `propose_action` only inserts a pending row; execution is `OMNISIGHT_P5_EXECUTE` default-OFF + a host-side executor; approval is human-only. | Good — the guard defers dangerous families INTO this substrate; it does not re-implement approval. |

## 2. Frozen invariants (the capability boundary — RB4 of the U6 re-audit, now grounded)
- **INV-1** — persistent memory (and any model-authored store: episodic/L2/L3) is NEVER current-turn authorization.
- **INV-2** — memory cannot change tool policy, approval policy, permissions, or review requirements; the
  action layer ignores any "policy" sourced from memory.
- **INV-3** — any protected side effect on a turn where memory was injected records the **complete**
  contributing memory-ID set in its decision provenance (a schema field, not prose).
- **INV-4** — a sensitive action requires request-local intent OR an existing independent human approval; a
  "standing policy" in memory can NEVER satisfy the check.
- **INV-5** — enforcement lives in the dispatch guard (code); prompt fencing is defense-in-depth only.

## 3. Increments

### H0 — step-0 containment (SHIPPED — Gerrit #2062, OP-2591)
Unbind the model-callable `save_solution` from Sora's live action set (`SORA_ACTION_TOOLS`) + remove the
prompt invitation + a regression guard (`test_save_solution_not_model_callable_in_sora_action_set`). The
"remember a verified rescue" feature is unaffected (the write is produced server-side by the Gerrit-merge
webhook, `webhooks._save_merged_solution_to_l3`); Sora keeps the read. One-line reversible. Verified: 17
tests green; the 104 `test_guild_loadout` failures are pre-existing (confirmed on the clean tree).

### H1 — unified fail-closed dispatch chokepoint
Introduce ONE `dispatch_tool(name, args, state, guard)` seam that BOTH invoke sites route through:
`_run_tool_rounds` (`nodes.py:1888`) and `tool_executor_node` (`nodes.py:1169`). Fix the fail-OPEN: on a
guard/PEP exception, **DENY + skip invoke** (never fall through) — `nodes.py:1162` must `continue` with a
`[BLOCKED: guard-error]` result, not proceed. Ships as a **pass-through** (no policy yet — behavior
identical) PLUS the fail-closed fix PLUS a test proving a raised guard denies on BOTH paths. This is the
seam H3 plugs the capability policy into; shipping it inert first de-risks the wiring.

### H2 — run-state identity + injected-memory provenance
Thread **server-derived** identity into `GraphState` from the authenticated `User` (`chat.py:505`) at
`run_graph`/`conversation_node`: `tenant_id` (stop dropping it at `_bind_chat_context`), `user_id`,
`session_id`, `request_id`, `message_id`. Track the **injected-memory-ID set** — the
`<related_past_solutions>` / RAG ids emitted at `rag_prefetch.py:236` and any tool-pulled memory — as a
first-class state field. All server-computed; NEVER accepted from model output or request body. This gives
H3 the principal + the contributing-memory set INV-3 requires.

### H3 — the capability guard + protected-action matrix (the payload)
Freeze the **protected-action families** and enforce at the H1 seam:
- families: `memory_write` (save_solution/L2/L3), `jira_write` (supervisor_* / create_task),
  `dangerous_propose` (propose_action / deploy / promote / restart / rollback), `code_write`
  (git/gerrit/shell/deploy — specialist guilds).
- rule: a protected action is allowed iff it carries **request-local intent** (the current human turn asked
  for it) OR an **existing independent human approval**; **memory-sourced authorization is never
  sufficient** (INV-1/2/4). Deny-closed on missing.
- dangerous families **defer into** the existing `propose_action → release_approval` human-approve
  substrate (compose, not duplicate); execution stays `OMNISIGHT_P5_EXECUTE`-gated + host-side.
- capability token (greenfield, minimal): bound to `principal + action_family + target + args_hash +
  expiry + single-use`; issued from request-local intent / an approval; the model cannot mint or substitute
  it (INV-2). Ships in **shadow/log-only** first (measure would-deny rate), then flip to enforce.
- metrics (§5) + a per-family kill-switch.

### H4 — durable containment (beyond the Sora surface)
- **read-side provenance gate**: `search_episodic_memory` returns only server-verified rows, or ranks
  verified above model-authored — needs a `verified`/`source` flag on `episodic_memory` (small migration +
  backfill) so a forged `quality_score` can't rank. Closes the loop for ALL write paths, not just Sora.
- **scope**: add `(tenant_id, user_id)` columns to `episodic_memory` + scope every reader/writer (3 readers,
  3 writers); the merge-webhook writer needs a tenant context. Cross-tenant/-user isolation test.
- **guild write path**: give the guild `EPISODIC_TOOLS` model-write the same treatment (unbind, or route
  through the provenance gate) — the lower-exposure surface H0 deliberately left.

## 4. What ships when
H0 shipped (dormant-safe: removes a tool). H1 (inert seam + fail-closed fix) → H2 (identity, additive) →
H3 (guard: shadow → enforce) → H4 (durable containment; migration + scope). Each is independently
audited/blind-tested/filed. The guard ships **log-only first** and flips to enforce only after the
would-deny metric shows no legitimate-action regression (the U4-J prove-then-enable discipline).

## 5. Metrics + kill-switch
`omnisight_action_guard_{decision_total{family,result=allow|deny|deny_closed},fail_closed_total,
memory_influenced_action_total{influence=authorization|content}}` + per-family kill-switches
(`OMNISIGHT_ACTION_GUARD_ENFORCE_{family}`, default log-only) + the H4 `episodic_read_verified_ratio`.
Rollout gates on numbers, not metric existence (would-deny rate on a legitimate-action corpus ≈ 0 before
enforce). Distinguish **authorization** influence (forbidden) from **content/tool-selection** influence
(legitimate) — a memory that changes *what* Sora says is fine; one that changes *whether a protected action
is authorized* is the violation.

## 6. Non-goals (deferred back to the full U6)
The L2/L3 memory tiers, the closed fact schema, crypto-shred storage, the memory-safety eval, anchors, and
consolidation are NOT in this phase. They resume as the full-rigour U6 **after** this foundation lands and
proves out — at which point "memory can never authorize a side effect" is an enforced invariant, not a
promise, and the re-audit's RB4/RB5 are structurally closed.

## 7. Next steps
3-way adversarial audit of THIS design (codex + safety-lens + correctness/feasibility) → fold → freeze
(the seam signature, the identity fields, the protected-action families, the token shape, the metric
names) → EPIC-SOP decompose H1–H4 into runner-pickable tickets → per-increment blind-test → file → review
→ +2. H0 is already in review (#2062).
