# Phase U6-0 (containment + action-capability guard) — 3-way audit (consolidated) — 2026-07-11

Design under audit: `2026-07-11-phase-u6-0-containment-and-action-capability-guard-design.md` (H0 shipped
= Gerrit #2062 merged; H1–H4 audited). Raw: codex `/tmp/u6-0-audit-codex.txt` (verdict @13736); safety +
correctness subagents (inline). **All three converge: direction right, scope massively understated.**

- **codex (gpt-5.6-sol/xhigh)** — **NO-GO.** 8 BLOCKER + 5 MAJOR. "The boundary is not a global
  chokepoint. Multiple live model-callable execution systems remain outside H1."
- **safety-lens** — GO-WITH-CHANGES. 3 BLOCKER + 3 MAJOR. Found the 3rd dispatch stack (ToolDispatcher).
- **correctness-lens** — SOUND-WITH-CHANGES. 7 findings. "H1 as one function is infeasible."

## The convergent verdict: H1 is a global authorization KERNEL, not a 2-site seam
v1 named 2 invoke sites (`nodes.py:1888` chat, `:1169` executor). The real set of live model→tool
execution adapters:
1. `_run_tool_rounds` (Sora chat, `nodes.py:1888`) — no PEP.
2. `tool_executor_node` (LangGraph/specialist, `nodes.py:1169`) — PEP fails OPEN on exception (`:1162`).
3. **`ToolDispatcher.execute` (`tool_dispatcher.py:189`)** — the LIVE RUNNER's stack (via
   `run_with_tools` → `dispatcher.execute`, `anthropic_native_client.py:618`, `auto-runner-sdk.py:637/920`
   which injects persistent project/HANDOFF memory THEN runs tools: file-write + shell, `runner_handlers
   .py:98/273`); its proficiency gate ALSO fails open (`tool_dispatcher.py:220` `allowed=True`).
4. **Memory Tool** (`memory_tool_handler.py:478`, create/replace/insert/delete/rename) registered model-
   callable (`run_s1_via_anthropic_sdk.py:1625`).
5. **A2A external-agent node** (`nodes.py:1275`) — invokes a remote endpoint outside the tool seam.
6. **Remote MCP** — `mcp_servers` sent DIRECTLY to Anthropic (`anthropic_native_client.py:593`,
   `mcp_integration.py:336`), forwarded without method restriction; catalog includes Figma/Calendar/Drive
   WRITES (`mcp_integration.py:149`). **Executes provider-side, before any local guard** → no local
   `dispatch_tool()` can enforce INV-5 on it.
⇒ **Freeze a portable `authorize_action(execution_context, tool_descriptor, canonical_args)` kernel + thin
adapters per system + fault-injection tests that prove authorize precedes the handler in EVERY adapter.**
NOT `dispatch_tool(name,args,state,guard)` (the two sites have irreconcilable signatures — `_run_tool_
rounds` has no `state`), and NOT `pep_gateway.evaluate` on the chat loop (its tier whitelist has ZERO Sora
tools → every Sora write would HOLD).

## The other frozen-contract blockers (all code-grounded)
- **Protected-action registry, not a hand-list (codex B3).** Freeze an authoritative tool-metadata
  registry: `read_only|mutating` + family + target-canonicalizer + base policy; **unknown model-callable
  tool ⇒ protected/DENY** (not unclassified-allow). v1's 4 families miss task-mutation, Slack/external
  comms (`tool_dispatcher.py:1121`), artifacts/reports, A2A delegation, MCP, guild `deploy_to_evk`.
- **"Request-local intent" must be a server-issued GRANT, not model text (codex B4, safety B3).** Rule:
  `base RBAC/PEP allows AND (server-issued request grant OR exact independent human approval)`. A grant is
  issued only from an explicit UI confirmation (exact family+target+args) or a deterministic server parser
  (validated slash command). **Never** accept a "confirmed"/capability field from tool args. `run_graph`
  gets only a string (`graph.py:254`); non-chat callers (`invoke.py:573`, `a2a_inbound.py:257`) have no
  human turn.
