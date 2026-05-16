---
id: SPRINT-S12-PHASE-31B-SPEC
version: v2 (post codex review)
title: Sprint S12 Phase 31.B — Runner Governance · Ticket Spec
scope: Branch namespacing + per-pickup ephemeral CLONE (not worktree) + Coordinator + pickup mutex + B-0 bootstrap + rolling cutover
status: Draft (2026-05-13)
related:
  - ADR-0023 §4 31.B (B-0 chicken-and-egg protocol)
  - Phase 31.A v2 spec (boundaries: + context_hint: schemas — extended in v2 here)
  - SOP-S12-TICKET-DECOMP
  - OP-977 (AUDIT-24 fencing-token pickup mutex — already shipped, lives in jira_dispatch.py:claim_ticket_atomic L2195)
  - OP-1045 (per-pickup ephemeral clone — absorbed into 31.B-2)
  - L1 #514 (--no-thin push fix already in jira_dispatch.py)
  - 31.B v1 (pre codex review, now superseded)
  - Codex independent review (2026-05-13, captured /tmp/phase31b-codex-review-final.txt)
---

# Sprint S12 Phase 31.B · Runner Governance — Ticket Spec v2

## §0. v2 Changelog (vs v1 — driven by codex independent review)

**BLOCKING fixes (P0):**
- **File-path correction** — v1 referenced non-existent `backend/agents/auto_runner_jira.py` and `backend/agents/coordinator/claim_ticket_atomic.py`. Actual locations:
  - Runner entry script: `auto-runner-jira.py` at repo root (1885 LOC)
  - Multi-instance launcher: `auto-runner-multi.py` at repo root (489 LOC)
  - Dispatch library: `backend/agents/jira_dispatch.py` (2603+ LOC; claim_ticket_atomic at L2195, _ephemeral_worktree_dir at L649)
  - Tests live in `backend/tests/test_jira_dispatch.py`, `backend/tests/test_atomic_claim_race.py`, etc.
- **DAG serialization for live-file edits** — 5c/5d/5e all touch jira_dispatch.py. v1 blockedBy chain allowed parallel; v2 forces 5c→5d→5e sequential.
- **Symbol-scope hints in `required_paths`** — for tickets touching the 106KB jira_dispatch.py, spec now names the specific function block (e.g., 5c → "_pickup_run_loop fork on USE_EPHEMERAL flag, ~L650-700"). Mechanism: spec calls out the function name + approximate line range; OP-1042 hook validates diff stays inside that range at filing.

**Schema additions (P1):**
- **`mutex_with:` boundary field** — list of sibling ticket IDs that touch same file/function; coordinator enforces only one in-flight at a time.
- **`destructive_op_classes:` boundary field** — list enum (state-append / tmp-delete / env-edit / systemd-control / git-ref-rewrite). Replaces the binary `destructive_ops_allowed`.

**Ticket mergers (P2):**
| v1 | v2 | Reason (per codex) |
|---|---|---|
| 1b + 1c | 1bc | runtime governance lib should not exist without tests |
| 2b + 2c | 2bc | same |
| 3b + 3c | 3bc | same |
| 4b + 4c | 4bc | same |
| 6c + 6d | 6cd | heartbeat emit + detect = one cohesive module |
| 7a + 7b | 7ab | prefer routing + class override = one precedence rule |
| 10a/10b | 10b before 10a (REORDER) | rollback must exist BEFORE cutover; v1 had it backwards |

**Intermediate integration gates added (P3):**
- **B-LibsGate** after 1bc/2bc/3bc/4bc — cross-lib contract verify
- **B-LegacySmoke** after 5b — confirm flag=0 path still works
- **B-SingleDryGate** after 5e + 6cd + 7ab — dry-run pickup on flag=1 with no JIRA mutation
- **B-CutoverReadyGate** after 10b + 10a — cutover↔rollback roundtrip on test runner

**Hardening AC additions (P4):**
- 4bc (coordinator_state): corrupt-JSON handling + atomic temp+rename + flock-timeout
- 9-Test (bootstrap verification): real state write + heartbeat roundtrip + clone lifecycle (not just imports)
- 5b: mixed cutover invariant — old runner + new runner cannot claim same ticket (proved via shared claim_ticket_atomic which both paths invoke)
- 5c/5d/5e: inline behavior smoke ACs (not just grep)

**Total ticket count**: v1 35 → v2 **33** (4 lib+test mergers, 1 heartbeat merge, 1 routing merge; +4 gates).

---

## §1. Pre-flight reading

1. ADR-0023 §4 31.B
2. **`backend/agents/jira_dispatch.py`** — required reading; specifically:
   - L645-700 worktree/clone path helpers
   - L2015-2200 ClaimResult + claim_ticket_atomic
   - L2440 release_ticket_claim
   - L2600+ pre_pickup_ok and below — the pickup orchestration
3. **`auto-runner-jira.py`** — the runner entry script (top-level fn `_bot_username`, `_default_worktree_for`, main pickup loop)
4. **`auto-runner-multi.py`** — the multi-instance launcher (489 LOC); each runner instance reads its own env via this
5. Phase 31.A v2 §3/§4 schemas
6. OP-977 + AUDIT-24 (fencing-token mutex)

---

## §2. Sub-META definition

```yaml
key:        Sprint S12 — Phase 31.B (sub-META)
summary:    "AUDIT-31.B: Runner governance (branch namespacing + ephemeral clone + Coordinator + mutex)"
type:       Story (ストーリー per AUDIT-27 locale alias)
sprint:     "S12: Bedrock" (sprint id 52)
parentMeta: Sprint S12 META
fixVersion: v0.5.0-rc2
labels:
  - area:devops, area:backend, area:tooling, area:tests, area:docs
  - tier:L (sub-META is roll-up)
  - type:meta
  - agent:auto
  - scope:sprint-s12, phase:31.B
  - adr:0023
  - "capability:enable=gerrit_push, capability:enable=code_edit, capability:enable=jira_update, capability:enable=run_lint, capability:enable=run_tests"
```

**Scope statement (unchanged from v1)**: After 31.B closes, all 4 runners pick up via `feature/{TICKET}-{INSTANCE}-pid{PID}-{EPOCH}-runner`, working in `/tmp/runner-pickup/<uuid>/` ephemeral clones, coordinated by library-mode Coordinator that respects `prefer:*` and enforces fencing-token mutex. Legacy worktree path remains as fallback (`RUNNER_USE_EPHEMERAL_CLONE=0`); removed at Phase 31.K.

**Sub-META AC**:
1. Code: 4 libs + scheduler + heartbeat + routing on develop
2. Deploy: 33 children all closed; feature flag wired in auto-runner-jira.py + jira_dispatch.py
3. Integration: 31.B-Integration ticket closes (concurrent 4-runner stress test passes)
4. Exercised: 7-day rolling cutover complete; all 4 runners on flag=1; 0 Missing-tree, 0 mutex races

**Go-Live**: T+5-7 wk (C4) / 6-9 wk (C2) / 9-12 wk (C1)

---

## §3. Boundaries schema — v2 extensions

Reuses Phase 31.A v2 §3 base schema. v2 adds two fields per codex Q5 findings:

### §3.1 New field: `mutex_with: [str, ...]`

List of sibling ticket IDs that touch the same file/function. Coordinator enforces only one is in-flight at a time (`assignee != null AND state != Done`). Filing-time hook checks tickets in `mutex_with` exist; runtime enforcement at pickup.

Example: `mutex_with: [31.B-5c, 31.B-5d]` on 31.B-5e means 5e cannot start until both 5c and 5d are closed (in addition to its blockedBy chain).

### §3.2 New field: `destructive_op_classes: [enum, ...]`

Replaces the binary `destructive_ops_allowed`. Enum values:

