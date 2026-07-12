# Phase U6-0 — Action-Capability Authorization Kernel (design **v3**, freeze-candidate)

- **Date**: 2026-07-11
- **Status**: **FROZEN (architecture + contracts) 2026-07-11.** Lineage: v1 3-way audit = codex
  NO-GO(8)/safety GO-W-C/correctness SOUND-W-C → v2 (full-kernel architecture, user chose it) → v2 codex
  re-audit *"the kernel direction is sound"* + 10 points → v3 folded all 10 → **v3 codex re-audit = 4
  CLOSED / 5 PARTIAL / 1 NOT (architecture affirmed sound a 3rd time)** → this doc folds the 2 substantive
  residuals (v3-#13 webhook-not-a-verification-authority in §2.F; v3-#14 Figma real-transport + two-step
  sequencing in §4) + the contract-precision residuals (#11 kernel API §1; #12 grant identity tuple §2.D;
  #15 rollback=disable-not-re-add §3). **FREEZE DECISION (my judgment + operator):** an xhigh adversarial
  auditor extends a security-kernel design's precision indefinitely; the architecture is affirmed 3× and
  the remaining fineness is increment-level — pinned in each ticket's own contract + blind-test + review
  (the U4-A0→per-increment model), NOT another design-doc round. **H0 SHIPPED + MERGED** (Gerrit #2062,
  OP-2591). Next: EPIC-SOP decompose (skeleton = `docs/architecture/2026-07-11-phase-u6-0-ticket-
  decomposition-skeleton.md`) → file T1/T2a/T4 first. ⚠ filename is legacy `…-v2.md`; content is the frozen
  design.
- **Supersedes**: the v1 doc (`…-containment-and-action-capability-guard-design.md`) — historical audited
  artifact, do not build from it.
- **Reads-with**: the 3-way audit report (`audits/2026-07-11-phase-u6-0-audit.md`) + the v2 codex re-audit
  (`/tmp/u6-0-v2-reaudit-codex.txt`) — all code citations live there.

## 0. The one rule
Persistent memory — and any model-authored content — can NEVER authorize a side effect. Enforcement is in
code (a single authorization kernel every execution adapter calls), not in the prompt. Prompt fencing is
defense-in-depth only.

## 1. The ~6 live model→tool execution adapters
The guard is a **portable authorization kernel** every adapter calls BEFORE its handler:
`authorize_action(execution_context, OperationRequest{adapter, tool, schema_version, raw_args},
provenance_snapshot) → Allow(PreparedAction) | Deny | RequiresGrant(challenge)`. The kernel itself performs
server defaults + canonicalization + **authoritative-registry resolution** and returns a registry-minted
immutable `PreparedAction` — it NEVER trusts an adapter-supplied descriptor (codex v3-#11). A
fault-injection test per adapter proves no handler runs when the kernel denies/raises.

| # | Adapter | Site | Today |
|---|---|---|---|
| 1 | Sora chat inline loop | `nodes.py:1888` (`_run_tool_rounds`) | NO PEP; write-dedup/budget only |
| 2 | LangGraph specialist | `nodes.py:1169` (`tool_executor_node`) | PEP **fails OPEN** on exc (`:1162`) |
| 3 | Runner SDK dispatcher | `tool_dispatcher.py:189` via `run_with_tools` (`anthropic_native_client.py:618`, `auto-runner-sdk.py:637/920`) | proficiency gate **fails OPEN** (`:220`); handlers = file-write + shell (`runner_handlers.py:98/273`); injects project/HANDOFF memory into the system prompt first (`:580/919`) |
| 4 | Memory Tool | `memory_tool_handler.py:478` (`view` read; create/replace/insert/delete/rename write) | model-callable (`run_s1_via_anthropic_sdk.py:1625`) |
| 5 | A2A external-agent node | `nodes.py:1275`; inbound `a2a_inbound.py:257/373` | authenticated at :373, principal DROPPED at :257 |
| 6 | Remote MCP | `anthropic_native_client.py:593` → Anthropic; `mcp_integration.py:336/149` | forwarded wholesale; §4 brings demanded servers UNDER the kernel via a read-only proxy |

## 2. Frozen contracts

### 2.A — `ExecutionContext` (immutable, server-constructed; identity + request metadata only)
`{principal_type ∈ (human|service|machine), tenant_id, actor_id, roles/scopes, session_id, request_id,
message_id, authorization_source}`. Rules:
- **`message_id` is the INBOUND (model-turn) id, generated BEFORE model execution.** Non-stream already
  creates it pre-pipeline (`chat.py:514`); the `/stream` path runs the pipeline first (`chat.py:549`) →
  move user-message creation before `:556`. The chat drop is 2-hop (`chat→_run_pipeline→run_graph`;
  `_bind_chat_context` must pass tenant, `chat.py:127`/`tools.py:73`).
- **Preserve authenticated principals — never downgrade.** A2A HAS an authenticated operator
  (`a2a_inbound.py:373`) that `_run_a2a_graph` drops (`:257`) — it must RETAIN the human/service principal.
- **`machine` principals ONLY for genuinely-unauthenticated internal scheduling**, minted by a central
  trusted factory with a registered service identity + base policy. **Adapters never self-assert** roles,
  scopes, or `authorization_source`. An empty/unknown identity **DENIES** every protected action (never
  ambient authority).
- `ExecutionContext` carries NO provenance (that is a separate per-turn snapshot, §2.E).

### 2.B — Tool-metadata registry (operation-aware; unknown ⇒ DENY)
The family map is data, resolved per operation — NOT one `effect` per tool name (Memory Tool `view` vs
`delete`; `Bash` observe vs mutate share a name). An **operation resolver**:
```
(adapter_namespace, tool_name, schema_version) + canonical_args
    → OperationDescriptor(effect ∈ read_only|mutating, family, canonical_target, prepared_action)
```
- `authorize_action` **resolves/validates the descriptor against the AUTHORITATIVE registry** — it never
  trusts an adapter-supplied descriptor.
- Families ≥ `memory_write`, `ticket_write`, `task_write`, `code_write` (git/gerrit/fs/shell), `deploy`,
  `external_comms` (Slack `tool_dispatcher.py:1121` / MCP), `artifact_write`, `delegation` (A2A/Agent).
- **A registry MISS ⇒ `Unknown/DENY`** (not a fabricated "mutating" descriptor lacking a canonicalizer).
  Dynamic tools stay denied until registered.
- CI enumerates every registered static tool and proves it has metadata (parity test). Every protected
  **background sink** needs a guarded entry point OR must accept an unforgeable `PreparedAction` — listing
  a sink in metadata does not stop a raw-handler call. Tests prove BOTH "all adapters call the kernel" AND
  "protected handlers are unreachable via any unguarded public path."

### 2.C — Authorization rule (server-issued grants, never model text)
Allow iff: **base RBAC/policy/PEP allows** AND (the operation is `read_only`, OR a valid **server-issued
request grant** matches this exact operation, OR an **exact independent human approval** matches). A grant
is issued ONLY by (a) an explicit UI confirmation of the canonical `PreparedAction`, or (b) a deterministic
server parser (a validated slash command). **The kernel never accepts a "confirmed"/capability field from
tool arguments.** Memory-sourced content may influence *what* the model says/selects (allowed); it can
never satisfy this predicate (forbidden).

### 2.D — `ActionGrant` + the two-stage challenge flow (composes with `proposed_actions`)
No model-visible token. `PreparedAction`, the challenge, and the `ActionGrant` all carry AND compare the
**same full operation identity** (codex v3-#12): `{tenant, principal, request_id, message_id/model_turn,
adapter_namespace, tool_name, schema_version, family, canonical_target, args_hash, provenance_snapshot_id,
authorization_source}` — operation identity is INSIDE the hash so `{family,target,args_hash}` cannot
collide across tool aliases/adapters. The challenge stores a hash of the entire `PreparedAction`, is
single-confirmation + expiring, and issues exactly ONE grant; the §2.C grant-match predicate requires
**snapshot equality** too. `args_hash` is computed **after** server defaults/injections. Lifecycle
semantics are explicit (not "TBD"): `pending→executing` is a CAS, results persist before `consumed`,
stranded `executing` rows are reconciled by the idempotency key, and replay is forbidden unless the sink is
idempotent. Deterministic slash commands with no model turn use a distinct authenticated
`no_model_input` provenance case (nullable/typed), not a fabricated snapshot.
- **Challenge flow** (because exact post-default args don't exist until the model emits the call): model
  emits candidate → server canonicalizes to a `PreparedAction` → kernel returns `RequiresGrant` + a
  server-side `challenge_id` → UI shows the EXACT prepared action → human confirms the challenge → server
  issues a **request-bound** grant → the same prepared action resumes.
- **P5 composition** reuses `proposed_actions.py` (the human-approve gate — NOT `release_approval.py`):
  scope **every** transition by tenant (get/decide/mark-executing/result-write/host-pickup, not just
  selects — `db.py:3843`); store both proposal subject and independent-approver identity; bind approval to
  exact tool/operation/schema/target/args_hash + provenance snapshot; **recheck base policy at EXECUTION
  time**, not only at proposal time. Deploy-like actions declare whether P5 is proposal-UX or the release
  train's first independent signal.

### 2.E — `ContentProvenance` — immutable per-model-turn SNAPSHOTS (not a union)
The runtime can only establish "content EXPOSED to the model before it emitted this action" — never
"causally contributed" (§5 records correlation, not causation). A mutable invocation-wide union
misattributes across turns (tool-call-1 results must not become provenance for tool-call-2). Freeze:
- immutable `ExecutionContext` (identity, §2.A) + a separate `ProvenanceAccumulator`;
- an **immutable `ProvenanceSnapshot` captured immediately before EACH model call**; every resulting action
  references that model-turn snapshot; tool results update the accumulator only for the NEXT model turn.
- each provenance record is namespaced + integrity-bearing: `{source_kind, source_id, content_digest,
  verification_status, verification_authority, tenant/visibility scope}` — a bare `verified: bool` a normal
  writer can set is unsafe.
- **Coverage** = every prompt-assembler and memory-returning tool emits structured provenance, not a string
  the guard scrapes: RAG prefetch (`rag_prefetch.py:224` returns only a string), `search_past_solutions`
  (no IDs, `tools.py:1925`), session-load (discards message IDs, `chat.py:106`), general-chat RAG
  (`nodes.py:2033`), **the runner system-prompt memory** (`auto-runner-sdk.py:580/919`), Read/A2A/MCP
  results, and `PepDecision` (add a memory-ID field, `pep_gateway.py:305/494`).

### 2.F — Source-aware episodic schema (trusted writers + a real quarantine STATE)
New alembic revision (~0260; `db.py:865` is only the SQLite bootstrap — both change). `episodic_memory`
gains `verified`, `source`, `verification_authority`, `tenant_id`, nullable `owner_user_id`, `visibility ∈
(user_private|tenant_shared)`, and an explicit **`state` incl. `quarantined`** (quarantine is a schema
state, not merely `verified=FALSE`). Invariants (DB + trusted-writer-API enforced; **callers cannot submit
these fields**):
- `verified=TRUE` ⇒ non-null `tenant_id` AND a recognized `verification_authority`; a model-supplied
  `gerrit_change_id` is NOT evidence.
- `user_private` ⇒ `owner_user_id` non-null; `tenant_shared` ⇒ `owner_user_id IS NULL`.
- **The merge webhook is NOT a verification authority (codex v3-#13).** `webhooks.py:253` is an
  unauthenticated Gerrit endpoint and `_on_change_merged` (`:320`) trusts the submitted `change-merged`
  body — a forged payload can reach it. A trusted-writer **independently re-verifies** the change's
  project/status/revision/approval via authenticated Gerrit SSH/API state (or consumes the authenticated
  SSH event stream) BEFORE setting `verified=TRUE` + `verification_authority='gerrit-ssh'`; the webhook
  body only *triggers* the write and only the trusted Gerrit-project→tenant map assigns `tenant_id`. If
  independent verification is unavailable, the row stays **`quarantined`** (never auto-read).
- **legacy rows** → `quarantined` (NOT assigned to `t-default`; `tenant_id` NULL allowed ONLY for
  quarantined).
- **automatic reads require `verified=TRUE` + tenant/visibility predicates in SQL** ("rank verified above"
  still injects poison when no verified match exists). Scope params keyword-only after `min_quality` (the 4
  positional callers). EVERY read/update/delete/access-count/decay/report query scoped — this includes
  `db.py:3545` get/list/delete/count, `intent_memory.py:118`, `project_report.py:201`, `memory_decay.py:115`
  (§3 folds decay/report scoping INTO the containment milestone, resolving the §2.F↔H4b overlap).

## 3. Increments (buildable order; each independently DEPLOY-SAFE with a rollback story — not "dormant")
The audit's correction: several increments deliberately change behavior; the requirement is a compatibility
+ rollback story per increment, not inert-until-flipped.
- **H0 (SHIPPED, #2062)** — unbind model-callable `save_solution` from `SORA_ACTION_TOOLS`.
- **H0.5 — finish containment**: unbind model-authored episodic writes from every guild (`EPISODIC_TOOLS`,
  `tools.py:3925`); **Figma interim read-only containment** (§4 T2a — restrict to the read allowlist at the
  forward boundary so reads survive + the mutating surface is removed immediately; the kernel-governed
  proxy is T2b, after H1b). **Security-safe rollback = keep the surface disabled/deferred, NEVER re-enable
  the model-authored write or the wholesale-MCP bypass** (codex v3-#15; consistent with §5 "never bypass").
- **H1a — tool-metadata registry + operation resolver** (§2.B): enumerate every model-callable + background
  sink across all 6 adapters; classify; unknown⇒deny; CI parity test. Deliverable = the verified inventory.
- **H2 — `ExecutionContext` + `ProvenanceSnapshot` scaffolding** (§2.A, §2.E): thread identity across ALL
  callers (chat + runner + A2A, preserving authenticated principals); per-turn provenance snapshots; IDs
  before model exec. **Lands BEFORE H1b** (H1b freezes `authorize_action(execution_context,…)` — the
  context must exist first).
- **H1b — the `authorize_action` kernel + adapters** (§2.C, adapters 1-5): the portable kernel + a thin
  wrapper per adapter that calls it before the handler; fault-injection tests; fix the two fail-opens
  (`nodes.py:1162`, `tool_dispatcher.py:220`). Ships in **shadow** (log the decision, Allow read_only, Deny
  unknown) — a behavior change for unknown tools, gated + rollback-documented.
- **H4a — source-aware episodic schema** (§2.F): the migration + quarantine backfill + verified-only
  auto-read + all query scoping needed for containment. (behavior change: legacy auto-prefetch goes EMPTY
  until rows re-verify — NOT behavior-neutral; ship with the compat/rollback story.)
- **H3a/H3b — grants + composition** (§2.C, §2.D): the challenge flow + request-grant issuance;
  `ActionGrant` lifecycle; harden + tenant-scope `proposed_actions`; atomic consume. **Dual-write** first
  (issue grants without mandating consumption) so proposal creation doesn't break before the flow is live.
- **H3c — enforce, family by family**: flip the kernel from shadow to enforcing per family (each gated on a
  legitimate-action corpus AND an adversarial false-allow corpus = 0). Behavior flip by design.
- **H4b — non-security cleanup ONLY**: admin/report ergonomics. **All containment-required scoping
  (episodic read/write/decay for the poisoning close) lands in H0.5/H4a, NOT deferred here** — so H0.5/H4a
  are each independently deploy-safe (codex v3-#15).

## 4. Remote MCP (adapter 6 — brought UNDER the kernel via a read-only proxy)
codex confirmed each MCP server is forwarded **wholesale** to Anthropic (`anthropic_native_client.py:593`;
`RemoteMCPRegistry` filters by server NAME not method, `mcp_integration.py:336`), so "disable writes, keep
reads" by method-name convention is NOT enforceable, and the read-only JIRA/Graphiti predicates
(OP-813/OP-853) aren't applied on the remote-dispatch path.

**Decision (user, 2026-07-11): Figma is high-demand across the customer cases (UI/UX design + products with
display functions) — KEEP it.** v1 does NOT disable Figma; it moves demanded servers OFF wholesale-forward
and UNDER the kernel:
- **Governed read-only MCP proxy**: register ONLY allowlisted READ methods (Figma `get_design_context` /
  `get_screenshot` / `get_metadata` / `get_variable_defs` / `get_code_connect_map` / `search_design_system`)
  as LOCAL in-process tools (the `mcp_gerrit` pattern, `mcp_integration.py:3409`), dispatched through the
  registry + `authorize_action`. The server is NEVER forwarded wholesale, so mutating methods
  (`create_new_file`, `*_code_connect_map`, `upload_assets`, design-system writes) are never exposed. The
  allowlist is **config/registry-enforced (method-level)**, not catalog-description-based.
- This turns MCP from "the un-guardable adapter 6" into a kernel-governed tool set (closes codex BLOCKER-2
  more completely).
- Servers with **no demanded flow**: disable.
- **Transport reality + sequencing (codex v3-#14).** `mcp_gerrit` is an SSH wrapper, NOT a remote-MCP
  proxy; the repo has a `tools/list` probe but no production remote `tools/call` client, and the governed
  proxy must run *through the kernel* — which does not exist until H1b. So Figma is a **two-step** increment
  that keeps reads working throughout: **(T2a, in H0.5) interim containment** — restrict Figma to its
  read-only method allowlist *at the forward boundary* (Anthropic per-server `allowed_tools` if available;
  else disable-writes-by-not-exposing-them), removing the mutating surface immediately while reads
  continue; **(T2b, after H1b) the governed local proxy** — a real authenticated remote-MCP `tools/call`
  client with the full transport contract: production model-tool registration path, pinned schema
  version(s), tenant-bound token selection, method-allowlist check immediately before network dispatch,
  response provenance, fail-closed, the invariant that Figma is **never simultaneously in `mcp_servers`**,
  and no raw dispatch path around the kernel. `allowed_tools` is interim containment, not equivalent to the
  kernel-governed proxy.

## 5. Metrics + kill-switch (enforce-safe)
Per-family switches transition to **deny / tool-disable / defer-to-approval — never bypass**. Record
`action_guard_decision_total{family,result,authorization_source}`, `fail_closed_total`, and — instead of an
unobservable "memory_influenced_action" — `memory_present` + the contributing provenance-snapshot IDs +
`authorization_source` on each decision (correlation, not a causal claim). A family flips to enforce only
when its adversarial false-allow rate = 0 AND its legitimate-action corpus passes.

## 6. Non-goals (deferred)
The U6 L2/L3 memory tiers (fact schema, crypto-shred store, memory-safety eval, anchors, consolidation)
remain deferred behind this kernel. Once H3c enforces, "memory can never authorize a side effect" is a code
invariant and the U6 re-audit's RB4/RB5 are structurally closed — U6 memory resumes then.

## 7. Next steps
Light re-audit of THIS v3 (codex — confirm the 10 folds close the blockers + introduce no new bypass) →
freeze the contracts (ExecutionContext, registry/OperationDescriptor, authorization rule, ActionGrant +
challenge flow, ProvenanceSnapshot, episodic schema) → EPIC-SOP decompose H0.5…H4b into runner-pickable
tickets → per-increment blind-test → file → review → +2. H0 (#2062) is merged; it rides to prod with the
H0.5/H4a containment batch.
