# Sprint A / B / C — Consolidated Master Plan

**Status**: planning artefact, source-of-truth for all sprint specs. JIRA tickets reference back to this doc; all implementations land per the specs here.
**Date**: 2026-05-11
**Purpose**: operator request — *"幫我把這幾天討論的這些部份的 sprint A/B/C 都詳細的確認，並且把所有的 task 都寫清楚，務必確認在實作的時候不會產生偏差。寧可讓 task 變多，也不要過於空泛沒想清楚就實作"*
**Predecessors**:
- `docs/audit/2026-05-09-aider-swe-agent-audit.md` (Gerrit #332) — initial audit
- `docs/audit/2026-05-09-sprint-b-runner-reliability-plan.md` PS3 (Gerrit #333) — Sprint B planning + charter
- `docs/architecture/sdk-runner-sprint-b-error-handling.md` — companion engineering doc for Sprint B (FSM, error catalog, cross-cutting concerns)

---

## 0. Executive summary

| Sprint | Status | Children | Days | Pilot $ | Re-pilot success target | Key innovation |
|---|---|---:|---:|---:|---:|---|
| **A** | Filed + partly in-flight | 15 (4 Under Review, 2 In Progress, 9 To Do) | ~30d (filed) | $50 | n/a (foundational) | Production activation of SDK runner |
| **B** | Specs complete, awaiting filing | **14** (B0–B13) | ~17d | $130 | 0/3 → ~90% | Reliability layer + Anthropic Memory Tool spike |
| **C** | Specs complete, blocked by Sprint B completion | **9** (C1–C9) | ~24d | $200 | maintain 90% + 50% cost reduction | **Failure-class-indexed memory + replay deprecation** (proprietary differentiation) |

**Total**: 38 children across 3 sprints, ~71 dev-days, ~$380 pilot budget. Single-pipeline architecture (no parallel sub-agents) per Cognition + Anthropic harness blog. Tier:X children (high-stakes, human-required approval) flagged explicitly.

**Three governance constants** (apply to every child in every sprint):
- **G-Tier**: every ticket has `tier:S/M/L/X` label. Runners' JQL excludes `tier:X`.
- **G-Class**: every ticket has `class:subscription-claude` or `class:subscription-codex`. Runners pick up only their own class.
- **G-Spec-Lint**: every ticket description must contain `## Acceptance criteria`, `## Error catalog`, `## State transitions`, `## Recovery / rollback`. CI lint extension rejects missing sections (per PS3 §13 G1).

---

## 1. Sprint A — current state + refinement notes

Sprint A is filed (OP-808 META + OP-809..OP-823). DO NOT refile. Refinement notes inline; operator decides which to amend in JIRA.

| ID | Key | Title | Status | Class | Tier | Refinement note (post-PS3 audit) |
|---|---|---|---|---|---|---|
| **A1** | OP-809 | Bash handler shell-mode + cwd-lock allowlist | Under Review | claude | M+X | **Conditional Won't-Do** if Sprint B1 (Anthropic built-in `bash_20250124`) pilot succeeds. Hold Under Review until B1 verified. |
| **A2** | OP-810 | Tool error feedback with structured "use X instead" hints | To Do | codex | M | **Scope shrunk** — built-in tools have structured errors out of the box. A2's scope = custom tools only (Skill/Agent/MCP wrappers). |
| **A3** | OP-811 | Skill / Agent / sub-agent tools wired in SDK launcher | In Progress | claude | M | Continue. |
| **A4** | OP-812 | run_tests / lint_changed / fmt higher-level tool primitives | To Do | codex | M | **Augment with B12 reflection-loop spec** — failure-feed-back path links to B3 + B12. |
| **A5** | OP-813 | MCP-JIRA integration | In Progress | claude | M | Continue. |
| **A6** | OP-814 | MCP-Gerrit integration | Under Review | claude | M | Continue. |
| **A7** | OP-815 | Tier-aware model routing (S/M/L → Haiku/Sonnet/Opus) | To Do | codex | S | **Defer to Sprint C** — research validated this as "premature without baseline". Move to C5 (model routing). |
| **A8** | OP-816 | Auto-escalation Sonnet → Opus on first retry | To Do | codex | S | Keep, but layer over B3 reset semantics (escalate on first reset, not first retry). |
| **A9** | OP-817 | Per-ticket worktree isolation | To Do | claude | M+X | **Phase 0 prerequisite** for Sprint B (B0/B1 require worktree isolation). Promote priority. |
| **A10** | OP-818 | Resume-from-partial — git stash on max_iterations | To Do | claude | L+X | **Subsumed by B3 + recovery primitives §7** in Sprint B companion doc. Reframe as "implement primitive per spec". |
| **A11** | OP-819 | Structural decomposer — auto-propose ticket split on max_iterations | To Do | claude | L+X | **Defer** per PS3 §11 ⑤ JIT-HTN evaluation. Re-evaluate after Sprint B data. |
| **A12** | OP-820 | Per-ticket budget tracker UI tile | To Do | codex | M | Continue. |
| **A13** | OP-821 | Daily SDK cost report — Slack summary + Grafana | To Do | codex | S | Continue. |
| **A14** | OP-822 | Failure pattern detector | Under Review | claude | M | **Foundation for Sprint C C2 (Failure-Class-Indexed Memory)**. The data this produces feeds C2's index. |
| **A15** | OP-823 | SDK runner daemon mode | To Do | claude | L+X | Continue (gates Sprint B B0+B1 pickup loop). |

**Operator action items for Sprint A** (not to be auto-executed by runner):
1. Decide A1 close-as-Won't-Do timing (after B1 pilot)
2. Decide A7 → C5 move (defer to Sprint C, close A7 as Won't Do or move)
3. Decide A11 → defer
4. Promote A9 priority (Phase 0 prerequisite for Sprint B)
5. Augment A4's description to reference B3 + B12

---

## 2. Sprint B — Reliability layer (full specs, 14 children)

**Filing strategy** (per PS3 O8): META + Phase 0 child filed immediately. Phase 1 children filed after Phase 0 passes G3 regression gate. Phase 2/3/4 staged similarly.

**Charter G1–G4** (PS3 §13) MUST pass before Phase 1 children file:
- G1 — every ticket has 4 required sections (AC + Error + FSM + Recovery)
- G2 — 8 cross-cutting mitigations landed in code/runbook
- G3 — B0 regression tests pass
- G4 — kill switch matrix acknowledged

### 2.1 Sprint B META

**Title**: META: Sprint B — SDK Runner Reliability Layer (14 children)
**Tier**: X (META, never auto-picked)
**Class**: meta
**Description (JIRA)**: Source-of-truth: `docs/audit/2026-05-11-sprint-abc-master-plan.md` §2. Companion engineering: `docs/architecture/sdk-runner-sprint-b-error-handling.md`. Pre-flight charter G1–G4 must pass before Phase 1 children enter In Progress.

### 2.2 B0 — Past-failure regression coverage (Phase 0 prerequisite)

| Field | Value |
|---|---|
| **Phase** | 0 (prerequisite, blocks all Sprint B children) |
| **Tier** | M |
| **Class** | subscription-claude |
| **Days** | 1.5 |
| **LOC budget** | ~400 (5 test classes × ~80 LOC each) |
| **BlockedBy** | OP-817 (worktree isolation A9) — needs worktree-per-test-runner support |
| **Blocks** | All other Sprint B children (B1..B13) |
| **Files touched** | `backend/tests/test_runner_pickup_bridge_currency.py` (NEW), `backend/tests/test_runner_cross_ticket_peer_detect.py` (NEW), `backend/tests/test_runner_prior_ps_rebase.py` (NEW), `backend/tests/test_runner_feature_list_staging.py` (NEW), `backend/tests/test_runner_stream_events_freshness.py` (NEW), `backend/agents/runner_health_checks.py` (NEW, ~50 LOC, the assertions invoked by tests) |

**Acceptance criteria**:
1. `test_runner_pickup_bridge_currency` (F4/F10): pytest fixture creates synthetic `bridge_lag_seconds = 7200` state; runner pickup helper raises `BridgeStaleError`. With `bridge_lag_seconds = 60`, helper passes silently.
2. `test_runner_cross_ticket_peer_detect` (F6/F7/F12): 2-ticket fixture (`OP-X` touching `backend/main.py`, `OP-Y` also touching same file with in-flight PS); `detect_peer_conflict(OP-X)` returns `PeerConflict(peer_key='OP-Y', shared_files=['backend/main.py'])`. With non-overlapping files, returns `None`.
3. `test_runner_prior_ps_rebase` (F11): fixture with Change-Id `Iabc...` and existing PS at gerrit; `pre_locate_check(OP-Z)` returns `RebaseRequired(change_id='Iabc...')`. With no prior PS, returns `None`.
4. `test_runner_feature_list_staging` (F20-analogue): fixture commits feature-list JSON via runner helper; `git status` shows file staged at HEAD. Without staging, helper raises `FeatureListNotStaged`.
5. `test_runner_stream_events_freshness` (F25): fixture with bridge `last_event_ts < now - 600s`; `check_stream_events()` returns `StaleStreamEvents`. With recent event, returns `Healthy`.
6. All 5 tests run in CI (added to `.github/workflows/ci.yml` `pytest` step).

**Error catalog**:
- `BridgeStaleError(lag_seconds: int)` — bridge daemon >1h behind develop
- `PeerConflict(peer_key: str, shared_files: list[str])` — F6/F7/F12 detected
- `RebaseRequired(change_id: str)` — F11 prior PS exists
- `FeatureListNotStaged(file: str)` — F20-analogue
- `StaleStreamEvents(last_event_ts: float)` — F25

**State transitions**: B0 itself is stateless — it's a test suite. Each test has its own setup → exercise → assert → teardown FSM (standard pytest).

**Recovery / rollback**: tests are idempotent. Failure ⇒ ticket stays In Progress until tests added or fixed. No rollback; can't break production.

**Test plan**: 5 test classes × 2-3 cases each (positive + negative + edge) = ~12 test cases.

**DoD**: all 5 tests green in CI. PS3 §13 G3 gate passed.

### 2.3 B1 — Anthropic built-in tools + Programmatic Tool Calling

| Field | Value |
|---|---|
| **Phase** | 1 |
| **Tier** | M |
| **Class** | subscription-claude |
| **Days** | 2 |
| **LOC budget** | ~600 (2 tool handlers + dispatcher refactor) |
| **BlockedBy** | OP-817 (A9 worktree), OP-823 (A15 daemon mode), B0 |
| **Blocks** | B2, B3, B4, B5, B6 (every Phase 2-3 child uses these tools) |
| **Files touched** | `scripts/run_s1_via_anthropic_sdk.py` (refactor: replace `RUNNER_TOOLS` with built-in spec), `backend/agents/tool_dispatcher.py` (add `text_editor_handler`, `bash_handler_v2`, `ptc_sandbox_handler`), `backend/tests/test_built_in_tools_dispatch.py` (NEW), `backend/tests/test_ptc_sandbox_isolation.py` (NEW) |

**Acceptance criteria**:
1. Runner calls Anthropic API with `tools=[{"type": "text_editor_20250728", "name": "str_replace_based_edit_tool"}, {"type": "bash_20250124", "name": "bash"}, {"type": "code_execution_20260120", "name": "code_execution", "allowed_callers": ["text_editor_20250728", "bash_20250124"]}]` (no schema property, schema-less per Anthropic spec).
2. PTC sandbox `cwd` MUST be `<worktree-path>` from runner; sandbox launch refuses if env var `OMNISIGHT_WORKTREE_PATH` unset.
3. PTC sandbox `allowed_callers` whitelist limits to text_editor + bash; HTTP / DB calls raise `sandbox_boundary_violation`.
4. `text_editor` 5 commands all routed: `view`, `create`, `str_replace`, `insert`, `undo_edit`. Each returns either result content or structured error (`tool_input_invalid`, `text_editor_no_match`, `text_editor_path_outside_worktree`).
5. `bash` tool with persistent shell state across calls (per Anthropic spec). `restart` action resets shell.
6. Re-run on 3 failed S1 pilot tickets (Phase 1 gate per charter G3): ≥2/3 produce a Gerrit PS.
7. Companion test `test_ptc_sandbox_isolation`: assert sandbox writes to worktree path, refuses writes outside. Assert sandbox cannot make HTTP requests.

**Error catalog**: see companion doc §4 B1 entry — 8 errors enumerated (`tool_input_invalid`, `text_editor_no_match`, `text_editor_path_outside_worktree`, `bash_metachar_blocked`, `bash_timeout`, `ptc_sandbox_oom`, `ptc_sandbox_network`, `ptc_creds_missing`).

**State transitions**: companion doc §4 B1 FSM diagram.

**Recovery / rollback**: per-tool-call recovery via `text_editor.undo_edit` + worktree git status snapshot. PTC sandbox restart on transient errors; abort on `ptc_creds_missing` / `sandbox_boundary_violation`.

**Test plan**: 2 test files × ~10 cases each. Includes mocked Anthropic responses for each error code, sandbox-launch isolation test, undo_edit roundtrip test.

**DoD**: all tests green + Phase 1 re-pilot ≥2/3.

### 2.4 B2 — Static-analysis pre-flight gate

| Field | Value |
|---|---|
| **Phase** | 1 |
| **Tier** | M |
| **Class** | subscription-codex |
| **Days** | 0.5 |
| **LOC budget** | ~150 (linter wrapper + cap counter) |
| **BlockedBy** | B1 (uses bash tool) |
| **Blocks** | B4 (Critic invokes lint as part of review) |
| **Files touched** | `backend/agents/static_analysis_gate.py` (NEW), `scripts/run_s1_via_anthropic_sdk.py` (integrate post-edit hook), `backend/tests/test_static_analysis_gate.py` (NEW) |

**Acceptance criteria**:
1. After every `text_editor` write, runner triggers `run_static_analysis(file_paths)` which dispatches per-language: Python → `ruff check + mypy --ignore-missing-imports`; TypeScript → `eslint + tsc --noEmit`.
2. If errors detected, structured output (`{file, line, col, code, message}`) fed back to model as next user turn.
3. **3-round hard cap** (Aider issue #1090 mitigation): per-ticket `lint_round_counter`. After 3 rounds without convergence, mark commit `lint_partial` and proceed to commit (no merge block for cosmetic).
4. Cap counter persisted to `progress.txt` (B9 dependency); survives restart.
5. `linter_binary_missing` → degrade gracefully (skip lint, log warning); do not block.
6. `lint_target_outside_worktree` raises `sandbox_boundary_violation` (I1 invariant).

**Error catalog**: companion doc §4 B2 — `linter_binary_missing`, `linter_config_conflict`, `lint_cap_exceeded`, `linter_crash`, `lint_target_outside_worktree`.

**State transitions**: companion doc §4 B2 FSM.

**Recovery / rollback**: counter persisted to progress.txt; on resume, counter restored. If progress.txt corrupt, counter resets to 0 (defensive: better to over-lint than under).

**Test plan**: 6 test cases — clean lint, dirty + 1 round resolves, dirty + 3 rounds cap, missing binary, config conflict, target outside worktree.

**DoD**: tests green + integration with S1 launcher.

### 2.5 B3 — 3x-loop hard reset + context reset + ToM scratchpad

| Field | Value |
|---|---|
| **Phase** | 1 |
| **Tier** | M |
| **Class** | subscription-claude |
| **Days** | 1.5 |
| **LOC budget** | ~350 (detector + reset orchestrator + scratchpad) |
| **BlockedBy** | B0, B1 |
| **Blocks** | B12 (reflection loop reuses this infrastructure) |
| **Files touched** | `backend/agents/loop_detector.py` (NEW), `backend/agents/context_reset.py` (NEW), `backend/agents/tom_scratchpad.py` (NEW), `scripts/run_s1_via_anthropic_sdk.py` (integrate detector hook), `backend/tests/test_loop_detector.py` (NEW), `backend/tests/test_context_reset.py` (NEW) |

**Acceptance criteria**:
1. After every tool call, append `(tool_name, args_hash, error_class)` triple to per-ticket detector log.
2. `args_hash = sha256(canonical_json(tool_args))[:16]`. Triple match (3× same `(tool, args_hash, err_class)`) triggers `reset_required = True`.
3. **Hash tolerance** (D3 mitigation): if successive args differ by ≥ 1 token (Levenshtein on canonical JSON), count as progress, NOT match.
4. Reset semantics:
   - In-flight tool call: **awaited, not killed** (avoids F3 ambiguous-state).
   - On reset: clear conversation history except (a) system prompt, (b) first user message (containing AC + ticket text).
   - Inject 1 paragraph: `"PRIOR ATTEMPT (now reset): you tried <tool_call_signature> 3 times with error <error_class>. Try a different approach."`
   - ToM scratchpad **persists across reset** (only continuous trace).
   - CostGuard tally **NOT reset** (same ticket, same budget).
5. Reset upper bound = 3. After 3 resets, abort with `loop_aborted_terminal`. Total raw attempts max = 9.
6. ToM scratchpad: structured JSON appended per assistant turn — `{"hypothesis": str, "verifying": str, "outcome": "pending|success|fail"}`. Reject malformed scratchpad (not counted as progress).
7. Scratchpad written to `progress.txt` (B9); survives crash.

**Error catalog**: companion doc §4 B3.

**State transitions**: companion doc §4 B3 FSM (large diagram). Q1-Q4 critical decisions encoded.

**Recovery / rollback**: detector state in progress.txt. On resume mid-resetting, re-issue reset (idempotent); failed-reset attempt counts toward N=3 cap.

**Test plan**: 12 test cases covering: 2x match (no trigger), 3x match (trigger), false-positive narrowing args (no trigger), reset-during-inflight, scratchpad cross-reset persistence, CostGuard not reset, 3-reset abort, malformed scratchpad rejection, args_hash collision robustness.

**DoD**: tests green + integration with S1 launcher + Phase 1 re-pilot ≥2/3 (combined with B1+B2).

### 2.6 B4 — Pre-commit Critic

| Field | Value |
|---|---|
| **Phase** | 2 |
| **Tier** | M |
| **Class** | subscription-claude |
| **Days** | 1 |
| **LOC budget** | ~250 (critic agent + reason-code enum + integration) |
| **BlockedBy** | B1, B2, B3 (Phase 1 stable) |
| **Blocks** | B6 (checklist invokes critic) |
| **Files touched** | `backend/agents/critic_agent.py` (NEW), `backend/agents/critic_reason_codes.py` (NEW enum), `scripts/run_s1_via_anthropic_sdk.py` (pre-commit hook), `backend/tests/test_critic_agent.py` (NEW) |

**Acceptance criteria**:
1. After coder phase produces commit (but before push), invoke critic with: diff + AC text + read-only tools (Read, Grep, run-tests, run-linter).
2. Critic model: **Haiku** by default (`claude-haiku-4-5`). Configurable via env `OMNISIGHT_CRITIC_MODEL`.
3. Critic emits structured verdict: `{"verdict": "pass|dissent", "reason_code": <enum>, "reason_text": str}`. Reason code enum: `missing_context`, `style_violation`, `logic_bug`, `test_inadequacy`, `ac_drift`, `governance_violation`, `other`. Free-text in `reason_text`.
4. Dissent protocol:
   - 1st dissent → coder retry with critic reasons injected (1 free retry).
   - 2nd dissent → ticket marked `under_review:critic_dissent`, escalate to operator.
5. Critic infrastructure failure (timeout, OOM, malformed output) → verdict = `pass` with audit log. Critic NEVER blocks coder due to its own bug.
6. Critic NEVER posts to Gerrit Code-Review label (governance: F9 ADR-0003 violation guard).
7. Verdict + reason logged to JIRA as comment + commit message footer.

**Error catalog**: `critic_timeout`, `critic_oom`, `critic_malformed_reason_code`, `critic_tool_failure`, `dissent_loop_unbreakable`.

**State transitions**: companion doc §5 B4 FSM.

**Recovery / rollback**: critic is read-only; safe to re-run. Idempotent.

**Test plan**: 8 cases covering: pass verdict, dissent + 1st-retry pass, dissent + 2nd-retry escalate, critic timeout, critic OOM, malformed reason code, governance guard (assert no Gerrit Code-Review API call), tool failure.

**DoD**: tests green + first-5-tickets dissent rate metric in JIRA dashboard.

### 2.7 B5 — TDD enforcement with applicability gate

| Field | Value |
|---|---|
| **Phase** | 2 |
| **Tier** | M |
| **Class** | subscription-codex |
| **Days** | 1.5 |
| **LOC budget** | ~300 |
| **BlockedBy** | B1, B2, B3 |
| **Blocks** | B6 |
| **Files touched** | `backend/agents/tdd_orchestrator.py` (NEW), `backend/agents/tdd_applicability.py` (NEW), `scripts/run_s1_via_anthropic_sdk.py`, `backend/tests/test_tdd_orchestrator.py` (NEW) |

**Acceptance criteria**:
1. Per-ticket `tdd_applicable` field reads from JIRA custom field (or label fallback `tdd:yes` / `tdd:no` / `tdd:conditional`). Default = `conditional`.
2. If `tdd_applicable=no` → bypass TDD phase.
3. If `tdd_applicable=yes` → enforce ordering:
   - Phase 1: write test (must fail when run).
   - Phase 2: confirm test fails (red).
   - Phase 3: edit source code.
   - Phase 4: confirm test passes (green).
   - Skip Phase 1-2 → block coder edit until test exists + fails.
4. If `tdd_applicable=conditional` → locator phase (B7) decides; if locator says "infra/scaffold work, no testable behavior", fall back to bypass.
5. Premature green (test passes before any code change) → block; require model to revise test to actually exercise the bug.
6. Test write fails 3 attempts → mark `tdd_unwriteable`, escalate.

**Error catalog**: `tdd_violated_premature_green`, `tdd_violated_unrunnable`, `tdd_unwriteable`, `tdd_conditional_indeterminate`.

**State transitions**: companion doc §5 B5 FSM.

**Recovery / rollback**: test file write idempotency token per `(ticket_key, "tdd_test_v<N>")`. Failure → revert test write via `text_editor.undo_edit`.

**Test plan**: 10 cases — applicable=no/yes/conditional × red→green / red→still-red / premature-green / unwriteable.

**DoD**: tests green + 3 sample tickets with each `tdd_applicable` value run end-to-end.

### 2.8 B6 — Submit-review checklist (feature-list JSON dual-mode)

| Field | Value |
|---|---|
| **Phase** | 2 |
| **Tier** | M |
| **Class** | subscription-claude |
| **JIRA** | OP-835 (公開済み — filed + shipped) |
| **Days** | 1 |
| **LOC budget** | ~250 |
| **BlockedBy** | B4, B5 |
| **Blocks** | (Phase 2 gate) |
| **Files touched** | `backend/agents/submit_checklist.py` (NEW), `backend/agents/feature_list_parser.py` (NEW), `scripts/jira_seed_example_tickets.py` (add feature-list JSON template), `backend/tests/test_submit_checklist.py` (NEW) |

**Acceptance criteria**:
1. Detect AC format: feature-list JSON (presence of `## Feature list (JSON)` section in description) vs legacy freeform.
2. Feature-list JSON schema:
   ```json
   [{"id": "FL1", "description": "...", "verify": "test_X passes" | "manual:operator-check"}, ...]
   ```
3. On submit: inject checklist as next user message, model must respond per item: `{"id": "FL1", "passed": bool, "evidence": str}`.
4. Any `passed=false` → coder returns to working state (re-edit). Max 3 cycles, then escalate.
5. Ambiguous response (`passed: "mostly"` or non-bool) → reject; require concrete pass/fail.
6. Legacy freeform AC → auto-convert: each bullet becomes feature-list item. If conversion produces empty list, escalate `format_unparseable`.
7. **Feature-list JSON MUST be staged before commit** (F20 staging analogue mitigation): integrity check post-write asserts file is in git index, raises `FeatureListNotStaged` otherwise.

**Error catalog**: `format_unparseable`, `legacy_up_convert_fail`, `checklist_ambiguous_response`, `checklist_3_cycle_fail`, `FeatureListNotStaged`.

**State transitions**: companion doc §5 B6 FSM.

**Recovery / rollback**: checklist is computed from AC; idempotent. Submit gate is one-shot per ticket; re-attempts use new idempotency token.

**Test plan**: 9 cases — feature-list valid, malformed JSON, legacy freeform conversion, empty conversion (escalate), all-passed flow, 1 fail + retry pass, 3-cycle escalate, ambiguous response, staging guard.

**DoD**: tests green + 5 sample tickets each with feature-list JSON.

### 2.9 B7 — Locator (Haiku) → Coder (Sonnet) sequential pipeline

| Field | Value |
|---|---|
| **Phase** | 3 |
| **Tier** | L |
| **Class** | subscription-claude |
| **Days** | 2 |
| **LOC budget** | ~500 |
| **BlockedBy** | B1, B2, B3, B4, B5, B6 (Phase 2 gate) |
| **Blocks** | B13 (blast-radius gate sits inside locator phase) |
| **Files touched** | `backend/agents/locator_agent.py` (NEW, Haiku-driven), `backend/agents/coder_agent.py` (refactor existing main coder), `backend/agents/locator_handoff_schema.py` (NEW), `backend/tests/test_locator_handoff.py` (NEW), `backend/tests/test_locator_coder_pipeline.py` (NEW) |

**Acceptance criteria**:
1. Locator agent runs first with `claude-haiku-4-5`. Tools available: `text_editor.view` only (read-only) + `Grep` + `Glob` + repo-map (B8 if available).
2. Locator output schema (frozen at filing time, in `locator_handoff_schema.py`):
   ```json
   {"files": [{"path": "...", "line_ranges": [[start,end]], "why_relevant": "..."}],
    "hypotheses": ["..."], "confidence": 0.0-1.0,
    "summary": "<200 word max>"}
   ```
3. Coder receives ONLY this JSON + AC + system prompt. NOT locator's tool-call history.
4. **Locator output verbose >2k tokens → reject** (force schema enforcement); retry with reminder (1×).
5. Locator returns 0 candidates → fallback `monolithic_coder` (full-repo Sonnet). Log warning.
6. Locator returns >50 candidates → escalate `needs:refinement`.
7. Locator timeout (60s) → fallback monolithic.
8. **Cross-ticket peer detection** (F6/F7/F12 mitigation): for each candidate file, query `gerrit query status:open project:<proj> path:<file>`; if peer PS exists for different ticket → escalate `peer_conflict`.
9. **Prior PS rebase detection** (F11 mitigation): on pickup, query `gerrit query change:I<change-id>`; if exists → rebase before locate.
10. Coder phase exits via Gerrit push; conflict handled by `_handle_gerrit_push_failure` (existing).

**Error catalog**: `locator_zero_candidates`, `locator_too_many`, `locator_timeout`, `locator_malformed_handoff`, `locator_handoff_pollution`, `coder_rejects_locator_input`, `prior_ps_exists_for_change_id`, `peer_conflict`.

**State transitions**: companion doc §5 B7 FSM.

**Recovery / rollback**: locator is read-only; idempotent. Coder writes via B1; per-write idempotency token.

**Test plan**: 14 cases — 0/few/many candidates, timeout fallback, malformed retry, pollution reject, coder reject, peer conflict, prior PS rebase, monolithic fallback path.

**DoD**: tests green + 5 sample tickets through locator-coder pipeline + handoff format frozen and committed.

### 2.10 B8 — Repo-map preamble (tree-sitter PageRank)

| Field | Value |
|---|---|
| **Phase** | 3 |
| **Tier** | M |
| **Class** | subscription-codex |
| **Days** | 2 |
| **LOC budget** | ~500 |
| **BlockedBy** | B1 |
| **Blocks** | B7 (locator uses repo-map), B13 (blast-radius reuses graph) |
| **Files touched** | `backend/agents/repo_map.py` (NEW), `backend/agents/treesitter_parser.py` (NEW), `requirements.txt` (add `tree-sitter==0.21.3` + `tree-sitter-python` + `tree-sitter-typescript`), `backend/tests/test_repo_map.py` (NEW) |

**Acceptance criteria**:
1. Build symbol-graph from `*.py`, `*.ts`, `*.tsx`. Nodes = files; edges = "file A's imports/refs file B" (extracted from AST).
2. Run PageRank seeded by ticket-mentioned files (Files Touched comment + AC text mentions).
3. Output top-N files token-budgeted. Default budget 1000 tokens; configurable per-ticket (raise to 2000 for tickets without "Files Touched" hints).
4. Cache key = repo HEAD SHA. Invalidate on every commit.
5. Per-language grammar missing → skip files of that language; log gap.
6. Per-file parse error → skip individual file; log; do not abort overall.
7. **OOM (huge repo)** → fall back to top-100 most-recently-modified files.
8. Output format injected as system-prompt prefix (not user message).

**Error catalog**: `tree_sitter_binary_missing`, `tree_sitter_grammar_missing_lang`, `parse_error_per_file`, `repo_map_oom`, `cache_corruption`.

**State transitions**: companion doc §5 B8 FSM.

**Recovery / rollback**: build is pure function; cache rebuild on corruption.

**Test plan**: 8 cases — happy path build, missing grammar (skip), per-file parse error (skip), OOM fallback, cache hit, cache miss, cache invalidation on commit, top-N truncation.

**DoD**: tests green + benchmark on current repo (build time <30s, cache hit <100ms).

### 2.11 B9 — Anthropic harness session-resume (`progress.txt` + 3-step opener)

| Field | Value |
|---|---|
| **Phase** | 3 |
| **Tier** | L |
| **Class** | subscription-claude |
| **JIRA** | OP-1122 (filed 2026-05-14 by OP-1072 B6/B9 verification) |
| **Days** | 2 |
| **LOC budget** | ~400 |
| **BlockedBy** | B1, B2, B3 |
| **Blocks** | (Phase 3 gate) |
| **Files touched** | `backend/agents/session_resume.py` (NEW), `backend/agents/progress_log.py` (NEW), `scripts/run_s1_via_anthropic_sdk.py`, `backend/tests/test_session_resume.py` (NEW) |

**Acceptance criteria**:
1. Per-ticket `progress.txt` at `<worktree-path>/.runner/progress.txt`. Append-only JSONL.
2. Schema per line: `{"ts": ISO8601, "iter": int, "role": str, "stop_reason": str, "tool_calls": [...], "content_summary": str}`. Max 500 chars per `content_summary`.
3. Synchronous fsync after every write (durability > throughput; ~100 lines/ticket).
4. **3-step opener** on session resume:
   - Step 1: verify `cwd == <worktree-path>` (F17/F24 mitigation); refuse to start if mismatch.
   - Step 2: read progress.txt; if exists, restore state; if absent → fresh session.
   - Step 3: smoke test — `python -c "import <ticket-touched-modules>"` + `pytest --collect-only`. Failure → log + warn but proceed (worktree may legitimately have unimported new modules).
5. **Bridge currency check** (F4/F10 mitigation): query `bridge daemon status`; if last event >1h, warn but proceed. If >24h, abort `bridge_stale_critical`.
6. **Stream-events freshness** (F25 mitigation): if no events in 5min, warn; flag for operator.
7. progress.txt corrupt → treat as absent; fresh session; log corruption for operator review.
8. progress.txt owner mismatch (different bot identity) → escalate `concurrent_runner_conflict`.
9. File-locking via `flock` to prevent concurrent writers.

**Error catalog**: `progress_txt_corrupt`, `progress_txt_owner_mismatch`, `cwd_not_worktree`, `smoke_test_fail`, `bridge_currency_check_fail`, `bridge_stale_critical`, `stream_events_dead`, `concurrent_runner_conflict`.

**State transitions**: companion doc §5 B9 FSM.

**Recovery / rollback**: progress.txt is single-writer per ticket. Reset on ticket pickup.

**Test plan**: 12 cases — fresh session, resume from valid progress, corrupt progress, owner mismatch, cwd mismatch, smoke test pass, smoke test fail, bridge fresh, bridge stale (warn), bridge critical-stale (abort), stream events fresh, stream events dead.

**DoD**: tests green + 3 sample tickets with mid-session simulated crash + clean resume.

### 2.12 B10 — Lesson auto-injection (BM25 over lessons-learned.md)

| Field | Value |
|---|---|
| **Phase** | 4 |
| **Tier** | S |
| **Class** | subscription-codex |
| **Days** | 0.5 |
| **LOC budget** | ~150 |
| **BlockedBy** | B1, B9 |
| **Blocks** | (deferred replacement by Sprint C C2) |
| **Files touched** | `backend/agents/lesson_retrieval.py` (NEW, BM25), `scripts/run_s1_via_anthropic_sdk.py`, `backend/tests/test_lesson_retrieval.py` (NEW) |

**Acceptance criteria**:
1. BM25 index built from `docs/sop/lessons-learned.md` at runner start. Each `## Lesson N` block = one document.
2. Query = ticket title + AC body. Top-3 retrieved.
3. Index rebuild triggered when `lessons-learned.md` mtime > index timestamp.
4. Retrieved lessons injected as system message (not user) with header `## Relevant prior lessons`.
5. Empty result → no-op; log.
6. Index unavailable → degrade silently.
7. **NOTE**: planned for Sprint C C2 replacement by Failure-Class-Indexed Memory + Replay Deprecation. B10 is the simple baseline.

**Error catalog**: `bm25_index_stale`, `lessons_file_unreadable`, `irrelevant_top_k` (no auto-detect; manual review).

**Test plan**: 5 cases — index build, query top-3, rebuild on mtime change, empty result, unreadable file degrade.

**DoD**: tests green + injection visible in 3 sample ticket logs.

### 2.13 B11 — Cache control on last 2 messages

| Field | Value |
|---|---|
| **Phase** | 4 |
| **Tier** | S |
| **Class** | subscription-codex |
| **Days** | 0.1 (10 LOC) |
| **LOC budget** | ~10 |
| **BlockedBy** | B1 |
| **Blocks** | none |
| **Files touched** | `scripts/run_s1_via_anthropic_sdk.py` (single edit in API call construction) |

**Acceptance criteria**:
1. Add `cache_control: {"type": "ephemeral"}` to last 2 messages on every API call.
2. Verify via API response `usage.cache_read_input_tokens` increments after first iteration.
3. Cache breakpoint rejected (Anthropic spec change) → silent fallback to no-cache.

**Error catalog**: `cache_breakpoint_rejected_by_api`.

**Test plan**: 2 cases — cache hit verification, fallback on rejection.

**DoD**: ~40-60% input cost reduction observed on multi-iteration tickets (per audit).

### 2.14 B12 — Reflection loop on test/lint failure (bounded retries)

| Field | Value |
|---|---|
| **Phase** | 4 |
| **Tier** | M |
| **Class** | subscription-claude |
| **Days** | 0.5 |
| **LOC budget** | ~150 |
| **BlockedBy** | B3 (reuses 3x-loop infrastructure) |
| **Blocks** | none |
| **Files touched** | `backend/agents/reflection_loop.py` (NEW), `scripts/run_s1_via_anthropic_sdk.py`, `backend/tests/test_reflection_loop.py` (NEW) |

**Acceptance criteria**:
1. On test/lint failure, structured failure object: `{"failure_type": "test|lint", "file": str, "line": int, "expected": str, "actual": str, "traceback": str}`.
2. Inject failure object into next user turn as `reflection_input`.
3. Max 3 reflection iterations per failure type; counted separately from main `max_iterations`.
4. Each reflection counts at half-weight toward main `max_iterations` (avoid double-cap).

**Error catalog**: subset of B3.

**Test plan**: 4 cases — single reflection resolves, 3 reflections resolve, 3 reflections cap, half-weight accounting.

**DoD**: tests green + integration with B2 + B5.

### 2.15 B13 — AST blast-radius gate (with component-aware threshold)

| Field | Value |
|---|---|
| **Phase** | 3 |
| **Tier** | M |
| **Class** | subscription-codex |
| **Days** | 0.5 |
| **LOC budget** | ~200 |
| **BlockedBy** | B7, B8 |
| **Blocks** | (Phase 3 gate) |
| **Files touched** | `backend/agents/blast_radius_gate.py` (NEW), `config/blast_radius_thresholds.yaml` (NEW), `backend/tests/test_blast_radius_gate.py` (NEW) |

**Acceptance criteria**:
1. After locator returns candidates, compute: `total_files = len(candidates)`, `total_loc = sum(loc per candidate)`, `dependency_depth = max graph depth in candidate set`.
2. Threshold lookup: `config/blast_radius_thresholds.yaml`:
   ```yaml
   default: {files: 3, loc: 300, depth: 4}
   area:tests: {files: 8, loc: 800, depth: 6}
   area:migrations: {files: 1, loc: 100, depth: 2}
   area:docs: {files: 10, loc: 2000, depth: 1}
   ```
3. Multi-area ticket (multiple `area:` labels) → take **max** of thresholds (most permissive); log decision in ticket comment.
4. Default fallback if `area:` label missing → strict default `(3, 300, 4)`. Log warning.
5. Tree-sitter parse fail on candidate → conservative bias: count file as 200 LOC; log.
6. Oversize → return `oversize_refuse` to main runner with structured response: `{"reason": "oversize", "files": int, "loc": int, "threshold": int}`.
7. Main runner downstream policy:
   - `option_a` auto-split: if ticket has clear seams (multiple AC items, distinct file groups) → propose split, escalate `needs:split` + auto-comment proposed split.
   - `option_b` refuse: no clear seams → label `needs:split` + revert to To Do + clear assignee.
   - `option_c` escalate: ambiguous → `under_review:oversize` for operator.
8. **Cross-ticket peer collision** (F6/F7/F12 mitigation): for each candidate file, query gerrit; if peer in-flight PS for different ticket → escalate `peer_conflict` (NOT B13's job to resolve, just detect).
9. Run in **worktree cwd** (F24 mitigation) not main repo.

**Error catalog**: `tree_sitter_parse_fail`, `threshold_yaml_malformed`, `multi_area_threshold_collision`, `cross_ticket_peer_collision`.

**State transitions**: companion doc §5 B13 FSM.

**Recovery / rollback**: read-only; cache invalidation per HEAD SHA.

**Test plan**: 10 cases — within threshold, oversize files, oversize LOC, oversize depth, multi-area collision (max), missing area (default), parse fail (conservative), cross-ticket peer detect, threshold yaml malformed, worktree cwd assertion.

**DoD**: tests green + 5 sample tickets exercising each branch.

### 2.16 Sprint B summary table

| Phase | Children | Days | Class split | Tier:X count |
|---|---|---:|---|---:|
| 0 | B0 | 1.5 | claude | 0 |
| 1 | B1, B2, B3 | 4 | 2 claude + 1 codex | 0 |
| 2 | B4, B5, B6 | 3.5 | 2 claude + 1 codex | 0 |
| 3 | B7, B8, B9, B13 | 6.5 | 2 claude + 2 codex | 1 (B9 is L) |
| 4 | B10, B11, B12 | ~1 | 2 codex + 1 claude | 0 |
| **Total** | **14 children** | **~17 days** | **8 claude + 6 codex** | **0** |

---

## 3. Sprint C — Memory layer + proprietary differentiation (full specs, 9 children)

**Premise**: Sprint B reaches ~90% pilot success at flat input cost. Sprint C target: maintain 90% + drive cost down 50% + add proprietary differentiation that no other system has.

**Filing strategy**: Sprint C META + Phase 1 children (memory layer) filed when Sprint B reaches Phase 3 gate. Phase 2 (proprietary) filed when Phase 1 lands. Phase 3+4 (optimization + research) staged later.

### 3.1 Sprint C META

**Title**: META: Sprint C — Memory layer + proprietary differentiation (9 children)
**Tier**: X
**BlockedBy**: OP-808 (Sprint B META) Phase 3 completion
**Description**: Source-of-truth: `docs/audit/2026-05-11-sprint-abc-master-plan.md` §3.

### 3.2 C1 — Anthropic Memory Tool standalone integration

| Field | Value |
|---|---|
| **Phase** | 1 (memory layer foundation) |
| **Tier** | M |
| **Class** | subscription-claude |
| **Days** | 1.5 |
| **LOC budget** | ~250 |
| **BlockedBy** | Sprint B Phase 3 gate (B7/B9 stable) |
| **Blocks** | C2, C3, C4 |
| **Files touched** | `backend/agents/memory_tool_handler.py` (NEW), `scripts/run_s1_via_anthropic_sdk.py`, `backend/tests/test_memory_tool.py` (NEW) |

**Acceptance criteria**:
1. Add Anthropic Memory Tool to runner: `tools=[..., {"type": "memory_20260120"}]` (verify exact spec from `platform.claude.com/docs`).
2. **Spike requirement**: verify Memory Tool can be used standalone WITHOUT Managed Agents runtime (confirm not coupled to `managed-agents-2026-04-01` beta).
3. Memory tool storage backed by per-fleet shared filesystem at `/var/omnisight/memory/<fleet-id>/`. NOT per-runner instance (memory must persist across runner restarts and be readable by all bot classes).
4. Storage size cap: 100MB per fleet. Eviction policy: oldest-first when cap reached.
5. Memory tool calls logged to `progress.txt` (B9) for audit.
6. **Tier-aware filter**: when reading memory, filter results by `tier:` (S/M/L/X) — runner can only auto-recall `tier:S` and `tier:M`. `tier:L` requires opt-in flag. `tier:X` requires operator approval.
7. Replace B10 (BM25 lesson retrieval) — lessons-learned.md content seeded into Memory Tool storage on first runner start.

**Error catalog**: `memory_tool_not_standalone` (depends on Managed Agents), `memory_storage_full`, `memory_corrupted`, `tier_violation_unauthorized_recall`.

**State transitions**: simple — write/read are individual API calls; storage is filesystem.

**Recovery / rollback**: memory storage is durable (filesystem). On corruption, restore from last backup.

**Test plan**: 8 cases — standalone use, write/read, eviction at cap, tier filter S/M/L/X, corruption recovery.

**DoD**: spike verifies standalone use; tests green; lessons-learned seeded.

### 3.3 C2 — Failure-Class-Indexed Memory + Replay Deprecation 🥇

**This is the proprietary differentiation child.** First-of-its-kind. No production system has this combination.

| Field | Value |
|---|---|
| **Phase** | 2 (proprietary differentiation) |
| **Tier** | L |
| **Class** | subscription-claude |
| **Days** | 5 |
| **LOC budget** | ~800 |
| **BlockedBy** | C1 |
| **Blocks** | C5 |
| **Files touched** | `backend/agents/failure_class_index.py` (NEW), `backend/agents/lesson_replay_deprecation.py` (NEW), `backend/agents/lesson_weighting.py` (NEW), `config/failure_classes.yaml` (NEW), `backend/tests/test_failure_class_index.py` (NEW), `backend/tests/test_replay_deprecation.py` (NEW), `docs/research/failure-class-indexed-memory.md` (NEW — paper outline) |

**Acceptance criteria**:
1. **Failure-class enum** in `config/failure_classes.yaml`: derived from past 25 F# incidents (Sprint B companion doc §3) + ongoing Pattern catalogue:
   ```yaml
   classes:
     - id: F-PEER-CONFLICT          # F6/F7/F12
       description: "Cross-ticket file collision"
       triggers: [["area:overlap"], ["recent_peer_ps"]]
       recovery: "B7 locator peer-detection check"
     - id: F-PRIOR-PS-REBASE         # F11
       description: "Prior PS exists for change-id"
       triggers: [["change_id_known"]]
       recovery: "B7 prior-PS rebase before locate"
     - id: F-BRIDGE-STALE            # F4/F10
       description: "Bridge daemon behind develop"
       triggers: [["bridge_lag>1h"]]
       recovery: "B9 bridge currency check at pickup"
     # ... 15+ more derived from F-list
   ```
2. **Indexer**: at ticket pickup, scan for trigger conditions; emit list of applicable failure-classes.
3. **Recovery dispatcher**: for each matched class, inject corresponding `recovery` text as system message. Pre-emptive, not reactive.
4. **Lesson weighting**: each lesson L in lessons-learned has metadata `{weight: 0.0-1.0, last_applied: ISO8601, success_count: int, failure_count: int}`.
5. **Replay deprecation**:
   - Every Friday cron: take 5 random "completed" tickets from last week. Re-run them through current runner (in dry-run mode, no Gerrit push).
   - For each ticket: which lessons did it use? Did the rerun also succeed?
   - If rerun failed despite lesson application → `failure_count++`, `weight *= 0.9`.
   - If `weight < 0.3` for >2 weeks → mark lesson `deprecation_candidate`; surface to operator review.
   - If `weight < 0.1` → auto-deprecate (move to `lessons-deprecated.md`).
6. **Lesson-effectiveness dashboard**: Grafana panel showing per-lesson weight trend.
7. **Paper outline** (`docs/research/failure-class-indexed-memory.md`): describe the system, novelty (vs Mem0/Cognee/HippoRAG), evaluation method, preliminary results. ~300 lines target.

**Error catalog**: `failure_class_yaml_malformed`, `replay_dry_run_fail` (rerun infrastructure broken), `weight_persistence_fail`, `lesson_deprecation_conflict` (operator wants to keep lesson with low weight).

**State transitions**: lesson lifecycle FSM:
```
new → active(weight=1.0) → deprecation_candidate(weight<0.3, age>2w) → 
  {operator_keeps → active (weight reset 0.5)} | {auto_deprecate (weight<0.1) → archived}
```

**Recovery / rollback**: lesson weights persisted in Postgres `lesson_weights` table (NEW migration). Backup table; rollback by restoring snapshot.

**Test plan**: 12 cases covering — failure class match + recovery dispatch, weighting math, replay scheduler, deprecation candidate surfacing, auto-deprecate, operator override, dashboard data freshness.

**DoD**: tests green + 5 lessons graded after 2 weeks of replay data + paper outline reviewed by operator.

### 3.4 C3 — Cognee migration (graph layer)

| Field | Value |
|---|---|
| **Phase** | 1 (memory layer foundation) |
| **Tier** | L |
| **Class** | subscription-claude |
| **Days** | 5 |
| **LOC budget** | ~600 + Postgres+Neo4j infra |
| **BlockedBy** | C1 |
| **Blocks** | C4 |
| **Files touched** | `backend/agents/cognee_integration.py` (NEW), `requirements.txt` (add `cognee` + `claude-agent-sdk`), `docker-compose.yml` (add Neo4j service), `backend/tests/test_cognee_integration.py` (NEW) |

**Acceptance criteria**:
1. Install Cognee with Claude Agent SDK integration (per https://github.com/topoteretes/cognee-integration-claude).
2. ECL pipeline ingests: codebase Python/TS files (AST-aware), JIRA tickets (status history), Gerrit PSes (diff + review comments), lessons-learned.md.
3. Graph stored in Neo4j; vector embeddings in pgvector (existing).
4. **Replace B8 (repo-map)** with Cognee's code KG: B8's PageRank query becomes a Cognee graph traversal query.
5. **Replace B10 (BM25 lessons)** — lessons-learned ingested into Cognee; retrieval via Cognee semantic search.
6. Memory Tool (C1) and Cognee co-exist: Memory Tool = filesystem-based scratchpad, Cognee = structured KG. Different access patterns.
7. Cognee re-indexing: incremental on every commit (not batch).

**Error catalog**: `cognee_neo4j_unavailable`, `cognee_index_corruption`, `cognee_query_timeout` (default 30s).

**State transitions**: ingestion is event-driven (commit hook → ingest); query is one-shot.

**Recovery / rollback**: Neo4j is a separate service; backup snapshots daily. On corruption, rebuild from git history (idempotent).

**Test plan**: 8 cases — initial ingestion, incremental ingestion on commit, code KG query, lessons KG query, Neo4j unavailable degrade, query timeout, multi-tenant tier filter.

**DoD**: tests green + 5 sample queries comparing Cognee vs B8/B10 baselines + B8 + B10 marked deprecated.

### 3.5 C4 — Graphiti for ticket-temporal queries

| Field | Value |
|---|---|
| **Phase** | 1 (memory layer foundation) |
| **Tier** | M |
| **Class** | subscription-codex |
| **Days** | 2 |
| **LOC budget** | ~300 + MCP server config |
| **BlockedBy** | C3 (decide overlap with Cognee) |
| **Blocks** | none |
| **Files touched** | `backend/agents/graphiti_mcp_client.py` (NEW), `config/mcp_servers.yaml` (add Graphiti MCP server), `backend/tests/test_graphiti.py` (NEW) |

**Acceptance criteria**:
1. Install Graphiti MCP server (https://github.com/getzep/graphiti).
2. Ingest: ticket history (status transitions over time), bot activity (which bot picked up which ticket when), Gerrit review timeline.
3. Temporal queries available to runner via MCP tool: "who fixed similar issue last 30 days", "which bot is most successful with this kind of ticket".
4. Steerable user summaries — operator can configure per-summary weights.

**Error catalog**: `graphiti_mcp_unavailable`, `temporal_query_no_match`.

**Test plan**: 5 cases — ingestion, temporal query, MCP unavailable degrade, ingestion of old data, summary steering.

**DoD**: tests green + 3 sample temporal queries answered by runner.

### 3.6 C5 — Capability Matrix / heterogeneous routing (formerly Sprint A A7)

| Field | Value |
|---|---|
| **Phase** | 3 (optimization) |
| **Tier** | M |
| **Class** | subscription-codex |
| **Days** | 3 |
| **LOC budget** | ~500 |
| **BlockedBy** | C2 (need failure class data to inform routing) |
| **Blocks** | none |
| **Files touched** | `backend/agents/capability_matrix.py` (NEW), `config/capability_matrix.yaml` (NEW), `backend/agents/model_router.py` (NEW), `backend/tests/test_capability_matrix.py` (NEW) |

**Acceptance criteria**:
1. `config/capability_matrix.yaml`:
   ```yaml
   models:
     claude-haiku-4-5:
       safe_for: {ast_depth_max: 3, files_max: 1, loc_max: 50}
       cost_per_1k_input: 0.001
     claude-sonnet-4-6:
       safe_for: {ast_depth_max: 5, files_max: 5, loc_max: 500}
       cost_per_1k_input: 0.003
     claude-opus-4-7:
       safe_for: {ast_depth_max: 10, files_max: 20, loc_max: 5000}
       cost_per_1k_input: 0.015
   ```
2. Pre-pickup classifier: compute ticket's AST features (using B8 repo-map / Cognee). Match to smallest model whose `safe_for` envelope covers it.
3. Track per-model success rate per ticket-feature. Update matrix monthly based on data.
4. Local-LLM cell (qwen3.6 / RTX 4080): **explicitly empty** until OP-769 unblock.
5. **Cost reduction target**: 40-50% on a balanced backlog (per industry data).

**Error catalog**: `feature_extraction_fail`, `no_model_covers_ticket` (escalate to operator).

**Test plan**: 6 cases — Haiku route (small ticket), Sonnet route (medium), Opus route (large), no-model-covers (escalate), feature extraction fail, monthly matrix update.

**DoD**: tests green + 1-week run shows ≥30% cost reduction.

### 3.7 C6 — Tier-Aware Memory Governance Wrapper (proprietary, deferred)

| Field | Value |
|---|---|
| **Phase** | 4 (research) |
| **Tier** | L |
| **Class** | subscription-claude |
| **Days** | 4 |
| **LOC budget** | ~700 |
| **BlockedBy** | C1, C2, C3 (need memory infrastructure) |
| **Blocks** | none |

**Acceptance criteria** (high-level — file detailed spec when promoting):
1. Wrap C1 + C3 with ADR-0005 Tier S/M/L/X classification per memory item.
2. Multi-bot voting: tier:M items need 2-of-3 bot consensus to write; tier:L need majority + 1 human ack; tier:X immutable except by operator.
3. Audit trail per memory write: who wrote, who voted, conflict log.

**Notes**: this is the angle B candidate from 2026-05-10 discussion — proprietary, no production analog. Deferred until Sprint B + Sprint C Phase 1+2 data shows tier-conflict scenarios actually occur. If they don't, this is over-engineering.

**Promotion trigger**: ≥3 tier-conflict incidents in C2's audit log over 4 weeks.

### 3.8 C7 — Anthropic Dreaming integration (research preview spike)

| Field | Value |
|---|---|
| **Phase** | 4 (research) |
| **Tier** | L |
| **Class** | subscription-claude |
| **Days** | 3 |
| **LOC budget** | ~400 |
| **BlockedBy** | C1 |
| **Blocks** | none |

**Acceptance criteria**:
1. Enable beta header `dreaming-2026-04-21`.
2. After every ticket completes (Phase = `completed`), trigger Dreaming background process on session trace.
3. Dreaming-extracted patterns written to memory via C1 Memory Tool.
4. Compare lift: ticket pickup with Dreaming-curated memory vs C1-only on next 5 tickets. Target: ≥20% reduction in iteration count (vs Harvey's reported 6× on different domain — adjust expectations).
5. **Risk gate**: Dreaming is research preview (4 weeks old). If breaking API change observed, abort spike.

**Notes**: spike, not product. Decision after spike: integrate fully, defer, or skip.

**DoD**: spike report `docs/research/dreaming-spike-2026-XX.md` with measured lift + recommendation.

### 3.9 C8 — Failure-Graph (proprietary, observe-only)

| Field | Value |
|---|---|
| **Phase** | 4 (research) |
| **Tier** | M |
| **Class** | subscription-codex |
| **Days** | 7 |
| **LOC budget** | ~800 |
| **BlockedBy** | C2 (Failure-class index) — need 6+ months of data |
| **Blocks** | none |

**Acceptance criteria** (high-level):
1. Build "failure graph" — nodes = F# incidents + Patterns + bug classes. Edges = causal links ("F6 unhandled → F7 cascade").
2. Use C2's failure-class data + Pattern catalogue + Gerrit conflict history.
3. Query: "ticket touches F6 + F12 area; predicted outcome based on graph?" → return likelihood + recovery procedure.

**Notes**: angle C from 2026-05-10 discussion. Strictly defer until C2 has 6 months of data. Mentioned for completeness; not actively planned.

### 3.10 C9 — Mastra Observational Memory bake-off (alternative to C1)

| Field | Value |
|---|---|
| **Phase** | 4 (research alternative) |
| **Tier** | M |
| **Class** | subscription-codex |
| **Days** | 2 |
| **LOC budget** | ~300 |
| **BlockedBy** | C1 (need C1 baseline) |
| **Blocks** | none |

**Acceptance criteria**:
1. Pilot Mastra OM as alternative to Anthropic Memory Tool.
2. Bake-off: same 5 tickets through Memory Tool (C1) vs Mastra OM. Measure: retrieval relevance, cost per query, integration friction.
3. Recommendation memo: keep C1, switch to Mastra, or hybrid.

**Notes**: Mastra is TS-native; Python integration via thin REST wrapper. If integration friction too high, skip.

### 3.11 Sprint C summary table

| Phase | Children | Days | Class split | Tier:X count |
|---|---|---:|---|---:|
| 1 (memory layer) | C1, C3, C4 | 8.5 | 2 claude + 1 codex | 0 |
| 2 (proprietary diff) | C2 | 5 | 1 claude | 0 |
| 3 (optimization) | C5 | 3 | 1 codex | 0 |
| 4 (research) | C6, C7, C8, C9 | 16 | 2 claude + 2 codex | 0 |
| **Total** | **9 children** | **~33 days** (likely partial in scope) | **5 claude + 4 codex** | **0** |

**Realistic Sprint C scope** = Phase 1 + Phase 2 = ~13.5 days. Phase 3+4 file later based on Phase 1+2 data.

---

## 4. Cross-sprint dependency graph

```
Sprint A (filed)
  ├─ A9 (worktree) ── PHASE 0 PREREQ for B0
  ├─ A15 (daemon) ── PHASE 0 PREREQ for B0+B1
  ├─ A14 (failure pattern detector) ── DATA SOURCE for C2
  └─ A1, A2 ── REPLACED by B1 (mark Won't Do after B1 pilot)

Sprint B (specs ready, awaiting filing)
  ├─ Phase 0: B0 ── BLOCKS Phase 1
  ├─ Phase 1: B1, B2, B3 ── BLOCKS Phase 2
  ├─ Phase 2: B4, B5, B6 ── BLOCKS Phase 3
  ├─ Phase 3: B7, B8, B9, B13 ── BLOCKS Phase 4 + Sprint C entry
  └─ Phase 4: B10, B11, B12 ── REPLACED by Sprint C (B10→C2; B8 partial→C3)

Sprint C (specs ready, blocked by Sprint B Phase 3)
  ├─ Phase 1: C1, C3, C4 ── BLOCKS Phase 2
  ├─ Phase 2: C2 ⭐ proprietary differentiation
  ├─ Phase 3: C5 (optimization, low priority)
  └─ Phase 4: C6, C7, C8, C9 (research; file conditional on data)
```

---

## 5. Risk register (per-sprint)

| Sprint | Risk | Probability | Impact | Mitigation |
|---|---|---|---|---|
| A | A1 already Under Review; if B1 pilot fails, A1 may need to ship anyway | Med | Med | Hold A1 in Under Review until B1 verified; if B1 fails, transition A1 to merge |
| A | A11 deferred but already filed | Low | Low | Operator amend to Won't-Do or close |
| B | B0 regression test infrastructure too brittle | Med | High | Phase 0 isolation; B0 failure delays Phase 1 not invalidates |
| B | B1 PTC sandbox isolation fails (writes outside worktree) | Low | High | Test 1.7 explicit assertion; sandbox refuse on env var unset |
| B | B7 locator handoff format brittle | High | Med | 1-2 iteration buffer in design; freeze format only after pilot |
| B | B9 progress.txt corruption common | Med | Med | flock + corruption detector + fresh-session fallback |
| B | B13 cross-ticket peer detection misses peers | Med | High | Relies on Gerrit query freshness; add health check |
| C | C2 failure-class enum incomplete | High | Med | Iterate on real failure data; start with 15 classes, grow to 30+ |
| C | C3 Cognee Neo4j ops complexity | Med | Med | Docker-compose; fallback to file-based on outage |
| C | C7 Dreaming API breaking change mid-spike | High (research preview) | Low | Spike contains risk; abort if breaking change observed |

---

## 6. Filing strategy (timeline)

**Today (2026-05-11)**:
- File Sprint B META + B0 (Phase 0 only)
- File Sprint C META (placeholder, no children yet)

**B0 passes (~2 days from now)**:
- File Sprint B Phase 1 children: B1, B2, B3

**Phase 1 re-pilot ≥2/3 (~1 week)**:
- File Sprint B Phase 2 children: B4, B5, B6

**Phase 2 lands (~1.5 weeks)**:
- File Sprint B Phase 3 children: B7, B8, B9, B13

**Phase 3 lands (~3 weeks)**:
- File Sprint B Phase 4 children: B10, B11, B12 (quick wins)
- File Sprint C Phase 1 children: C1, C3, C4

**Sprint C Phase 1 lands (~5 weeks)**:
- File Sprint C Phase 2 child: C2 (proprietary)

**C2 pilot (~7-8 weeks)**:
- Decide on C5/C6/C7/C8/C9 file based on data

---

## 7. Charter — consolidated G1–G4 (PS3 §13 + PS4 additions)

### G1 — Filing-time per-child spec lint (UNCHANGED from PS3)

Every Sprint B/C ticket description must contain 4 sections:
- `## Acceptance criteria`
- `## Error catalog`
- `## State transitions`
- `## Recovery / rollback`

Plus per-PS4 (B0 spec confirmed):
- `## Files touched` (explicit list)
- `## Test plan` (specific cases enumerated)
- `## DoD` (definition of done)

CI lint extension `scripts/jira_ticket_lint.py` (NEW) validates these sections at filing-time. Missing → reject.

### G2 — Cross-cutting mitigations landed (PS3 §13 G2 unchanged)

8 items C1-C8 in companion doc must be in code/runbook before Phase 1 children file. Status as of 2026-05-11:
- C1 (process crash): not started → land as part of B0/B1
- C2 (concurrent runners orphan reaper): not started → file as Sprint A operator-action
- C3 (TOCTOU): partially documented, enforce via I6 invariant in B1 code review
- C4 (Gerrit-first JIRA-second ordering): partially in `_handle_gerrit_push_failure`, codify in B7
- C5 (semantic drift refresh): not started → land as part of B3 (every 10 iter, view most-edited file)
- C6 (CostGuard race): already in current launcher
- C7 (sandbox boundary): codified in B1 spec AC#2
- C8 (API drift watch cron): not started → file as Sprint A operator-action

### G3 — Past-failure regression tests (B0)

5 test classes per B0 spec §2.2. PASS = green in CI. Phase 1 cannot file until B0 green.

### G4 — Kill switch matrix (PS3 §13 G4 unchanged)

| Trigger | Action | Recovery seed |
|---|---|---|
| Phase 1 re-pilot < 2/3 success | Abort Sprint B | Sprint C "B1 didn't lift baseline" hypothesis |
| Critic dissent rate > 50% on first 5 tickets (B4) | Pause Phase 2; review B4 model | Switch B4 to Sonnet |
| 3+ consecutive `loop_aborted_terminal` (B3) | Pause; B3 detector mistuned | Tune args_hash |
| CostGuard hits global cap before Phase 3 entry | Pause + escalate | Operator scope reduction |
| 2+ orphan tickets in 24h (C2) | Pause + investigate | Fix mutex_with / reaper |
| Anthropic API breaking change (C8) | Pause Sprint B | Operator pin model version |
| Sprint C C2 failure-class auto-deprecate flags >5 lessons in week 1 | Pause C2 replay; investigate weighting math | Operator review weights |

### Charter sign-off

This consolidated plan supersedes PS3 §13. Operator + AI fleet sign here:

- [ ] Operator reviewed master plan §1–§7 (Sprint A confirmation, Sprint B+C specs, charter G1-G4)
- [ ] AI fleet reviewed companion doc (`docs/architecture/sdk-runner-sprint-b-error-handling.md`)
- [ ] Operator approves filing strategy §6 (staged by phase)
- [ ] Operator approves runner deployment: subscription-claude + subscription-codex on respective class JQL with Phase 0/1 pickup gates honoured

Sign date: TBD after 2026-05-11 review.

---

## 8. Notes for the runner pickup

When subscription-claude and subscription-codex runners come online to execute these tickets:

- Pickup JQL respects `tier:X` exclusion (correct — A9, A10, A11, A15 + B0/B9 if upgraded require human-touch).
- Pickup respects `blockedBy` (correct — Phase 1-4 children blocked by predecessor phase).
- Each ticket description references back to this master plan §X.Y for full spec.
- Runner MUST read the §X.Y referenced section before starting work; partial information will cause drift.
- If the spec is ambiguous, runner files clarification-needed comment + waits for operator (NOT autonomous interpretation).

---

## 9. Cross-references

- Initial audit (Aider/SWE-agent): `docs/audit/2026-05-09-aider-swe-agent-audit.md` (Gerrit #332)
- Sprint B planning PS3: `docs/audit/2026-05-09-sprint-b-runner-reliability-plan.md` (Gerrit #333)
- Sprint B engineering deep-dive: `docs/architecture/sdk-runner-sprint-b-error-handling.md` (Gerrit #333 PS3)
- Lessons archive: `docs/sop/lessons-learned.md` L1-L27
- ADR-0005 Tier S/M/L/X authority: `docs/adr/0005-*` (referenced)
- ADR-0007 Multi-Provider Subscription Orchestrator: referenced in tier policy
