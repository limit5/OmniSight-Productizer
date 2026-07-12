# U6-0 · T7 phase — SESSION HANDOFF (2026-07-12)

Read this to resume the memory-system work in a fresh session. Pairs with the FROZEN design
`docs/design/2026-07-12-phase-u6-0-t7-wire-kernel-adapters-design.md` (the technical spec) and the parent
frozen kernel design `docs/design/2026-07-11-phase-u6-0-action-capability-guard-design-v2.md`.

---

## 0. THE NORTH STAR — why any of this exists (do not lose this at handoff)
The "memory system" is **ONE unified, governed, safety-boundaried memory system that serves THREE respective
principals, each with its own scoped memory, all sharing one kernel + governance + store**:

1. **Sora** (orchestrator / chat persona) — the U6 3-tier persistent memory: L1 working [exists] + L2 episodic
   + L3 semantic per-user facts. Greenfield L2/L3 (design v2 written, deferred behind the kernel).
2. **worker** (the runner / specialist agents) — the runner-learning / "3D memory" system: episodic
   error→solution, BM25 lessons (LIVE), 3D project-state (LIVE), + the deferred experience loop
   (completion-hook → quarantine → curator → pickup / playbook / lifecycle). This is the original
   `project_3d_memory_revival` goal.
3. **Claude (this assistant), across sessions** — user-CONFIRMED (2026-07-12): Claude should ALSO have
   cross-session long-term memory THROUGH this same governed system (UNIFY, don't run a separate ad-hoc
   mechanism). Interim = the `~/.claude/.../memory/` file store (works today). Target = a runtime-agnostic
   governed store OR an OmniSight memory API/bridge the Claude Code runtime can read/write, so Claude's memory
   shares the same governance (provenance, scoping, never-poison) as Sora/worker. ⚠ Claude Code is a SEPARATE
   runtime from the OmniSight backend, so this is a real cross-runtime integration — design it AFTER the kernel
   + at least Sora's tier exist (so there's a store to bridge to).

**Why the shared kernel comes first (Phase U6-0):** both Sora AND worker ACT and both get memory INJECTED
before acting, so a poisoned memory could make either act. The shared safety foundation = an
**action-capability authorization kernel** with the invariant: *persistent memory — and any model-authored
content — can NEVER authorize a side effect; enforce in code, not prompt.* Only after the kernel ENFORCES can
each memory leg safely feed an acting agent. So the build order is: **kernel (U6-0) → then the three memory
legs on top.**

---

