---
id: SPRINT-S12-PHASE-31J-SPEC
version: v2 (post codex review)
title: Sprint S12 Phase 31.J — Production Cutover · Ticket Spec
scope: semantic readiness audit + per-step scope_components + ADR-0031 rationale + mid-cutover pause/resume + GHA legacy classification + rc2 supersede-not-delete + synthetic-change fallback + 31.I-reopen handling
status: Draft (2026-05-13)
related:
  - ADR-0023
  - 31.I-StabilityCheckpoint (cross-phase blocker)
  - Phase 31.A-31.I v2
  - 31.J v1 (pre codex review, superseded)
  - Codex independent review (2026-05-13, /tmp/phase31j-codex-review-final.txt)
---

# Sprint S12 Phase 31.J · Production Cutover — Ticket Spec v2

## §0. v2 Changelog (vs v1 — driven by codex independent review)

**P0 BLOCKING fixes (codex said "do not file v1 as-is"):**

| Item | v1 problem | v2 fix |
|---|---|---|
| **ReadinessGate semantic gap** | only queried JIRA status + commit SHA → stub Integration could pass | v2: **semantic evidence audit per phase** — specific field checks (e.g., 31.H integration.json `restore_readiness=true`; 31.I `prod_p1_incidents_unexplained=0`; 31.F `ghcr_cutover_completed=true`; 31.B `runner_states[].flag==1`; 31.G `dashboards_rendered_count=5`); rejects "stub closed" tickets |
| **Cutover steps homogeneous boundary** | all 5 collapsed to `system+network-production`; hides blast differences | v2: each step has **explicit `scope_components`** per codex spec (6a: [host-runner-env, systemd-user, runner-prod-state]; 6e: [git-refs-shared, release-tag, wave2-build, cosign-sign]; etc.) |
| **rc2 tag rollback most-dangerous** | spec said "delete tag from Gerrit + GitLab" | v2: **rc2 supersede-not-delete policy** — annotated tag; created from recorded SHA; protected; same-name re-tagging FORBIDDEN; if rollback needed → cut rc3 instead (per ADR-0031 v2) |
| **ADR-0031 order rationale thin** | "minimize blast" | v2: ADR-0031 REWRITTEN — WHY this order (each step establishes invariant for next); alternatives considered + rejected; explicit step-pairs justification |
| **Mid-cutover pause/resume undefined** | emergency mid-session = mixed state | v2: NEW §X "Cutover pause/resume rules" — named partial states (post-6a / post-6b / etc.); max pause 4h; re-run ReadinessGate deltas before resume |
| **GHA mixed-authority confusion** | GHA + GitLab CI both run during 14d soak | v2: **`gha_status_classification: legacy_observational` field** in 6d evidence; 8-Doc runbook adds "interpreting red/green mismatch"; GHA red ≠ blocking |
| **Real-prod-change ambiguous + freeze conflict** | feature freeze = no organic change → Integration stuck | v2: **NEW ticket 31.J-Integration-Synthetic-Change** (pre-approved low-risk no-op change filed BEFORE Integration starts; comment-only metadata bump that exercises full new path) |

**P1 STRUCTURAL fixes:**

| Item | v1 problem | v2 fix |
|---|---|---|
| 1bc + 2bc isolated tests | shared state machine but isolated unit tests | v2: **shared-state contract tests** — cutover step N → rollback step N round-trip; global rollback from partial states ("6a+6b applied, 6c failed"); evidence: same fixture used by both |
| 7a + 7b parallel risky | rollback dry-run can surface real recommendation → stale 7a report | v2: **sequential 7a → 7b** (7a snapshots health; 7b dry-runs rollback per step; final post-rollback-verify before 8-Doc) |
| 6e visual equivalence with 6a-6d | hides release-governance + shared-ref mutation criticality | v2: 6e gets dedicated description block + stronger AC (annotated tag verified + recorded SHA + protected-tag check + supersede-not-delete reminder) |

