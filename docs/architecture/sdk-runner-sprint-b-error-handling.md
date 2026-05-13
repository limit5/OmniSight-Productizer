# Sprint B — Error Handling, State Machines, Exit Mechanisms

**Status**: Implementation reference / appendix for ADR-0035 — non-binding details for §4 + §5 + §6
**Date**: 2026-05-09 (initial), 2026-05-10 (PS3 expansion per operator request to "挖深一點 + 把過去失敗案例考慮進去")
**Audit method**: 25 historical failure incidents mined from `docs/sop/lessons-learned.md` (L1-L27) + governance memory + recent Gerrit conflict series + the 3 S1 launcher pilot failures. Each incident classified by causal layer + mapped to Sprint B child where it could recur. Then 7-dimension coverage check (errors / exceptions / exit / FSM / propagation / recovery / idempotency) per child, plus 8 cross-cutting concerns.

---

## 1. Scope & methodology

### Why this document exists

The PS2 plan answered *what to build* and *why*. It did not answer:

- What happens when each child crashes mid-execution?
- What exact errors produce what exact exit paths?
- Which states survive a process restart and which evaporate?
- How does failure in child X propagate (or not) to children Y and Z?

The honest coverage estimate at the start of this audit was **~14%** (PS2 §11 self-assessment). This document raises it to **≥85%** by enumerating gaps explicitly. Remaining gaps are listed in §8 as "known unknowns" with watch flags.

### Methodology

Three passes:

