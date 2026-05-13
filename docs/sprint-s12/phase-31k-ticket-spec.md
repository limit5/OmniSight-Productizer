---
id: SPRINT-S12-PHASE-31K-SPEC
version: v2 (post codex review)
title: Sprint S12 Phase 31.K — Legacy Removal + Docs + Retro + v0.5.0 Final · Ticket Spec
scope: lane-serialized legacy removal + behavior-specific smoke + 31.J concurrency rules + Synthetic-Change fallback + DocConsistencyGate + ADR-0032 hotfix policy + repo-content-delete + meta-closure schemas
status: Draft (2026-05-13)
related:
  - ADR-0023
  - 31.J-StabilityCheckpoint (cross-phase blocker; 31.J Integration 14d soak runs concurrently with frozen-on-fail rules)
  - Phase 31.A-31.J v2
  - 31.K v1 (pre codex review, superseded)
  - Codex independent review (2026-05-13, /tmp/phase31k-codex-review-final.txt)
---

# Sprint S12 Phase 31.K · Legacy Removal + Final Wrap — Ticket Spec v2

## §0. v2 Changelog (vs v1 — driven by codex independent review)

**P0 BLOCKING fixes:**

| Item | v1 problem | v2 fix |
|---|---|---|
| **Ticket count bookkeeping defect** | spec said "22" but table had rows 1-24; routing tally codex 3 + claude 21 = 24 | v2: ticket count **26** (24 v1 + Synthetic-Change + DocConsistencyGate) |
| **6d boundary contradiction** | required_paths `[.github/]` + forbidden `[.gitlab-ci.yml]` but context removes gha-enforcement from `.gitlab-ci.yml` | v2: 6d required_paths = `[.github/, .gitlab-ci.yml]`; forbidden no longer lists `.gitlab-ci.yml` |
| **6a/6b/6e all touch jira_dispatch.py** | "parallel since different files" FALSE | v2: **serialize jira_dispatch.py lane** — 6a → 6b → 6e via blockedBy chain; 6c + 6d parallel (different files) |
| **31.J concurrency unhandled** | 31.J Integration 14d may fail during 31.K | v2: NEW **§X "31.J Concurrency Rules"** — fail before GA → freeze 31.K; fail after GA → keep tag + file hotfix per ADR-0032 |
| **31.K-Integration no Synthetic-Change fallback** | quiet 7-day window can deadlock | v2: NEW ticket **31.K-Integration-Synthetic-Change** (mirrors 31.J-3d pattern) |
| **6e claim-label validator insufficient** | "0 callers" doesn't prove concurrency-safe | v2: 6e adds AC `legacy_mode_disabled_days >= 14 AND operator_override_count == 0` |
| **PostRemovalGate failure attribution undefined** | 5 removals merged → smoke fail = can't identify culprit | v2: PostRemovalGate adds **triage rules** + staged mini-smoke per cluster (jira_dispatch lane / CI lane / GHA lane); baseline diff with PreRemovalGate |
| **GA tag retroactive policy ambiguous** | v0.5.0.1 vs v0.5.1 vs delete unclear | v2: ADR-0032 explicit — **v0.5.1 default for hotfix** (NOT v0.5.0.1; NEVER rewrite v0.5.0); 9a depends on ADR-0032 Accepted |
| **6c required_paths underdeclared** | context mentions compose refs + GitLab variables | v2: 6c required_paths add `docker-compose.yml` + `deploy/`; operator action log documents `GHCR_PUSH_TOKEN` GitLab CI variable deletion |

**P1 STRUCTURAL fixes:**