- **Approval substrate misidentified (codex B5).** It's `routers/proposed_actions.py` (not
  `release_approval.py`); its selects omit tenant/user (`db.py:3843`); human-detection is naming-pattern
  (`api/release_approval.py:64`). Compose as: proposal consumes a grant; approval binds
  tenant+principal+family+canonical-target+args-hash+provenance; execution atomically CONSUMES it. Prefer
  an opaque server-side **`ActionGrant`** (`pending→executing→consumed` + idempotency key, no tool-schema
  field) — the model never sees a token. `args_hash`+expiry+single-use ARE required (replay / payload-swap).
- **INV-3 provenance can't be met by string-scraping (codex B10, correctness F2).** Memory IDs are lost:
  `rag_prefetch` returns only an XML string (`:224`), `search_past_solutions` emits no IDs (`tools.py:1925`),
  session-load discards message IDs (`chat.py:106`), `PepDecision` has no memory-ID field. Require a
  structured `ContentProvenance` from every prompt-assembler/tool, unioned in runtime state, persisted on
  every action decision. Also: the Sora chat path NEVER runs `rag_prefetch` (topology
  orchestrator→conversation→context_gate→summarizer→END) — v1 cited the wrong capture site.
- **ExecutionContext, not loose fields (codex M9, correctness F3).** principal-type + tenant + user/service
  actor + roles/scopes + session + request + message + authorization-source; generate the message ID
  BEFORE model exec (currently after, `chat.py:549`); the 2-hop drop is `chat→_run_pipeline→run_graph`;
  identity-less runner/A2A callers must use an explicit MACHINE principal (empty identity must DENY on
  protected actions, never become ambient authority).
- **H4 ownership/backfill wrong (codex B11, correctness F5).** More than "3 readers/3 writers" (unscoped
  get/list/delete/count `db.py:3545`, intent_memory, project_report, decay jobs). The merge webhook has no
  user/tenant (`webhooks.py:1493`, unauth Gerrit endpoint) → a merge is a **service-principal,
  tenant-shared** fact (tenant from a trusted Gerrit-project map), NOT a fabricated user. `db.py:865` is the
  SQLite bootstrap — the real change is a NEW alembic rev (~0260) + bootstrap; scope params keyword-only
  after `min_quality`. **Backfill legacy ⇒ verified=FALSE/quarantined** (a model gerrit_change_id is NOT
  evidence); **auto-prefetch verified-ONLY** ("rank verified above" still injects poison when no verified
  match exists).
- **Log-only-by-default leaves the invariant OPEN (codex B12).** Guild `EPISODIC_TOOLS` (`tools.py:3925`)
  + SDK Memory/file-write still model-write. Shadow is OK only while the phase is explicitly incomplete AND
  every uncovered mutating surface is separately contained. Per-family switches must
  deny/disable/defer-to-approval, never bypass. `memory_influenced_action` isn't observable → record
  `memory_present` + provenance IDs + `authorization_source`.

## codex's re-decomposition (8 steps, adopted as the v2 shape)
H0.5 containment (unbind guild episodic writes + disable/govern mutating MCP) → H1a tool-metadata registry
(unknown⇒deny) → H1b the `authorize_action` kernel + thin adapters (LangGraph×2, ToolDispatcher, Memory
Tool, A2A, MCP proxy, host executor) + fault-injection tests → H2 ExecutionContext + structured provenance
→ H4a source-aware episodic schema + quarantine backfill + verified-only auto-read → H3a/H3b request-grant
issuance + P5/ActionGrant composition + atomic consume → H3c enforce-by-default → H4b decay/report/admin
scoping + remaining guild migration.

## The scope reality (for the phase decision)
This is a **cross-cutting authorization subsystem**, not a 4-increment guard. Two natural milestones fall
out: **(M-close) containment-complete** = H0.5 + H4a — closes the actual memory-poisoning LOOP across every
path (verified-only read + quarantine backfill + unbind guild write + govern MCP), most of the safety value,
proportionate, shippable soon. **(M-kernel) the full authorization kernel** = H1a…H3c — the complete
"memory can never authorize any side effect via any adapter" boundary, a phase-sized security subsystem.
Recommend banking M-close now and funding M-kernel as its own phase (the same bank-the-win discipline as
the U6→U6-0 split). H0 (#2062, merged) already removed the worst single surface.