1. **Failure mining**: read `lessons-learned.md` L1–L27, the 3 S1 pilot transcripts, and the Gerrit conflict series (#233, #234, #240, #241, #245/246, #266, #297, #307, #323, #324). Catalog each failure with: layer (process / network / state / model / external), root cause, what would have prevented it, and which Sprint B child surfaces a similar risk.
2. **Per-child deep-dive**: for each Sprint B child (B1–B13 except B12 trivial), build the internal FSM, error enum, exception classification, exit path enumeration, recovery semantics, and idempotency contract.
3. **Cross-cutting sweep**: identify failure modes that don't belong to any single child but emerge from interaction (process crash, concurrent runners, TOCTOU, external cascade, semantic drift, CostGuard race, sandbox boundary, API drift).

### Reading order

§2 (top-level FSM) → §3 (past-failure library, mapped to children) → §4–5 (per-child catalogs) → §6 (cross-cutting) → §7 (recovery primitives) → §8 (charter gates).

---

## 2. Top-level runner FSM

[Authoritative version in ADR-0035 §Decision + §Invariants I1-I6 — see there for binding form.]

This is the *aggregate* state machine across all Sprint B children. Each child plugs into specific transitions; child-internal FSMs are in §4.

```
                              ┌───────────────────┐
                              │   idle            │
                              └────────┬──────────┘
                                       │ pickup-trigger
                                       ▼
                              ┌───────────────────┐
                              │   picking_up      │
                              └────────┬──────────┘
                                       │ ticket claimed
                                       ▼
                  ┌────────────────────────────────────┐
                  │   restoring_session     (B9)       │  ← reads progress.txt; if absent → fresh
                  └─────────────────┬──────────────────┘
                                    ▼
                  ┌────────────────────────────────────┐
                  │   injecting_lessons     (B10)      │  ← BM25 over lessons-learned.md
                  └─────────────────┬──────────────────┘
                                    ▼
                  ┌────────────────────────────────────┐
                  │   working                          │ ◄────────────┐
                  │   ┌──────────────────────────────┐ │              │
                  │   │ locating              (B7)   │ │              │
                  │   │   ↓                          │ │              │
                  │   │ blast_radius_check    (B13)  │ │ ─── oversize │
                  │   │   ↓                          │ │              │ context
                  │   │ tdd_phase             (B5)   │ │              │  reset
                  │   │   ↓                          │ │              │  (B3)
                  │   │ static_analysis       (B2)   │ │              │
                  │   │   ↓                          │ │              │
                  │   │ coding                       │ │ ─── 3x-loop ─┘
                  │   │   ↓ ↑ reflection (B12)       │ │
                  │   │ critic_review         (B4)   │ │ ── dissent ──► retry coding
                  │   │   ↓                          │ │
                  │   │ committing                   │ │
                  │   └──────────────────────────────┘ │
                  └─────────────────┬──────────────────┘
                                    ▼
                              ┌──────────────┐
                              │ submitting   │ ─── push race ──► classifier ──► retry / abort
                              └──────┬───────┘
                                     ▼
                       ┌───────┬─────────┬──────────┐
                       ▼       ▼         ▼          ▼
                   completed  failed  aborted  partially_done
                                      │
                  ┌───────────────────┴──────────────────────┐
                  │ aborted decomposes:                       │
                  │  loop_aborted          (B3)               │
                  │  oversized_aborted     (B13)              │
                  │  critic_blocked        (B4 2nd dissent)   │
                  │  tdd_unwriteable       (B5)               │
                  │  lint_cap_exceeded     (B2)               │
                  │  budget_exhausted      (CostGuard)        │
                  │  network_aborted       (Anthropic API)    │
                  │  external_aborted      (Gerrit / JIRA)    │
                  │  surrender_structural  (max_iterations)   │
                  └────────────────────────────────────────────┘
```

**Critical invariants** (each must hold across every transition):

| Invariant | Why | Where enforced |
|---|---|---|
| **I1** Worktree is the single writable surface | Avoids main-repo contamination (Lesson 17) | Every child's tool handler must reject writes outside `--worktree-path` |
| **I2** JIRA assignee = bot identity throughout `working`, cleared on `aborted`/`completed` | Avoids orphan In-Progress (Sprint A pre-mortem) | `_release_ticket()` in jira_dispatch must run on every exit path |
| **I3** Gerrit Change-Id is **stable across retries**, **fresh per fresh ticket** | Avoids #334-style duplicate change creation; preserves PS history | commit-msg hook + amend discipline |
| **I4** Conversation history is checkpoint-able at any state boundary | B3 context reset requires it | Runner persists every assistant turn to `progress.txt` (B9) |
| **I5** All side-effecting operations are idempotency-token guarded | Bridge daemon resends, stream-event replays | Per-operation token derived from `(ticket_key, operation, args_hash)` |
| **I6** Live-state injection happens at **state boundary**, not at pickup | Lesson 3 (alembic head drift), Lesson 17 (worktree-vs-main cwd) | Each transition that depends on repo state re-reads at transition time |

---

## 3. Past-failure case library — mapped to Sprint B children

Twenty-five historical incidents. Each row: incident, layer, root cause, **what new Sprint B child would surface a similar risk**, and current mitigation status (🟢 mitigated / 🟡 partial / 🔴 still possible).

| # | Incident | Layer | Root cause | Sprint B child at risk | Mitigation status |
|---|---|---|---|---|---|
| F1 | S1 pilot v1–v3: model loops on `find \| grep` | model behaviour | Custom tool schema not in RLHF distribution | **B1** (built-in tools is the fix); **B3** would have caught the loop | 🟢 by B1+B3 design |
| F2 | W14.5: `max_iterations_exceeded` treated as retryable | exit semantics | No structural-vs-transient distinction | **B3** must encode `NON_RETRYABLE_STOP_REASONS` enum and route differently; surrender vs context-reset | 🟡 partial (codified in `run_s1_via_anthropic_sdk.py` but Sprint B inherits) |
| F3 | OP-771: codex CLI internal-push race → "Missing tree" | external + concurrency | Codex pushed before our handler captured; classifier reverted Under-Review→To-Do | **B1** PTC sandbox could fire the same race (server-side execution opaque to us) | 🟡 — `_handle_gerrit_push_failure` covers Missing-tree but B1 PTC path not yet wired |
| F4 | OP-779 + 7 tickets stuck Under Review with merged PSes | external + state drift | Bridge daemon 233 commits behind develop, missing OP-743 force-walk | **B9** progress.txt cross-session resume **requires bridge fresh** | 🔴 — bridge auto-sync (OP-798) not yet implemented; B9 must verify bridge currency at pickup |
| F5 | OP-32/116 `ticket_unexpected_status_skip` | bridge protocol | Bridge required Approved status, ticket was at Under Review | **B6** feature-list dual-mode must handle bridge-vs-ticket state divergence | 🟡 — dual-mode covers parsing, not state-protocol drift |
| F6 | Pattern 9: sequential identifier append race (`backend/main.py` router list) | concurrency | Two tickets both append router; merge sees add/add | **B7** locator must detect "this ticket has peer in-flight tickets touching same file"; **B13** blast radius is per-ticket, blind to cross-ticket peers | 🔴 — known gap; B13 single-ticket scoped |
| F7 | Pattern 12: spike + final-version add/add scaffold race | concurrency | Spike ticket creates scaffold; final ticket creates fresh version, both add same file path | **B7** locator + **B13** blast radius blind to "this scaffold already exists in another ticket's PS" | 🔴 — known gap (filed OP-799/800 follow-ups) |
| F8 | Gerrit conflict series #233/234/240/241/245/246/266/297/307/323/324 | concurrency + governance | High-frequency multi-bot patchset rate exceeds resolution rate; merger resolves but new conflicts arrive | **B7** coder phase exits via Gerrit push; conflict-handling FSM unspecified | 🟡 — merger-agent (OP-269) handles Gerrit-side, runner-side conflict response in B7 missing |
| F9 | AI Reviewer never appearing on Gerrit | governance | AI Reviewer never wired to events; submit-rules missing applicableIf | **B4** Critic must NOT inadvertently take AI-Reviewer governance role (would violate ADR-0003 separation) | 🟢 — B4 stays runner-internal; never posts Gerrit Code-Review label |
| F10 | sora-bridge 233 commits behind develop | deployment | No auto-sync; manual updates only | **B9** explicitly relies on bridge currency | 🔴 — same as F4 |
| F11 | #297 conflict | concurrency | Prior PS of this ticket existed; pickup didn't rebase | **B7** locator must detect "prior PS exists for this Change-Id" before locate phase | 🟡 — `ensure_change_ids` covers fresh tickets, not prior-PS-rebase |
| F12 | #323/#324 cross-ticket conflicts on `tool_dispatcher.py` | concurrency | Two tickets both edited dispatcher; landed at near-same time | **B13** blast radius blind to peer-ticket co-edits | 🔴 — same gap as F6 |
| F13 | Gerrit 3.13 rejected `rules.pl` (declarative submit-requirements only) | external API drift | Plugin behaviour changed across Gerrit versions | **B4** Critic if it ever scores via SSH — must use declarative path | 🟢 — B4 stays runner-internal, no Gerrit side effect |
| F14 | Bridge `change-merged` Approved-required (post-OP-743) | governance | Force-walk added; some tickets at To-Do bypassed | **B6** feature-list ↔ bridge state interaction unspecified | 🟡 — dual-mode handles parsing, state protocol still implicit |
| F15 | LLM creds in PG `llm_credentials.encrypted_value` (Fernet), not `api_key` | schema drift | Migration 0145 changed schema; old code still queries `api_key` | **B1** PTC sandbox loading creds must use right schema | 🟡 — runner reads from disk file (`~/.config/omnisight/anthropic-api-key`), bypasses DB; if PTC sandbox loads creds itself, repeat the bug |
| F16 | API key extraction via `docker exec backend env` workaround | deploy / sec | Schema mismatch led to OP-693 emergency workaround | **B1** PTC must explicitly use disk-file path, not assume env | 🟡 — discipline; needs codification |
| F17 | Worktree `git config user.email` must be set before commits | env | Lesson 15 — Gerrit rejects "email not registered" | **B1** + **B7** + any agent that commits | 🟢 — `setup_worktree()` handles it; verify Sprint B inherits |
| F18 | Migration auto-area-tag (Lesson 8) | metadata | Tickets without `area:tests` label silently skipped lint/test bp | **B13** component-aware threshold depends on `area:` labels — what if missing? | 🟡 — D9 mentions but doesn't spec fallback policy |
| F19 | Live alembic head injection (Lesson 3) | state drift | Stale TODO.md text vs live alembic head | **B7** locator must inject live-state at locate-time, not pickup-time | 🟡 — pattern documented for codex prompts; B7 must inherit |
| F20 | TODO.md `[x][G]` staging order (Lesson 2) | concurrency | Runner-managed file unstaged at merge time | **B6** feature-list JSON managed by runner — same staging risk | 🔴 — same class of bug; B6 spec must mandate "feature-list JSON staged before commit" |
| F21 | JQL English-vs-JP-locale issuetype name | external API gotcha | Atlassian Cloud localised "Story" → "ストーリー" | **B10** BM25 retrieval over lessons-learned must handle locale variants if multi-lingual | 🟢 — lessons-learned is Markdown, locale-agnostic |
| F22 | Worktree common-dir hooks (Lesson 14) | env | Hooks installed at `--git-dir`, never fired | **B1** sandbox if it installs hooks; **B7** likewise | 🟢 — `_git_common_dir()` helper exists, must be reused |
| F23 | Per-ticket fresh-sync (Lesson 16) | state drift | Local main lagged Gerrit develop | **B7** locator must fresh-sync, not pickup-sync | 🟢 — runbook covers; B7 must inherit |
| F24 | `pre_pickup_ok` worktree-cwd vs main-repo-cwd (Lesson 17) | env | Same vantage-point bug class | **B13** blast radius must run in worktree cwd, not main | 🟡 — easy gotcha; B13 spec must be explicit |
| F25 | Stream-events vs webhook auth (Lesson 24/L24-webhooks variant) | governance | Plugin had no auth surface; daemon never deployed | **B9** cross-session events depend on stream-events being live | 🟢 — bridge daemon runs stream-events; verify health-check at runner pickup |

**Counts**: 🟢 9 / 🟡 11 / 🔴 5. The five 🔴s are the highest-leverage gaps to close before Phase 1 starts — they're all "would silently recur in Sprint B without explicit countermeasure".

---

## 4. Per-child error / exit / FSM catalog (Phase 1)

### B1 — Anthropic built-in tools + Programmatic Tool Calling

#### Internal FSM
```
inactive → init (tool registration) → ready
ready → text_editor_call(view|create|str_replace|insert|undo_edit) → return | error
ready → bash_call(command|restart) → return | error | timeout
ready → ptc_call(allowed_callers) → sandbox_running → return | sandbox_error | sandbox_timeout
```

#### Error enum (catch + classify)
| Error code | Origin | Action | Past failure |
|---|---|---|---|
| `tool_input_invalid` | client-side schema lint | retry with corrected input (1×) | F2 |
| `text_editor_no_match` | str_replace `old_str` not found | return error to model; do NOT retry silently | n/a |
| `text_editor_path_outside_worktree` | I1 invariant violation | abort with `sandbox_boundary_violation` | F17 |
| `bash_metachar_blocked` | legacy custom-Bash compatibility shim during migration | should not occur post-B1; if it does, log + abort | F1 |
| `bash_timeout` | server-side timeout | feed result to model, mark `transient` | n/a |
| `ptc_sandbox_oom` | server resource | abort with `sandbox_oom`; do NOT retry (deterministic failure) | n/a |
| `ptc_sandbox_network` | sandbox lost connection mid-execution | server-state-ambiguous: classify as **non-idempotent failure**, escalate (do not retry blindly) | F3 |
| `ptc_creds_missing` | sandbox can't load API key | abort with `creds_missing`; require operator | F15, F16 |

#### Exception classification
- `anthropic.APIError` (5xx) → exponential backoff, max 3 retries
- `anthropic.RateLimitError` → wait per Retry-After header; do not count as iteration
- `anthropic.BadRequestError` → log + abort (likely tool schema drift)
- `socket.timeout` → retry with shorter request
- Generic `Exception` → log full traceback to JIRA + abort (defence in depth)

#### Exit paths
- ✅ `completed`: model emits `end_turn` after submitted commit
- ❌ `tool_error_unrecoverable`: `text_editor_path_outside_worktree`, `ptc_creds_missing`
- ❌ `sandbox_oom`: PTC sandbox over-resource
- ❌ `network_aborted`: 3 consecutive APIError 5xx
- ❌ `surrender_structural`: `max_iterations` or `max_tokens` per F2

#### Recovery point
- **Per-tool-call**: `text_editor`'s `undo_edit` (built-in) + worktree git status snapshot
- **Per-state-boundary** (transition between `coding` and `critic_review`): explicit worktree commit checkpoint

#### Idempotency contract
- `text_editor` writes are NOT idempotent (str_replace fails second time because `old_str` no longer matches). Runner must dedupe via `(ticket_key, target_file, operation_id)` token.
- `bash` calls with same command + same env can be re-invoked; sandbox state is server-managed.
- PTC sandbox is **stateful per session**; a re-invocation in a fresh session is a fresh execution.

#### Crash semantics
- Process crash mid-`text_editor` call: file write may be partial. **Mitigation**: text_editor writes are buffered server-side; if API returns successfully, write is durable. If API call fails mid-flight, file is unchanged. Worktree is single source of truth.
- Process crash mid-PTC sandbox: sandbox may complete but result never returned. **Mitigation**: use idempotency token; on resume, query sandbox by token.

---

### B2 — Static-analysis pre-flight gate

#### Internal FSM
```
inactive → triggered_post_edit → running_linters → analyzing_output → 
   {clean → pass} | 
   {dirty + cap_not_reached → feed_to_model → re-edit → loop back} |
   {dirty + cap_reached → escalate (lint_partial)}
```

#### Error enum
| Error code | Origin | Action | Past failure |
|---|---|---|---|
| `linter_binary_missing` | env | abort sprint child install; require ops | n/a |
| `linter_config_conflict` | tool config | log warning; use defaults | n/a |
| `lint_cap_exceeded` | 3-round hard cap (Aider issue #1090 mitigation) | mark commit `lint_partial`; do NOT block merge for cosmetic | n/a |
| `linter_crash` | tool-side bug | log; treat as `linter_unavailable`; pass without check (degrade gracefully) | n/a |
| `lint_target_outside_worktree` | I1 violation | refuse to lint; flag bug | F17 |

#### Exit paths
- ✅ `lint_clean` (0 errors)
- ✅ `lint_partial` (cap reached; non-blocking)
- ❌ `linter_binary_missing` (sprint-config bug; abort)

#### Idempotency
- Linters are pure functions of file content; safe to re-run unconditionally.
- The 3-round cap counter must persist across restarts (write to progress.txt §B9).

#### Crash semantics
- Crash mid-lint: counter loss → potential infinite loop on resume. **Mitigation**: persist counter to `progress.txt` after each round.

---

### B3 — 3x-loop hard reset + context reset + ToM scratchpad

This is the most subtle child; it is itself a state machine.

#### Internal FSM
```
                       ┌─────────────┐
                       │   normal    │
                       └──────┬──────┘
                              │ tool_call_recorded
                              ▼
                  ┌──────────────────────────┐
                  │   detection_window       │
                  │   (sliding window of 3)  │
                  └────┬───────┬─────────────┘
                       │       │ no match
                       │       └─────────────────► normal
                       │ args_hash matches 2x
                       ▼
                  ┌──────────────────────────┐
                  │   warning                │  ← scratchpad note injected
                  │   (next call is 3rd)     │
                  └────────────┬─────────────┘
                               │ args_hash matches 3rd
                               ▼
                  ┌──────────────────────────┐
                  │   triggered              │
                  └────────────┬─────────────┘
                               ▼
                  ┌──────────────────────────┐
                  │   resetting              │  ← in-flight tool call awaited (not killed)
                  │                          │     (kill creates ambiguous server state per F3)
                  └────────────┬─────────────┘
                               ▼
                  ┌──────────────────────────┐
                  │   restarted              │  ← fresh conversation:
                  │                          │     keep system prompt + first user message
                  │                          │     drop all assistant turns
                  │                          │     inject failure-summary paragraph
                  └────────────┬─────────────┘
                               │ reset_count++
                               ▼
                       ┌─────────────┐
                       │   normal    │  ← but reset_count tracked
                       └─────────────┘

                  ┌──────────────────────────┐
                  │   abort_after_N_resets   │  ← N=3 (so 9 raw attempts max)
                  └──────────────────────────┘
```

#### Error enum
| Error code | Origin | Action | Past failure |
|---|---|---|---|
| `args_hash_collision` | hash function returns same hash for genuinely different args | rare; mitigated by including full arg JSON in hash | n/a |
| `false_positive_detection` | legitimate progressive narrowing tripped detector | hash includes "input changed" delta; 1-token-difference exempts | (D3 in plan) |
| `reset_during_inflight_call` | reset fires mid-tool-call | **policy: await, do not kill** (avoids F3 sandbox-state-ambiguous) | F3 |
| `reset_count_exceeded` | N=3 resets used; still looping | hard abort `loop_aborted_terminal` | n/a |
| `scratchpad_corrupt` | model writes invalid JSON | reject scratchpad turn; do not count as progress | n/a |

#### Exit paths
- ✅ `loop_resolved`: detector returns to `normal` after reset
- ❌ `loop_aborted_terminal`: 3 resets exceeded
- ❌ `reset_disrupted_inflight`: in-flight tool call returned error post-reset; ambiguous state — escalate

#### Idempotency
- The detector's state is **per-ticket**; reset on ticket pickup. Surviving across crashes requires `progress.txt` integration.
- Scratchpad entries are append-only; never deleted, never edited.

#### Crash semantics
- Crash during `resetting`: on resume, runner observes incomplete reset. Resolution: re-issue reset (idempotent); failed-reset attempt counts as 1 reset toward N=3 cap.

#### Critical decisions encoded
- **Q1: Reset upper bound (the gap I flagged earlier)**: N=3 resets. After that, abort. Not configurable per-ticket; this is a sprint-charter constant.
- **Q2: What survives a reset?** System prompt, first user message (containing AC + ticket text). Everything else dropped. Reset injects 1 paragraph: `"PRIOR ATTEMPT (now reset): you tried <tool_call_signature> 3 times with error <error_class>. Try a different approach."`
- **Q3: ToM scratchpad cross-reset**: kept. The scratchpad is a separate JSON artifact; reset clears conversation but keeps scratchpad. This makes scratchpad the **only continuous trace** across resets.
- **Q4: CostGuard tally cross-reset**: same ticket, same budget. Reset does NOT reset the spending counter (would defeat the budget cap).

---

## 5. Per-child catalogs (Phase 2 + 3 + 4)

For brevity, Phase 2 + 3 + 4 children are summarised at FSM + key-error level. Full schemas land in each ticket's filing-time spec (per §8 charter).

### B4 — Pre-commit Critic (Haiku critic, Sonnet coder)

**FSM**:
```
post_coder_commit → critic_review → 
   {pass → commit_finalized} |
   {dissent_1st → coder_retry_with_critic_reasons → critic_review (loop, max 1 retry)} |
   {dissent_2nd → escalate (critic_blocked, ticket-state: under_review:critic_dissent)}
```

**Error enum**:
- `critic_timeout` (30s soft cap) → treat as **pass** (do not block on critic infrastructure failure)
- `critic_oom` → same as timeout
- `critic_malformed_reason_code` → reject critic turn; downgrade critic verdict to `pass` with audit log
- `critic_tool_failure` (e.g., critic ran pytest, pytest hung) → critic verdict = `unable_to_review`; treat as pass (defence in depth)
- `dissent_loop_unbreakable` → 2nd dissent without convergence → escalate to operator

**Idempotency**: critic is read-only. Re-runnable. **CRITICAL governance constraint**: critic NEVER posts to Gerrit Code-Review label (avoids F9 ADR-0003 violation). Verdict stays in JIRA + commit message metadata.

**Past-failure relevance**: F9 (AI Reviewer governance) — critic is *internal pre-commit gate*, not a Gerrit reviewer. F13 (Gerrit declarative-only) — irrelevant since no Gerrit side effects.

### B5 — TDD enforcement with applicability gate

**FSM**:
```
pickup → tdd_applicable_check 
   {tdd_applicable=no → bypass, normal coding flow} |
   {tdd_applicable=yes → tdd_phase_active} |
   {tdd_applicable=conditional → locator_decides → tdd_phase_active OR bypass}

tdd_phase_active → write_test → 
   {test_runs_red → coder_unlocked → write_code → run_test_green → pass} |
   {test_runs_green_immediately → tdd_violated (test didn't actually test the bug)} |
   {test_doesnt_run → tdd_violated (test framework error)}
```

**Error enum**:
- `tdd_violated_premature_green`: written test passes before any code change → enforcement blocks coder from edit phase; require model to revise test
- `tdd_violated_unrunnable`: test file syntax error / fixture error → escalate
- `tdd_unwriteable`: model fails to write meaningful test in 3 attempts → mark `tdd_blocked`, escalate
- `tdd_conditional_indeterminate`: locator says "can't tell if TDD applies" → fall back to bypass; log

**Idempotency**: test file write is non-idempotent (str_replace via B1's text_editor). Use idempotency token per `(ticket_key, "tdd_test_v<N>")`.

**Past-failure relevance**: F2 (max_iterations) — TDD attempts count toward iterations; reset count after successful red→green. F18 (area: missing) — `tdd_applicable` field may be missing on legacy tickets; default = `conditional` (defensive).

### B6 — Submit-review checklist (feature-list JSON, dual-mode)

**FSM**:
```
pre_submit → ac_format_detect → 
   {feature_list_json → checklist_inject → model_answers_each → submit_unlocked} |
   {legacy_freeform → up_convert_or_pass} |
   {malformed → escalate format_unparseable}

checklist_inject → model_must_respond_each_item →
   {all_passed → submit} |
   {any_failed → coder_returns_to_working (re-edit; up to 3 cycles)} |
   {ambiguous_response → reject; require concrete pass/fail per item}
```

**Error enum**:
- `format_unparseable`: feature-list JSON not valid → fall back to legacy mode; log
- `legacy_up_convert_fail`: auto-conversion produces empty checklist → block submit; require operator
- `checklist_ambiguous_response`: model says "yes, mostly" → reject as non-binary
- `checklist_3_cycle_fail`: 3 attempts at re-edit + checklist still fails → escalate

**Idempotency**: checklist is a read-only artifact computed from AC. Idempotent re-injection. Submit gate is one-shot per ticket; re-attempts use new idempotency token.

**Past-failure relevance**: F5 (`ticket_unexpected_status_skip`) — checklist must include "bridge state correct" as item if any AC depends on bridge transitions. F20 (TODO.md staging) — feature-list JSON if persisted to disk **MUST be staged before commit**, same class of bug.

### B7 — Locator (Haiku) → Coder (Sonnet) sequential pipeline

**FSM**:
```
working_start → locator_invoke (Haiku) → 
   {locator_returns_candidates → blast_radius_check (B13)} |
   {locator_zero_candidates → fallback_monolithic (full-repo Sonnet)} |
   {locator_too_many_candidates >50 → reject_for_refinement} |
   {locator_timeout → fallback_monolithic} |
   {locator_malformed → reject; retry locator (max 1)}

blast_radius_check → 
   {within_threshold → coder_invoke (Sonnet, with locator output)} |
   {oversize → main_runner_decides (split / refuse / escalate)}

coder_invoke → coding loop → critic_review (B4) → 
   {pass → submit} |
   {dissent → coder_retry (max 1)} |
   {dissent_2nd → escalate}
```

**Error enum**:
- `locator_zero_candidates`: locator finds nothing relevant → fallback to monolithic agent (full-repo coder); log warning
- `locator_too_many`: >50 files → ticket scope unclear; escalate `needs:refinement`
- `locator_timeout`: Haiku 60s timeout → fallback monolithic
- `locator_malformed_handoff`: JSON schema mismatch → retry locator with strict-format reminder (1×)
- `locator_handoff_pollution`: locator output >2k tokens → enforce schema with `summary` field; reject verbose
- `coder_rejects_locator_input`: coder says "insufficient context" → expand candidate set + retry
- `prior_ps_exists_for_change_id` (F11): on pickup, detect prior PS; rebase before locate

**Idempotency**: locator is read-only; safe to re-run. Coder writes via B1 text_editor; idempotency per write op.

**Past-failure relevance**:
- F6, F7, F12 (cross-ticket peer conflicts): locator must check JIRA for `mutex_with` peers before locate; if peer ticket has in-flight PS touching same files → escalate `peer_conflict`
- F8 (Gerrit conflict series): coder phase exits via Gerrit push; on conflict, classify (rebase / abandon / escalate) per `_handle_gerrit_push_failure`
- F11 (#297 prior PS): pre-locate check `gerrit query change:<change-id>` to detect prior PS
- F19 (live state): locator must inject live alembic head + live commit SHA at locate-time

### B8 — Repo-map preamble (tree-sitter PageRank)

**FSM**:
```
pickup → repo_map_check (cache valid?) → 
   {cache_valid → inject preamble} |
   {cache_stale → rebuild (tree-sitter parse all *.py/*.tsx) → inject} |
   {tree_sitter_unavailable → degrade to no-preamble; log}
```

**Error enum**:
- `tree_sitter_binary_missing` → degrade gracefully (no preamble)
- `tree_sitter_grammar_missing_lang` → skip files of that language; log per-language gap
- `parse_error_per_file` → skip individual file; do not abort
- `repo_map_oom` (huge repo) → fall back to top-100 most-recently-modified files
- `cache_corruption` → rebuild from scratch

**Idempotency**: repo-map build is pure function of repo HEAD SHA; cache key = HEAD SHA. Safe.

**Past-failure relevance**: F19 (live state) — cache invalidation must happen on every commit, not on timer.

### B9 — Anthropic harness session-resume (`progress.txt` + 3-step opener)

**FSM**:
```
pickup → 3_step_opener → 
   step_1: verify_cwd (must be worktree path; per F24)
   step_2: read_progress_txt (if exists)
   step_3: smoke_test (lightweight assertion: imports work, tests collect)
   
progress_txt_present → restore_state → resume working
progress_txt_absent → fresh_session
progress_txt_corrupt → log; treat as absent (defensive)
```

**Error enum**:
- `progress_txt_corrupt`: invalid JSON / partial write → treat as absent
- `progress_txt_owner_mismatch`: progress.txt written by different bot identity → escalate (concurrent runner conflict)
- `cwd_not_worktree` (F17, F24): refuse to start; log
- `smoke_test_fail`: imports broken on resume (worktree bitrot) → fresh-sync per F23
- `bridge_currency_check_fail` (F4, F10): bridge daemon >1 hour behind develop → warn but proceed (operator should fix; not a runner-blocking issue)
- `stream_events_dead` (F25): no events received for 5+ minutes → warn; proceed; flag for operator

**Idempotency**: progress.txt is single-writer per ticket. Owner = bot identity. Concurrent reads OK; concurrent writes prohibited (file-locking via `flock`).

**Past-failure relevance**:
- F4, F10 (bridge currency): runner pickup MUST verify bridge is current. New invariant.
- F25 (stream-events dead): same.
- F17, F24 (cwd vantage): step_1 enforces.

### B10 — Lesson auto-injection (BM25 over lessons-learned.md)

**FSM**:
```
post_pickup → bm25_search (top_k=3, query=ticket title + AC) → 
   {results_returned → inject as system message} |
   {empty → no-op; log} |
   {bm25_index_stale → rebuild from disk (cheap)} |
   {bm25_unavailable → degrade gracefully (no injection)}
```

**Error enum**:
- `bm25_index_stale` → rebuild from current `lessons-learned.md`
- `lessons_file_unreadable` → degrade silently
- `irrelevant_top_k` → no automatic detection; rely on operator review post-hoc

**Idempotency**: read-only.

**Past-failure relevance**: F21 (locale variants in JQL) — irrelevant since lessons-learned is Markdown English/Chinese mix; BM25 handles both as bag-of-words.

### B11 — Cache control on last 2 messages

**FSM**: trivial. Add `cache_control: {"type": "ephemeral"}` to last 2 messages on every API call. No state.

**Error enum**: `cache_breakpoint_rejected_by_api` (Anthropic spec change) → silent fallback to no-cache.

**Past-failure relevance**: none specific; generic API drift class.

### B12 — Reflection loop on test/lint failure

**FSM**: subset of B3's FSM (reuses 3x-loop infrastructure). Specific to test/lint failures: structured failure object → bounded reflection cycle (max 3) → escalate.

**Error enum**: covered by B3.

### B13 — AST blast-radius gate

**FSM**:
```
locator_returns → blast_radius_compute → 
   {within_threshold → pass to coder} |
   {oversize → main_runner_decides:
       option_a: auto_split (if ticket has clear seams)
       option_b: refuse + label 'needs:split' + return To Do
       option_c: escalate to operator (default if no seams found)}
```

**Error enum**:
- `tree_sitter_parse_fail`: file unparseable → exclude from LOC count; log; **conservative bias: count file as 200 LOC** (exclude underestimates)
- `threshold_yaml_malformed`: fall back to hardcoded `(3 files, 300 LOC)` defaults
- `multi_area_threshold_collision` (D9 follow-up): ticket has `area:tests` + `area:integration` → take **max** of thresholds (most permissive); document in ticket comment
- `cross_ticket_peer_collision` (F6, F7, F12): locator candidates overlap with peer ticket's in-flight files → escalate `peer_conflict`; not B13's job to resolve, but must detect

**Idempotency**: read-only. Cache invalidation per repo HEAD SHA.

**Past-failure relevance** (the cross-ticket gap is the big one):
- F6, F7, F12 (peer conflicts): B13 alone can't see peers. **New requirement**: B13 queries Gerrit `gerrit query status:open path:<file>` for each candidate file; flag if peer PS exists.
- F18 (area missing): default fallback = `(3 files, 300 LOC)` strict; force operator to add area label.
- F24 (cwd vantage): blast radius computed in worktree cwd, not main.

---

## 6. Cross-cutting concerns

Eight concerns that don't belong to any single child but emerge from interaction.

### C1 — Process crash mid-execution

**Failure modes**:
- Runner Python killed (OOM, SIGKILL, host reboot)
- Network died mid-API-call (TCP reset, DNS flap)
- Anthropic API 5xx mid-stream
- JIRA 5xx
- Gerrit SSH connection drop

**Coverage**:
- ✅ Per-ticket fresh-sync on resume (Lesson 16)
- ✅ Worktree is durable; reads survive crash
- 🟡 In-flight `text_editor` partial write — server-side atomic per call but multi-call sequence may be partial (e.g., `create` then `str_replace` interleaved with crash)
- 🔴 PTC sandbox state on resume (F3): server may have completed execution but result never returned

**New requirement**: per-state-boundary worktree git-stash before transitioning into next phase. On resume, runner observes stash → choice: pop (restore in-progress) or drop (start phase fresh). Default = drop after 1h staleness (avoid stale stashes accumulating).

### C2 — Concurrent runners

**Failure modes**:
- Two runners pick up same ticket (mutex_with broken)
- Runner crashes leaves ticket in In Progress (orphan)
- Worktree locked by zombie runner
- JIRA assignee not cleared

**Coverage**:
- ✅ JIRA pickup uses transition + assign atomically (single REST call)
- ✅ Worktree path includes ticket key + bot identity (collision-free)
- 🟡 mutex_with is operator-set; partial coverage
- 🔴 Orphan ticket detection: no automated reaper

**New requirement**: orphan-reaper cron — every 30 min, find tickets in `In Progress` assigned to bot with no recent activity (>2h since last comment) → revert to `To Do` + clear assignee. Runs as part of bridge daemon.

### C3 — TOCTOU (time-of-check vs time-of-use)

**Failure modes**:
- Ticket pickup → status checked, but status changes before pickup completes
- Live alembic head injected, but another runner advances head before we run
- Repo-map computed, but file changed before locator reads it

**Coverage**:
- 🟡 Pickup uses optimistic check + atomic transition (Lesson 18 stream-event idempotency)
- 🟡 Live-state injection at every transition (Invariant I6)
- 🔴 Repo-map cache invalidation on every commit, not on timer

**New requirement** (codifies Invariant I6 + repo-map invalidation): every transition that depends on repo state MUST re-read state at transition time (not at pickup time, not at session start). Repo-map cache key = HEAD SHA, invalidated on every observed commit.

### C4 — External system cascade failures

[Authoritative version in ADR-0035 §External system ordering (§C4) — see there for binding form.]

**Failure modes**:
- Gerrit rejects push → JIRA still transitioned to Under Review → orphaned state
- JIRA transition succeeds → Gerrit push fails → ticket stuck in wrong state
- Bridge daemon down → change-merged events queue, what happens when it comes back?
- Atlassian Cloud rate-limit → transition fails → runner mid-state

**Coverage**:
- 🟡 `_handle_gerrit_push_failure` covers known classes
- 🔴 No two-phase commit between JIRA + Gerrit
- 🔴 Bridge backlog catchup behaviour unspecified post-F4

**New requirement**: state-transition order is **Gerrit-push first, JIRA-transition second**. If Gerrit push fails, JIRA stays at In Progress (no orphan). If JIRA transition fails after Gerrit success, retry transition with idempotency token (Gerrit Change-URL); next pickup detects existing PS via F11 detector.

### C5 — Semantic drift over long contexts

**Failure modes**:
- Model "remembers" file content from iteration 5; iteration 30 model thinks file is still that way but it isn't
- Anthropic prompt cache breakpoint includes stale file content
- Context reset (B3) — what if reset clears legitimate progress?

**Coverage**:
- ✅ Anthropic prompt cache is byte-exact (no semantic compression)
- 🟡 B3 reset preserves system prompt + first user message; intermediate progress relies on scratchpad
- 🔴 Stale file content in cached turns: no detection mechanism

**New requirement**: every `text_editor` `view` call returns current file content; runner periodically (every N iterations, N=10) issues unsolicited `view` of the most-edited file to refresh model's mental model. Cost: ~500 input tokens per refresh; 10× per ticket = ~5k tokens (~$0.005). Cheap insurance.

### C6 — CostGuard race

**Failure modes**:
- Model in middle of expensive call when cap is hit
- CostGuard throttle — what does runner do? Wait? Skip?
- Multiple runners share global cap — race on spend tally

**Coverage**:
- ✅ Per-ticket cap enforced pre-call
- 🟡 Global cap shared across runners via Postgres sequence (eventual consistency)
- 🔴 Mid-call cap-hit policy: undefined

**New requirement**: pre-call estimate → if `current_spend + estimate > cap`, **abort before call** (don't truncate mid-stream; partial response wastes budget without progress). Throttle action = wait 60s + re-estimate. Block action = surrender ticket + log.

### C7 — Tool sandbox boundary violation

**Failure modes**:
- B1 PTC sandbox writes to main repo (vs worktree) — F17
- Sandbox times out → server-side state ambiguous — F3
- Sandbox produces side effects we can't observe (e.g., HTTP call to external service)

**Coverage**:
- 🟡 PTC `allowed_callers` whitelist limits tools sandbox can invoke
- 🔴 Sandbox cwd enforcement: must be worktree path; not yet codified
- 🔴 Side-effect observation: sandbox stdout captured but external network calls not gated

**New requirement**: PTC sandbox launch config sets `cwd=<worktree-path>` and `allowed_callers=[text_editor, bash]` strictly (no HTTP / no DB). Side-effect attempt → sandbox abort with `sandbox_boundary_violation`.

### C8 — API surface drift

**Failure modes**:
- Tool spec updated (`text_editor_20250728` → next version) breaks our handler
- Built-in tool behaviour changes silently between model versions
- Beta header (`computer-use-2025-11-24`) deprecated mid-Sprint

**Coverage**:
- ✅ Pinned dated tool types per audit recommendation
- 🟡 Model version pinned per call (not auto-upgrade)
- 🔴 No CI watch for Anthropic spec changelog

**New requirement**: weekly cron polls Anthropic platform docs RSS (or equivalent) → diff against last-known spec → file `META — Anthropic API drift` ticket if drift detected. Sprint maintainer (1 person) reviews quarterly.

---

## 7. Recovery primitives — codified spec

[Authoritative version in ADR-0035 §Idempotency primitives (§7) — see there for binding form.]

This section defines the primitive operations Sprint B children rely on.

### Worktree snapshot

**Format**: `git stash push -m "<phase_name> @ <ISO timestamp> @ <ticket_key>"`

**Trigger points** (per Sprint charter):
- Phase boundary entry (e.g., transitioning into `coding` from `tdd_phase`)
- Pre-commit (so a critic dissent can roll back to pre-edit state)

**Retention policy**: 1 hour after Sprint completion. Reaper deletes older.

### Conversation history checkpoint

**Format**: JSONL append-only, one line per assistant turn, written to `progress.txt`.

**Schema**:
```json
{"ts": "2026-05-09T12:34:56Z", "iter": 17, "role": "assistant", "stop_reason": "tool_use", "tool_calls": [...], "content_summary": "..."}
```

**Trigger**: every assistant turn. Synchronous fsync (durability > throughput).

### Tool call replay log

**Purpose**: post-hoc debugging; required for B3 detector retrospective tuning.

**Format**: same JSONL stream as conversation history; tool-side events (input + result) interleaved.

**Retention**: 30 days, then archive to S3 (per `archive_window_days` in spec).

### JIRA idempotency tokens

**Token format**: `(ticket_key, operation_type, args_hash)` where:
- `operation_type` ∈ {`pickup`, `transition`, `comment`, `assign`, `release`}
- `args_hash` = SHA256(JSON-canonical(args))

**Storage**: Postgres `jira_operations` table, unique index on token. Retried operations match token → no-op.

**TTL**: 24h; older tokens dropped (re-issue allowed for genuinely new operation).

### Gerrit Change-Id reuse rules

**New ticket** (no prior PS): generate fresh Change-Id via commit-msg hook. Pin to ticket via JIRA comment "Gerrit: <change-url>".

**Resume ticket** (PS exists): pickup detects via F11 — `gerrit query change:I<id>`. Reuse Change-Id; amend new PS.

**Forbidden**: regenerating Change-Id on amend. Always pass through original Change-Id explicitly (lesson learned from #334 duplicate during PS2 push).

### Cache invalidation triggers

- **Repo-map cache (B8)**: invalidated on every observed commit (HEAD SHA change).
- **BM25 lessons index (B10)**: invalidated when `lessons-learned.md` mtime changes.
- **Anthropic prompt cache (B11)**: managed by Anthropic; no client-side invalidation needed.
- **Tree-sitter parse cache**: per-file mtime; invalidated on file edit.

---

## 8. Sprint charter — pre-flight gates

This section converts §2–§7 into a **machine-checkable pre-flight gate**. Before any Phase 1 child enters In Progress, the following conditions must hold:

### G1 — Filing-time per-child spec

Every Sprint B JIRA ticket must include three required sections in description (CI lint will reject empty / `TODO`):
- `## Error catalog` — enum (codes from §4 / §5) + free-text fallback distinguished
- `## State transitions` — FSM nodes + legal transitions + non-legal-transition handling
- `## Recovery / rollback` — what survives crash, where can we roll back to

**Enforcement**: extend `jira-ticket-conventions.md` §14 vague-rejection lint with these three required-section regex.

### G2 — Cross-cutting gate

Before Phase 1 starts, the following 8 cross-cutting items (§6 C1–C8) must each have a documented mitigation **in code or runbook**:

| # | Concern | Required mitigation | Where landed |
|---|---|---|---|
| C1 | Process crash | Phase-boundary git-stash; 1h reaper | `runner_recovery.py` (new) |
| C2 | Concurrent runners | Orphan reaper cron (30min) | bridge daemon extension |
| C3 | TOCTOU | Live-state re-read at every transition; repo-map invalidate per commit | Invariant I6 in code |
| C4 | External cascade | Gerrit-first, JIRA-second order; idempotency tokens | jira_dispatch + push handler |
| C5 | Semantic drift | Periodic file-view refresh (every 10 iter) | runner main loop |
| C6 | CostGuard race | Pre-call abort if `current + estimate > cap` | cost_estimator.py |
| C7 | Sandbox boundary | PTC `cwd=worktree`, strict `allowed_callers` | B1 implementation |
| C8 | API drift | Weekly Anthropic spec watch cron | new ops cron |

### G3 — Past-failure regression check

Before Phase 1 starts, the 5 🔴 incidents from §3 must each have a Sprint-B-internal regression test:
- F4/F10 (bridge currency): runner pickup health-check
- F6/F7/F12 (cross-ticket peer): B13 + B7 peer-detection test
- F11 (prior PS rebase): B7 prior-PS detector test
- F20 (TODO.md staging analogue): B6 feature-list-JSON staging test
- F25 (stream-events dead): bridge-state freshness check

### G4 — Kill switches matrix

Sprint-level abort criteria. Each row: condition → action. Triggered automatically via runner observability.

| Condition | Action | Recovery seed |
|---|---|---|
| Phase 1 re-pilot < 2/3 success | Abort Sprint B | File Sprint C with "B1 didn't lift baseline" hypothesis |
| Critic dissent rate > 50% on first 5 tickets | Pause Phase 2; review B4 model choice | Switch B4 to Sonnet; re-pilot |
| 3+ consecutive `loop_aborted_terminal` (B3) on different tickets | Pause Sprint B; B3 detector mistuned | Tune args_hash; re-test |
| CostGuard hits global cap before Phase 3 entry | Pause + escalate | Operator decides scope reduction |
| 2+ orphan tickets in 24h (C2) | Pause + investigate | Fix mutex_with / reaper before resume |
| Anthropic API drift (C8) breaking change | Pause Sprint B | Operator pin model version; resume after spec fix |

---

## 9. Open gaps (known unknowns post-this-audit)

After the deep dive, ~5% of failure surface remains explicitly unmodelled. Operator should be aware:

- **Cross-language repo growth**: tree-sitter grammars cover Python + TypeScript today. Adding Go / Rust modules invalidates B8 + B13's coverage assumptions; needs grammar audit before language addition.
- **Multi-region Anthropic API**: if we ever hit a region cutover mid-sprint, idempotency tokens may not survive. Untested.
- **Sandbox-side state in PTC after process restart**: server-side sandbox lifetime opaque; documented assumption is "fresh per session" but not Anthropic-confirmed.
- **`progress.txt` corruption recovery beyond "treat as absent"**: full corruption recovery (e.g., reconstruct from tool-call log) not implemented.
- **Scratchpad evolution across sprints**: format change in Sprint C will break B3 cross-reset continuity; needs schema versioning.

These are **known unknowns**: operator decides whether to pre-empt or accept-then-watch.

---

## 10. Cross-references

- Main plan: `docs/audit/2026-05-09-sprint-b-runner-reliability-plan.md` (Gerrit #333)
- Audit antecedent: `docs/audit/2026-05-09-aider-swe-agent-audit.md` (Gerrit #332)
- Lessons-learned source: `docs/sop/lessons-learned.md` (L1-L27)
- Anti-pattern catalogue: `docs/sop/architecture-anti-patterns.md` (Patterns 1-12 referenced; Patterns 9 + 12 directly cited as F6/F7)
- Runner state machine source code (post-implementation): `backend/agents/runner_state_machine.py` (to be created)
- Recovery primitives source code (post-implementation): `backend/agents/runner_recovery.py` (to be created)

---

## 11. Action items pre-Sprint-B-start

- [ ] Operator reviews this document; flags gaps / disagreements
- [ ] §3 🔴 entries (5 items) get explicit countermeasure spec before Phase 1 children file
- [ ] §6 C1–C8 each get a 1-page implementation spec (~8 spec docs)
- [ ] §8 G1 lint gate added to `jira-ticket-conventions.md`
- [ ] §8 G3 regression test set authored as Sprint B child **B0** (the prerequisite child) before any other child enters In Progress
- [ ] Charter signed: operator + AI fleet acknowledge §8 gates