| Class | What | Blast radius | Operator review required? |
|---|---|---|---|
| `state-append` | Appends to log/cache files | local-state | no |
| `tmp-delete` | Deletes /tmp/* dirs only | local-disk | no |
| `env-edit` | Modifies runner env files (e.g., setting RUNNER_USE_EPHEMERAL_CLONE) | one runner instance | yes |
| `systemd-control` | systemctl stop/start unit | one runner process | yes |
| `git-ref-rewrite` | force-push, rename, delete branches/refs | shared remote state | yes (highest) |

`destructive_op_classes: []` (empty list) = pure read/structural ticket.

### §3.3 Symbol-scope hint pattern

For tickets touching jira_dispatch.py (106KB), required_paths uses **path + function name + line range** in `context_hint.produces`:

```yaml
context_hint:
  produces: "backend/agents/jira_dispatch.py:function_name (approx L650-700)"
required_paths: [backend/agents/jira_dispatch.py]
```

Reviewer + OP-1042 hook check the diff hunk stays within the named function/range. Outside-range edit = scope-creep follow-up.

---

## §4. Children — overview table (33 tickets)

| Layer | # | ID | Title | Tier | Prefer | blockedBy |
|---|---|---|---|---|---|---|
| L0 | 1 | 31.B-1a | Branch namespacing ABNF + format spec (doc) | S | claude | — |
| L0 | 2 | 31.B-2a | Ephemeral clone path naming spec (doc) | S | claude | — |
| L0 | 3 | 31.B-3a | Fencing-token format spec (doc) | S | claude | — |
| L0 | 4 | 31.B-4a | Coordinator scope decision (ADR-0024) | S | claude | — |
| L1 | 5 | 31.B-1bc | branch_naming.py + tests (MERGED) | S | claude | 1a |
| L1 | 6 | 31.B-2bc | ephemeral_clone.py + tests (MERGED) | S | claude | 2a |
| L1 | 7 | 31.B-3bc | pickup_mutex.py wrapper + tests (MERGED) | S | claude | 3a |
| L1 | 8 | 31.B-4bc | coordinator_state.py + tests (MERGED + HARDENED) | S | claude | 4a |
| L2-Gate | 9 | 31.B-LibsGate | Cross-library contract integration check | S | claude | 1bc, 2bc, 3bc, 4bc |
| L3 | 10 | 31.B-5a | Feature flag in auto-runner-jira.py | S | claude | LibsGate |
| L3 | 11 | 31.B-5b | Preserve legacy worktree path under flag=0 | S | claude | 5a |
| L4-Gate | 12 | 31.B-LegacySmoke | Legacy flag=0 path one-pickup smoke | S | claude | 5b |
| L5 | 13 | 31.B-5c | Wire ephemeral_clone (jira_dispatch.py, flag=1 branch) | S | claude | LegacySmoke, 2bc |
| L6 | 14 | 31.B-5d | Wire branch_naming (jira_dispatch.py, flag=1 branch) | S | claude | 5c, 1bc |
| L7 | 15 | 31.B-5e | Wire pickup_mutex (jira_dispatch.py, flag=1 branch) | S | claude | 5d, 3bc |
| L5 | 16 | 31.B-6a | Scheduler logic (coordinator_scheduler.py) | S | claude | 4bc |
| L6 | 17 | 31.B-6b | Scheduler tests | S | claude | 6a |
| L5 | 18 | 31.B-6cd | Heartbeat emit + stuck-runner detect (MERGED) | S | codex | 4bc |
| L6 | 19 | 31.B-7ab | prefer routing + class override (MERGED) | S | claude | 6a |
| L7 | 20 | 31.B-7c | Routing audit log (JSONL) | S | codex | 7ab |
| L7 | 21 | 31.B-7d | Routing logic tests | S | claude | 7ab |
| L8-Gate | 22 | 31.B-SingleDryGate | Single-runner dry-pickup on flag=1 | S | claude | 5e, 6cd, 7d |
| L8 | 23 | 31.B-8a | Existing-branch rename utility | S | codex | 1bc |
| L8 | 24 | 31.B-8b | Stale /tmp cleanup script | S | codex | 2bc |
| L9 | 25 | 31.B-8c | Migration utility tests | S | claude | 8a, 8b |
| L9 | 26 | 31.B-9-Doc | B-0 operator bootstrap runbook | S | claude | SingleDryGate |
| L10 | 27 | 31.B-9-Test | Bootstrap verification suite (HARDENED) | S | claude | 9-Doc |
| L10 | 28 | 31.B-10-Doc | Rolling cutover runbook | S | claude | 9-Test |
| L11 | 29 | 31.B-10b | Rollback script (BUILT FIRST per codex) | S | codex | 10-Doc |
| L11 | 30 | 31.B-10a | Cutover script (CALLS rollback on failure) | S | codex | 10b |
| L12-Gate | 31 | 31.B-CutoverReadyGate | Cutover↔rollback roundtrip on test runner | S | claude | 10a, 10b |
| L13 | 32 | 31.B-Integration-Doc | E2E stress-test runbook | S | claude | CutoverReadyGate |
| L14 | 33 | 31.B-Integration | **E2E concurrent 4-runner stress test** | **L** | claude | ALL 1-32 |

**Routing tally**: codex 6, claude 27. Heavy claude-routed because of jira_dispatch.py edits + docs + cross-lib judgement.

**Critical path**: 1a → 1bc → LibsGate → 5a → 5b → LegacySmoke → 5c → 5d → 5e → SingleDryGate → 9-Doc → 9-Test → 10-Doc → 10b → 10a → CutoverReadyGate → Integration-Doc → Integration. ~18 sequential hops (similar to v1 but with safety gates inserted).

---

## §5. Per-child specs

Universal labels + `boundaries.on_scope_creep: file-followup` + `boundaries.scope_summary_max_chars: 500` + universal Deploy/Integration/Exercised AC: see Phase 31.A v2 §6 preamble.

Universal `context_hint.phase_goal`: "Phase 31.B: shared-worktree+label-race → ephemeral-clone+Coordinator-mediated mutex. Move all 4 runners to new path via rolling cutover."

---

### 31.B-1a — Branch namespacing ABNF + format spec

```yaml
tier: S, prefer: claude, class: subscription-claude
area_labels: [area:docs, area:devops]
blockedBy: []
context_hint:
  produces: "docs/sop/runner-branch-naming-spec.md"
  consumed_by: "31.B-1bc (parser+tests), 31.B-5d (wiring into jira_dispatch.py)"
  inputs_expected: "(first in chain)"
  outputs_guaranteed: "ABNF grammar for `feature/{TICKET}-{INSTANCE}-pid{PID}-{EPOCH}-runner`; INSTANCE enum; EPOCH=epoch_us format; reserved chars; max length 250"
  non_goals:
    - "DO NOT write parser — 1bc owns"
    - "DO NOT touch jira_dispatch.py — 5d owns"
boundaries:
  loc_delta_max: 200, files_touched_max: 1
  required_paths: [docs/sop/runner-branch-naming-spec.md]
  forbidden_paths: [auto-runner-jira.py, backend/agents/jira_dispatch.py, backend/runner/]
  test_scope: defer-to-integration
  destructive_op_classes: []
  execution_mode: structural-only
  dependency_artifacts: []
  mutex_with: []
  interface_contract:
    inputs_from_deps: []
    outputs_for_downstream: ["ABNF grammar", "INSTANCE enum (claude-1/2/codex-1/2)", "epoch_us format"]
ac.code:
  - {desc: "ABNF section", verify: {command: "grep -cE '^##.*ABNF|^```abnf' docs/sop/runner-branch-naming-spec.md", expect_stdout_match: "[1-9]"}}
  - {desc: "INSTANCE enum present", verify: {command: "grep -cE 'claude-[12]|codex-[12]' docs/sop/runner-branch-naming-spec.md", expect_stdout_match: "^[4-9]$|^[1-9][0-9]+$"}}
go_live: T+0.5d
```

### 31.B-2a — Ephemeral clone path naming spec

```yaml
tier: S, prefer: claude, class: subscription-claude
area_labels: [area:docs, area:devops]
blockedBy: []
context_hint:
  produces: "docs/sop/runner-ephemeral-clone-spec.md"
  consumed_by: "31.B-2bc (clone library), 31.B-5c (wiring), 31.B-8b (stale cleanup)"
  inputs_expected: "(first in chain)"
  outputs_guaranteed: "Path `/tmp/runner-pickup/{INSTANCE}-{TICKET}-{UUID4}/`; lifecycle (create on pickup; delete on success+failure); 24h stale TTL; distinguishes from EXISTING `_ephemeral_worktree_dir` at jira_dispatch.py L649 (worktree shares .git; clone doesn't)"
  non_goals:
    - "DO NOT specify clone impl — 2bc owns"
    - "DO NOT modify EXISTING `_ephemeral_worktree_dir` behavior (it stays for flag=0 path)"
boundaries:
  loc_delta_max: 200, files_touched_max: 1
  required_paths: [docs/sop/runner-ephemeral-clone-spec.md]
  forbidden_paths: [auto-runner-jira.py, backend/agents/jira_dispatch.py, backend/runner/, docs/sop/runner-branch-naming-spec.md]
  test_scope: defer-to-integration
  destructive_op_classes: []
  execution_mode: structural-only
  dependency_artifacts: []
  mutex_with: []
  interface_contract:
    inputs_from_deps: []
    outputs_for_downstream: ["path format", "lifecycle table", "24h TTL", "clone vs worktree distinction"]
ac.code:
  - {desc: "/tmp/runner-pickup/ prefix", verify: {command: "grep -c '/tmp/runner-pickup/' docs/sop/runner-ephemeral-clone-spec.md", expect_stdout_match: "^[2-9]$|^[1-9][0-9]+$"}}
  - {desc: "distinguishes from worktree (L649 ref)", verify: {command: "grep -cE 'worktree|_ephemeral_worktree_dir|L649' docs/sop/runner-ephemeral-clone-spec.md", expect_stdout_match: "[1-9]"}}
go_live: T+0.5d
```

### 31.B-3a — Fencing-token format spec

```yaml
tier: S, prefer: claude, class: subscription-claude
area_labels: [area:docs, area:devops]
blockedBy: []
context_hint:
  produces: "docs/sop/runner-pickup-mutex-spec.md"
  consumed_by: "31.B-3bc (wrapper lib), 31.B-5e (wiring)"
  inputs_expected: "Existing OP-977 logic in jira_dispatch.py:claim_ticket_atomic (L2195) + _mint_claim_token (L2094)"
  outputs_guaranteed: "Formalised spec for existing format `claim:{instance}:{epoch_us:016d}-{uuid8}`; tie-break: lowest-uuid wins (already in jira_dispatch.py:_lowest_uuid_claim_winner L2160); 5-min TTL (existing); legacy bare-label fallback removal deferred to 31.K"
  non_goals:
    - "DO NOT change format (it works)"
    - "DO NOT implement — 3bc wraps existing"
    - "DO NOT remove legacy fallback (Phase 31.K)"
boundaries:
  loc_delta_max: 200, files_touched_max: 1
  required_paths: [docs/sop/runner-pickup-mutex-spec.md]
  forbidden_paths: [auto-runner-jira.py, backend/agents/jira_dispatch.py, backend/runner/]
  test_scope: defer-to-integration
  destructive_op_classes: []
  execution_mode: structural-only
  dependency_artifacts: []
  mutex_with: []
  interface_contract:
    inputs_from_deps: []
    outputs_for_downstream: ["fencing-token format documented", "tie-break rule documented", "TTL documented"]
ac.code:
  - {desc: "references OP-977/AUDIT-24", verify: {command: "grep -cE 'OP-977|AUDIT-24' docs/sop/runner-pickup-mutex-spec.md", expect_stdout_match: "[1-9]"}}
  - {desc: "references existing impl at jira_dispatch.py", verify: {command: "grep -cE 'jira_dispatch.py.*claim_ticket_atomic|claim_ticket_atomic.*L2195' docs/sop/runner-pickup-mutex-spec.md", expect_stdout_match: "[1-9]"}}
go_live: T+0.5d
```

### 31.B-4a — Coordinator scope decision (ADR-0024)

```yaml
tier: S, prefer: claude, class: subscription-claude
area_labels: [area:docs, area:backend]
blockedBy: []
context_hint:
  produces: "docs/adr/ADR-0024-coordinator-scope.md"
  consumed_by: "31.B-4bc (state library), 31.B-6a (scheduler), 31.B-7ab (prefer routing)"
  inputs_expected: "(refs ADR-0023)"
  outputs_guaranteed: "Decision: library-first; file-based shared state at ~/.cache/omnisight/coordinator-state.json; daemon mode OUT OF SCOPE for 31.B (revisit at 31.K); schema versioning REQUIRED v1; flock + atomic temp+rename + corrupt-JSON recovery REQUIRED"
  non_goals:
    - "DO NOT implement Coordinator — 4bc owns library"
    - "DO NOT spec daemon mode"
boundaries:
  loc_delta_max: 350, files_touched_max: 1
  required_paths: [docs/adr/ADR-0024-coordinator-scope.md]
  forbidden_paths: [docs/adr/ADR-0023-foundation-rebuild.md, backend/runner/]
  test_scope: defer-to-integration
  destructive_op_classes: []
  execution_mode: structural-only
  dependency_artifacts: []
  mutex_with: []
  interface_contract:
    inputs_from_deps: []
    outputs_for_downstream: ["library-first decision", "state file path", "v1 schema requirement", "corrupt-JSON + atomic-write requirements"]
ac.code:
  - {desc: "ADR sections present", verify: {command: "grep -cE '^## (Context|Decision|Consequences|Status)' docs/adr/ADR-0024-coordinator-scope.md", expect_stdout_match: "^[4-9]$"}}
  - {desc: "status Accepted", verify: {command: "grep -ciE '^status:.*accepted|^## status.*accepted' docs/adr/ADR-0024-coordinator-scope.md", expect_stdout_match: "[1-9]"}}
  - {desc: "documents corrupt-JSON + atomic-write requirements", verify: {command: "grep -ciE 'corrupt.*json|atomic.*write|temp.*rename' docs/adr/ADR-0024-coordinator-scope.md", expect_stdout_match: "[2-9]"}}
go_live: T+0.5d
```

### 31.B-1bc — branch_naming.py + tests (MERGED)

```yaml
tier: S, prefer: claude, class: subscription-claude
area_labels: [area:backend, area:tooling, area:tests]
blockedBy: [1a]
context_hint:
  produces: "backend/runner/branch_naming.py + backend/runner/tests/test_branch_naming.py (lib AND tests in one ticket; per codex: don't ship runtime-governance lib without tests)"
  consumed_by: "31.B-LibsGate, 31.B-5d (wiring), 31.B-8a (rename utility)"
  inputs_expected: "ABNF from 1a"
  outputs_guaranteed: "Module: format_branch_name(ticket,instance,pid,epoch_us)→str; parse_branch_name(name)→BranchInfo; validate(name)→bool. Tests: >=8 functions covering roundtrip + 4 invalid-parse reasons + stability."
  non_goals:
    - "DO NOT modify jira_dispatch.py — 5d owns"
    - "DO NOT add ephemeral_clone — 2bc owns"
boundaries:
  loc_delta_max: 250, files_touched_max: 2
  required_paths: [backend/runner/branch_naming.py, backend/runner/tests/test_branch_naming.py]
  forbidden_paths: [auto-runner-jira.py, backend/agents/jira_dispatch.py, backend/runner/ephemeral_clone.py, backend/runner/pickup_mutex.py, backend/runner/coordinator_state.py]
  test_scope: inline
  destructive_op_classes: []
  execution_mode: unit-testable
  dependency_artifacts: [docs/sop/runner-branch-naming-spec.md]
  mutex_with: []
  interface_contract:
    inputs_from_deps: [ABNF grammar]
    outputs_for_downstream:
      - "format_branch_name(ticket: str, instance: str, pid: int, epoch_us: int) -> str"
      - "parse_branch_name(name: str) -> BranchInfo"
      - "validate(name: str) -> bool"
      - "test suite >=8 functions, all pass"
ac.code:
  - {desc: "module importable", verify: {command: "python3 -c 'from backend.runner.branch_naming import format_branch_name, parse_branch_name, validate'", expect_exit_code: 0}}
  - {desc: "format canonical", verify: {command: "python3 -c \"from backend.runner.branch_naming import format_branch_name; assert format_branch_name('OP-100', 'claude-1', 12345, 1700000000000000) == 'feature/OP-100-claude-1-pid12345-1700000000000000-runner'\"", expect_exit_code: 0}}
  - {desc: "test count >=8", verify: {command: "pytest --collect-only backend/runner/tests/test_branch_naming.py 2>&1 | grep -c 'test_'", expect_stdout_match: "^[8-9]$|^[1-9][0-9]+$"}}
  - {desc: "tests pass", verify: {command: "pytest backend/runner/tests/test_branch_naming.py -v", expect_exit_code: 0, timeout_seconds: 30}}
go_live: T+1d after 1a
```

### 31.B-2bc — ephemeral_clone.py + tests (MERGED)

```yaml
tier: S, prefer: claude, class: subscription-claude
area_labels: [area:backend, area:tooling, area:tests]
blockedBy: [2a]
context_hint:
  produces: "backend/runner/ephemeral_clone.py + backend/runner/tests/test_ephemeral_clone.py"
  consumed_by: "31.B-LibsGate, 31.B-5c (wiring), 31.B-8b (cleanup script)"
  inputs_expected: "Spec from 2a"
  outputs_guaranteed: "Module: create_pickup_clone(instance,ticket,source_repo_url)→Path; cleanup_pickup_clone(path)→None; list_stale(ttl=86400)→List[Path]. Uses git clone (NOT worktree). Push uses --no-thin (per L1 #514). Tests: >=6 fns covering create/cleanup/stale paths (with tmp_path fixture)."
  non_goals:
    - "DO NOT use git worktree (the whole point is no shared object store)"
    - "DO NOT modify jira_dispatch.py — 5c owns"
    - "DO NOT add stale-cleanup script — 8b owns"
boundaries:
  loc_delta_max: 350, files_touched_max: 2
  required_paths: [backend/runner/ephemeral_clone.py, backend/runner/tests/test_ephemeral_clone.py]
  forbidden_paths: [auto-runner-jira.py, backend/agents/jira_dispatch.py, backend/runner/branch_naming.py, backend/runner/pickup_mutex.py, backend/runner/coordinator_state.py]
  test_scope: inline
  destructive_op_classes: [tmp-delete]   # cleanup deletes /tmp dirs
  execution_mode: unit-testable
  dependency_artifacts: [docs/sop/runner-ephemeral-clone-spec.md]
  mutex_with: []
  interface_contract:
    inputs_from_deps: [path format + lifecycle rules]
    outputs_for_downstream:
      - "create_pickup_clone(instance, ticket, source_repo_url) -> Path"
      - "cleanup_pickup_clone(path: Path) -> None"
      - "list_stale(ttl_seconds: int = 86400) -> List[Path]"
      - "test suite >=6 fns; tmp_path fixture; no real /tmp/runner-pickup writes"
ac.code:
  - {desc: "module + 3 fns importable", verify: {command: "python3 -c 'from backend.runner.ephemeral_clone import create_pickup_clone, cleanup_pickup_clone, list_stale'", expect_exit_code: 0}}
  - {desc: "NO git worktree usage", verify: {command: "grep -c 'git worktree' backend/runner/ephemeral_clone.py", expect_stdout_match: "^0$"}}
  - {desc: "uses --no-thin push", verify: {command: "grep -cE '\\-\\-no-thin' backend/runner/ephemeral_clone.py", expect_stdout_match: "[1-9]"}}
  - {desc: "tests use tmp_path (no real /tmp writes)", verify: {command: "grep -c 'tmp_path' backend/runner/tests/test_ephemeral_clone.py", expect_stdout_match: "[1-9]"}}
  - {desc: "tests pass", verify: {command: "pytest backend/runner/tests/test_ephemeral_clone.py -v", expect_exit_code: 0, timeout_seconds: 30}}
go_live: T+1d after 2a
```

### 31.B-3bc — pickup_mutex.py wrapper + tests (MERGED)

```yaml
tier: S, prefer: claude, class: subscription-claude
area_labels: [area:backend, area:tooling, area:tests]
blockedBy: [3a]
context_hint:
  produces: "backend/runner/pickup_mutex.py (wrapper around existing jira_dispatch.py:claim_ticket_atomic L2195) + backend/runner/tests/test_pickup_mutex.py"
  consumed_by: "31.B-LibsGate, 31.B-5e (wiring)"
  inputs_expected: "Spec from 3a; existing claim_ticket_atomic in jira_dispatch.py (DO NOT MODIFY)"
  outputs_guaranteed: "Module: claim_with_fencing_token(client,key,instance,pid)→ClaimResult; release_claim(client,key,token)→None; verify_holds_lock(client,key,token)→bool. All delegate to jira_dispatch.py:claim_ticket_atomic. Tests: >=5 fns concurrency/race/release."
  non_goals:
    - "DO NOT modify jira_dispatch.py:claim_ticket_atomic (proven core; this wraps)"
    - "DO NOT remove legacy bare-label fallback (Phase 31.K)"
    - "DO NOT modify auto-runner-jira.py — 5e owns"
boundaries:
  loc_delta_max: 350, files_touched_max: 2
  required_paths: [backend/runner/pickup_mutex.py, backend/runner/tests/test_pickup_mutex.py]
  forbidden_paths: [auto-runner-jira.py, backend/agents/jira_dispatch.py, backend/runner/branch_naming.py, backend/runner/ephemeral_clone.py, backend/runner/coordinator_state.py]
  test_scope: inline
  destructive_op_classes: []
  execution_mode: unit-testable
  dependency_artifacts: [docs/sop/runner-pickup-mutex-spec.md, backend/agents/jira_dispatch.py]
  mutex_with: []
  interface_contract:
    inputs_from_deps: [claim_ticket_atomic callable from jira_dispatch]
    outputs_for_downstream:
      - "claim_with_fencing_token(client, key, instance, pid) -> ClaimResult"
      - "release_claim(client, key, token) -> None"
      - "verify_holds_lock(client, key, token) -> bool"
      - "test suite >=5 fns including concurrent claimers tie-break"
ac.code:
  - {desc: "module importable", verify: {command: "python3 -c 'from backend.runner.pickup_mutex import claim_with_fencing_token, release_claim, verify_holds_lock'", expect_exit_code: 0}}
  - {desc: "delegates to jira_dispatch.claim_ticket_atomic", verify: {command: "grep -cE 'from backend.agents.jira_dispatch import.*claim_ticket_atomic|jira_dispatch\\.claim_ticket_atomic' backend/runner/pickup_mutex.py", expect_stdout_match: "[1-9]"}}
  - {desc: "did NOT modify jira_dispatch.py", verify: {command: "git diff --name-only HEAD~1 HEAD | grep -c 'backend/agents/jira_dispatch.py'", expect_stdout_match: "^0$"}}
  - {desc: "tests pass", verify: {command: "pytest backend/runner/tests/test_pickup_mutex.py -v", expect_exit_code: 0, timeout_seconds: 60}}
go_live: T+1d after 3a
```

### 31.B-4bc — coordinator_state.py + tests (MERGED + HARDENED)

```yaml
tier: S, prefer: claude, class: subscription-claude
area_labels: [area:backend, area:tooling, area:tests]
blockedBy: [4a]
context_hint:
  produces: "backend/runner/coordinator_state.py + backend/runner/tests/test_coordinator_state.py (HARDENED per codex Q4: corrupt-JSON + atomic temp+rename + flock-timeout)"
  consumed_by: "31.B-LibsGate, 31.B-6a (scheduler), 31.B-6cd (heartbeat), 31.B-7ab (prefer routing)"
  inputs_expected: "ADR-0024 (4a) decision: library-first, file-based, single-host"
  outputs_guaranteed: |
    Module: read_state()→CoordinatorState; with_lock(timeout=5) context manager; update_state(callable) atomic via temp+rename;
    schema v1; flock-based mutual exclusion; CORRUPT-JSON recovery (rename .corrupt → init fresh + alert); MISSING-FILE handled (init from defaults).
    Tests: >=8 fns covering happy path + corrupt + concurrent + missing + schema-mismatch + atomic-write atomicity.
  non_goals:
    - "DO NOT implement scheduler — 6a owns"
    - "DO NOT implement heartbeat — 6cd owns"
    - "DO NOT add HTTP/SSE (library-only per ADR-0024)"
boundaries:
  loc_delta_max: 400, files_touched_max: 2
  required_paths: [backend/runner/coordinator_state.py, backend/runner/tests/test_coordinator_state.py]
  forbidden_paths: [auto-runner-jira.py, backend/agents/jira_dispatch.py, backend/runner/branch_naming.py, backend/runner/ephemeral_clone.py, backend/runner/pickup_mutex.py, backend/runner/coordinator_scheduler.py]
  test_scope: inline
  destructive_op_classes: [state-append]
  execution_mode: unit-testable
  dependency_artifacts: [docs/adr/ADR-0024-coordinator-scope.md]
  mutex_with: []
  interface_contract:
    inputs_from_deps: [ADR-0024]
    outputs_for_downstream:
      - "read_state() -> CoordinatorState (handles missing-file + corrupt-JSON)"
      - "with_lock(timeout: int = 5) context manager"
      - "update_state(callable) atomic via temp+rename"
      - "state file: ~/.cache/omnisight/coordinator-state.json"
      - "schema_version: v1"
      - "test suite >=8 fns; hardened for corrupt + concurrent + missing"
ac.code:
  - {desc: "module importable", verify: {command: "python3 -c 'from backend.runner.coordinator_state import read_state, with_lock, update_state'", expect_exit_code: 0}}
  - {desc: "uses flock", verify: {command: "grep -cE 'fcntl.*flock|fcntl.LOCK_EX' backend/runner/coordinator_state.py", expect_stdout_match: "[1-9]"}}
  - {desc: "atomic temp+rename present", verify: {command: "grep -cE 'os\\.replace|os\\.rename.*\\.tmp|NamedTemporaryFile' backend/runner/coordinator_state.py", expect_stdout_match: "[1-9]"}}
  - {desc: "corrupt-JSON recovery present", verify: {command: "grep -cE 'JSONDecodeError|\\.corrupt|json.decoder' backend/runner/coordinator_state.py", expect_stdout_match: "[1-9]"}}
  - {desc: "tests count >=8", verify: {command: "pytest --collect-only backend/runner/tests/test_coordinator_state.py 2>&1 | grep -c 'test_'", expect_stdout_match: "^[8-9]$|^[1-9][0-9]+$"}}
  - {desc: "tests pass", verify: {command: "pytest backend/runner/tests/test_coordinator_state.py -v", expect_exit_code: 0, timeout_seconds: 30}}
go_live: T+1d after 4a
```

### 31.B-LibsGate — Cross-library contract integration check (INTERMEDIATE GATE)

```yaml
tier: S, prefer: claude, class: subscription-claude
area_labels: [area:tests, area:backend]
blockedBy: [1bc, 2bc, 3bc, 4bc]
context_hint:
  produces: "scripts/sprint-s12/phase31b-libs-gate.py + backend/runner/tests/test_libs_contract.py"
  consumed_by: "31.B-5a (gates entry into live-runner wiring); 31.B-9-Test (referenced)"
  inputs_expected: "All 4 libraries shipped (1bc/2bc/3bc/4bc)"
  outputs_guaranteed: "Cross-lib contract check: (1) branch_naming output → valid INSTANCE+EPOCH parse-able by pickup_mutex token-mint convention; (2) ephemeral_clone path includes INSTANCE+TICKET+UUID4 distinct from worktree path; (3) coordinator_state schema_version=v1 readable+writeable post-restart; (4) all 4 lib test suites green; (5) emits libs-gate.json evidence"
  non_goals:
    - "DO NOT modify any library — 1bc/2bc/3bc/4bc own"
    - "DO NOT touch live runner — 5a-5e own"
boundaries:
  loc_delta_max: 250, files_touched_max: 2
  required_paths: [scripts/sprint-s12/phase31b-libs-gate.py, backend/runner/tests/test_libs_contract.py]
  forbidden_paths: [auto-runner-jira.py, backend/agents/jira_dispatch.py, backend/runner/branch_naming.py, backend/runner/ephemeral_clone.py, backend/runner/pickup_mutex.py, backend/runner/coordinator_state.py]
  test_scope: inline
  destructive_op_classes: [state-append]
  execution_mode: unit-testable
  dependency_artifacts: [4 libraries]
  mutex_with: []
  interface_contract:
    inputs_from_deps: [all 4 libs importable + their tests green]
    outputs_for_downstream:
      - "libs-gate.json: {all_libs_importable, cross_lib_roundtrip, all_unit_tests_green, schema_version_check}"
      - "PASS gates entry to 5a"
ac.code:
  - {desc: "script + contract test exist", verify: {command: "test -x scripts/sprint-s12/phase31b-libs-gate.py && test -f backend/runner/tests/test_libs_contract.py", expect_exit_code: 0}}
  - {desc: "contract test passes", verify: {command: "pytest backend/runner/tests/test_libs_contract.py -v", expect_exit_code: 0, timeout_seconds: 60}}
  - {desc: "ALL 4 lib test suites green when invoked", verify: {command: "bash scripts/sprint-s12/phase31b-libs-gate.py | jq -r .all_unit_tests_green", expect_stdout_match: "^true$"}}
  - {desc: "libs-gate.json emitted with required schema", verify: {command: "python3 -c \"import json; d=json.load(open('docs/audit/AUDIT-31-phase-B-evidence/libs-gate.json')); assert all(k in d for k in ['all_libs_importable','cross_lib_roundtrip','all_unit_tests_green','schema_version_check'])\"", expect_exit_code: 0}}
go_live: T+0.5d after 1bc/2bc/3bc/4bc
```

### 31.B-5a — Feature flag in auto-runner-jira.py

```yaml
tier: S, prefer: claude, class: subscription-claude
area_labels: [area:backend, area:tooling]
blockedBy: [LibsGate]
context_hint:
  produces: "patch to auto-runner-jira.py (repo root, NOT in backend/agents) — read RUNNER_USE_EPHEMERAL_CLONE env var (default 0); expose as module-level constant USE_EPHEMERAL; log at startup."
  consumed_by: "31.B-5b (uses flag for legacy preservation), 31.B-5c/d/e (use flag for new paths)"
  inputs_expected: "LibsGate PASSED (libs importable + cross-contract verified)"
  outputs_guaranteed: "auto-runner-jira.py L~125 region: module constant `USE_EPHEMERAL: bool` exists; reads from env via `os.environ.get('RUNNER_USE_EPHEMERAL_CLONE', '0') == '1'`; startup log emits the value."
  non_goals:
    - "DO NOT change any pickup logic — 5b owns preserve, 5c/d/e wire new"
    - "DO NOT use ephemeral_clone/branch_naming/pickup_mutex libs yet — 5c/d/e wire them"
    - "DO NOT modify jira_dispatch.py — 5c/d/e own (different ticket scope)"
boundaries:
  loc_delta_max: 30, files_touched_max: 1
  required_paths: [auto-runner-jira.py]
  forbidden_paths: [backend/agents/jira_dispatch.py, backend/runner/, auto-runner-multi.py]
  test_scope: defer-to-integration
  destructive_op_classes: []
  execution_mode: structural-only
  dependency_artifacts: [LibsGate PASS evidence]
  mutex_with: []
  interface_contract:
    inputs_from_deps: [libraries ready (LibsGate)]
    outputs_for_downstream:
      - "module-level `USE_EPHEMERAL: bool` constant in auto-runner-jira.py"
      - "startup log 'RUNNER_USE_EPHEMERAL_CLONE=<value>'"
ac.code:
  - {desc: "USE_EPHEMERAL constant in auto-runner-jira.py", verify: {command: "grep -cE '^USE_EPHEMERAL\\s*=' auto-runner-jira.py", expect_stdout_match: "[1-9]"}}
  - {desc: "reads env var", verify: {command: "grep -cE \"RUNNER_USE_EPHEMERAL_CLONE\" auto-runner-jira.py", expect_stdout_match: "[1-9]"}}
  - {desc: "default is 0/False", verify: {command: "grep -cE \"RUNNER_USE_EPHEMERAL_CLONE.*'0'|RUNNER_USE_EPHEMERAL_CLONE.*\\\"0\\\"\" auto-runner-jira.py", expect_stdout_match: "[1-9]"}}
go_live: T+0.5d after LibsGate
```

### 31.B-5b — Preserve legacy worktree path under flag=0

```yaml
tier: S, prefer: claude, class: subscription-claude
area_labels: [area:backend, area:tooling]
blockedBy: [5a]
context_hint:
  produces: |
    Patches to jira_dispatch.py at the worktree-using functions (`_ephemeral_worktree_dir` L649, `cleanup_ephemeral_worktree` L654, the pickup orchestration around `pre_pickup_ok` L2603+). Wrap each worktree-touching block in `if not USE_EPHEMERAL:` checks where the USE_EPHEMERAL flag is imported from auto-runner-jira.py.
    Symbol scope: ONLY the worktree path callers, NOT the helpers themselves (they stay unchanged). Approximate edit zones: jira_dispatch.py L640-720 (worktree helpers — leave intact) + the call sites that invoke them.
  consumed_by: "31.B-LegacySmoke (proves flag=0 still works), 31.B-5c/d/e (each adds `else:` block adjacent to the `if not USE_EPHEMERAL:`)"
  inputs_expected: "USE_EPHEMERAL flag importable from auto-runner-jira.py (5a)"
  outputs_guaranteed: "Existing worktree pickup logic intact + reachable ONLY when flag=0; `else:` placeholders added (with `raise NotImplementedError('5c/5d/5e own')`) for 5c-5e to fill"
  non_goals:
    - "DO NOT delete worktree helpers (`_ephemeral_worktree_dir`, `cleanup_ephemeral_worktree` etc — they stay forever as fallback until Phase 31.K)"
    - "DO NOT add new behavior — 5c/d/e own"
    - "DO NOT modify auto-runner-jira.py — 5a owns the flag declaration"
boundaries:
  loc_delta_max: 80, files_touched_max: 1
  required_paths: [backend/agents/jira_dispatch.py]
  forbidden_paths: [auto-runner-jira.py, backend/runner/, auto-runner-multi.py]
  test_scope: defer-to-integration
  destructive_op_classes: []
  execution_mode: structural-only
  dependency_artifacts: [auto-runner-jira.py post-5a]
  mutex_with: [5c, 5d, 5e, 6cd, 7c]   # all touch jira_dispatch.py
  interface_contract:
    inputs_from_deps: [USE_EPHEMERAL importable]
    outputs_for_downstream:
      - "jira_dispatch.py has `if not USE_EPHEMERAL:` guards on worktree call sites"
      - "matching `else: raise NotImplementedError('5c/5d/5e own')` placeholders"
      - "MIXED CUTOVER INVARIANT: both branches call the same claim_ticket_atomic, so legacy + new runners cannot race on the same ticket"
ac.code:
  - {desc: "USE_EPHEMERAL imported in jira_dispatch.py", verify: {command: "grep -cE 'from .*auto.runner.jira import.*USE_EPHEMERAL|import.*USE_EPHEMERAL' backend/agents/jira_dispatch.py", expect_stdout_match: "[1-9]"}}
  - {desc: "`if not USE_EPHEMERAL:` guards added", verify: {command: "grep -cE 'if not USE_EPHEMERAL:' backend/agents/jira_dispatch.py", expect_stdout_match: "[1-9]"}}
  - {desc: "matching `else:` placeholders with NotImplementedError", verify: {command: "grep -cE 'else:\\s*$|NotImplementedError.*5c|NotImplementedError.*31.B' backend/agents/jira_dispatch.py", expect_stdout_match: "[1-9]"}}
  - {desc: "did NOT delete worktree helpers (sanity)", verify: {command: "grep -cE '^def _ephemeral_worktree_dir|^def cleanup_ephemeral_worktree' backend/agents/jira_dispatch.py", expect_stdout_match: "^[2-9]$"}}
go_live: T+0.5d after 5a
```

### 31.B-LegacySmoke — Legacy flag=0 path one-pickup smoke (INTERMEDIATE GATE)

```yaml
tier: S, prefer: claude, class: subscription-claude
area_labels: [area:tests, area:devops]
blockedBy: [5b]
context_hint:
  produces: "scripts/sprint-s12/phase31b-legacy-smoke.sh + docs/audit/AUDIT-31-phase-B-evidence/legacy-smoke.json"
  consumed_by: "Gates entry to 5c"
  inputs_expected: "5b wrapped jira_dispatch.py worktree paths in `if not USE_EPHEMERAL:`"
  outputs_guaranteed: "One legacy pickup completes successfully: file a no-op test ticket → runner with flag=0 claims/branches/pushes/cleans up; evidence emitted"
  non_goals:
    - "DO NOT modify jira_dispatch.py or auto-runner-jira.py"
    - "DO NOT enable flag=1 (that's 5c-5e)"
boundaries:
  loc_delta_max: 200, files_touched_max: 2
  required_paths: [scripts/sprint-s12/phase31b-legacy-smoke.sh, docs/audit/AUDIT-31-phase-B-evidence/legacy-smoke.json]
  forbidden_paths: [auto-runner-jira.py, backend/agents/jira_dispatch.py, backend/runner/]
  test_scope: inline
  destructive_op_classes: [state-append]
  execution_mode: requires-WSL
  dependency_artifacts: [post-5b state of jira_dispatch.py]
  mutex_with: []
  interface_contract:
    inputs_from_deps: [USE_EPHEMERAL=0 environment + ticket can be picked up]
    outputs_for_downstream: ["legacy-smoke.json with `pickup_complete: true`"]
ac.code:
  - {desc: "smoke script exists", verify: {command: "test -x scripts/sprint-s12/phase31b-legacy-smoke.sh", expect_exit_code: 0}}
  - {desc: "evidence emitted", verify: {command: "jq -r .pickup_complete docs/audit/AUDIT-31-phase-B-evidence/legacy-smoke.json", expect_stdout_match: "^true$"}, run_as: operator}
go_live: T+0.5d after 5b
```

### 31.B-5c — Wire ephemeral_clone (jira_dispatch.py flag=1 branch)

```yaml
tier: S, prefer: claude, class: subscription-claude
area_labels: [area:backend, area:tooling]
blockedBy: [LegacySmoke, 2bc]
context_hint:
  produces: |
    Patches to jira_dispatch.py at the `else:` placeholders (from 5b). Specifically: replace the `NotImplementedError` for clone-creation with calls to `backend.runner.ephemeral_clone.create_pickup_clone()` and `cleanup_pickup_clone()`. Cleanup placed in try/finally so it runs on success AND failure.
    Symbol scope: ONLY the `else:` branches placed by 5b. DO NOT add new functions; DO NOT alter the `if not USE_EPHEMERAL:` branch (5b owns).
  consumed_by: "31.B-5d (uses pickup_dir variable)"
  inputs_expected: "5b's else-placeholders + ephemeral_clone library (2bc)"
  outputs_guaranteed: "When flag=1: `pickup_dir = create_pickup_clone(instance, ticket, source_repo_url)` runs; cleanup invoked in `finally` block (success AND failure paths)"
  non_goals:
    - "DO NOT modify auto-runner-jira.py"
    - "DO NOT wire branch_naming — 5d owns next"
    - "DO NOT wire pickup_mutex — 5e owns next"
    - "DO NOT alter the `if not USE_EPHEMERAL:` (legacy) branch"
boundaries:
  loc_delta_max: 80, files_touched_max: 1
  required_paths: [backend/agents/jira_dispatch.py]
  forbidden_paths: [auto-runner-jira.py, backend/runner/, auto-runner-multi.py]
  test_scope: defer-to-integration
  destructive_op_classes: [tmp-delete]   # cleanup deletes /tmp dirs
  execution_mode: structural-only
  dependency_artifacts: [jira_dispatch.py post-5b, backend/runner/ephemeral_clone.py]
  mutex_with: [5b, 5d, 5e, 6cd, 7c]   # all touch jira_dispatch.py
  interface_contract:
    inputs_from_deps: [`else:` placeholders + create_pickup_clone callable]
    outputs_for_downstream:
      - "in flag=1 branch: pickup_dir = create_pickup_clone(...)"
      - "cleanup_pickup_clone(pickup_dir) in finally"
      - "variable `pickup_dir: Path` is in-scope for 5d to use"
ac.code:
  - {desc: "imports ephemeral_clone", verify: {command: "grep -cE 'from backend.runner.ephemeral_clone import|import.*ephemeral_clone' backend/agents/jira_dispatch.py", expect_stdout_match: "[1-9]"}}
  - {desc: "calls create_pickup_clone in else branch", verify: {command: "grep -c 'create_pickup_clone(' backend/agents/jira_dispatch.py", expect_stdout_match: "[1-9]"}}
  - {desc: "cleanup in finally block", verify: {command: "grep -cE 'cleanup_pickup_clone.*finally|finally:.*cleanup_pickup_clone' backend/agents/jira_dispatch.py", expect_stdout_match: "[1-9]"}}
  - {desc: "NO change to `if not USE_EPHEMERAL:` branch (codex Q2 drift check)", verify: {command: "git diff HEAD~1 HEAD backend/agents/jira_dispatch.py | grep -cE '^[+\\-]\\s*if not USE_EPHEMERAL:'", expect_stdout_match: "^0$"}}
go_live: T+0.5d after LegacySmoke + 2bc
```

### 31.B-5d — Wire branch_naming (jira_dispatch.py flag=1 branch)

```yaml
tier: S, prefer: claude, class: subscription-claude
area_labels: [area:backend, area:tooling]
blockedBy: [5c, 1bc]   # NOTE: blockedBy 5c not 5b (codex DAG fix)
context_hint:
  produces: "Patches to jira_dispatch.py — in the flag=1 branch (post-5c), call `format_branch_name()` for new branch creation. Symbol scope: same `else:` region 5c populated."
  consumed_by: "31.B-5e (mutex uses branch name in claim context)"
  inputs_expected: "5c's ephemeral clone setup; branch_naming lib (1bc)"
  outputs_guaranteed: "Flag=1 path produces branches `feature/{TICKET}-{INSTANCE}-pid{PID}-{EPOCH}-runner`"
  non_goals:
    - "DO NOT modify the `if not USE_EPHEMERAL:` legacy branch"
    - "DO NOT alter ephemeral_clone wiring (5c owns)"
    - "DO NOT wire pickup_mutex — 5e owns next"
boundaries:
  loc_delta_max: 50, files_touched_max: 1
  required_paths: [backend/agents/jira_dispatch.py]
  forbidden_paths: [auto-runner-jira.py, backend/runner/, auto-runner-multi.py]
  test_scope: defer-to-integration
  destructive_op_classes: []
  execution_mode: structural-only
  dependency_artifacts: [jira_dispatch.py post-5c, backend/runner/branch_naming.py]
  mutex_with: [5b, 5c, 5e, 6cd, 7c]
  interface_contract:
    inputs_from_deps: [pickup_dir from 5c, format_branch_name from 1bc]
    outputs_for_downstream:
      - "branch_name = format_branch_name(ticket, instance, pid, epoch_us)"
      - "passed to git checkout -b"
ac.code:
  - {desc: "imports format_branch_name", verify: {command: "grep -cE 'from backend.runner.branch_naming import.*format_branch_name' backend/agents/jira_dispatch.py", expect_stdout_match: "[1-9]"}}
  - {desc: "calls format_branch_name in flag=1 path", verify: {command: "grep -c 'format_branch_name(' backend/agents/jira_dispatch.py", expect_stdout_match: "[1-9]"}}
  - {desc: "NO change to `if not USE_EPHEMERAL:` branch", verify: {command: "git diff HEAD~1 HEAD backend/agents/jira_dispatch.py | grep -cE '^[+\\-]\\s*if not USE_EPHEMERAL:'", expect_stdout_match: "^0$"}}
go_live: T+0.5d after 5c + 1bc
```

### 31.B-5e — Wire pickup_mutex (jira_dispatch.py flag=1 branch)

```yaml
tier: S, prefer: claude, class: subscription-claude
area_labels: [area:backend, area:tooling]
blockedBy: [5d, 3bc]   # NOTE: blockedBy 5d not 5b (codex DAG fix)
context_hint:
  produces: "Patches to jira_dispatch.py — in flag=1 branch, use `claim_with_fencing_token()` / `verify_holds_lock()` from pickup_mutex. Symbol scope: same `else:` region from 5c/5d."
  consumed_by: "31.B-SingleDryGate, 31.B-Integration"
  inputs_expected: "5d's branch_naming wiring; pickup_mutex lib (3bc)"
  outputs_guaranteed: "Flag=1 path: claim via fencing token; before any state-mutating action, verify_holds_lock() returns True; release on success/failure"
  non_goals:
    - "DO NOT modify legacy `if not USE_EPHEMERAL:` branch (codex MIXED CUTOVER invariant: legacy still calls existing claim_ticket_atomic directly; no double-claim possible)"
    - "DO NOT alter ephemeral_clone (5c) or branch_naming (5d) wiring"
boundaries:
  loc_delta_max: 60, files_touched_max: 1
  required_paths: [backend/agents/jira_dispatch.py]
  forbidden_paths: [auto-runner-jira.py, backend/runner/, auto-runner-multi.py]
  test_scope: defer-to-integration
  destructive_op_classes: []
  execution_mode: structural-only
  dependency_artifacts: [jira_dispatch.py post-5d, backend/runner/pickup_mutex.py]
  mutex_with: [5b, 5c, 5d, 6cd, 7c]
  interface_contract:
    inputs_from_deps: [branch_name from 5d, pickup_mutex callable]
    outputs_for_downstream:
      - "claim_result = claim_with_fencing_token(client, key, instance, pid)"
      - "verify_holds_lock(client, key, token) called before mutating actions"
      - "release_claim() in finally"
ac.code:
  - {desc: "imports pickup_mutex", verify: {command: "grep -cE 'from backend.runner.pickup_mutex import' backend/agents/jira_dispatch.py", expect_stdout_match: "[1-9]"}}
  - {desc: "calls claim_with_fencing_token", verify: {command: "grep -c 'claim_with_fencing_token(' backend/agents/jira_dispatch.py", expect_stdout_match: "[1-9]"}}
  - {desc: "calls verify_holds_lock", verify: {command: "grep -c 'verify_holds_lock(' backend/agents/jira_dispatch.py", expect_stdout_match: "[1-9]"}}
  - {desc: "NO change to `if not USE_EPHEMERAL:` branch (MIXED CUTOVER invariant)", verify: {command: "git diff HEAD~1 HEAD backend/agents/jira_dispatch.py | grep -cE '^[+\\-]\\s*if not USE_EPHEMERAL:'", expect_stdout_match: "^0$"}}
go_live: T+0.5d after 5d + 3bc
```

### 31.B-6a — Scheduler logic (coordinator_scheduler.py)

```yaml
tier: S, prefer: claude, class: subscription-claude
area_labels: [area:backend, area:tooling]
blockedBy: [4bc]
context_hint:
  produces: "backend/runner/coordinator_scheduler.py — pick_next_ticket(state, available_instances) function"
  consumed_by: "31.B-6b (tests), 31.B-7ab (extends with prefer routing)"
  inputs_expected: "coordinator_state from 4bc"
  outputs_guaranteed: "pick_next_ticket(state: CoordinatorState, available_instances: List[str]) → Optional[Ticket]; honors capacity (C1/C2/C4); deterministic ordering for tests"
  non_goals:
    - "DO NOT implement prefer routing — 7ab owns"
    - "DO NOT implement heartbeat — 6cd owns"
    - "DO NOT modify coordinator_state"
boundaries:
  loc_delta_max: 200, files_touched_max: 1
  required_paths: [backend/runner/coordinator_scheduler.py]
  forbidden_paths: [backend/runner/coordinator_state.py, backend/runner/branch_naming.py, backend/runner/ephemeral_clone.py, backend/runner/pickup_mutex.py, auto-runner-jira.py, backend/agents/jira_dispatch.py]
  test_scope: defer-to-integration
  destructive_op_classes: []
  execution_mode: unit-testable
  dependency_artifacts: [backend/runner/coordinator_state.py]
  mutex_with: []
  interface_contract:
    inputs_from_deps: [CoordinatorState dataclass]
    outputs_for_downstream: ["pick_next_ticket(state, available_instances) -> Optional[Ticket]"]
ac.code:
  - {desc: "module + fn importable", verify: {command: "python3 -c 'from backend.runner.coordinator_scheduler import pick_next_ticket'", expect_exit_code: 0}}
  - {desc: "handles 1/2/4 capacity", verify: {command: "grep -cE 'C1|C2|C4|available_instances|capacity' backend/runner/coordinator_scheduler.py", expect_stdout_match: "^[3-9]$|^[1-9][0-9]+$"}}
go_live: T+1d after 4bc
```

### 31.B-6b — Scheduler unit tests

```yaml
tier: S, prefer: claude, class: subscription-claude
area_labels: [area:tests, area:backend]
blockedBy: [6a]
context_hint:
  produces: "backend/runner/tests/test_coordinator_scheduler.py"
  consumed_by: "CI"
  inputs_expected: "pick_next_ticket from 6a"
  outputs_guaranteed: ">=6 tests: C4-fills-all, C2-fills-pair, C1-single, no-runners-None, no-tickets-None, deterministic-ordering"
  non_goals:
    - "DO NOT modify scheduler"
    - "DO NOT add prefer-routing tests — 7d owns"
boundaries:
  loc_delta_max: 250, files_touched_max: 1
  required_paths: [backend/runner/tests/test_coordinator_scheduler.py]
  forbidden_paths: [backend/runner/coordinator_scheduler.py]
  test_scope: inline
  destructive_op_classes: []
  execution_mode: unit-testable
  dependency_artifacts: [backend/runner/coordinator_scheduler.py]
  mutex_with: []
  interface_contract:
    inputs_from_deps: [pick_next_ticket]
    outputs_for_downstream: [>=6 tests pass]
ac.code:
  - {desc: "tests >=6", verify: {command: "pytest --collect-only backend/runner/tests/test_coordinator_scheduler.py 2>&1 | grep -c 'test_'", expect_stdout_match: "^[6-9]$|^[1-9][0-9]+$"}}
  - {desc: "pass", verify: {command: "pytest backend/runner/tests/test_coordinator_scheduler.py -v", expect_exit_code: 0, timeout_seconds: 30}}
go_live: T+0.5d after 6a
```

### 31.B-6cd — Heartbeat emit + stuck-runner detect (MERGED v1 6c+6d)

```yaml
tier: S, prefer: codex, class: subscription-codex
area_labels: [area:backend, area:tooling]
blockedBy: [4bc]
context_hint:
  produces: "backend/runner/coordinator_heartbeat.py + backend/runner/tests/test_coordinator_heartbeat.py — emit_heartbeat(instance) + read_heartbeats() + detect_stuck_runners(threshold_seconds=600). Codex merged 6c+6d because emit and detect form one cohesive module."
  consumed_by: "Operator alerting (Phase 31.C); 31.B-Integration"
  inputs_expected: "coordinator_state from 4bc"
  outputs_guaranteed: "3 functions in one module; >=4 tests (emit, read, detect stale, detect not-stale)"
  non_goals:
    - "DO NOT add alerting — Phase 31.C owns"
    - "DO NOT auto-kill stuck runners — operator decision"
boundaries:
  loc_delta_max: 200, files_touched_max: 2
  required_paths: [backend/runner/coordinator_heartbeat.py, backend/runner/tests/test_coordinator_heartbeat.py]
  forbidden_paths: [backend/runner/coordinator_state.py, backend/runner/coordinator_scheduler.py, auto-runner-jira.py, backend/agents/jira_dispatch.py]
  test_scope: inline
  destructive_op_classes: [state-append]
  execution_mode: unit-testable
  dependency_artifacts: [backend/runner/coordinator_state.py]
  mutex_with: []
  interface_contract:
    inputs_from_deps: [with_lock from coordinator_state]
    outputs_for_downstream:
      - "emit_heartbeat(instance: str) -> None"
      - "read_heartbeats() -> Dict[str, datetime]"
      - "detect_stuck_runners(threshold_seconds: int = 600) -> List[str]"
ac.code:
  - {desc: "3 fns importable", verify: {command: "python3 -c 'from backend.runner.coordinator_heartbeat import emit_heartbeat, read_heartbeats, detect_stuck_runners'", expect_exit_code: 0}}
  - {desc: "default threshold 600s", verify: {command: "grep -cE 'threshold_seconds.*=.*600' backend/runner/coordinator_heartbeat.py", expect_stdout_match: "[1-9]"}}
  - {desc: "tests >=4", verify: {command: "pytest --collect-only backend/runner/tests/test_coordinator_heartbeat.py 2>&1 | grep -c 'test_'", expect_stdout_match: "^[4-9]$|^[1-9][0-9]+$"}}
  - {desc: "pass", verify: {command: "pytest backend/runner/tests/test_coordinator_heartbeat.py -v", expect_exit_code: 0, timeout_seconds: 30}}
go_live: T+1d after 4bc
```

### 31.B-7ab — prefer routing + class override (MERGED v1 7a+7b)

```yaml
tier: S, prefer: claude, class: subscription-claude
area_labels: [area:backend, area:tooling]
blockedBy: [6a]
context_hint:
  produces: "Patch to backend/runner/coordinator_scheduler.py — extend pick_next_ticket to honor `prefer:claude`/`prefer:codex` AND `class:subscription-*` override. Codex merged 7a+7b because precedence rule should be defined as one piece, not stages."
  consumed_by: "31.B-7c (audit log), 31.B-7d (tests)"
  inputs_expected: "pick_next_ticket from 6a"
  outputs_guaranteed: "Precedence: explicit `class:subscription-claude/codex` > `prefer:claude/codex` > first-available. Single-pass routing logic with documented decision tree."
  non_goals:
    - "DO NOT add audit log — 7c owns"
    - "DO NOT modify function signature"
boundaries:
  loc_delta_max: 100, files_touched_max: 1
  required_paths: [backend/runner/coordinator_scheduler.py]
  forbidden_paths: [backend/runner/coordinator_state.py, backend/runner/coordinator_heartbeat.py, auto-runner-jira.py, backend/agents/jira_dispatch.py]
  test_scope: defer-to-integration
  destructive_op_classes: []
  execution_mode: unit-testable
  dependency_artifacts: [coordinator_scheduler.py from 6a]
  mutex_with: [7c]   # 7c also touches coordinator_scheduler.py
  interface_contract:
    inputs_from_deps: [pick_next_ticket from 6a]
    outputs_for_downstream:
      - "pick_next_ticket honors prefer + class precedence"
      - "decision tree documented in comments"
ac.code:
  - {desc: "prefer routing", verify: {command: "grep -cE 'prefer:claude|prefer:codex' backend/runner/coordinator_scheduler.py", expect_stdout_match: "^[2-9]$|^[1-9][0-9]+$"}}
  - {desc: "class override", verify: {command: "grep -cE 'class:subscription-(claude|codex)' backend/runner/coordinator_scheduler.py", expect_stdout_match: "^[2-9]$|^[1-9][0-9]+$"}}
go_live: T+0.5d after 6a
```

### 31.B-7c — Routing audit log (JSONL)

```yaml
tier: S, prefer: codex, class: subscription-codex
area_labels: [area:backend, area:tooling]
blockedBy: [7ab]
context_hint:
  produces: "Patch to coordinator_scheduler.py — emit JSONL to ~/.cache/omnisight/routing-audit.jsonl per pick_next_ticket call"
  consumed_by: "operator debug; Phase 31.G prometheus"
  inputs_expected: "pick_next_ticket from 7ab"
  outputs_guaranteed: "Each scheduling decision emits JSONL `{timestamp, ticket, decided_instance, prefer_label, class_label, override_applied, rationale}`"
  non_goals:
    - "DO NOT modify routing logic (7ab owns)"
    - "DO NOT add prometheus metrics (Phase 31.G)"
boundaries:
  loc_delta_max: 60, files_touched_max: 1
  required_paths: [backend/runner/coordinator_scheduler.py]
  forbidden_paths: [backend/runner/coordinator_state.py, backend/runner/coordinator_heartbeat.py, auto-runner-jira.py, backend/agents/jira_dispatch.py]
  test_scope: defer-to-integration
  destructive_op_classes: [state-append]
  execution_mode: unit-testable
  dependency_artifacts: [coordinator_scheduler.py post-7ab]
  mutex_with: [7ab]
  interface_contract:
    inputs_from_deps: [pick_next_ticket result]
    outputs_for_downstream:
      - "JSONL log at ~/.cache/omnisight/routing-audit.jsonl"
ac.code:
  - {desc: "emits to routing-audit.jsonl", verify: {command: "grep -c 'routing-audit.jsonl' backend/runner/coordinator_scheduler.py", expect_stdout_match: "[1-9]"}}
  - {desc: "schema fields", verify: {command: "grep -cE 'decided_instance|override_applied|rationale' backend/runner/coordinator_scheduler.py", expect_stdout_match: "^[2-9]$|^[1-9][0-9]+$"}}
go_live: T+0.5d after 7ab
```

### 31.B-7d — Routing logic tests

```yaml
tier: S, prefer: claude, class: subscription-claude
area_labels: [area:tests, area:backend]
blockedBy: [7ab]
context_hint:
  produces: "backend/runner/tests/test_coordinator_routing.py"
  consumed_by: "CI"
  inputs_expected: "7ab prefer + class merged routing"
  outputs_guaranteed: ">=8 tests: prefer-claude→claude, prefer-codex→codex, no-prefer-first-avail, class-overrides-prefer, class-only-routes-correctly, mismatched-class-rejects, audit-emitted, two-tickets-routed-independently"
  non_goals:
    - "DO NOT modify scheduler"
    - "DO NOT test capacity — 6b owns"
boundaries:
  loc_delta_max: 250, files_touched_max: 1
  required_paths: [backend/runner/tests/test_coordinator_routing.py]
  forbidden_paths: [backend/runner/coordinator_scheduler.py]
  test_scope: inline
  destructive_op_classes: []
  execution_mode: unit-testable
  dependency_artifacts: [backend/runner/coordinator_scheduler.py]
  mutex_with: []
  interface_contract:
    inputs_from_deps: [pick_next_ticket with prefer+class merged]
    outputs_for_downstream: [>=8 tests pass]
ac.code:
  - {desc: "tests >=8", verify: {command: "pytest --collect-only backend/runner/tests/test_coordinator_routing.py 2>&1 | grep -c 'test_'", expect_stdout_match: "^[8-9]$|^[1-9][0-9]+$"}}
  - {desc: "pass", verify: {command: "pytest backend/runner/tests/test_coordinator_routing.py -v", expect_exit_code: 0, timeout_seconds: 30}}
go_live: T+0.5d after 7ab
```

### 31.B-SingleDryGate — Single-runner dry-pickup on flag=1 (INTERMEDIATE GATE)

```yaml
tier: S, prefer: claude, class: subscription-claude
area_labels: [area:tests, area:devops, area:backend]
blockedBy: [5e, 6cd, 7d]
context_hint:
  produces: "scripts/sprint-s12/phase31b-single-dry-gate.sh + docs/audit/AUDIT-31-phase-B-evidence/single-dry-gate.json"
  consumed_by: "Gates entry to 9-Doc (B-0 bootstrap)"
  inputs_expected: "5e wiring complete (full flag=1 path); 6cd heartbeat; 7d routing tests green"
  outputs_guaranteed: "ONE runner with flag=1 + DRY_RUN env runs through full pickup logic WITHOUT JIRA mutation or Gerrit push. Confirms: ephemeral clone created at /tmp, branch name correct format, fencing token minted, prefer hint routed, heartbeat emitted, cleanup runs. Evidence JSON."
  non_goals:
    - "DO NOT modify any module"
    - "DO NOT trigger real JIRA/Gerrit (this is dry)"
boundaries:
  loc_delta_max: 250, files_touched_max: 2
  required_paths: [scripts/sprint-s12/phase31b-single-dry-gate.sh, docs/audit/AUDIT-31-phase-B-evidence/single-dry-gate.json]
  forbidden_paths: [auto-runner-jira.py, backend/agents/jira_dispatch.py, backend/runner/]
  test_scope: inline
  destructive_op_classes: [tmp-delete, state-append]
  execution_mode: requires-WSL
  dependency_artifacts: [post-5e jira_dispatch.py + 6cd + 7d]
  mutex_with: []
  interface_contract:
    inputs_from_deps: [flag=1 + DRY_RUN supported in jira_dispatch.py]
    outputs_for_downstream:
      - "single-dry-gate.json: {clone_created, branch_name_correct, fencing_token_minted, prefer_routed, heartbeat_emitted, cleanup_ran}"
ac.code:
  - {desc: "script exists", verify: {command: "test -x scripts/sprint-s12/phase31b-single-dry-gate.sh", expect_exit_code: 0}}
  - {desc: "ALL gate checks PASS", verify: {command: "jq -r '. | to_entries | map(select(.value==false)) | length' docs/audit/AUDIT-31-phase-B-evidence/single-dry-gate.json", expect_stdout_match: "^0$"}, run_as: operator}
go_live: T+0.5d after 5e + 6cd + 7d
```

### 31.B-8a — Existing-branch rename utility

```yaml
tier: S, prefer: codex, class: subscription-codex + operator-prepare-only
area_labels: [area:tooling, area:devops]
blockedBy: [1bc]
context_hint:
  produces: "scripts/runner-migration/rename-existing-branches.py"
  consumed_by: "31.B-8c (tests), operator at cutover"
  inputs_expected: "branch_naming.py format_branch_name (1bc)"
  outputs_guaranteed: "Survey emits plan JSONL `{old_branch, new_branch, ticket, instance_guess, action}`; --apply renames refs (push new + delete old via Gerrit API); default is dry"
  non_goals:
    - "DO NOT auto-rename during pickup"
    - "DO NOT delete without --apply"
    - "DO NOT touch closed Gerrit changes"
boundaries:
  loc_delta_max: 200, files_touched_max: 1
  required_paths: [scripts/runner-migration/rename-existing-branches.py]
  forbidden_paths: [auto-runner-jira.py, backend/agents/jira_dispatch.py, backend/runner/]
  test_scope: defer-to-integration
  destructive_op_classes: [git-ref-rewrite]   # --apply rewrites refs
  execution_mode: unit-testable
  dependency_artifacts: [backend/runner/branch_naming.py]
  mutex_with: []
  interface_contract:
    inputs_from_deps: [format_branch_name from 1bc]
    outputs_for_downstream: [JSONL plan + --apply rewrites]
ac.code:
  - {desc: "has --apply flag", verify: {command: "grep -cE '\\-\\-apply' scripts/runner-migration/rename-existing-branches.py", expect_stdout_match: "[1-9]"}}
  - {desc: "default is dry-run", verify: {command: "grep -cE 'default.*False.*apply|apply.*default.*False' scripts/runner-migration/rename-existing-branches.py", expect_stdout_match: "[1-9]"}}
go_live: T+1d after 1bc
```

### 31.B-8b — Stale /tmp/runner-pickup cleanup script

```yaml
tier: S, prefer: codex, class: subscription-codex
area_labels: [area:tooling, area:devops]
blockedBy: [2bc]
context_hint:
  produces: "scripts/runner-migration/cleanup-stale-pickup-dirs.py"
  consumed_by: "31.B-8c (tests), future systemd timer"
  inputs_expected: "list_stale from ephemeral_clone (2bc)"
  outputs_guaranteed: "List stale /tmp/runner-pickup/ >24h; --apply deletes; suitable as systemd timer"
  non_goals:
    - "DO NOT add systemd unit"
    - "DO NOT delete without --apply"
boundaries:
  loc_delta_max: 80, files_touched_max: 1
  required_paths: [scripts/runner-migration/cleanup-stale-pickup-dirs.py]
  forbidden_paths: [auto-runner-jira.py, backend/agents/jira_dispatch.py, backend/runner/]
  test_scope: defer-to-integration
  destructive_op_classes: [tmp-delete]
  execution_mode: unit-testable
  dependency_artifacts: [backend/runner/ephemeral_clone.py]
  mutex_with: []
  interface_contract:
    inputs_from_deps: [list_stale]
    outputs_for_downstream: [list + --apply deletes]
ac.code:
  - {desc: "uses list_stale", verify: {command: "grep -c 'list_stale' scripts/runner-migration/cleanup-stale-pickup-dirs.py", expect_stdout_match: "[1-9]"}}
  - {desc: "has --apply", verify: {command: "grep -c '\\-\\-apply' scripts/runner-migration/cleanup-stale-pickup-dirs.py", expect_stdout_match: "[1-9]"}}
go_live: T+0.5d after 2bc
```

### 31.B-8c — Migration utility tests

```yaml
tier: S, prefer: claude, class: subscription-claude
area_labels: [area:tests, area:tooling]
blockedBy: [8a, 8b]
context_hint:
  produces: "scripts/runner-migration/tests/test_migration_utils.py (mocked Gerrit + tmp_path)"
  consumed_by: "CI"
  inputs_expected: "rename + cleanup scripts (8a/8b)"
  outputs_guaranteed: ">=6 tests: rename-plan-correct, rename-applied-correct (mocked Gerrit), rename-dry-default, cleanup-finds-stale, cleanup-applies-deletes, cleanup-dry-default"
  non_goals:
    - "DO NOT modify the migration scripts"
    - "DO NOT hit real Gerrit (mocked)"
boundaries:
  loc_delta_max: 300, files_touched_max: 1
  required_paths: [scripts/runner-migration/tests/test_migration_utils.py]
  forbidden_paths: [scripts/runner-migration/rename-existing-branches.py, scripts/runner-migration/cleanup-stale-pickup-dirs.py]
  test_scope: inline
  destructive_op_classes: []
  execution_mode: unit-testable
  dependency_artifacts: [migration scripts]
  mutex_with: []
  interface_contract:
    inputs_from_deps: [scripts importable]
    outputs_for_downstream: [>=6 tests pass]
ac.code:
  - {desc: "tests >=6", verify: {command: "pytest --collect-only scripts/runner-migration/tests/test_migration_utils.py 2>&1 | grep -c 'test_'", expect_stdout_match: "^[6-9]$|^[1-9][0-9]+$"}}
  - {desc: "pass", verify: {command: "pytest scripts/runner-migration/tests/test_migration_utils.py -v", expect_exit_code: 0, timeout_seconds: 30}}
go_live: T+0.5d after 8a + 8b
```

### 31.B-9-Doc — B-0 operator bootstrap runbook

```yaml
tier: S, prefer: claude, class: subscription-claude
area_labels: [area:docs, area:devops]
blockedBy: [SingleDryGate]
context_hint:
  produces: "docs/sop/runner-phase-31b-bootstrap.md (B-0 chicken-and-egg protocol)"
  consumed_by: "31.B-9-Test, operator at cutover"
  inputs_expected: "SingleDryGate PASS (dry pickup proves new path works in isolation)"
  outputs_guaranteed: |
    Runbook for first-runner manual bootstrap:
    1. Stop ALL 4 runners
    2. cd to ~/runner-claude-1; set RUNNER_USE_EPHEMERAL_CLONE=1 in env
    3. Run one pickup cycle in foreground (file a no-op JIRA test ticket; observe state file + heartbeat + clone create/cleanup)
    4. Validate via 9-Test verification script
    5. If clean, restart that runner via systemctl
    6. Other 3 runners stay flag=0 (cutover via 31.B-10 sequence)
  non_goals:
    - "DO NOT execute bootstrap (operator-only)"
    - "DO NOT include rolling cutover plan — 10-Doc owns"
boundaries:
  loc_delta_max: 400, files_touched_max: 1
  required_paths: [docs/sop/runner-phase-31b-bootstrap.md]
  forbidden_paths: [auto-runner-jira.py, backend/agents/jira_dispatch.py, backend/runner/]
  test_scope: none
  destructive_op_classes: []
  execution_mode: operator-rehearsal
  dependency_artifacts: [SingleDryGate PASS]
  mutex_with: []
  interface_contract:
    inputs_from_deps: [system in "flag=1 path proven dry-runnable" state]
    outputs_for_downstream: [operator can perform B-0 reproducibly]
ac.code:
  - {desc: "runbook ≥6 numbered steps", verify: {command: "grep -cE '^(##|###) Step [0-9]+|^[0-9]+\\. ' docs/sop/runner-phase-31b-bootstrap.md", expect_stdout_match: "^[6-9]$|^[1-9][0-9]+$"}}
  - {desc: "references B-0 / chicken-and-egg", verify: {command: "grep -ciE 'B-0|chicken-and-egg|bootstrap' docs/sop/runner-phase-31b-bootstrap.md", expect_stdout_match: "[1-9]"}}
go_live: T+1d after SingleDryGate
```

### 31.B-9-Test — Bootstrap verification suite (HARDENED per codex Q4)

```yaml
tier: S, prefer: claude, class: subscription-claude
area_labels: [area:tests, area:devops]
blockedBy: [9-Doc]
context_hint:
  produces: "scripts/runner-migration/verify-bootstrap.py + docs/audit/AUDIT-31-phase-B-evidence/bootstrap-verify-schema.json"
  consumed_by: "operator at B-0 step 4"
  inputs_expected: "9-Doc runbook references this; all 4 libs importable"
  outputs_guaranteed: |
    HARDENED per codex Q4 — script does REAL state write + heartbeat roundtrip + clone lifecycle, not just imports:
    1. Import all 4 libs (sanity)
    2. Write a test entry to coordinator-state.json + read back (verify atomic+flock)
    3. Emit heartbeat for `claude-1-test` instance + read back
    4. create_pickup_clone in temp path + cleanup_pickup_clone (verify lifecycle)
    5. Test fencing-token mint + parse roundtrip
    6. Emit PASS/FAIL JSONL with each step's result
  non_goals:
    - "DO NOT modify any module"
    - "DO NOT trigger a real JIRA pickup (SingleDryGate did that; this is post-bootstrap verification)"
boundaries:
  loc_delta_max: 250, files_touched_max: 2
  required_paths: [scripts/runner-migration/verify-bootstrap.py, docs/audit/AUDIT-31-phase-B-evidence/bootstrap-verify-schema.json]
  forbidden_paths: [auto-runner-jira.py, backend/agents/jira_dispatch.py, backend/runner/]
  test_scope: defer-to-integration
  destructive_op_classes: [state-append, tmp-delete]
  execution_mode: operator-rehearsal
  dependency_artifacts: [all 4 libs + scheduler + heartbeat]
  mutex_with: []
  interface_contract:
    inputs_from_deps: [libs importable + state file works]
    outputs_for_downstream: [bootstrap-verify.jsonl with PASS/FAIL per step]
ac.code:
  - {desc: "checks all 6 verify points", verify: {command: "grep -cE 'state.*write.*read|heartbeat.*roundtrip|clone.*lifecycle|fencing.*token.*mint|libs.*import' scripts/runner-migration/verify-bootstrap.py", expect_stdout_match: "^[4-9]$|^[1-9][0-9]+$"}}
  - {desc: "writes real state (NOT just import check)", verify: {command: "grep -cE 'update_state\\(|with_lock\\(|emit_heartbeat\\(|create_pickup_clone\\(' scripts/runner-migration/verify-bootstrap.py", expect_stdout_match: "^[3-9]$|^[1-9][0-9]+$"}}
go_live: T+0.5d after 9-Doc
```

### 31.B-10-Doc — Rolling cutover runbook

```yaml
tier: S, prefer: claude, class: subscription-claude
area_labels: [area:docs, area:devops]
blockedBy: [9-Test]
context_hint:
  produces: "docs/sop/runner-phase-31b-rolling-cutover.md"
  consumed_by: "31.B-10b (rollback script — built first per codex), 31.B-10a (cutover script — built second), operator"
  inputs_expected: "Bootstrap done (9-Test); first runner stable 24h"
  outputs_guaranteed: |
    Runbook:
    Day 1: claude-runner-1 already bootstrapped (9-Doc); 24h soak.
    Day 2: cutover claude-runner-2 via 10a; 24h soak.
    Day 3: cutover codex-runner-1; 24h soak.
    Day 4: cutover codex-runner-2; 24h soak.
    Day 5-11: 7-day continuous observation. Goal: 0 Missing-tree, 0 mutex races, 0 stuck-runner alerts.
    Rollback trigger criteria (call 10b): >=1 Missing-tree OR mutex race OR stuck >10min on flag=1 runner.
  non_goals:
    - "DO NOT execute (operator-only)"
    - "DO NOT remove old worktree code (Phase 31.K)"
boundaries:
  loc_delta_max: 400, files_touched_max: 1
  required_paths: [docs/sop/runner-phase-31b-rolling-cutover.md]
  forbidden_paths: [scripts/runner-migration/]
  test_scope: none
  destructive_op_classes: []
  execution_mode: operator-rehearsal
  dependency_artifacts: [9-Test PASS]
  mutex_with: []
  interface_contract:
    inputs_from_deps: [bootstrap done]
    outputs_for_downstream: [day-by-day operator playbook]
ac.code:
  - {desc: "Day 1-5 structure", verify: {command: "grep -cE '^(##|###) Day [1-5]|^Day [1-5]:' docs/sop/runner-phase-31b-rolling-cutover.md", expect_stdout_match: "^[4-9]$|^[1-9][0-9]+$"}}
  - {desc: "24h soak rule", verify: {command: "grep -ciE '24h soak|24-hour observation' docs/sop/runner-phase-31b-rolling-cutover.md", expect_stdout_match: "[1-9]"}}
  - {desc: "rollback trigger criteria documented", verify: {command: "grep -ciE 'rollback.*trigger|trigger.*rollback' docs/sop/runner-phase-31b-rolling-cutover.md", expect_stdout_match: "[1-9]"}}
go_live: T+1d after 9-Test
```

### 31.B-10b — Rollback script (BUILT FIRST per codex)

```yaml
tier: S, prefer: codex, class: subscription-codex + operator-prepare-only
area_labels: [area:tooling, area:devops]
blockedBy: [10-Doc]
context_hint:
  produces: "scripts/runner-migration/rollback-one-runner.sh"
  consumed_by: "31.B-10a (cutover may invoke rollback on failure); 31.B-CutoverReadyGate"
  inputs_expected: "Rolling cutover runbook (10-Doc)"
  outputs_guaranteed: |
    Script INSTANCE_NAME → reverts flag to 0:
    1) systemctl --user stop runner-<INSTANCE>
    2) Set RUNNER_USE_EPHEMERAL_CLONE=0 in instance env file
    3) systemctl --user start runner-<INSTANCE>
    4) Tail logs 60s; confirm legacy pickup works
    5) File `runner-cutover-rollback` JIRA incident ticket auto
    Built FIRST (before 10a) so rollback exists when 10a's exception path may need it.
  non_goals:
    - "DO NOT auto-trigger (operator manual)"
    - "DO NOT delete ephemeral /tmp state (8b cleanup owns that)"
boundaries:
  loc_delta_max: 150, files_touched_max: 1
  required_paths: [scripts/runner-migration/rollback-one-runner.sh]
  forbidden_paths: [auto-runner-jira.py, backend/agents/jira_dispatch.py, backend/runner/]
  test_scope: defer-to-integration
  destructive_op_classes: [systemd-control, env-edit]
  execution_mode: operator-rehearsal
  dependency_artifacts: [10-Doc]
  mutex_with: []
  interface_contract:
    inputs_from_deps: [cutover plan]
    outputs_for_downstream: [reverted runner + filed incident ticket]
ac.code:
  - {desc: "flips flag to 0", verify: {command: "grep -cE 'RUNNER_USE_EPHEMERAL_CLONE=0|RUNNER_USE_EPHEMERAL_CLONE=\"0\"' scripts/runner-migration/rollback-one-runner.sh", expect_stdout_match: "[1-9]"}}
  - {desc: "files JIRA incident", verify: {command: "grep -cE 'runner-cutover-rollback|file.*incident' scripts/runner-migration/rollback-one-runner.sh", expect_stdout_match: "[1-9]"}}
go_live: T+0.5d after 10-Doc
```

### 31.B-10a — Cutover script (CALLS rollback on failure)

```yaml
tier: S, prefer: codex, class: subscription-codex + operator-prepare-only
area_labels: [area:tooling, area:devops]
blockedBy: [10b]
context_hint:
  produces: "scripts/runner-migration/cutover-one-runner.sh — calls 10b's rollback on any failure"
  consumed_by: "31.B-CutoverReadyGate, operator Day 2-4"
  inputs_expected: "10b's rollback script + verify-bootstrap.py (9-Test)"
  outputs_guaranteed: |
    Script INSTANCE_NAME →:
    1) systemctl --user stop runner-<INSTANCE>
    2) Set RUNNER_USE_EPHEMERAL_CLONE=1 in env file
    3) Run verify-bootstrap.py; if FAIL → invoke rollback-one-runner.sh + exit nonzero
    4) systemctl --user start runner-<INSTANCE>
    5) Tail logs 60s; confirm pickup attempt OK; if not → invoke rollback + exit nonzero
  non_goals:
    - "DO NOT auto-cutover all runners (operator manual per runner)"
    - "DO NOT implement rollback inline — calls 10b's script"
boundaries:
  loc_delta_max: 180, files_touched_max: 1
  required_paths: [scripts/runner-migration/cutover-one-runner.sh]
  forbidden_paths: [scripts/runner-migration/rollback-one-runner.sh, auto-runner-jira.py, backend/agents/jira_dispatch.py, backend/runner/]
  test_scope: defer-to-integration
  destructive_op_classes: [systemd-control, env-edit]
  execution_mode: operator-rehearsal
  dependency_artifacts: [10b rollback script + 9-Test verify script]
  mutex_with: []
  interface_contract:
    inputs_from_deps: [rollback-one-runner.sh + verify-bootstrap.py]
    outputs_for_downstream: [one runner cut over + verified, OR rolled back + incident filed]
ac.code:
  - {desc: "takes INSTANCE arg", verify: {command: "grep -cE '\\$1|getopts.*i' scripts/runner-migration/cutover-one-runner.sh", expect_stdout_match: "[1-9]"}}
  - {desc: "requires verify-bootstrap PASS", verify: {command: "grep -c 'verify-bootstrap' scripts/runner-migration/cutover-one-runner.sh", expect_stdout_match: "[1-9]"}}
  - {desc: "invokes rollback on failure", verify: {command: "grep -c 'rollback-one-runner' scripts/runner-migration/cutover-one-runner.sh", expect_stdout_match: "[1-9]"}}
go_live: T+0.5d after 10b
```

### 31.B-CutoverReadyGate — Cutover↔rollback roundtrip on test runner (INTERMEDIATE GATE)

```yaml
tier: S, prefer: claude, class: subscription-claude
area_labels: [area:tests, area:devops]
blockedBy: [10a, 10b]
context_hint:
  produces: "scripts/sprint-s12/phase31b-cutover-gate.sh + docs/audit/AUDIT-31-phase-B-evidence/cutover-gate.json"
  consumed_by: "Gates entry to Integration ticket"
  inputs_expected: "10b + 10a scripts both exist"
  outputs_guaranteed: |
    Round-trip exercise on a TEST runner instance (NOT prod):
    1. Pick spare instance (e.g., claude-runner-test)
    2. Run cutover-one-runner.sh on it
    3. Verify flag=1 + new path active + verify-bootstrap PASS
    4. Run rollback-one-runner.sh
    5. Verify flag=0 + legacy path active + incident filed
    6. Cleanup test instance state
    7. Emit cutover-gate.json with each step
  non_goals:
    - "DO NOT run on production runners (test instance only)"
    - "DO NOT modify cutover or rollback scripts"
boundaries:
  loc_delta_max: 200, files_touched_max: 2
  required_paths: [scripts/sprint-s12/phase31b-cutover-gate.sh, docs/audit/AUDIT-31-phase-B-evidence/cutover-gate.json]
  forbidden_paths: [scripts/runner-migration/, auto-runner-jira.py, backend/agents/jira_dispatch.py, backend/runner/]
  test_scope: inline
  destructive_op_classes: [systemd-control, env-edit, state-append]
  execution_mode: requires-WSL + operator-rehearsal
  dependency_artifacts: [10a + 10b scripts]
  mutex_with: []
  interface_contract:
    inputs_from_deps: [cutover + rollback scripts available]
    outputs_for_downstream: ["cutover-gate.json with full round-trip evidence"]
ac.code:
  - {desc: "script exists", verify: {command: "test -x scripts/sprint-s12/phase31b-cutover-gate.sh", expect_exit_code: 0}}
  - {desc: "round-trip evidence schema", verify: {command: "python3 -c \"import json; d=json.load(open('docs/audit/AUDIT-31-phase-B-evidence/cutover-gate.json')); assert all(k in d for k in ['cutover_success','rollback_success','round_trip_clean'])\"", expect_exit_code: 0}, run_as: operator}
  - {desc: "round-trip clean", verify: {command: "jq -r .round_trip_clean docs/audit/AUDIT-31-phase-B-evidence/cutover-gate.json", expect_stdout_match: "^true$"}, run_as: operator}
go_live: T+1d after 10a + 10b
```

### 31.B-Integration-Doc — E2E stress-test runbook

```yaml
tier: S, prefer: claude, class: subscription-claude
area_labels: [area:docs, area:devops, area:tests]
blockedBy: [CutoverReadyGate]
context_hint:
  produces: "docs/audit/AUDIT-31-phase-B-evidence/runbook.md"
  consumed_by: "31.B-Integration (operator + 4 runners)"
  inputs_expected: "CutoverReadyGate PASS"
  outputs_guaranteed: |
    Stress-test runbook:
    1. All 4 runners on flag=1 (post rolling cutover)
    2. File 8 test tickets in S12-stress sprint, mixed prefer:* (4 claude / 4 codex)
    3. Watch routing-audit.jsonl + heartbeat + state in real time
    4. Confirm: 0 Missing-tree, 0 mutex races, prefer honored 8/8, ephemeral dirs cleaned
    5. Capture evidence integration.json
  non_goals:
    - "DO NOT execute (operator+runner driven)"
boundaries:
  loc_delta_max: 300, files_touched_max: 1
  required_paths: [docs/audit/AUDIT-31-phase-B-evidence/runbook.md]
  forbidden_paths: [scripts/runner-migration/, auto-runner-jira.py, backend/agents/jira_dispatch.py, backend/runner/]
  test_scope: none
  destructive_op_classes: []
  execution_mode: operator-rehearsal
  dependency_artifacts: [CutoverReadyGate PASS]
  mutex_with: []
  interface_contract:
    inputs_from_deps: [cutover ready proven]
    outputs_for_downstream: [runbook + evidence schema]
ac.code:
  - {desc: "runbook ≥5 numbered steps", verify: {command: "grep -cE '^(##|###) Step [0-9]+|^[0-9]+\\. ' docs/audit/AUDIT-31-phase-B-evidence/runbook.md", expect_stdout_match: "^[5-9]$|^[1-9][0-9]+$"}}
  - {desc: "references 8 concurrent tickets", verify: {command: "grep -cE '8 test|eight tests|S12-stress' docs/audit/AUDIT-31-phase-B-evidence/runbook.md", expect_stdout_match: "[1-9]"}}
go_live: T+1d after CutoverReadyGate
```

### 31.B-Integration — **E2E concurrent 4-runner stress test** (TIER:L)

```yaml
title: "AUDIT-31.B-Integration: End-to-end concurrent 4-runner stress test"
tier: L
prefer: claude
class: subscription-claude + operator-window
area_labels: [area:devops, area:tests, area:docs, area:backend, area:tooling]
type_label: type:integration
blockedBy: [ALL 32 prior siblings — expanded explicitly at filing]
context_hint:
  produces: "docs/audit/AUDIT-31-phase-B-evidence/integration.json + final commit"
  consumed_by: "Sprint S12 Phase 31.B sub-META closure; unblocks Phase 31.E"
  inputs_expected: "All 32 prior siblings closed; all 4 runners on flag=1"
  outputs_guaranteed: |
    - 8 concurrent test tickets filed → picked up by 4 runners
    - 0 Missing-tree push failures
    - 0 mutex races (every claim is single-winner per routing-audit.jsonl)
    - prefer:* honored 8/8
    - All /tmp/runner-pickup dirs cleaned (success AND failure paths)
    - Heartbeat continuous (no >600s gaps)
    - 7-day post-observation: 0 incidents
  non_goals:
    - "DO NOT 'fix' a failing component (file follow-up)"
    - "DO NOT extend Phase 31.B scope mid-test"
boundaries:
  loc_delta_max: 80, files_touched_max: 1
  required_paths: [docs/audit/AUDIT-31-phase-B-evidence/integration.json]
  forbidden_paths: [auto-runner-jira.py, backend/agents/jira_dispatch.py, backend/runner/, scripts/runner-migration/]
  test_scope: inline
  destructive_op_classes: []
  execution_mode: requires-WSL + operator-rehearsal
  dependency_artifacts: [all 32 siblings closed; 4 runners flag=1]
  mutex_with: []
  interface_contract:
    inputs_from_deps: [fully cut-over system]
    outputs_for_downstream: [integration.json → 31.B closes → 31.E unblocks]
ac.code:
  - {desc: "integration.json schema", verify: {command: "python3 -c \"import json; d=json.load(open('docs/audit/AUDIT-31-phase-B-evidence/integration.json')); assert all(k in d for k in ['concurrent_tickets','missing_tree_count','mutex_race_count','prefer_honored_count','stale_tmp_dirs','heartbeat_gaps','post_7d_health'])\"", expect_exit_code: 0}}
ac.deploy:
  - {desc: "all 4 runners flag=1", verify: {command: "jq -r '.runner_states | map(select(.flag==1)) | length' docs/audit/AUDIT-31-phase-B-evidence/integration.json", expect_stdout_match: "^4$"}, run_as: operator}
ac.integration:
  - {desc: "0 Missing-tree", verify: {command: "jq -r .missing_tree_count docs/audit/AUDIT-31-phase-B-evidence/integration.json", expect_stdout_match: "^0$"}}
  - {desc: "0 mutex races", verify: {command: "jq -r .mutex_race_count docs/audit/AUDIT-31-phase-B-evidence/integration.json", expect_stdout_match: "^0$"}}
  - {desc: "prefer 8/8", verify: {command: "jq -r .prefer_honored_count docs/audit/AUDIT-31-phase-B-evidence/integration.json", expect_stdout_match: "^8$"}}
  - {desc: "0 stale /tmp", verify: {command: "jq -r '.stale_tmp_dirs | length' docs/audit/AUDIT-31-phase-B-evidence/integration.json", expect_stdout_match: "^0$"}}
ac.exercised:
  - {desc: "7-day healthy", verify: {command: "jq -r .post_7d_health docs/audit/AUDIT-31-phase-B-evidence/integration.json", expect_stdout_match: "^healthy$"}, run_as: operator, timeout_seconds: 604800}
decision_points:
  - id: dp_any_failure
    trigger: "jq -r 'select(.missing_tree_count>0 or .mutex_race_count>0 or .prefer_honored_count<8 or (.stale_tmp_dirs|length)>0) | .overall_pass // \"fail\"' docs/audit/AUDIT-31-phase-B-evidence/integration.json"
    pause_if: "fail"
    action: file-followup
go_live: T+8d after siblings (1d execute + 7d observation)
```

---

## §6. Filing batch order (DAG)

```
L0: 1a, 2a, 3a, 4a (4 specs)
L1: 1bc, 2bc, 3bc, 4bc (4 lib+test merged)
L2: LibsGate (1)
L3: 5a, 6a, 6cd, 8a, 8b (5 — 5a after LibsGate; scheduler/heartbeat/migration utils start in parallel)
L4: 5b, 6b, 7ab, 8c (4)
L5: LegacySmoke, 7c, 7d (3)
L6: 5c (1)
L7: 5d (1)
L8: 5e (1)
L9: SingleDryGate (1)
L10: 9-Doc (1)
L11: 9-Test (1)
L12: 10-Doc (1)
L13: 10b (1, codex P0 fix: rollback before cutover)
L14: 10a (1)
L15: CutoverReadyGate (1)
L16: Integration-Doc (1)
L17: Integration (1, tier:L)
```

18 layers, 33 tickets. The 4 intermediate gates (LibsGate, LegacySmoke, SingleDryGate, CutoverReadyGate) are explicit halt-points: filing the NEXT layer requires the gate ticket to be CLOSED + JSON evidence committed.

---

## §7. Operator answers (locked 2026-05-13)

| # | Question | Locked answer |
|---|---|---|
| 1 | Coordinator daemon (ADR-0024) | **Library-first; daemon deferred to Phase 31.K** |
| 2 | Flag default flip timing | **Stays `RUNNER_USE_EPHEMERAL_CLONE=0` throughout 31.B; flip at end of Phase 31.K (legacy removal)** |
| 3 | B-0 first runner | **claude-runner-1** |
| 4 | Soak duration between cutovers | **24h** |
| 5 | Integration stress concurrent ticket count | **Default 4; optional configurations 2 / 8 / 16 via env `S12_STRESS_TICKET_COUNT`** |
| 6 | `mutex_with:` enforcement scope | **Filing-time hook for 31.B; runtime enforcement deferred to Phase 31.K** |
| 7 | `destructive_op_classes` auto-vs-confirm rule | **See §3.2.1 below — auto allowed for project-scope only; system-scope REQUIRES operator authorization (operator-prepare-only or operator-window class label).** |
| 8 | Mixed cutover invariant explicit AC | **Yes — added to 31.B-5b code AC: verifies both branches delegate to the same `claim_ticket_atomic`** |

### §3.2.1 destructive_op_classes scope rule (NEW per operator answer 7)

Adds a `destructive_op_scope:` field. Three values, derived rules:

| Scope | Definition | Runner auto-pickup |
|---|---|---|
| `none` | No destructive ops | ✓ always |
| `project` | All destructive ops scoped to: `/home/user/work/sora/**`, `~/.cache/omnisight/**`, `~/.config/omnisight/**`, `/tmp/runner-pickup/**`, `~/runner-{claude,codex}-{1,2}/**`, `~/.ssh/id_ed25519_*-bot*`, `systemctl --user` units **for runner-* services only** | ✓ allowed |
| `system` | ANY destructive op outside the project-scope list (e.g., `/etc/`, `/var/`, `/usr/`, host-wide `systemctl`, deletions outside project tree, git refs on shared remotes) | ✗ REQUIRES `class:operator-prepare-only` or `class:operator-window` label; runner refuses pickup otherwise |

Mapping from existing `destructive_op_classes` enum to default `destructive_op_scope`:

| Class | Default scope |
|---|---|
| `state-append` (writes ~/.cache/omnisight/*) | `project` |
| `tmp-delete` (deletes /tmp/runner-pickup/*) | `project` |
| `env-edit` (modifies env files in ~/runner-*/* OR ~/.config/omnisight/*) | `project` |
| `systemd-control` — user units `runner-*` | `project` |
| `systemd-control` — system units OR user units outside `runner-*` | `system` |
| `git-ref-rewrite` — shared Gerrit/GitLab refs | `system` |
| `git-ref-rewrite` — local-only refs | `project` |

Tickets with `destructive_op_scope: system` MUST carry `class:operator-prepare-only` or `class:operator-window`. OP-1042 filing-time hook validates. Runner pickup-time check (Phase 31.K): if ticket has `destructive_op_scope: system` AND lacks operator-* class label, runner refuses claim with comment "needs-operator-authorization-system-scope".

Operator (sora or designated operator with sudo) may explicitly override by adding `class:operator-window` to grant scope. Any other authority chain MUST go through operator first.

**Impact on 31.B ticket set**:

- 31.B-8a (rename-existing-branches.py with --apply) — `destructive_op_classes: [git-ref-rewrite]` + `destructive_op_scope: system` (rewrites shared Gerrit refs) → already has `class:operator-prepare-only` ✓
- 31.B-10a/10b (cutover/rollback scripts) — `destructive_op_classes: [systemd-control, env-edit]` + `destructive_op_scope: project` (only `runner-*` user units; env files in project paths) → no operator-class required ✓
- All other tickets: project-scope; auto allowed ✓

---

## §8. Submission flow

1. This spec at `docs/sprint-s12/phase-31b-ticket-spec.md` (v2)
2. Operator review of §7 open questions; answer + lock
3. Commit to feature branch `feature/sprint-s12-phase-31b-spec-v2`
4. Push to Gerrit refs/for/develop
5. Operator + 1 reviewer +2
6. Submit (merge to develop)
7. Filing script (separate ticket; same script as 31.A, schema-extended for `mutex_with:` + `destructive_op_classes:`) files 33 children in DAG batch order with explicit gates
8. Post-filing spot-check first 3 tickets

---

**End of Phase 31.B ticket spec v2 (33-ticket atomic + boundaries + 4 intermediate gates + codex-driven hardening). Awaiting operator §7 answers.**
