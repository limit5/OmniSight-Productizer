# Phase U6-0 · T7 — Wire the kernel into live dispatch (design **v2**, post 3-way audit)

- **Date**: 2026-07-12
- **Status**: **v3** (post 2 codex passes). v1 = codex NO-GO (4 BLOCKER); v2 re-scoped → codex re-audit =
  NO-GO but B4 CLOSED / B1,B2,B3 PARTIAL (right direction) + code_execution provider-side hole (new) +
  increment-level precision; v3 folds all of it. The audit proved the frozen foundations (H1a registry / H2
  identity / H0.5 containment) were built only PARTIALLY, so "wire the adapters" has unmet prerequisites. v3
  re-decomposes T7 into an **~8-ticket phase** (foundations → containment → identity → guard → wiring). Per the
  working-mode freeze rule (an xhigh auditor extends precision indefinitely; freeze by judgment when residuals
  are increment-level, the per-ticket blind-test+review being the net), v3's residuals are now AC-precision +
  the two structural folds (P-PROV provider-side, P-ID concrete sources) are closed → **FROZEN (architecture +
  ticket contracts) 2026-07-12**. Freeze basis: the provider-side surface enumeration was verified COMPLETE
  across BOTH launchers (`auto-runner-sdk.py` uses `make_runner_dispatcher()` client-side handlers + `RUNNER_TOOLS`
  + the SAME `to_anthropic_mcp_servers()` boundary, NO `code_execution`; only `run_s1`'s `BUILT_IN_TOOLS_SPEC`
  carries the provider-side `code_execution`). Remaining residuals are per-ticket AC precision → netted by each
  ticket's blind-test + review, NOT another design round. Next: file the parallel leaves (P-REG first).
- **Parent**: `2026-07-11-phase-u6-0-action-capability-guard-design-v2.md` (FROZEN kernel design).
- **Audit trail**: codex NO-GO + 2 subagent audits captured below (§A). All 3 top claims VERIFIED against
  develop @ b822ed94 (MCP wholesale-forward, Skill subprocess-exec, A2A no-prod-caller).