| Item | v1 problem | v2 fix |
|---|---|---|
| 1bc validator narrative overstated | "pass → safe" false for dynamic refs / shell / CI / config | v2: 1bc narrative softened to "necessary-but-not-sufficient"; each 6X adds **behavior-specific smoke AC** (runner pickup for 6a/6e; alert deliver for 6b; build+sign for 6c; GHA inactivity for 6d) |
| 7a/7b/7c parallel docs no consistency gate | cross-references can contradict | v2: NEW **31.K-DocConsistencyGate** (after 7a/7b/7c; verifies architecture decisions / retro conclusions / release notes don't contradict) |
| 7c retro before post-GA observation | can't honestly include post-GA outcomes | v2: 7c renamed "pre-GA S12 retrospective"; Integration AC adds post-GA addendum mechanism if observation contradicts |
| Sprint S12 META closure timing | risk of closing before 7-day criteria met | v2: Integration closure language EXPLICIT — "META Closed ONLY if all 4 closure criteria met (0 P1 / 0 rollback / ≥1 organic-or-synthetic change / 7-day observation duration)" |

**P2 SCHEMA additions:**

```yaml
destructive_op_classes: ... | repo-content-delete   # NEW (distinct from git-ref-rewrite)
external_side_effect: ... | meta-closure   # NEW (Sprint META Closed is governance, not routine jira-write)
tag_type: lightweight | annotated   # NEW field on git-ref-rewrite tickets that create tags
```

`repo-content-delete` semantics:
- In-repo file/code deletion via Gerrit-merged PR
- Shared-remote effect happens at MERGE (not at patch creation)
- Different from `git-ref-rewrite` (force-push, rename, delete branch/tag)
- Auto-allowed (no operator-* required); merge governance via Gerrit +2 handles authority

`meta-closure` semantics:
- Closes Sprint META (Status=Closed; Resolution=Done)
- Project-governance side effect; harder to unwind than routine ticket update
- REQUIRES `class:operator-window` (operator confirms closure criteria met)
- Filing-time hook: ticket with `meta-closure` MUST list closure criteria + AC verifies each

`tag_type: annotated` semantics:
- Tag is created with `-a` flag (annotated; carries author + date + message)
- Remote-verifiable property (NOT just JSON assertion)
- Mandatory for release tags (per ADR-0031 v2 + ADR-0032)
- 9a AC verifies via Gerrit query

**P3 NEW: 31.J Concurrency Rules (§X)**

If 31.J Integration FAILS during 31.K execution:
- **Fail BEFORE 31.K-9a (GA tag)**: FREEZE 31.K immediately. PreRemovalGate / removal tickets / PostRemovalGate / docs all PAUSE. Operator triages: did 31.K removal cause 31.J regression? If yes → revert. If no → 31.J failure stays in 31.J domain.
- **Fail AFTER 31.K-9a (GA tagged)**: do NOT rewrite v0.5.0 tag. Keep 31.K-Integration open (do NOT close S12 META). File hotfix per ADR-0032 (cut v0.5.1 with fix). 31.K-Integration closes only when stability restored.
- **Daily check**: 31.K-Integration daily snapshot reads 31.J-Integration evidence file; if 31.J `integration_clock_frozen_periods` adds entry → 31.K alerts P1.

**P4 OPERATIONAL:**

- 8-Doc adds "Operator rest-day recommendation" — 1+ day between PreRemovalGate close and 9a GA tag
- 9a moved from "same-day after CallSiteInventoryGate" to "T+1d minimum" (operator rest)

**Ticket count**: v1 24 → v2 **26** (+ Synthetic-Change + DocConsistencyGate)

---

## §1, §2 — unchanged structure

---

## §3. Schemas (extended)

Reuses all prior + adds:
- `destructive_op_classes: repo-content-delete` (above)
- `external_side_effect: meta-closure` (above)
- `tag_type: lightweight | annotated` field on git-ref-rewrite tag tickets

---

## §4. Children — overview table (26 tickets v2)

| Layer | # | ID | Title | Tier | Prefer | Class | blockedBy |
|---|---|---|---|---|---|---|---|
| L0 | 1-5 | 31.K-1a..5a | specs (4a = ADR-0032 with explicit hotfix policy) | S | claude | claude | — |
| L1 | 6 | 31.K-1bc | legacy-removal-validator + tests + softened narrative | S | codex | codex | 1a |
| L1 | 7 | 31.K-2bc | architecture-doc-generator + tests | S | codex | codex | 2a |
| L1 | 8 | 31.K-3bc | retro-aggregator + tests | S | codex | codex | 3a |
| L1 | 9 | 31.K-3d | **NEW** Synthetic-Change pre-approval for Integration (mirrors 31.J-3d) | S | claude | claude | 3a |
| L2-Gate | 10 | 31.K-LibsGate | + 31.J semantic audit + Synthetic-Change exists | S | claude | claude | 1bc, 2bc, 3bc, 3d, 4a, 5a, 31.J-StabilityCheckpoint-external |
| L3-Gate | 11 | 31.K-PreRemovalGate | (unchanged structure; adds 31.J-concurrency check rule reference) | S | claude | claude + operator-window | LibsGate |
| **jira_dispatch lane (serialize)** | | | | | | | |
| L4 | 12 | 31.K-6a | Remove worktree code (jira_dispatch.py) + runner-pickup smoke AC | S | claude | claude + operator-prepare-only | PreRemovalGate, 1bc |
| L5 | 13 | 31.K-6b | Remove ALERTS_ENABLED=0 branch (jira_dispatch.py) + alert-deliver smoke AC | S | claude | claude + operator-prepare-only | **6a**, 1bc |
| L6 | 14 | 31.K-6e | Remove legacy claim-label (jira_dispatch.py) + claim-flow smoke AC + 14d legacy-disabled evidence | S | claude | claude + operator-prepare-only | **6b**, 1bc, 31.B-Integration-external |
| **CI/GHA lane (parallel)** | | | | | | | |
| L4 | 15 | 31.K-6c | Remove ghcr-push job + compose refs + GHCR_PUSH_TOKEN doc + build+sign smoke AC | S | claude | claude + operator-prepare-only | PreRemovalGate, 1bc |
| L4 | 16 | 31.K-6d | Remove .github/workflows/* + gha-enforcement from .gitlab-ci.yml + GHA-inactivity smoke AC | S | claude | claude + operator-prepare-only | PreRemovalGate, 1bc, 31.E-Integration-external |
| L7-Gate | 17 | 31.K-PostRemovalGate | + triage rules + per-cluster mini-smoke + baseline-diff | S | claude | claude + operator-rehearsal | 6a, 6b, 6c, 6d, 6e |
| L8 | 18 | 31.K-7a | Final architecture doc | S | claude | claude | PostRemovalGate, 2bc |
| L8 | 19 | 31.K-7b | Foundation rebuild narrative | S | claude | claude | PostRemovalGate |
| L8 | 20 | 31.K-7c | **Renamed**: pre-GA S12 retrospective (post-GA addendum at Integration if needed) | S | claude | claude | PostRemovalGate, 3bc |
| L9-Gate | 21 | 31.K-DocConsistencyGate | **NEW**: cross-doc fact consistency (architecture vs retro vs release notes) | S | claude | claude | 7a, 7b, 7c |
| L10 | 22 | 31.K-8-Doc | Final operator runbook + rest-day recommendation | S | claude | claude | DocConsistencyGate |
| L11 | 23 | 31.K-CallSiteInventoryGate | (unchanged structure) | S | claude | claude | 8-Doc |
| L12 | 24 | 31.K-9a | **OPERATOR**: cut v0.5.0 GA tag (annotated; rest-day post-CallSiteInventoryGate) | S | claude | claude + operator-window | CallSiteInventoryGate |
| L13 | 25 | 31.K-9b | v0.5.0 release notes | S | claude | claude | 9a |
| L14 | 26 | 31.K-Integration | **Sprint S12 META closure + 7-day post-GA + 31.J-concurrency-aware + Synthetic-Change fallback + retro addendum** | L | claude | claude + operator-window | 9b, 3d |

**Routing tally**: codex 3, claude 23 (totals 26 — verified).

**Operator-window tickets**: 4 (PreRemovalGate + 9a + Integration; sub-META closure inside Integration).

**Critical path**: 1a → 1bc → LibsGate → PreRemovalGate → [6a → 6b → 6e (jira_dispatch lane)] || [6c, 6d (CI lane)] → PostRemovalGate → 7a/7b/7c → DocConsistencyGate → 8-Doc → CallSiteInventoryGate → 9a (rest-day) → 9b → Integration. ~14 hops.

---

## §5. Per-child specs (v2 deltas)

### 31.K-1a, 2a, 3a, 5a — unchanged

### 31.K-4a — ADR-0032 with EXPLICIT hotfix policy (codex P0)

```yaml
delta_from_v1:
  - outputs_guaranteed: ADD explicit hotfix policy section:
    - **Hotfix for v0.5.0 post-GA**: cut **v0.5.1** (semantic version minor bump for hotfix)
    - **NEVER** v0.5.0.1 (no four-part numbering)
    - **NEVER** rewrite v0.5.0 tag (force-push, recreate; per ADR-0031 supersede policy)
    - All v0.5.0 references in production (image tags / configs) stay valid; v0.5.1 deployed alongside
    - Hotfix process: identify regression → fix → tag v0.5.1 (annotated, protected) → Wave 2 build + cosign + deploy
ac.code_additions:
  - {desc: "ADR has explicit hotfix policy section", verify: {command: "grep -ciE 'hotfix.*policy|v0\\.5\\.1.*default|NEVER.*rewrite.*v0\\.5\\.0' docs/adr/ADR-0032-v0.5.0-ga-release-policy.md", expect_stdout_match: "^[3-9]$|^[1-9][0-9]+$"}}
```

### 31.K-1bc — softened validator narrative (codex P1)

```yaml
delta_from_v1:
  - context_hint.outputs_guaranteed: ADD wording — "Validator PASS is NECESSARY but NOT SUFFICIENT — each 6X removal ticket must additionally provide behavior-specific smoke proof (runner pickup / alert deliver / build+sign / GHA inactivity / claim flow). Validator catches direct symbol references; semantic / dynamic / config dependencies require behavior verification."
ac.code_additions:
  - {desc: "validator output structure includes 'necessary_not_sufficient' field", verify: {command: "grep -cE 'necessary_not_sufficient|necessary but not sufficient' scripts/sprint-s12/legacy-removal-validator.py", expect_stdout_match: "[1-9]"}}
```

### 31.K-2bc — unchanged

### 31.K-3bc — unchanged

### 31.K-3d — NEW Synthetic-Change pre-approval (mirrors 31.J-3d)

```yaml
title: "AUDIT-31.K-3d: pre-approved Synthetic-Change for Integration (feature-freeze fallback)"
tier: S, prefer: claude, class: subscription-claude
area_labels: [area:devops, area:release, area:docs]
blockedBy: [3a]
context_hint:
  produces: "docs/sop/synthetic-change-pre-approval-31k.md + jira/AUDIT-31.K-Integration-fallback.json"
  consumed_by: "31.K-Integration"
  inputs_expected: "3a retrospective protocol"
  outputs_guaranteed: |
    Mirrors 31.J-3d pattern. Pre-approved low-risk no-op operational change for use if 31.K-Integration 7-day post-GA window has 0 organic changes.
    Type: docs-only or config-metadata bump (e.g., update version comment from `v0.5.0-rc2` to `v0.5.0`)
    Activation: Integration day 5 + no organic change → operator activates
    NOT counted as real change unless no organic alternative existed
  non_goals:
    - "DO NOT activate during 6a-6e removals"
    - "DO NOT trigger before Integration day 5 (give organic change a chance)"
boundaries:
  loc_delta_max: 150, files_touched_max: 2
  required_paths: [docs/sop/synthetic-change-pre-approval-31k.md, jira/AUDIT-31.K-Integration-fallback.json]
  forbidden_paths: [backend/, scripts/, .gitlab-ci.yml]
  test_scope: defer-to-integration
  destructive_op_classes: []
  destructive_op_scope: none
  external_side_effect: none
  external_payload_class: operational
  execution_mode: structural-only
  dependency_artifacts: [docs/sop/sprint-s12-retro-protocol.md]
  runtime_capability: none
  mutex_with: []
  interface_contract:
    inputs_from_deps: [retro protocol]
    outputs_for_downstream:
      - "pre-approval doc"
      - "pre-filed fallback ticket"
ac.code:
  - {desc: "pre-approval doc + fallback ticket exist", verify: {command: "test -f docs/sop/synthetic-change-pre-approval-31k.md && test -f jira/AUDIT-31.K-Integration-fallback.json", expect_exit_code: 0}}
  - {desc: "activation condition day-5 documented", verify: {command: "grep -ciE 'day 5.*no organic|fallback activation.*day 5' docs/sop/synthetic-change-pre-approval-31k.md", expect_stdout_match: "[1-9]"}}
go_live: T+1d after 3a
```

### 31.K-LibsGate — + 31.J semantic audit + Synthetic-Change verified

```yaml
delta_from_v1:
  - blockedBy adds: 3d
  - new ACs: semantic audit covers 31.J Integration (concurrent — uses latest snapshot); 3d pre-approval exists
ac.code_additions:
  - {desc: "31.J Integration current snapshot available", verify: {command: "test -f docs/audit/AUDIT-31-phase-J-evidence/integration.json && jq -r '.integration_clock_frozen_periods // [] | length' docs/audit/AUDIT-31-phase-J-evidence/integration.json", expect_stdout_match: "^0$"}}
  - {desc: "3d pre-approval exists", verify: {command: "test -f docs/sop/synthetic-change-pre-approval-31k.md", expect_exit_code: 0}}
```

### 31.K-PreRemovalGate — + 31.J concurrency reference

```yaml
delta_from_v1:
  - context_hint.outputs_guaranteed: ADD "Operator confirms understanding of §X 31.J Concurrency Rules; signs that any 31.J failure during 31.K will trigger freeze/hotfix per rules"
ac.code_additions:
  - {desc: "31.J concurrency rules acknowledged", verify: {command: "jq -r .operator_acknowledged_31j_concurrency_rules docs/audit/AUDIT-31-phase-K-evidence/pre-removal-gate.json", expect_stdout_match: "^true$"}, run_as: operator}
```

### 31.K-6a, 6b, 6e — SERIALIZED jira_dispatch.py lane (codex P0) + behavior-specific smoke

```yaml
6a_delta:
  - blockedBy: [PreRemovalGate, 1bc]
  - destructive_op_classes: **[repo-content-delete]** (was [git-ref-rewrite]; codex P2)
  - new AC: runner-pickup smoke (synthetic pickup cycle on dev WSL post-merge)

6b_delta:
  - blockedBy: [**6a**, 1bc]   (was [PreRemovalGate, 1bc])
  - destructive_op_classes: [repo-content-delete]
  - new AC: alert-deliver smoke (synthetic P1 → Discord round-trip)

6e_delta:
  - blockedBy: [**6b**, 1bc, 31.B-Integration-external]   (was [PreRemovalGate, ...])
  - destructive_op_classes: [repo-content-delete]
  - new ACs:
    - claim-flow smoke (concurrent claim test still passes; OP-977 fencing-token alone handles)
    - legacy_mode_disabled_days >= 14
    - operator_override_count == 0 during 14d window

example_6a_AC:
  - {desc: "runner-pickup smoke PASS post-merge", verify: {command: "bash scripts/sprint-s12/phase31k-6a-pickup-smoke.sh && jq -r .pickup_completed docs/audit/AUDIT-31-phase-K-evidence/6a-pickup-smoke.json", expect_stdout_match: "^true$"}, run_as: operator}

example_6e_AC:
  - {desc: "legacy_mode disabled ≥14 days", verify: {command: "jq -r .legacy_mode_disabled_days docs/audit/AUDIT-31-phase-K-evidence/6e-evidence.json | awk '{if ($1 >= 14) print \"OK\"; else print \"FAIL\"}'", expect_stdout_match: "^OK$"}, run_as: operator}
  - {desc: "0 operator overrides during window", verify: {command: "jq -r .operator_override_count docs/audit/AUDIT-31-phase-K-evidence/6e-evidence.json", expect_stdout_match: "^0$"}, run_as: operator}
```

### 31.K-6c — CI lane + boundary fix (codex P0)

```yaml
delta_from_v1:
  - required_paths: [.gitlab-ci.yml, Dockerfile, **docker-compose.yml, deploy/**]   (added compose + deploy)
  - destructive_op_classes: [repo-content-delete]
  - new AC: build+sign smoke (push a synthetic tag; verify build + cosign produces signed image)
  - new AC: GHCR_PUSH_TOKEN GitLab CI variable deletion documented in operator action log
```

### 31.K-6d — boundary fix + GHA-inactivity proof (codex P0)

```yaml
delta_from_v1:
  - required_paths: [.github/, **.gitlab-ci.yml**]   (NEEDS .gitlab-ci.yml to remove gha-enforcement job)
  - forbidden_paths: removes `.gitlab-ci.yml` from forbidden list
  - destructive_op_classes: [repo-content-delete]
  - new AC: GHA-inactivity proof — `gh run list --limit 30 --json status,conclusion,startedAt | jq -r '[.[] | select((now - (.startedAt|fromdateiso8601)) < 1209600)] | length' == 0` (no GHA runs in last 14d)
```

### 31.K-PostRemovalGate — triage rules + per-cluster mini-smoke (codex P1)

```yaml
delta_from_v1:
  - context_hint.outputs_guaranteed: REWRITTEN with TRIAGE RULES:
    1. Cluster mini-smoke: jira_dispatch lane (6a/6b/6e) runs combined smoke first; CI lane (6c) build+sign smoke; GHA lane (6d) inactivity smoke
    2. Compare current smoke results vs PreRemovalGate baseline
    3. For each FAIL: identify likely owner via path matching (which 6X touched the failing component?); if ambiguous → operator investigates before revert
    4. Revert decision tree: clear attribution → revert offending 6X; ambiguous → operator investigates 1h max; pre-existing latent bug → file follow-up, do NOT revert removal
    5. Output: post-removal-gate.json with per-smoke result + attribution + revert decisions
ac.code_additions:
  - {desc: "per-cluster mini-smoke completed", verify: {command: "jq -r '.cluster_smokes | length' docs/audit/AUDIT-31-phase-K-evidence/post-removal-gate.json", expect_stdout_match: "^[3-9]$"}, run_as: operator}
  - {desc: "baseline diff captured", verify: {command: "jq -r .baseline_diff_summary docs/audit/AUDIT-31-phase-K-evidence/post-removal-gate.json", expect_stdout_match: ".+"}, run_as: operator}
  - {desc: "attribution clear for each failure (or 0 failures)", verify: {command: "jq -r '[.cluster_smokes[] | select(.pass == false and .attribution_clear == false)] | length' docs/audit/AUDIT-31-phase-K-evidence/post-removal-gate.json", expect_stdout_match: "^0$"}, run_as: operator}
```

### 31.K-7a, 7b — unchanged structure

### 31.K-7c — renamed pre-GA retrospective (codex P1)

```yaml
delta_from_v1:
  - title: "AUDIT-31.K-7c: Pre-GA Sprint S12 retrospective (post-GA addendum at Integration if needed)"
  - context_hint.outputs_guaranteed: ADD "If post-GA 7-day observation contradicts retro conclusions, Integration appends addendum section to same document; retro frozen otherwise"
ac.code_additions:
  - {desc: "doc title indicates pre-GA", verify: {command: "grep -ciE 'pre.GA|before.GA' docs/retrospectives/2026-*-sprint-s12-bedrock-retro.md", expect_stdout_match: "[1-9]"}}
  - {desc: "addendum mechanism documented", verify: {command: "grep -ciE 'addendum|post.GA correction' docs/retrospectives/2026-*-sprint-s12-bedrock-retro.md", expect_stdout_match: "[1-9]"}}
```

### 31.K-DocConsistencyGate — NEW (codex P1)

```yaml
title: "AUDIT-31.K-DocConsistencyGate: Cross-doc fact consistency (architecture vs retro vs release notes)"
tier: S, prefer: claude, class: subscription-claude
area_labels: [area:docs, area:tests, area:release]
blockedBy: [7a, 7b, 7c]
context_hint:
  produces: "scripts/sprint-s12/phase31k-doc-consistency.py + docs/audit/AUDIT-31-phase-K-evidence/doc-consistency.json"
  consumed_by: "31.K-8-Doc"
  inputs_expected: "7a/7b/7c committed"
  outputs_guaranteed: |
    Cross-doc consistency check:
    - Same facts referenced in architecture doc (7a) + narrative (7b) + retro (7c) don't contradict
    - Example: if 7a says "Coordinator is library-first", 7c must not say "Coordinator daemon was painful"
    - Run via diff-style analysis: extract key claims from each doc; compare claims that refer to same fact
    - Output: doc-consistency.json with {claims_examined, contradictions_found, contradictions_list}
    - GATE PASS: contradictions_found == 0
boundaries:
  loc_delta_max: 200, files_touched_max: 2
  required_paths: [scripts/sprint-s12/phase31k-doc-consistency.py, docs/audit/AUDIT-31-phase-K-evidence/doc-consistency.json]
  forbidden_paths: [docs/architecture/, docs/retrospectives/, docs/release-notes/]
  test_scope: defer-to-integration
  destructive_op_classes: []
  destructive_op_scope: project
  external_side_effect: none
  external_payload_class: operational
  execution_mode: unit-testable
  dependency_artifacts: [7a, 7b, 7c]
  mutex_with: []
  interface_contract:
    inputs_from_deps: [3 committed docs]
    outputs_for_downstream:
      - "doc-consistency.json with 0 contradictions"
ac.code:
  - {desc: "script exists", verify: {command: "test -x scripts/sprint-s12/phase31k-doc-consistency.py", expect_exit_code: 0}}
  - {desc: "0 contradictions", verify: {command: "jq -r .contradictions_found docs/audit/AUDIT-31-phase-K-evidence/doc-consistency.json", expect_stdout_match: "^0$"}}
go_live: T+1d after 7a + 7b + 7c
```

### 31.K-8-Doc — + rest-day recommendation

```yaml
delta_from_v1:
  - new section: "Operator cadence recommendation — 1+ rest day between 31.J cutover session + 31.K PreRemovalGate; 1+ rest day between PostRemovalGate close + 9a GA tag"
ac.code_additions:
  - {desc: "rest-day recommendation present", verify: {command: "grep -ciE 'rest.day|operator cadence|1.*day between' docs/sop/operator-daily-ops-runbook-v0.5.0.md", expect_stdout_match: "[1-9]"}}
```

### 31.K-CallSiteInventoryGate — unchanged

### 31.K-9a — rest-day + tag_type field + ADR-0032 dependency (codex P0/P2)

```yaml
delta_from_v1:
  - blockedBy: [CallSiteInventoryGate, **4a**]   (now requires ADR-0032 Accepted)
  - boundaries.scope_components: [git-refs-shared, release-tag]
  - boundaries.tag_type: **annotated** (NEW field; mandatory)
  - new AC: T+1d minimum after CallSiteInventoryGate (rest-day enforcement; operator action log shows ≥1 day gap)
ac.code_additions:
  - {desc: "tag is annotated (remote-verified via gerrit query)", verify: {command: "ssh -p 29418 admin@sora.services 'gerrit query --format json refs/tags/v0.5.0' | jq -r .tagger_present", expect_stdout_match: "^true$"}, run_as: operator}
  - {desc: "rest-day gap ≥1d", verify: {command: "python3 -c \"import json, datetime; cg=json.load(open('docs/audit/AUDIT-31-phase-K-evidence/call-site-inventory.json')); tag=json.load(open('docs/audit/AUDIT-31-phase-K-evidence/v0.5.0-tag.json')); cgt=datetime.datetime.fromisoformat(cg['committed_at']); tgt=datetime.datetime.fromisoformat(tag['created_at']); h=(tgt-cgt).total_seconds()/3600; assert h >= 24\"", expect_exit_code: 0}, run_as: operator}
  - {desc: "ADR-0032 Accepted (hotfix policy locked)", verify: {command: "grep -ciE '^status:.*accepted' docs/adr/ADR-0032-v0.5.0-ga-release-policy.md", expect_stdout_match: "[1-9]"}}
```

### 31.K-9b — unchanged

### 31.K-Integration — concurrency + Synthetic-Change + addendum + conditional closure (codex P0/P1)

```yaml
delta_from_v1:
  - blockedBy: [9b, **3d**]
  - context_hint.outputs_guaranteed: REWRITTEN with 4 closure criteria + 31.J concurrency + Synthetic-Change fallback + retro addendum:
    Closure criteria (ALL must pass — codex P1):
    1. 0 P1 incidents during 7-day post-GA
    2. 0 rollback / tag-rewrite events
    3. ≥1 organic change OR 3d Synthetic-Change activated (codex P0 fallback)
    4. ≥7 day observation duration measured
    31.J concurrency (§X):
    - Daily: read 31.J-Integration evidence; if integration_clock_frozen_periods grew → P1 alert
    - If 31.J fails before this Integration started → re-evaluate; freeze 31.K
    - If 31.J fails after GA tagged → keep S12 META open; file hotfix v0.5.1 per ADR-0032
    Post-GA retro addendum:
    - If post-GA observation contradicts 7c retro claims → append addendum section to retro doc
    - Addendum is part of Integration evidence
    Closure:
    - META Closed ONLY when ALL 4 closure criteria PASS; otherwise Integration stays Open until met
  - boundaries.external_side_effect: **meta-closure** (NEW; codex P2)
  - integration.json schema now 16 fields (was 14): + closure_criteria_pass_count + retro_addendum_present + j_concurrency_alerts + synthetic_change_activated_at + ... etc.
ac.code_replacements:
  - {desc: "integration.json has 16 fields", verify: {command: "python3 -c \"import json; d=json.load(open('docs/audit/AUDIT-31-phase-K-evidence/integration.json')); assert all(k in d for k in ['phase_count','ticket_count','retro_path','arch_doc_path','release_notes_path','post_ga_days','p1_incidents','ga_tag_rollback_count','organic_changes_count','synthetic_change_activated_at','closure_criteria_pass_count','retro_addendum_present','j_concurrency_alerts','sprint_s12_meta_status','closed_at','daily_snapshots'])\"", expect_exit_code: 0}}
  - {desc: "META closes ONLY if all 4 closure criteria PASS", verify: {command: "python3 -c \"import json; d=json.load(open('docs/audit/AUDIT-31-phase-K-evidence/integration.json')); criteria_pass = d['closure_criteria_pass_count']; meta_status = d['sprint_s12_meta_status']; assert (meta_status == 'Closed') == (criteria_pass == 4)\"", expect_exit_code: 0}, run_as: operator}
  - {desc: "31.J concurrency alerts handled", verify: {command: "jq -r .j_concurrency_alerts docs/audit/AUDIT-31-phase-K-evidence/integration.json", expect_stdout_match: "^[0-9]+$"}, run_as: operator}
```

---

## §6. Filing batch order (DAG v2)

```
L0: 1a, 2a, 3a, 4a, 5a (5 specs)
L1: 1bc, 2bc, 3bc, 3d (4 — incl. Synthetic-Change pre-approval)
L2: LibsGate (cross-phase 31.J-StabilityCheckpoint + semantic audit)
L3: PreRemovalGate (operator-window + 31.J concurrency rules acknowledged)
**Lane A (jira_dispatch.py serialized)**:
  L4: 6a (worktree)
  L5: 6b (ALERTS_ENABLED=0 branch)
  L6: 6e (legacy claim-label fallback)
**Lane B (CI/GHA parallel — different files)**:
  L4: 6c (ghcr-push + compose)
  L4: 6d (GHA workflows + gha-enforcement)
L7: PostRemovalGate (triage rules + per-cluster mini-smoke + baseline diff)
L8: 7a, 7b, 7c (parallel docs)
L9: DocConsistencyGate (NEW)
L10: 8-Doc (with rest-day recommendation)
L11: CallSiteInventoryGate
L12: (rest-day gap ≥1d)
L13: 9a (operator-window; v0.5.0 GA annotated tag)
L14: 9b release notes
L15: Integration (tier:L 7-day + concurrency + Synthetic-Change fallback + addendum + conditional META closure)
```

15 layers, 26 tickets.

---

## §7. Operator answers (all locked 2026-05-13)

| # | Question | Locked answer |
|---|---|---|
| 1 | v0.5.0 GA timing | **Cut at 9a (rest-day after CallSiteInventoryGate)**; 7-day observation IS GA stability proof; tag exists during observation |
| 2 | GHA dependabot.yml | **Operator decides at 6d filing** (defer if uncertain) |
| 3 | rc2 retention | **Keep indefinitely** as historical artifact (per ADR-0031 supersede policy) |
| 4 | Post-GA support window | **6 months** per ADR-0032; quarterly minor versions; security patches as-needed |
| 5 | Sub-META aggregation report | **Both JSON + markdown** (JSON for tooling; markdown for human review) |
| 6 | Future-phase planning | **New META per initiative in JIRA**; do NOT extend S12 |
| 7 | Sprint S12 META closure | **Atomic in Integration**, conditional on 4 closure criteria ALL pass (per v2 fix) |
| 8 | Post-GA observation duration | **7 days** (31.K is wrap-up; 31.J already proved 14-day stability) |

---

## §8. Submission flow

1. Spec at `docs/sprint-s12/phase-31k-ticket-spec.md` (v2)
2. Operator §7 lock
3. Commit + Gerrit +2 + submit
4. Filing script files 26 children (with 31.J concurrency preflight rule applied)
5. Spot-check first 3

---

**End of Phase 31.K ticket spec v2 (26 tickets; lane-serialized removal + behavior-specific smoke + 31.J concurrency rules + Synthetic-Change fallback + DocConsistencyGate + ADR-0032 hotfix policy + repo-content-delete + meta-closure schemas). Final phase of Foundation Rebuild. Awaiting operator §7 lock.**