## 1. WHERE WE ARE (2026-07-12)
- **U6-0 kernel is BUILT + DORMANT** on `gerrit-sora/develop` (7 changes: containment #2062/#2064/#2065 +
  identity ExecutionContext #2066/#2067 + registry #2068 + `authorize_action` core #2069). Nothing calls the
  kernel yet.
- **T7 (wire the kernel into live dispatch) was DESIGNED + AUDITED + FROZEN this session.** The 3-way audit
  (2 codex xhigh passes + a safety-lens + a correctness-lens subagent) proved "T7 = wire 6 adapters" was
  UNDER-SCOPED: the frozen foundations (registry / identity / containment) were built only PARTIALLY. T7 is
  now an **~8-ticket phase** (see §2). 6+ verified blockers folded; freeze by judgment.
- **P-REG ✅ MERGED** = OP-2598 / Gerrit #2070. develop tip is now `a73070f7`. The tool registry now classifies
  the whole production dispatcher surface (`code_execution`, `memory`, `KnowledgeRetrieval`, `SlackPostMessage`,
  the dynamic `external_agent:` prefix) + `Skill`→new `skill_exec` family + a coverage-parity test. The kernel
  stays dormant (verified).

---

## 2. THE REMAINING T7-PHASE TICKETS (file these next; dependency-ordered)
Each needs its OWN mini-design + blind-test before filing (the containment ones are behavior changes — do NOT
rush them). Blast-radius was checked 2026-07-12; the nuances below are load-bearing.

### Foundation leaves (parallel)
- **P-ID** — runner ExecutionContext identity (the missing "T4c") + `for_unbound` factory + kernel
  unbound-hard-deny rule. **Dormant / populate-only (T4b-safe).** tier L — but **SPLIT substrate vs threading**
  (T4a/T4b pattern) to avoid the ~1800s runner timeout (a prior 6-file ticket timed out). Concrete per-launcher
  identity sources (already mapped): `run_s1_via_anthropic_sdk.py::process_ticket` = the JIRA pickup record
  (tenant via project→tenant map); `auto-runner-sdk.py::run_one_item` = the runner PROCESS's service identity
  (no per-item human/tenant — it's TODO-driven). Thread through `AnthropicClient.run_with_tools` →
  `ToolDispatcher.execute`, across `DetectorAwareDispatcher`, + child-ctx for `sub_agent.make_agent_tool_handler`.

### Containment leaves (parallel; BEHAVIOR CHANGES — design carefully)
- **P-PROV** — provider-side execution-surface containment (the surfaces the in-process guard CANNOT reach).
  Verified provider-side set = **`code_execution_20260120`** (in run_s1's `BUILT_IN_TOOLS_SPEC`, runs in
  Anthropic's PTC sandbox) + **remote MCP servers**. ⚠ `code_execution` is actively offered to the model —
  check runner dependency before disabling (they also have client-side `bash`/edit). Gmail/Calendar/Drive MCP
  are **token-gated** (`OMNISIGHT_MCP_{GMAIL,GOOGLE_CALENDAR,GOOGLE_DRIVE}_TOKEN`) — inactive unless the token
  is set. Fix: per-server policy map at `RemoteMCPRegistry.to_anthropic_mcp_servers` + **default-DENY unknown
  servers** + **exact** (not prefix) JIRA/Graphiti method allowlist (the current `is_graphiti_mcp_read_only_tool`
  `get*`/`find*` prefix is too loose) + disable/read-only-PTC for `code_execution`. Keep Figma. Rollback =
  keep-disabled.
- **P-SKILL** — contain executable `*.skill` from MODEL use. ⚠ **3 TRUSTED repo-committed skills are in live
  use** (`scripts/skills/{run_tests,fmt,lint_changed}.skill`), so a blanket disable would break the runner's
  test/fmt/lint. Containment MUST be **trusted/signed registration** (allow committed skills, block
  MODEL-created `*.skill` exec) — codex B4's second option — NOT a blanket disable. First verify whether these
  3 are invoked via the model's `Skill` tool vs the harness directly (that decides the exact seam).

### Guard core + wiring
- **T7-0** — shared `action_guard.guard_tool_dispatch` helper + metrics + **`(adapter_namespace, family)`
  CLOSED-enum mode flag (unknown key FAILS CLOSED, not shadow)** + 2-stage telemetry isolation + `for_unbound`
  for missing ctx + **retarget** `test_authorization_kernel.py::test_kernel_is_dormant_no_callers_in_backend`.
  Dormant. ⟵ {P-REG ✅, P-ID}.
- **T7a** — wire `nodes.py` adapters (chat `_run_tool_rounds` kw-only ctx + `tool_executor_node`; A2A outbound
  is DORMANT — no prod caller). **Fix the PEP fail-open OUTRIGHT.** A2A tenant-mismatch = explicit denial (NOT
  a Python `assert`). **HAND-DRIVE** (2,491-line file, same-file mutex, security-critical). ⟵ T7-0.
- **T7b** — wire `ToolDispatcher.execute` (one chokepoint — covers the Memory Tool + all three wrappers
  `ToolWrapper`/`invoke_tool`/`DetectorAwareDispatcher`, verified). **Fix the proficiency fail-open OUTRIGHT.**
  Inventory tests. ⟵ {T7-0, P-ID, P-PROV}.

**Dependency graph**: {P-REG ✅, P-ID, P-PROV, P-SKILL} are leaves. T7-0 ⟵ {P-REG, P-ID}. T7a ⟵ T7-0.
T7b ⟵ {T7-0, P-ID, P-PROV}. Downstream: T9/T10 grants ⟵ T7a+T7b; **T11 enforce** ⟵ everything + T8 + T5.

## 3. LOCKED DECISIONS (carry forward)
1. **T7-shadow has NO standalone security value** — containment (H0/H0.5 + P-PROV + P-SKILL) holds the line
   until **T11** flips enforcement family-by-family.
2. Fail-opens fixed **OUTRIGHT** (fail-closed for mutating/unknown, read-only pass-through), decoupled from the
   kernel family flag (3/3 auditors).
3. Enforce config keyed **`(adapter_namespace, family)`** (closed enum; unknown key fails closed) so a family
   can enforce for chat while deferred for the runner until its grant path (T9/T10) exists.
4. **T11 runner grants must come from a TRUSTED CONTROL-PLANE assignment record** — never ticket text / labels
   / model comments / memory, and never a blanket `Bash` capability.
5. **Poisoning is NOT "closed" by T7.** The Memory-tool `handle()` raw write-sink + verified-only auto-read =
   **T8** (source-aware episodic schema). T11's memory family is hard-blocked on T8.

## 4. HOW TO RESUME (working mode — it worked all session)
1. Fetch the real develop tip; do reads/reviews in a throwaway worktree
   (`git worktree add -q -d /tmp/u6 gerrit-sora/develop`). My OmniSight-sora worktree is STALE.
2. Per ticket: mini-design → 3-way adversarial audit for the tricky ones (codex xhigh
   `~/.nvm/versions/node/v24.15.0/bin/codex exec -C <dir> -s workspace-write -c model_reasoning_effort=xhigh -`
   via stdin + subagents) → fold → **blind-test** (fresh general-purpose agent, plan-only, verify symbol claims
   against develop) → fold → file `scripts/file_jira_ticket.py --priority High --tier {M|L|X} --type feature
   --areas backend,tests --class subscription-claude` → runner builds → **I review** (fetch the Gerrit change,
   run its tests in a throwaway worktree) → recommend +2 → **USER +2s (the hard gate)** → merges.
3. Recommended next order: **P-ID first** (safe/dormant, unblocks T7-0; split it), then P-PROV + P-SKILL
   (containment, real security value, need blast-radius-aware design), then T7-0 → T7a/T7b.

## 5. KEY ARTIFACTS
- FROZEN T7 design: `docs/design/2026-07-12-phase-u6-0-t7-wire-kernel-adapters-design.md`
- FROZEN kernel design: `docs/design/2026-07-11-phase-u6-0-action-capability-guard-design-v2.md`
- U6 Sora 3-tier memory design (the eventual L2/L3 payload, deferred): `docs/design/2026-07-11-phase-u6-sora-3tier-persistent-memory-design-v2.md`
- ⚠ All these design docs + this handoff are UNCOMMITTED in the OmniSight-sora worktree — commit to develop from a FRESH tree (hand-driven, `--tier X` tracking ticket).