## 0. The honest reframe (codex M10)
**T7 wiring in shadow provides NO security boundary by itself.** It records verdicts and, by default, proceeds.
The line against memory-poisoning is held by the *containment* increments (H0 save_solution unbind ✅ #2062;
H0.5 guild unbind ✅ #2592; Figma read-only ✅ #2593; + this phase's MCP-others + executable-skill containment)
UNTIL T11 flips enforcement family-by-family. v2 states this explicitly so shadow telemetry is never read as
"safe," and preserves every existing containment through the shadow window.

## 1. What the audit proved is NOT yet done (the real prerequisites)
| Frozen increment | Believed done | Audit reality (verified) |
|---|---|---|
| H1a — registry (T3) | complete | **Incomplete**: `resolve()` omits `memory`, `code_execution`, `KnowledgeRetrieval`, `SlackPostMessage`, and dynamic `external_agent:<id>` → all `__unknown_deny__` (codex B2). `Skill` mislabeled `delegation` though `*.skill` = arbitrary subprocess exec (codex B4). No coverage test over the runner-dispatcher surface. |
| H2 — identity (T4) | complete | **Only T4b (graph entries) done. Runner identity (T4c) NOT built**: `ToolDispatcher._current_agent_id` is `None` on the runner main loops (no proficiency gate installed); `for_machine` needs `request_id` (codex M5). |
| H0.5 — containment | Figma done | **MCP-others still wholesale-forwarded** (Gmail/Calendar/Drive/JIRA/Graphiti reach Anthropic provider-side, never `ToolDispatcher.execute`; only Figma has `allowed_tools`) (codex B1). **Executable `*.skill` files run un-contained** (codex B4). |
| T6 — kernel | done | Sound, but has **no explicit unbound-principal hard-deny** rule; an empty/`unbound` identity only reaches `requires_grant`, not `deny` (codex M7). |

## 2. Corrected adapter / dispatch-path map (verified @ b822ed94)
| # | Path | Live? | Guard mechanism in this phase |
|---|---|---|---|
| 1 | chat `nodes.py::_run_tool_rounds` (`fn.ainvoke`) | **live** | in-process `guard_tool_dispatch`; ctx = `state.execution_context` (thread kw-only from `conversation_node`, `nodes.py:2228`) |
| 2 | specialist `nodes.py::tool_executor_node` (`tool_fn.ainvoke`) | **live** | in-process guard; ctx = `state.execution_context`; **fix PEP fail-open outright** |
| 3 | runner SDK `tool_dispatcher.py::ToolDispatcher.execute` (`handler`) | **live** | in-process guard; ctx = **real runner ctx (T4c)**; **fix proficiency fail-open outright**; covers the model-callable Memory Tool (registered on this dispatcher) |
| 4 | Memory Tool (model-callable) | **live, via #3** | covered by #3; raw `handle()` write-sink is a *separate* trusted/T8 concern (codex M11) |
| 5 | A2A outbound `nodes.py::external_agent_node` (`client.invoke`) | **DORMANT** (no prod caller — codex M13) | wire the guard defensively + note not-live; tenant-assert closure==ctx when productionized |
| 6 | **PROVIDER-SIDE surfaces**: Remote MCP (`run_with_tools(mcp_servers=…)`) + `code_execution_20260120` built-in (`BUILT_IN_TOOLS_SPEC`) | **live, PROVIDER-SIDE** | **cannot be guarded in-process** — Anthropic dispatches server-side. Lever = the **forward boundary** (P-PROV): disable `code_execution`; per-server disable/exact-allowlist + unknown-server-deny for MCP (codex B1 + re-audit R#2). |
| — | `Skill` executable | **live escape** | contain: unbind `*.skill` subprocess exec from model use (codex B4) |

## 3. Re-decomposition — the U6-0 "wire + enforce-ready" phase (dependency-ordered)
Each ticket: Story, 4-AC, `capability:enable=gerrit_push`, `class:subscription-claude`, areas span all AC areas.

### Foundations (file first; independent leaves)
- **P-REG (extends T3) — complete the registry + coverage.** tier M. blockedBy T3(done).
  - Classify the 5 misses: `code_execution`→(mutating,`code_write`); `memory`→(mutating,`memory_write`) —
    operation-aware `view→read_only` refinement DEFERRED (fail-closed name-level now); `KnowledgeRetrieval`→
    (read_only); `SlackPostMessage`→(mutating,`external_comms`) — and fix the false "`external_comms` reserved,
    no current mapping" comment.
  - `Skill`: reclassify off bare `delegation` — a `*.skill` is arbitrary exec. Give it its own high-risk
    family (e.g. `skill_exec`, mutating) so an enforce flip on `code_write`/`deploy` isn't silently bypassed.
    (Structural containment of executable skills = **P-SKILL**.)
  - Dynamic names: `resolve()` must map the `external_agent:<id>` PREFIX → (mutating,`delegation`), not
    unknown_deny (codex B2/M13).
  - **Coverage test**: reproduce the **actual assembled dispatcher surface of BOTH launchers** (codex re-audit
    R#2-registry) — `run_s1_via_anthropic_sdk.py` (bind built-ins + `bind_memory_tool` + `Skill`/`Agent` at
    `:1656-1657`) AND `auto-runner-sdk.py`'s dispatcher build — plus `make_runner_dispatcher()` and the
    `_default_dispatcher` — and assert `resolve(name).family != "__unknown_deny__"` for every
    `registered_tools()` name (including the launcher-added `Skill`/`Agent`); keep the `TOOL_MAP` parity. This
    is the anti-drift guard the frozen §2.B demanded but T3's parity test never covered for the SDK surface.
- **P-ID (extends T4b = the missing T4c) — runner identity + unbound rule.** tier L. blockedBy T4a(done).
  Build ONE trusted `ExecutionContext` per launcher from a **named trusted source** (codex re-audit R#4 — the
  two launchers differ, so the AC names each):
  - **`run_s1_via_anthropic_sdk.py::process_ticket[_full]`** (JIRA runner): source = the **pickup record** —
    `tenant_id` via the trusted Gerrit/JIRA project→tenant map, actor = the runner's claim identity, request =
    ticket_key+attempt, model-turn id per turn. `for_service`/`for_machine`.
  - **`auto-runner-sdk.py::run_one_item`** (TODO runner — verified: NO per-item JIRA ticket/tenant, drives
    Claude through a `TODO.md` item): source = the runner PROCESS's configured **service identity** (stable
    runner id + the configured dev-project tenant); `for_machine`/`for_service`. There is no human principal here.
  - **Thread it end-to-end** — through `AnthropicClient.run_with_tools` (`anthropic_native_client.py`) →
    onto the dispatcher → readable inside `ToolDispatcher.execute`; and **across every wrapper on that path**
    (`DetectorAwareDispatcher.execute` must pass it through) — do NOT defer wrapper propagation to T7b. Derive
    a **child context** for nested delegations (`sub_agent.py::make_agent_tool_handler` currently drops
    context). Do NOT reuse `_current_agent_id`/proficiency state as identity (it's `None` there anyway).
    Populate-only/dormant-consume (T4b pattern).
  - Add `execution_context.for_unbound(...)` (distinct `authorization_source="unbound"`) + a kernel rule:
    an unbound principal **hard-DENYs** protected (mutating/unknown) ops in enforce mode and can never match a
    grant (codex M7). (Touches `execution_context.py` + `authorization_kernel.py` — small.)

### Containment (independent; hold the line during shadow)
- **P-PROV (extends T2a) — PROVIDER-SIDE execution-surface containment.** tier M. blockedBy T2a(done).
  The in-process guard CANNOT reach tools Anthropic executes server-side. Verified the complete provider-side
  set in `scripts/run_s1_via_anthropic_sdk.py::BUILT_IN_TOOLS_SPEC` + the forwarded `mcp_servers`:
  **`code_execution_20260120`** (line 208 — "runs server-side in Anthropic's PTC sandbox"; the local
  `ptc_sandbox_handler` on the dispatcher is only a rarely-used emulation, NOT the live path — codex re-audit
  R#2) and **remote MCP servers** (codex B1). (`text_editor_20250728` / `bash_20250124` / `memory_20260120`
  are CLIENT-side → dispatched via `ToolDispatcher.execute` → governed by T7b — NOT provider-side.)
  - **`code_execution`**: DISABLE the provider-side entry in `BUILT_IN_TOOLS_SPEC` (or expose ONLY a proven
    read-only PTC surface with no side-effect callers) — a registry family label does not govern a server-side
    tool. Do this BEFORE T7b (else T7b's "runner surface is guarded" claim is false).
  - **Remote MCP** in `to_anthropic_mcp_servers`: an explicit **per-server policy map** with **default-DENY
    for any unknown/unlisted server** (today `RemoteMCPRegistry` forwards arbitrary configs); **disable**
    Gmail/Calendar/Drive; **exact method allowlists** for JIRA/Graphiti (the current
    `is_graphiti_mcp_read_only_tool` PREFIX predicate `get*`/`find*` can admit future write-shaped methods —
    replace with an exact set); keep Figma; **probe/schema-failure ⇒ deny**. Until T2b's governed proxy.
  - Rollback = keep-disabled (never re-open a provider-side write surface — the H0 pattern); tests assert
    unknown-server and disabled-tool both fail closed (codex re-audit R#1/R#2).
- **P-SKILL — contain executable skills.** tier M.
  - Unbind model-invoked `*.skill` subprocess exec (`make_skill_handler`/`_run_executable_skill`,
    `env=os.environ.copy()`); markdown skill *retrieval* stays read-only. Rollback = keep disabled (never
    re-enable model-authored exec — the H0 pattern) (codex B4).

### Guard core + wiring (the original T7)
- **T7-0 — shared `action_guard.guard_tool_dispatch` helper.** tier M. blockedBy P-REG, P-ID.
  - `GuardOutcome{proceed: bool, decision: AuthorizationDecision | None, family: str, mode, blocked_reason,
    error_reason}` — `decision` is **Optional**. **Two-stage isolation** (codex re-audit R#5): stage-1
    computes the verdict+`proceed` (resolve→authorize) inside a try that, on raise, yields a well-formed
    ERROR outcome (never a fabricated verdict — codex M6) and fails-closed-if-enforce; stage-2 telemetry
    (metrics+log) is **best-effort in its own try** and its failure NEVER alters the already-computed
    `proceed`. (The v2 "any raise → error outcome" wrongly coupled telemetry to the decision.)
  - Mode config keyed by **`(adapter_namespace, family)`** from a **CLOSED enum/registry** (codex B3 + re-audit
    R#3): `adapter_namespace` and `family` are validated constants, not free strings; a **startup matrix
    validation** rejects unknown keys; and an unknown/typo `(adapter,family)` at call time **fails CLOSED in
    enforce** (never silently defaults to shadow → bypass). Default all-`shadow`; the exact knob T11 turns.
  - Missing ctx → `for_unbound` (codex M7), recorded `authorization_source="unbound"`.
  - Metrics in `metrics.py` at BOTH the `_AVAILABLE` block AND `reset_for_tests()` (multiproc lockstep):
    `action_guard_decision_total{adapter,family,verdict,authorization_source,mode}` +
    `action_guard_fail_closed_total{adapter,reason}`.
  - **Retarget** `test_authorization_kernel.py::test_kernel_is_dormant_no_callers_in_backend` → assert the
    kernel is reachable ONLY via `guard_tool_dispatch` (allow `action_guard.py`; assert no adapter imports
    `authorize_action`/`OperationRequest` directly) — else T7-0 turns it red (correctness BLOCKER).
  - Dormant (no caller). Note: NOT fully inert (adds metric exposition) (codex M15).
- **T7a — wire nodes.py adapters (chat #1 + specialist #2; A2A #5 dormant-guarded).** tier L. blockedBy T7-0.
  **HAND-DRIVEN** (2,491-line file, same-file mutex, security-critical — codex M15).
  - `guard_tool_dispatch` before each `ainvoke`; **fix the PEP fail-open OUTRIGHT** — fail-closed for
    mutating/unknown, allow only authoritatively-classified read-only; decoupled from the family flag (codex
    M8, unanimous). Chat: `execution_context` **keyword-only defaulted** param threaded from `conversation_node`;
    update ALL **4** test callers (`test_conversation_tool_rounds.py:62,251`; `test_sora_chain_stress.py:128,219`)
    (codex M14/correctness). A2A: resolve dynamic `external_agent:<id>` name → `delegation`; on
    closure-tenant ≠ ctx-tenant, an **explicit denial** before endpoint/client invocation — NOT a Python
    `assert` (stripped under `-O`) (codex M13 + re-audit R#6).
  - Fault-injection tests per site (enforce-patched): deny/requires_grant/kernel-raise/gate-raise ⇒ handler NOT
    run; shadow ⇒ handler run + metric.
- **T7b — wire `ToolDispatcher.execute` (#3, covers Memory Tool).** tier M. blockedBy T7-0, P-ID.
  - `guard_tool_dispatch` before the handler using the T4c runner ctx; **fix the proficiency fail-open
    OUTRIGHT** (codex M8). **Guard placement = INSIDE `ToolDispatcher.execute`** — VERIFIED all three alternate
    wrappers funnel through it (`ToolWrapper.__call__`→`dispatcher.execute` `tool_wrapper.py:61`;
    `invoke_tool`→`effective_dispatcher.execute` `tool_call_wrapper.py:99`; `DetectorAwareDispatcher.execute`→
    `self._inner.execute` `context_reset.py:135`), so one chokepoint covers all of them; the inventory test
    (codex M12) asserts no wrapper reaches a handler except via `execute`. Memory-Tool structural note (raw
    `handle()` sink hardening = T8, codex M11).

**Dependency graph**: P-REG, P-ID, P-PROV, P-SKILL are parallel leaves. T7-0 ⟵ {P-REG, P-ID}. T7a ⟵ T7-0.
**T7b ⟵ {T7-0, P-ID, P-PROV}** (P-PROV must disable `code_execution` first, else T7b's "runner surface guarded"
claim is false). Downstream (unchanged): T9/T10 grants ⟵ T7a+T7b; **T11 enforce** ⟵ everything + T8(schema)
+ T5(provenance). **T11 note (codex M9)**: runner grants must derive from a **trusted control-plane assignment
record**, binding canonical action/target/args — NEVER ticket text/labels/model-comments/memory, and never a
blanket `Bash` capability. Do NOT enforce runner `code_write`/`deploy` families until that grant path exists.

## 4. Decisions locked by this audit (carry forward)
1. **Fail-opens fixed OUTRIGHT** (fail-closed for mutating/unknown, read-only pass-through), decoupled from the
   kernel family flag — not the v1 "observe-then-flip." (3/3 auditors.)
2. **Enforce config = `(adapter_namespace, family)`**, so a family can enforce for chat/specialist while
   deferred for the runner until its grant path (T9/T10) exists.
3. **Unknowns**: after P-REG completes, `resolve` returns `__unknown_deny__` ONLY for genuinely-unregistered
   tools; the kernel/guard treat unknown as deny **independent of family flags** (reconciles frozen H1b
   "deny unknown" with shadow — unknowns were never a legitimate model tool). Real tools now carry real
   families, so a family flip is gradual, not a discontinuous "unknowns sweep" (codex B2/M10).
4. **MCP (#6) is a forward-boundary problem, not an in-process guard** — contained via allowlist/disable
   (P-MCP) until T2b's governed proxy.
5. **T7 has no standalone security value**; containment holds the line until T11.

## A. Audit findings ledger (folded)
codex(NO-GO): B1 MCP-bypass→P-MCP · B2 registry-miss→P-REG · B3 global-flag→(adapter,family) · B4 Skill-escape
→P-SKILL+registry family · M5 runner-ctx→P-ID(T4c) · M6 raise-contract→Optional-decision+whole-body-catch ·
M7 unbound→for_unbound+kernel-deny · M8 fail-opens→outright · M9 runner-authority→T11 control-plane note ·
M10 shadow-no-boundary→§0 · M11 memory-sink→T8 note · M12 completeness→inventory+wrapper tests · M13 A2A→dormant
+resolver+tenant-assert · M14 chat kw-only+per-round-provenance(T5) · M15 decomp→hand-drive T7a.
safety-lens(GO-W-C): registry-coverage(≡B2), write-back poison-loop hard-blocks T11-memory on T8, fix-outright
fail-opens(≡M8). correctness-lens(GO-W-C): dormancy-test BLOCKER→T7-0 retarget, `for_machine` None+request_id
(≡M5), 4-not-3 chat callers, `reset_for_tests` metric mirror, mode double-resolve advisory (harmless).
**codex RE-AUDIT of v2 (NO-GO, folded into v3)**: B4 CLOSED; B1→P-PROV +unknown-server-deny +exact-allowlist
(prefix predicate too loose) +rollback; B2→P-REG cover BOTH launchers incl Skill/Agent; B3→closed-enum
`(adapter,family)` +startup validation +unknown-key-fail-closed. Residual BLOCKERs: R#1 MCP(≡B1)→P-PROV;
**R#2 `code_execution_20260120` provider-side (NEW structural)→P-PROV disable-before-T7b**; R#3 mode unknown-key
fail-closed→T7-0; R#4 P-ID name each launcher source +wrapper/child-ctx propagation. MAJ: R#5 telemetry-isolate
(2-stage)→T7-0; R#6 A2A explicit-deny not `assert`→T7a. All folded above.