**P2 SCHEMA additions:**

```yaml
external_side_effect: ... | jira-read | jira-write   # NEW jira-read (queries only; no mutation)
runtime_capability: <enum>   # NEW field on lib tickets that ship production-capable scripts
```

`runtime_capability` semantics: distinguishes ticket-creation-time (writes script + tests; mocked) from artifact use-time (operator runs --apply against prod). Values:
- `none` — pure-data ticket (specs, configs that don't execute)
- `unit-only` — module + tests; not directly executable against prod
- `network-production-when-apply` — script has --dry-run default + --apply gated by operator confirm; production side effect only at apply-time

**31.I-reopen-during-31.J-Integration workflow** (NEW per codex Q5):
- If any 31.I-7-* / 31.I-Integration / prior phase StabilityCheckpoint REOPENS during 31.J Integration 14-day soak:
- Action: FREEZE Integration day count; emit `integration_clock_frozen.json`; run post-cutover-verification; classify severity
- Rollback ONLY if severity matches thresholds from 2a/5a (P1 unsigned production OR rollback-orchestrator-required-evidence)
- Otherwise: address via incident process + resume clock when reopen closes

**Ticket count**: v1 22 → v2 **23** (+ Synthetic-Change pre-approved ticket)

---

## §1, §2 — unchanged structure

---

## §3. Schemas (extended)

Reuses all prior + adds:
- `external_side_effect: jira-read` (above)
- `runtime_capability` (above)

---

## §4. Children — overview table (23 tickets v2)

| Layer | # | ID | Title | Tier | Prefer | Class | blockedBy |
|---|---|---|---|---|---|---|---|
| L0 | 1-5 | 31.J-1a..5a | specs (4a = ADR-0031 REWRITTEN with order rationale) | S | claude | claude | — |
| L1 | 6 | 31.J-1bc | cutover-orchestrator + tests + **shared-state contract tests** + `runtime_capability: network-production-when-apply` | S | claude | claude + operator-prepare-only | 1a |
| L1 | 7 | 31.J-2bc | rollback-orchestrator + tests + **same shared-state contract tests as 1bc** + `runtime_capability` | S | codex | codex + operator-prepare-only | 2a |
| L1 | 8 | 31.J-3bc | pre-cutover-checklist + **semantic evidence audit per phase** | S | codex | codex | 3a |
| L2 | 9 | 31.J-3d | **NEW** 31.J-Integration-Synthetic-Change (pre-approved no-op operational change; filed BEFORE Integration; uses full new path) | S | claude | claude | 3a |
| L3 | 10 | 31.J-LibsGate | + verifies 1bc/2bc shared-state contract; 3bc semantic audit fields present; 3d pre-approved | S | claude | claude | 1bc, 2bc, 3bc, 3d, 4a, 5a, 31.I-StabilityCheckpoint-external |
| L4 | 11 | 31.J-ReadinessGate | **HARDENED**: semantic audit per phase (specific evidence fields, NOT just status) | S | claude | claude + operator-window | LibsGate |
| L5-9 | 12-16 | 31.J-6a..6e | 5 cutover steps with **explicit scope_components per step**; 6e has rc2 supersede-not-delete reminder | S | claude | claude + operator-window | sequential |
| L10 | 17 | 31.J-PostCutoverGate | (unchanged) | S | claude | claude + operator-rehearsal | 6e |
| L11 | 18 | 31.J-7a | post-cutover health snapshot (sequential before 7b) | S | codex | codex | PostCutoverGate, 5a |
| L12 | 19 | 31.J-7b | **rollback dry-run (sequential after 7a; per-step)** | S | claude | claude + operator-rehearsal | **7a** |
| L13 | 20 | 31.J-8-Doc | + GHA mismatch interpretation section + rc2 supersede policy + mid-cutover pause/resume | S | claude | claude | 7a, 7b |
| L14 | 21 | 31.J-CallSiteInventoryGate | + `gha_status_classification` field + 31.I-reopen handling check | S | claude | claude | 8-Doc |
| L15 | 22 | 31.J-StabilityCheckpoint | (unchanged) | S | claude | claude + operator-window | CallSiteInventoryGate |
| L16 | 23 | 31.J-Integration | **14-day soak with 31.I-reopen handling + 3d Synthetic-Change as fallback if no organic change** | L | claude | claude + operator-window | StabilityCheckpoint, 3d |

**Routing tally**: codex 3, claude 20 (orchestration + docs heavy).

**Operator-window**: 8 (ReadinessGate + 6a-6e + StabilityCheckpoint + Integration).

**Critical path**: 1a → 1bc → 3d → LibsGate → ReadinessGate → 6a → 6b → 6c → 6d → 6e → PostCutoverGate → 7a → 7b → 8-Doc → CallSiteInventoryGate → StabilityCheckpoint → Integration. ~16 hops.

---

## §5. Per-child specs (v2 deltas)

### 31.J-1a, 2a, 3a, 5a — unchanged

### 31.J-4a — ADR-0031 REWRITTEN (codex P0)

```yaml
delta_from_v1:
  - context_hint.outputs_guaranteed: REWRITTEN — explicit order rationale:
    - "Why ephemeral-clone default FIRST (6a)": establishes runtime invariant that all pickups use new clone path; downstream alerts (6c) + image push (6b) assume ephemeral path
    - "Why GHCR off BEFORE alerts (6b before 6c)": once alerts go live, ghcr-push failure → P1 noise; turn off pre-flight
    - "Why alerts BEFORE CI sole (6c before 6d)": full alerting active means any CI authority change is observable in real-time
    - "Why CI sole BEFORE rc2 tag (6d before 6e)": tag push triggers Wave 2 build; building under GitLab-only authority ensures rc2 was built by canonical path
    - "Why rc2 LAST (6e)": final release-governance artifact; if any prior step fails, no rc2 created
    - rc2 SUPERSEDE-NOT-DELETE policy: rollback = cut rc3, NOT delete rc2
    - alternatives considered: alerts-first (rejected: noise pre-cutover); rc2-first (rejected: builds under mixed-state)
ac.code_additions:
  - {desc: "ADR has order rationale per step pair", verify: {command: "grep -cE 'Why.*FIRST|Why.*BEFORE|Why.*LAST' docs/adr/ADR-0031-rc2-cut-policy.md", expect_stdout_match: "^[4-9]$|^[1-9][0-9]+$"}}
  - {desc: "supersede-not-delete policy documented", verify: {command: "grep -ciE 'supersede.not.delete|cut.*rc3.*instead' docs/adr/ADR-0031-rc2-cut-policy.md", expect_stdout_match: "[1-9]"}}
  - {desc: "alternatives section present", verify: {command: "grep -ciE 'alternatives considered|alternative.*rejected' docs/adr/ADR-0031-rc2-cut-policy.md", expect_stdout_match: "[1-9]"}}
```

### 31.J-1bc — + shared-state contract tests + runtime_capability (codex P1/Q5)

```yaml
delta_from_v1:
  - boundaries.runtime_capability: **network-production-when-apply** (NEW field)
  - tests: NEW shared-state contract suite (with 2bc rollback-orchestrator): cutover step N → rollback step N → assert state identical to pre-N; global-rollback from partial state {6a+6b applied, 6c failed}
ac.code_additions:
  - {desc: "shared-state contract test exists", verify: {command: "grep -cE 'def test_.*contract.*shared.*state|def test_.*round.*trip|def test_.*partial.*state' scripts/sprint-s12/tests/test_cutover_orchestrator.py scripts/sprint-s12/tests/test_rollback_orchestrator.py 2>/dev/null | awk -F: '{sum+=$2} END {print sum}'", expect_stdout_match: "^[3-9]$|^[1-9][0-9]+$"}}
  - {desc: "--apply requires operator_confirm (cannot run without)", verify: {command: "python3 -c 'import sys; sys.path.insert(0,\"scripts/sprint-s12\"); from cutover_orchestrator import cutover;\\ntry:\\n    cutover(1, dry_run=False, operator_confirm=False)\\nexcept (ValueError, RuntimeError) as e:\\n    pass\\nelse:\\n    raise AssertionError(\"should have refused\")'", expect_exit_code: 0}}
```

### 31.J-2bc — same shared-state contract tests

```yaml
delta_from_v1:
  - boundaries.runtime_capability: network-production-when-apply
  - tests: same shared-state contract suite as 1bc (proof shared by both tickets)
```

### 31.J-3bc — semantic evidence audit (codex P0)

```yaml
delta_from_v1:
  - context_hint.outputs_guaranteed: REWRITTEN — semantic audit per phase (NOT just status):
    Per phase, checklist verifies specific evidence fields:
    - 31.A-Integration: `integration.json.runner_pickup_test.passed == true`
    - 31.B-Integration: `runner_states[].flag == 1` for all 4; `missing_tree_count == 0`; `mutex_race_count == 0`
    - 31.C-Integration: `p0_received and p1_received and digest_received`; `acks_recorded >= 2`
    - 31.D-StabilityCheckpoint: `ok_to_unblock_31E == true`; `p1_alerts == 0`
    - 31.E-Integration: `silent_failures == 0`; `p95_w1_seconds <= 900`
    - 31.F-Soak + GHCR-Cutover: `ghcr_cutover_completed == true`; `sign_success_rate_14d >= 99`
    - 31.G-Integration: `dashboards_rendered_count >= 5`; AM bridge live
    - 31.H-Integration: `restore_readiness == true`; `last_dr_drill_ref` within 30d
    - 31.I-Integration: `prod_p1_incidents_unexplained == 0`; pilot ready_for_parallel_dynamic
    Reject "stub closed" Integration tickets — if any required field missing → checklist FAIL
  - tests >=10 (was 6; +4 per-phase semantic check tests)
ac.code_additions:
  - {desc: "checklist queries all 9 phases with specific evidence fields", verify: {command: "grep -cE 'restore_readiness|prod_p1_incidents_unexplained|ghcr_cutover_completed|missing_tree_count|dashboards_rendered_count' scripts/sprint-s12/pre-cutover-checklist.py", expect_stdout_match: "^[5-9]$|^[1-9][0-9]+$"}}
  - {desc: "tests cover semantic-field-missing case (stub Integration detected)", verify: {command: "grep -cE 'def test_.*stub.*closed|def test_.*missing.*field|def test_.*semantic' scripts/sprint-s12/tests/test_pre_cutover_checklist.py", expect_stdout_match: "[1-9]"}}
```

### 31.J-3d — NEW Synthetic-Change ticket (codex P0)

```yaml
title: "AUDIT-31.J-3d: pre-approved synthetic prod-change (feature-freeze fallback for Integration)"
tier: S, prefer: claude, class: subscription-claude
area_labels: [area:devops, area:prod, area:docs]
blockedBy: [3a]
context_hint:
  produces: "docs/sop/synthetic-prod-change-pre-approval.md + JIRA ticket pre-filed for the change itself (state: pending operator activation during Integration)"
  consumed_by: "31.J-Integration (uses as fallback if no organic change during 14d)"
  inputs_expected: "3a checklist spec"
  outputs_guaranteed: |
    Document defines exactly ONE pre-approved synthetic operational change:
    - Type: comment-only config metadata bump (e.g., update version comment in docker-compose.yml from `# v0.5.0-rc1` to `# v0.5.0-rc2`)
    - Triggers full new path: GitLab CI lint+unit → build → push to GitLab CR → cosign sign → replication parity → no runtime behavior change
    - Pre-approval: operator + 1 reviewer +2 (committed AHEAD of Integration; ready to deploy on day 14 if no organic change)
    - NOT a feature change; NOT a real fix
    - Exercises but does NOT mutate prod runtime behavior
    Activation procedure: if Integration day 12 + no organic change → operator merges this pre-prepared PR
  non_goals:
    - "DO NOT trigger this change during 6a-6e cutover (Integration window only)"
    - "DO NOT count this as 'real change' unless no organic change occurred"
    - "DO NOT use this if any organic change happened (real-only counts first)"
boundaries:
  loc_delta_max: 200, files_touched_max: 2
  required_paths: [docs/sop/synthetic-prod-change-pre-approval.md, jira/AUDIT-31.J-Integration-fallback.json]
  forbidden_paths: [docker-compose.yml, backend/, scripts/]
  test_scope: defer-to-integration
  destructive_op_classes: []
  destructive_op_scope: none   # pre-approval doc + pre-filed ticket only
  external_side_effect: none
  external_payload_class: operational
  execution_mode: structural-only
  dependency_artifacts: [docs/sop/pre-cutover-readiness-checklist.md]
  runtime_capability: none
  mutex_with: []
  interface_contract:
    inputs_from_deps: [checklist spec]
    outputs_for_downstream:
      - "pre-approval doc"
      - "pre-filed ticket for fallback activation"
ac.code:
  - {desc: "pre-approval doc + pre-filed ticket exist", verify: {command: "test -f docs/sop/synthetic-prod-change-pre-approval.md && test -f jira/AUDIT-31.J-Integration-fallback.json", expect_exit_code: 0}}
  - {desc: "doc specifies comment-only/metadata-only change", verify: {command: "grep -ciE 'comment.only|metadata.only|no runtime behavior' docs/sop/synthetic-prod-change-pre-approval.md", expect_stdout_match: "^[2-9]$|^[1-9][0-9]+$"}}
  - {desc: "activation condition documented", verify: {command: "grep -ciE 'day 12.*no organic|fallback.*activation' docs/sop/synthetic-prod-change-pre-approval.md", expect_stdout_match: "[1-9]"}}
go_live: T+1d after 3a
```

### 31.J-LibsGate — adds 1bc/2bc contract check + 3bc semantic audit + 3d pre-approval

```yaml
delta_from_v1:
  - new ACs:
    - 1bc + 2bc shared-state contract tests pass against same fixture
    - 3bc checklist semantic audit functions present (not just status query)
    - 3d pre-approval doc + pre-filed ticket exists
ac.code_additions:
  - {desc: "shared-state contract tests pass (both 1bc + 2bc green on same fixture)", verify: {command: "pytest scripts/sprint-s12/tests/test_cutover_orchestrator.py scripts/sprint-s12/tests/test_rollback_orchestrator.py -k 'contract or round_trip or partial_state' -v", expect_exit_code: 0, timeout_seconds: 60}}
  - {desc: "3d pre-approval exists", verify: {command: "test -f docs/sop/synthetic-prod-change-pre-approval.md", expect_exit_code: 0}}
```

### 31.J-ReadinessGate — semantic audit per phase (codex P0)

```yaml
delta_from_v1:
  - context_hint.outputs_guaranteed: REWRITTEN per 3bc — REQUIRES semantic evidence audit (not just JIRA status). If ANY phase Integration evidence shows malformed/stub/missing field → checklist FAIL
ac.code_additions:
  - {desc: "checklist evidence per phase shows semantic_audit_pass", verify: {command: "python3 -c \"import json; d=json.load(open('docs/audit/AUDIT-31-phase-J-evidence/readiness-checklist.json')); assert all(p['semantic_audit_pass'] == True for p in d['phases'])\"", expect_exit_code: 0}, run_as: operator}
  - {desc: "0 stub-Integration detected", verify: {command: "jq -r .stub_integrations_detected docs/audit/AUDIT-31-phase-J-evidence/readiness-checklist.json", expect_stdout_match: "^0$"}, run_as: operator}
```

### 31.J-6a/6b/6c/6d/6e — explicit per-step scope_components (codex P0)

```yaml
delta_from_v1 (per-step):
  6a:
    boundaries.scope_components: [host-runner-env, systemd-user, runner-prod-state]
    boundaries.destructive_op_classes: [env-edit, systemd-control]
  6b:
    boundaries.scope_components: [gitlab-ci-settings, registry-publish-policy]
    boundaries.destructive_op_classes: [env-edit]
  6c:
    boundaries.scope_components: [alert-routing-prod, discord-prod, email-prod]
    boundaries.destructive_op_classes: [env-edit]
    boundaries.external_payload_class: secret-adjacent   # touches webhook URLs
  6d:
    boundaries.scope_components: [ci-authority-policy, gha-legacy-observational]
    boundaries.destructive_op_classes: []   # no programmatic action; policy change
    new AC: classification of GHA workflows updated in docs
  6e:
    boundaries.scope_components: [git-refs-shared, release-tag, wave2-build, cosign-sign]
    boundaries.destructive_op_classes: [git-ref-rewrite]
    NEW prominent reminder block: "rc2 IS SUPERSEDE-NOT-DELETE per ADR-0031: if rollback needed, cut rc3; do NOT delete rc2 tag"
    new AC: tag is annotated + protected on Gerrit + recorded SHA in step-5.json
6e_new_ACs:
  - {desc: "rc2 tag is annotated (-a flag, not lightweight)", verify: {command: "ssh -p 29418 admin@sora.services 'gerrit query --format json branch:refs/tags/v0.5.0-rc2' | jq -r .annotation_present", expect_stdout_match: "^true$"}, run_as: operator}
  - {desc: "rc2 tag commit SHA recorded in evidence", verify: {command: "jq -r .rc2_tag_commit_sha docs/audit/AUDIT-31-phase-J-evidence/step-5.json | grep -cE '^[0-9a-f]{40}$'", expect_stdout_match: "[1-9]"}, run_as: operator}
  - {desc: "tag is protected on Gerrit", verify: {command: "jq -r .rc2_tag_protected docs/audit/AUDIT-31-phase-J-evidence/step-5.json", expect_stdout_match: "^true$"}, run_as: operator}
```

### 31.J-6d additional — gha_status_classification field

```yaml
6d_additional:
  - new AC: 8-Doc + GHAEnforcementJob output includes `gha_status_classification: legacy_observational` field
  - new AC: GHA red NOT blocking (operator runbook documents this in 8-Doc)
```

### 31.J-PostCutoverGate, 31.J-7a — unchanged (now sequential per below)

### 31.J-7b — sequential after 7a (codex P1)

```yaml
delta_from_v1:
  - blockedBy: [**7a**]   (was [6e]; now sequential after 7a snapshot health)
  - context_hint.outputs_guaranteed: ADD "Per-step rollback dry-run reads health snapshot from 7a; if any step's rollback would mutate prod beyond dry-run, escalate. After all 5 step dry-runs PASS → emit rollback-dry-run.json. Final 'post-rollback-verify' check confirms 7a's health snapshot still matches (no drift)."
ac.code_additions:
  - {desc: "rollback dry-run for ALL 5 steps", verify: {command: "jq -r '.steps | length' docs/audit/AUDIT-31-phase-J-evidence/rollback-dry-run.json", expect_stdout_match: "^[5-9]$"}, run_as: operator}
  - {desc: "post-rollback-verify health matches 7a snapshot", verify: {command: "jq -r .post_rollback_verify_health_matches_snapshot docs/audit/AUDIT-31-phase-J-evidence/rollback-dry-run.json", expect_stdout_match: "^true$"}, run_as: operator}
```

### 31.J-8-Doc — + GHA mismatch + rc2 supersede + mid-cutover pause/resume

```yaml
delta_from_v1:
  - context_hint.outputs_guaranteed: ADD sections (now 7+ total):
    - "GHA red/green vs GitLab CI red/green interpretation" (per codex Q4)
    - "rc2 supersede-not-delete policy + how to cut rc3 if needed"
    - "Mid-cutover pause/resume rules" (named partial states: post-6a / post-6b / post-6c / post-6d / NO pause post-6e; max pause 4h; re-run ReadinessGate deltas before resume)
    - "31.I-reopened-during-31.J-Integration workflow" (freeze Integration clock + classify + threshold rollback)
ac.code_additions:
  - {desc: "GHA mismatch section present", verify: {command: "grep -cE '^##.*GHA.*[Mm]ismatch|^##.*[Ii]nterpret.*GHA' docs/sop/cutover-operator-runbook.md", expect_stdout_match: "[1-9]"}}
  - {desc: "rc2 supersede policy + rc3 procedure documented", verify: {command: "grep -cE '^##.*[Ss]upersede|^##.*rc3' docs/sop/cutover-operator-runbook.md", expect_stdout_match: "[1-9]"}}
  - {desc: "pause/resume rules section", verify: {command: "grep -cE '^##.*[Pp]ause|^##.*[Rr]esume' docs/sop/cutover-operator-runbook.md", expect_stdout_match: "[1-9]"}}
  - {desc: "31.I-reopen workflow section", verify: {command: "grep -cE '31\\.I.*reopen|integration.clock.*freeze' docs/sop/cutover-operator-runbook.md", expect_stdout_match: "[1-9]"}}
```

### 31.J-CallSiteInventoryGate — + gha_status_classification verified

```yaml
delta_from_v1:
  - new AC: verifies gha_status_classification field set to legacy_observational in 6d evidence + GHAEnforcementJob output
ac.code_additions:
  - {desc: "gha_status_classification=legacy_observational", verify: {command: "jq -r .gha_status_classification docs/audit/AUDIT-31-phase-J-evidence/step-4.json", expect_stdout_match: "^legacy_observational$"}, run_as: operator}
```

### 31.J-Integration — 31.I-reopen handling + Synthetic fallback (codex P0/Q5)

```yaml
delta_from_v1:
  - blockedBy: [StabilityCheckpoint, **3d**]   (NEW 3d pre-approval available as fallback)
  - context_hint.outputs_guaranteed: REWRITTEN — adds:
    - "31.I-reopen handling: if any 31.A-31.I StabilityCheckpoint or 31.I-7-* reopens → freeze day count; emit integration_clock_frozen.json; classify; rollback only if severity matches 2a/5a thresholds; resume clock when reopen closes"
    - "Real-prod-change fallback: if Integration day 12 + 0 organic prod changes → operator activates 3d Synthetic-Change pre-filed ticket; activate triggers full new path"
    - "Allowed organic change classes: deploy / hotfix / config-change / dependency-bump / cosign-key-rotation / new-replication-target. Excludes: cutover itself, audit-only tickets, doc-only PRs"
  - integration.json schema now 14 fields (was 12): + integration_clock_frozen_periods (List), + change_class (enum of allowed organic OR synthetic)
ac.code_additions:
  - {desc: "integration.json has 14 fields including 31.I-reopen + change_class", verify: {command: "python3 -c \"import json; d=json.load(open('docs/audit/AUDIT-31-phase-J-evidence/integration.json')); assert all(k in d for k in ['integration_clock_frozen_periods', 'change_class', 'rc2_tag_present_in_gerrit_and_gitlab', 'real_prod_changes_count', 'legacy_flag_overrides'])\"", expect_exit_code: 0}}
  - {desc: "change_class is allowed enum value", verify: {command: "jq -r .change_class docs/audit/AUDIT-31-phase-J-evidence/integration.json | grep -cE '^(deploy|hotfix|config-change|dependency-bump|cosign-key-rotation|new-replication-target|synthetic-no-op)$'", expect_stdout_match: "[1-9]"}, run_as: operator}
  - {desc: "if synthetic-no-op chosen, 3d ticket was activated (not pre-existing change)", verify: {command: "python3 -c \"import json; d=json.load(open('docs/audit/AUDIT-31-phase-J-evidence/integration.json')); if d['change_class'] == 'synthetic-no-op': assert d.get('synthetic_3d_activated_at') is not None\"", expect_exit_code: 0}, run_as: operator}
go_live: T+15d after StabilityCheckpoint (+ 3d pre-filed)
```

---

## §6. Filing batch order (DAG v2)

```
L0: 1a, 2a, 3a, 4a, 5a (5 specs)
L1: 1bc, 2bc, 3bc, 3d (4 libs/pre-approvals)
L2: LibsGate (cross-phase 31.I-StabilityCheckpoint)
L3: ReadinessGate (operator-window + semantic audit)
**cutover session** (single 2-4h operator session with explicit pause/resume rules from 8-Doc):
  L4: 6a (scope_components: host-runner-env / systemd-user / runner-prod-state)
  L5: 6b (gitlab-ci-settings / registry-publish-policy)
  L6: 6c (alert-routing-prod / discord-prod / email-prod)
  L7: 6d (ci-authority-policy / gha-legacy-observational)
  L8: 6e (git-refs-shared / release-tag / wave2-build / cosign-sign; SUPERSEDE-NOT-DELETE)
L9: PostCutoverGate
L10: 7a (health snapshot)
L11: 7b (rollback dry-run sequential after 7a)
L12: 8-Doc
L13: CallSiteInventoryGate
L14: StabilityCheckpoint
L15: Integration (tier:L 14d; 31.I-reopen handling; 3d fallback available)
```

16 layers, 23 tickets.

---

## §7. Operator answers (all locked 2026-05-13)

| # | Question | Locked answer |
|---|---|---|
| 1 | Cutover session scheduling | **ONE planned operator session 2-4 hours** (per codex 31.H pattern; shorter mixed-state exposure) |
| 2 | rc2 tag naming | **`v0.5.0-rc2`** (annotated tag per ADR-0031 v2) |
| 3 | Rollback dry-run scope | **5 separate per-step evidence entries** in rollback-dry-run.json (per codex Q3) |
| 4 | Post-cutover soak duration | **14 days** (parity with 31.D/31.F/31.G; final phase before 31.K) |
| 5 | GHA workflows post-cutover | **Keep running**; `gha_status_classification: legacy_observational` field disambiguates; removal in 31.K only after stable parallel-run |
| 6 | rc2 tag promotion automation | **Operator manual tag + push** (highest-stakes shared-ref mutation; no automation) |
| 7 | Real-prod-change Integration | **≥1 organic change in allowed classes** (deploy/hotfix/config/dep/cosign-key/new-replication-target); excludes cutover/audit-only/doc-only; feature-freeze fallback via 3d Synthetic-Change |
| 8 | 31.K gating | **blockedBy 31.J-StabilityCheckpoint** (24-72h); 31.J Integration 14d soak runs concurrently with 31.K work |

---

## §8. Submission flow

1. Spec at `docs/sprint-s12/phase-31j-ticket-spec.md` (v2)
2. Operator §7 lock
3. Commit + Gerrit +2 + submit
4. Filing script files 23 children (with semantic-audit cross-phase preflight rule applied)
5. Spot-check first 3

---

**End of Phase 31.J ticket spec v2 (23 tickets; semantic ReadinessGate + per-step scope_components + ADR-0031 rationale + mid-cutover pause/resume + rc2 supersede-not-delete + Synthetic-Change fallback + 31.I-reopen handling). Awaiting operator §7 lock.**
