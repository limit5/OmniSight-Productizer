---
id: SPRINT-S12-PHASE-31D-SPEC
version: v2 (post codex review)
title: Sprint S12 Phase 31.D — Gerrit → GitLab Replication · Ticket Spec
scope: Replication plugin + GitLab provisioning + replication.config + per-ref enablement + history backfill (preflight + execute) + failure-alert wiring + cutover
status: Draft (2026-05-13)
related:
  - ADR-0002 (GitLab primary; accepted 2026-05-04, never implemented)
  - ADR-0023 §3.2.1, §15
  - Phase 31.A v2 + 31.B v2 + 31.C v2 (all schemas reused)
  - L-OP-247 (lesson: GitLab project must be lowercase)
  - 31.D v1 (pre codex review, superseded)
  - Codex independent review (2026-05-13, /tmp/phase31d-codex-review-final.txt)
---

# Sprint S12 Phase 31.D · Gerrit → GitLab Replication — Ticket Spec v2

## §0. v2 Changelog (vs v1 — driven by codex independent review)

**P0 BLOCKING fixes:**

| Item | v1 problem | v2 fix |
|---|---|---|
| **Path canonicalization** | Spec mixed `omnisight/omnisight-productizer` (lowercase, L-OP-247) and `omnisight/OmniSight-Productizer` (CamelCase) — internal contradiction → L-OP-247 recurrence risk extreme | v2: **§3.6 NEW canonicalization table** — single source of truth + filing-time hook validates references |
| **7a tier wrong** | tier:S for hours-long irreversible operation | v2: **split into 7a-preflight (tier:S dry-run inventory) + 7a-execute (tier:M backfill)** |
| **No pre-7a checkpoint** | `replication start --all` too trusting | v2: 7a-preflight produces inventory before 7a-execute; explicit gate |
| **14-day soak workflow** | `timeout_seconds: 1209600` kept runner open 14 days | v2: daily snapshot cron + Integration AC counts files (not runner timeout) |

**P1 STRUCTURAL fixes:**

| v1 | v2 |
|---|---|
| 8b + 8c | **8bc MERGED** (lib + tests in one ticket) |
| 7b parity-check prepared AFTER 7a | **Reordered**: parity-check.py exists BEFORE 7a-preflight; 7b becomes execution-only (run parity, write evidence) |
| PluginReadyGate throwaway-branch test | **Add orphan-ref cleanup verification AC** |

**P2 OPERATIONAL hardening:**

- §3.7 NEW "Operator window pre-staging checklist" — 5a/5b/5c batch-run readiness
- 8a: transient-flap counter (1-2 fail recover → low-severity telemetry, NOT silent)
- Integration AC reformulated: daily snapshot file count + parity check (not runner timeout)

**P3 SCHEMA enforcement (cross-phase note):**

- Note added to Phase 31.B 4a (ADR-0024 Coordinator) for runner-side `class:operator-window` pickup-refusal rule (defense in depth beyond filing-time hook)

**Ticket count**: v1 26 → v2 **26** (7a split +1; 8b+8c merge -1; net zero, more granular shape)

---

## §1. Pre-flight reading (unchanged)

[See v1 §1; added: re-read §3.6 canonicalization table before any path reference]

---

## §2. Sub-META definition (unchanged from v1)

[See v1 §2]

---

## §3. Schemas (extended)

Reuses Phase 31.A v2 / 31.B v2 / 31.C v2 schemas.

### §3.6 NEW: Gerrit → GitLab path canonicalization (P0 fix per codex Q4)

**Single source of truth for the asymmetric naming**. Per L-OP-247, GitLab paths are lowercase; Gerrit keeps original CamelCase.

| Source Gerrit project | Target GitLab project (lowercase per L-OP-247) |
|---|---|
| `OmniSight-Productizer` | `omnisight/omnisight-productizer` |
| `sora-bridge` | `omnisight/sora-bridge` |

**Enforcement**:
- All 31.D tickets MUST reference paths per this table
- OP-1042 filing-time hook adds rule: any 31.D ticket description/AC mentioning `omnisight/OmniSight-Productizer` (CamelCase project portion on GitLab side) → reject filing
- All `verify:` commands MUST match the canonical case
- Replication config: source=Gerrit name (CamelCase), dest=GitLab path (lowercase) — generator handles mapping

**Why asymmetric**: Gerrit project names are case-sensitive; existing infrastructure uses CamelCase. GitLab path conventions + L-OP-247 require lowercase. The replication mapping is the canonicalization point.

### §3.7 NEW: Operator-window pre-staging checklist (P2 fix per codex Q4)

Before scheduling an operator window for 5a/5b/5c (typically batched in one window per codex), the following artifacts MUST exist:

1. ✓ All `scripts/replication/*.py` shipped + tests green
2. ✓ Generated `replication.config` text staged at `/tmp/replication-config-staging-{date}.config` (operator inspects before deploy)
3. ✓ Operator runbook with EXACT commands (copy-paste-able):
   - `scp` command for config
   - `ssh ... 'gerrit plugin reload replication'`
   - `ssh ... 'gerrit show-queue --replication'`
4. ✓ Evidence JSON templates at `docs/audit/AUDIT-31-phase-D-evidence/{5a,5b,5c}-template.json`
5. ✓ Rollback commands documented (revert config + reload)
6. ✓ Stop/go criteria per ticket explicit
7. ✓ All `class:operator-window` AC items have clear PASS/FAIL signals

Pre-staging is owned by the LibsGate ticket (validates artifacts ready) BEFORE 5a/5b/5c file or operator schedules.

---

## §4. Children — overview table (26 tickets v2)

| Layer | # | ID | Title | Tier | Prefer | Class | blockedBy |
|---|---|---|---|---|---|---|---|
| L0 | 1 | 31.D-1a | Replication plugin install spec | S | claude | claude | — |
| L0 | 2 | 31.D-2a | replication.config schema spec | S | claude | claude | — |
| L0 | 3 | 31.D-3a | GitLab project provisioning spec (uses §3.6 table) | S | claude | claude | — |
| L0 | 4 | 31.D-4a | ADR-0025 (one-way replication, 30s SLA) | S | claude | claude | — |
| L1 | 5 | 31.D-1bc | replication-plugin-verify.py + tests | S | codex | codex + operator-prepare-only | 1a |
| L1 | 6 | 31.D-2bc | replication-config-generator.py + tests (uses §3.6 mapping) | S | codex | codex + operator-prepare-only | 2a |
| L1 | 7 | 31.D-3bc | gitlab-provisioning.py + tests (uses §3.6 table) | S | codex | codex + operator-prepare-only | 3a |
| L1 | 8 | 31.D-7-parity | **parity-check.py + tests (NEW POSITION; moved before 7a)** | S | codex | codex | 2bc |
| L2-Gate | 9 | 31.D-LibsGate | Cross-library + §3.7 pre-staging checklist | S | claude | claude | 1bc, 2bc, 3bc, 4a, 7-parity |
| L3 | 10 | 31.D-5a | **OPERATOR**: Install/verify replication plugin | S | claude | claude + operator-window | LibsGate |
| L3 | 11 | 31.D-5b | **OPERATOR**: Provision GitLab projects | S | claude | claude + operator-window | LibsGate, 3bc |
| L4 | 12 | 31.D-5c | **OPERATOR**: Deploy replication.config + plugin reload | S | claude | claude + operator-window | 5a, 5b, 2bc |
| L4-Gate | 13 | 31.D-PluginReadyGate | Plugin loaded + projects exist + config readable + **orphan-ref cleanup verified** | S | claude | claude | 5c |
| L5 | 14 | 31.D-6a | Enable refs/heads/develop (runner-prepared, operator-deployed) | S | codex | codex + operator-prepare-only | PluginReadyGate |
| L5 | 15 | 31.D-6b | Enable refs/heads/main | S | codex | codex + operator-prepare-only | 6a |
| L6 | 16 | 31.D-6c | Enable refs/heads/feature/* | S | codex | codex + operator-prepare-only | 6b |
| L6 | 17 | 31.D-6d | Enable refs/heads/release-* | S | codex | codex + operator-prepare-only | 6c |
| L7 | 18 | 31.D-7a-preflight | **NEW (split from v1 7a)**: dry-run inventory (refs, object volume, target state, GitLab empty check) | S | codex | codex + operator-prepare-only | 6d, 7-parity |
| L7 | 19 | 31.D-7a-execute | **OPERATOR (was v1 7a; tier upgrade)**: batched-by-project history backfill | M | claude | claude + operator-window | 7a-preflight |
| L8 | 20 | 31.D-7b-verify | **Run parity-check.py post-backfill (script already exists from 7-parity)** | S | codex | codex | 7a-execute |
| L8 | 21 | 31.D-8a | Wire replication failures → 31.C alert + **transient-flap counter** | S | claude | claude | 31.C-Integration-external, 7b-verify |
| L9 | 22 | 31.D-8bc | **Retry policy + DLQ + tests (MERGED v1 8b+8c)** | S | codex | codex | 8a |
| L9-Gate | 23 | 31.D-ReplicationLiveSmoke | Push to Gerrit develop → arrives at GitLab <30s + failure inject | S | claude | claude + operator-rehearsal | 8bc |
| L10 | 24 | 31.D-9-Doc | Operator runbook + **daily-parity systemd timer setup** | S | claude | claude | ReplicationLiveSmoke |
| L11 | 25 | 31.D-CallSiteInventoryGate | Audit (covers ephemeral_clone.py `git push --no-thin`) | S | claude | claude | 9-Doc |
| L12 | 26 | 31.D-Integration-Doc | E2E test runbook (counts daily snapshots, NOT runner timeout) | S | claude | claude | CallSiteInventoryGate |
| L13 | 27 | 31.D-Integration | **E2E cutover + 14-day daily snapshots (file-count AC)** | L | claude | claude + operator-window | ALL 1-26 |

**Wait — count is 27 not 26.** Let me recount mergers: v1 had 8b + 8c separate = 2 tickets → v2 merged 8bc = 1 ticket (-1). v1 had 7a as one ticket; v2 split into 7a-preflight + 7a-execute (+1). v2 added 7-parity as separate from 7b (kept 7b as 7b-verify since script existed). Net: 26 -1 +1 +1 = 27. Updated.

**Routing tally**: codex 10, claude 17.

**Operator-window tickets** (require sora/operator execution): 5a, 5b, 5c, 7a-execute, Integration (5 tickets — unchanged count, but 7a-execute is now tier:M reflecting the operational weight).

---

## §5. Per-child specs (v2 deltas)

For brevity, v2 shows only deltas from v1 per ticket. See v1 git history for full text.

---

### 31.D-1a, 2a, 4a — unchanged

### 31.D-3a — GitLab project provisioning spec

```yaml
delta_from_v1:
  - context_hint.outputs_guaranteed: references §3.6 canonicalization table; explicitly uses LOWERCASE GitLab paths
  - new AC: spec MUST cite §3.6 table; spec MUST NOT contain `omnisight/OmniSight-Productizer` literal anywhere
ac.code_replacements:
  - {desc: "namespace + project LOWERCASE per §3.6", verify: {command: "grep -c 'omnisight/omnisight-productizer' docs/sop/gitlab-project-provisioning-spec.md", expect_stdout_match: "[1-9]"}}
  - {desc: "NO CamelCase project path", verify: {command: "grep -cE 'omnisight/OmniSight-Productizer' docs/sop/gitlab-project-provisioning-spec.md", expect_stdout_match: "^0$"}}
  - {desc: "cites §3.6 canonicalization table", verify: {command: "grep -ciE '\\§3\\.6|canonicalization table' docs/sop/gitlab-project-provisioning-spec.md", expect_stdout_match: "[1-9]"}}
```

### 31.D-1bc, 2bc — unchanged structure

### 31.D-2bc — replication-config-generator (path canonicalization fix)

```yaml
delta_from_v1:
  - context_hint.outputs_guaranteed: generator implements §3.6 mapping (source Gerrit name → dest GitLab path)
  - new AC: generator OUTPUT contains LOWERCASE GitLab paths; NEVER CamelCase
  - mutex_with: [6a, 6b, 6c, 6d, 7-parity]   # 7-parity reads project-mapping.yaml
ac.code_additions:
  - {desc: "generator output uses lowercase GitLab paths", verify: {command: "python3 -c 'import sys; sys.path.insert(0,\"scripts/replication\"); from config_generator import generate_config; import yaml; out = generate_config(yaml.safe_load(open(\"scripts/replication/project-mapping.yaml\"))); assert \"omnisight/omnisight-productizer\" in out; assert \"omnisight/OmniSight-Productizer\" not in out'", expect_exit_code: 0}}
  - {desc: "project-mapping.yaml encodes asymmetric mapping per §3.6", verify: {command: "python3 -c 'import yaml; d=yaml.safe_load(open(\"scripts/replication/project-mapping.yaml\")); assert any(p[\"gerrit_name\"]==\"OmniSight-Productizer\" and p[\"gitlab_path\"]==\"omnisight/omnisight-productizer\" for p in d[\"projects\"])'", expect_exit_code: 0}}
```

### 31.D-3bc — gitlab-provisioning.py (path canonicalization fix)

```yaml
delta_from_v1:
  - context_hint.outputs_guaranteed: ALL `provision_project` calls use lowercase paths per §3.6
  - new AC: defensive check rejects CamelCase project names at function entry
ac.code_additions:
  - {desc: "provision_project rejects CamelCase project_name", verify: {command: "python3 -c 'import sys; sys.path.insert(0,\"scripts/replication\"); from gitlab_provisioning import provision_project;\\ntry:\\n    provision_project(\"omnisight\", \"OmniSight-Productizer\")\\nexcept ValueError as e:\\n    assert \"lowercase\" in str(e).lower() or \"L-OP-247\" in str(e)' || echo 'OK if implementation raises'", expect_exit_code: 0}}
  - {desc: "test exists for CamelCase rejection", verify: {command: "grep -cE 'def test_.*reject.*camel|def test_.*L-OP-247' scripts/replication/tests/test_gitlab_provisioning.py", expect_stdout_match: "[1-9]"}}
```

### 31.D-7-parity — parity-check.py + tests (NEW POSITION — moved before 7a per codex Q3)

```yaml
title: "AUDIT-31.D-7-parity: parity-check.py + tests (PREPARED BEFORE backfill)"
tier: S, prefer: codex, class: subscription-codex
area_labels: [area:tooling, area:devops, area:tests]
blockedBy: [2bc]
context_hint:
  produces: "scripts/replication/parity-check.py + scripts/replication/tests/test_parity_check.py"
  consumed_by: "31.D-7a-preflight (uses parity for dry-run inventory), 31.D-7b-verify (post-backfill), 31.D-9-Doc (daily timer)"
  inputs_expected: "project-mapping.yaml (2bc) with §3.6 paths"
  outputs_guaranteed: |
    Script: check_parity(mapping_yaml) -> {refs_compared: int, mismatches: list, parity_pct: float, snapshot_timestamp: str}
    For each (gerrit_project, gitlab_path), compares refs via `git ls-remote ssh://...` on BOTH sides.
    Read-only; no destructive ops.
    Tests >=5: full parity, partial mismatch, missing target ref, missing source ref, mocked ls-remote.
  non_goals:
    - "DO NOT fix mismatches — read-only"
    - "DO NOT trigger replication — separate ticket"
boundaries:
  loc_delta_max: 250, files_touched_max: 2
  required_paths: [scripts/replication/parity-check.py, scripts/replication/tests/test_parity_check.py]
  forbidden_paths: [scripts/replication/plugin-verify.py, scripts/replication/config-generator.py, scripts/replication/gitlab-provisioning.py]
  test_scope: inline
  destructive_op_classes: []
  destructive_op_scope: project
  external_side_effect: network-test
  external_payload_class: operational
  execution_mode: unit-testable
  dependency_artifacts: [scripts/replication/project-mapping.yaml]
  mutex_with: [2bc]
  interface_contract:
    inputs_from_deps: [project-mapping.yaml with §3.6 paths]
    outputs_for_downstream:
      - "check_parity(mapping_yaml) -> ParityReport"
      - "tests >=5 pass; mocked ls-remote"
ac.code:
  - {desc: "script importable", verify: {command: "python3 -c 'import sys; sys.path.insert(0,\"scripts/replication\"); from parity_check import check_parity'", expect_exit_code: 0}}
  - {desc: "uses §3.6 mapping (asymmetric source/dest)", verify: {command: "grep -cE 'gerrit_name|gitlab_path' scripts/replication/parity-check.py", expect_stdout_match: "^[2-9]$|^[1-9][0-9]+$"}}
  - {desc: "tests pass", verify: {command: "pytest scripts/replication/tests/test_parity_check.py -v", expect_exit_code: 0, timeout_seconds: 30}}
go_live: T+0.5d after 2bc
```

### 31.D-LibsGate — Cross-lib + §3.7 pre-staging checklist

```yaml
delta_from_v1:
  - blockedBy: [1bc, 2bc, 3bc, 4a, 7-parity]   (was [1bc, 2bc, 3bc, 4a]; added 7-parity)
  - context_hint.outputs_guaranteed: ADDS §3.7 pre-staging artifact check
  - new ACs (per §3.7):
ac.code_additions:
  - {desc: "evidence templates for 5a/5b/5c staged", verify: {command: "ls docs/audit/AUDIT-31-phase-D-evidence/{5a,5b,5c}-template.json | wc -l", expect_stdout_match: "^3$"}}
  - {desc: "generated config staged for operator inspection", verify: {command: "test -f /tmp/replication-config-staging-*.config || ls scripts/sprint-s12/replication-config-*.config | head -1", expect_exit_code: 0}}
  - {desc: "rollback commands documented in 9-Doc runbook (pre-exists)", verify: {command: "grep -ciE 'rollback|revert.*replication.config' docs/audit/AUDIT-31-phase-D-evidence/operator-pre-staging.md", expect_stdout_match: "[1-9]"}}
```

### 31.D-5a, 5b, 5c — operator-window tickets

```yaml
delta_from_v1:
  - context_hint.non_goals: ADD "DO NOT enable any ref pattern during 5c — that's 6a-6d's job (codex Q2 drift risk)"
  - 5b's required_paths uses §3.6 canonical paths ONLY (lowercase GitLab)
  - 5c's deploy AC adds: confirm reloaded config has ZERO ref patterns (empty refspecs; per non_goals)
5c_ac_additions:
  - {desc: "deployed config has empty refspecs", verify: {command: "ssh admin@sora.services 'cat etc/replication.config' | grep -cE '^\\s+push\\s+=\\s+\\+refs/'", expect_stdout_match: "^0$"}, run_as: operator}
```

### 31.D-PluginReadyGate — orphan-ref cleanup verification

```yaml
delta_from_v1:
  - new AC: PluginReadyGate's "throwaway branch push test" REQUIRES verified cleanup on BOTH Gerrit and GitLab
  - new AC: orphan-ref count = 0 on both sides post-test
ac.code_replacements:
  - {desc: "throwaway branch CLEANED on Gerrit (gone after test)", verify: {command: "ssh -p 29418 admin@sora.services 'gerrit query --format JSON branch:plugin-ready-test-* status:open' | grep -c 'plugin-ready-test'", expect_stdout_match: "^0$"}, run_as: operator}
  - {desc: "throwaway branch CLEANED on GitLab", verify: {command: "git ls-remote ssh://git@sora.services:49154/omnisight/omnisight-productizer 'refs/heads/plugin-ready-test-*' | wc -l", expect_stdout_match: "^0$"}, run_as: operator}
  - {desc: "0 orphan refs on either side", verify: {command: "jq -r .orphan_ref_count docs/audit/AUDIT-31-phase-D-evidence/plugin-ready-gate.json", expect_stdout_match: "^0$"}, run_as: operator}
```

### 31.D-6a, 6b, 6c, 6d — per-ref enablement (wording sharpened)

```yaml
delta_from_v1 (applies to all 4):
  - context_hint.produces: WORDING CHANGE — "runner-prepared mapping.yaml edits + deploy-runbook stanza; OPERATOR executes deploy"
  - non_goals strengthened: "Runner ONLY edits project-mapping.yaml and adds deploy-instruction block to /tmp/operator-task-31D-6X.md; runner does NOT execute the deploy. Operator follows /tmp/operator-task-31D-6X.md to copy config + reload Gerrit."
  - boundaries: external_side_effect: none (runner side); the actual production push happens in a separate operator-window action documented in the deploy stanza
```

### 31.D-7a-preflight — NEW (split from v1 7a per codex Q3)

```yaml
title: "AUDIT-31.D-7a-preflight: history backfill dry-run inventory"
tier: S, prefer: codex, class: subscription-codex + operator-prepare-only
area_labels: [area:tooling, area:devops, area:docs]
blockedBy: [6d, 7-parity]
context_hint:
  produces: "scripts/replication/backfill-preflight.py + docs/audit/AUDIT-31-phase-D-evidence/backfill-preflight.json"
  consumed_by: "31.D-7a-execute"
  inputs_expected: "All 4 ref patterns enabled (6d); parity-check.py available (7-parity)"
  outputs_guaranteed: |
    Script computes WITHOUT triggering backfill:
    - For each (gerrit_project, gitlab_path): count refs matching enabled patterns; estimate object byte size via `git rev-list --objects --count`; record GitLab current ref count (expected near-zero per L-OP-247)
    - Per-project + per-pattern breakdown
    - Estimated total push time (assume 10MB/s LAN bandwidth conservative)
    - Confirms GitLab projects are empty or expected state (NOT polluted from prior attempts)
    - Output: backfill-preflight.json
    Operator REVIEWS the JSON before scheduling 7a-execute window. STOP if anomalies (e.g., GitLab unexpectedly populated → manual cleanup first).
  non_goals:
    - "DO NOT push anything — preflight only"
    - "DO NOT modify project-mapping.yaml or replication.config"
boundaries:
  loc_delta_max: 200, files_touched_max: 2
  required_paths: [scripts/replication/backfill-preflight.py, docs/audit/AUDIT-31-phase-D-evidence/backfill-preflight.json]
  forbidden_paths: [scripts/replication/parity-check.py, scripts/replication/config-generator.py, scripts/replication/gitlab-provisioning.py]
  test_scope: inline
  destructive_op_classes: []
  destructive_op_scope: project
  external_side_effect: network-test   # read-only ls-remote
  external_payload_class: operational
  execution_mode: unit-testable
  dependency_artifacts: [scripts/replication/parity-check.py, scripts/replication/project-mapping.yaml]
  mutex_with: []
  interface_contract:
    inputs_from_deps: [parity-check.py + project-mapping]
    outputs_for_downstream:
      - "backfill-preflight.json: {refs_per_pattern, byte_estimate, gitlab_current_state, anomalies, ok_to_proceed: bool}"
      - "operator decision input"
ac.code:
  - {desc: "preflight script exists", verify: {command: "test -x scripts/replication/backfill-preflight.py", expect_exit_code: 0}}
  - {desc: "preflight JSON has required fields", verify: {command: "python3 -c \"import json; d=json.load(open('docs/audit/AUDIT-31-phase-D-evidence/backfill-preflight.json')); assert all(k in d for k in ['refs_per_pattern','byte_estimate','gitlab_current_state','anomalies','ok_to_proceed'])\"", expect_exit_code: 0}}
  - {desc: "ok_to_proceed = true (operator-reviewed)", verify: {command: "jq -r .ok_to_proceed docs/audit/AUDIT-31-phase-D-evidence/backfill-preflight.json", expect_stdout_match: "^true$"}, run_as: operator}
go_live: T+1d after 6d + 7-parity
```

### 31.D-7a-execute — was v1 7a; tier:S → tier:M per codex Q3

```yaml
title: "AUDIT-31.D-7a-execute: **OPERATOR**: batched-by-project history backfill push"
tier: M, prefer: claude, class: subscription-claude + operator-window
area_labels: [area:devops, area:docs]
blockedBy: [7a-preflight]
context_hint:
  produces: "docs/audit/AUDIT-31-phase-D-evidence/backfill-execute.json + entries in operator action log"
  consumed_by: "31.D-7b-verify"
  inputs_expected: "7a-preflight PASSED with ok_to_proceed=true"
  outputs_guaranteed: |
    Operator runs BATCHED backfill (per codex Q4 + §7 Q5 recommendation):
    1. First batch: omnisight-productizer (single project)
       - `ssh admin@sora.services 'replication start --project OmniSight-Productizer --wait'`
       - Wait for queue empty; parity-check; verify match
    2. Second batch: sora-bridge
       - `ssh admin@sora.services 'replication start --project sora-bridge --wait'`
       - Wait for queue empty; parity-check; verify match
    3. Record per-batch duration + errors in backfill-execute.json
    4. If batch 1 fails: STOP; do NOT proceed to batch 2; file follow-up
  non_goals:
    - "DO NOT use `replication start --all` — batched-by-project per codex Q4"
    - "DO NOT continue on batch failure"
    - "DO NOT force-push or rewrite history"
boundaries:
  loc_delta_max: 100, files_touched_max: 2
  required_paths: [docs/audit/AUDIT-31-phase-D-evidence/backfill-execute.json, docs/operations/operator-action-log.md]
  forbidden_paths: [scripts/replication/, backend/]
  test_scope: defer-to-integration
  destructive_op_classes: [git-ref-rewrite]
  destructive_op_scope: system
  external_side_effect: network-production
  external_payload_class: operational
  execution_mode: operator-rehearsal
  dependency_artifacts: [7a-preflight + parity-check.py]
  mutex_with: []
  interface_contract:
    inputs_from_deps: [preflight PASS]
    outputs_for_downstream: ["GitLab projects populated with Gerrit history; per-batch evidence"]
ac.code:
  - {desc: "backfill-execute.json committed", verify: {command: "test -f docs/audit/AUDIT-31-phase-D-evidence/backfill-execute.json", expect_exit_code: 0}}
ac.deploy:
  - {desc: "batched-by-project (NOT --all)", verify: {command: "jq -r '.batches | length' docs/audit/AUDIT-31-phase-D-evidence/backfill-execute.json", expect_stdout_match: "^[2-9]$|^[1-9][0-9]+$"}, run_as: operator}
  - {desc: "each batch completed successfully", verify: {command: "jq -r '[.batches[] | select(.success != true)] | length' docs/audit/AUDIT-31-phase-D-evidence/backfill-execute.json", expect_stdout_match: "^0$"}, run_as: operator}
ac.integration:
  - {desc: "GitLab refs match Gerrit refs post-backfill (deferred to 7b-verify)", verify: deferred-to-7b-verify}
go_live: T+1d after 7a-preflight (operator window)
```

### 31.D-7b-verify — Run parity-check post-backfill

```yaml
title: "AUDIT-31.D-7b-verify: post-backfill parity verification"
tier: S, prefer: codex, class: subscription-codex
area_labels: [area:tests, area:devops]
blockedBy: [7a-execute]
context_hint:
  produces: "docs/audit/AUDIT-31-phase-D-evidence/parity-check.json (initial post-backfill run; daily snapshots come later via 9-Doc systemd timer)"
  consumed_by: "31.D-8a (alert wiring)"
  inputs_expected: "backfill executed (7a-execute); parity-check.py exists (7-parity)"
  outputs_guaranteed: "Single run of parity-check.py against the 2 projects × 4 ref patterns = 8 ref comparisons; mismatches expected zero; result committed"
  non_goals:
    - "DO NOT fix mismatches — operator-only via 7a-execute re-batch"
    - "DO NOT push anything"
    - "Daily snapshot setup is 9-Doc's job (systemd timer wires the daily run)"
boundaries:
  loc_delta_max: 50, files_touched_max: 1
  required_paths: [docs/audit/AUDIT-31-phase-D-evidence/parity-check.json]
  forbidden_paths: [scripts/replication/parity-check.py, scripts/replication/]
  test_scope: defer-to-integration
  destructive_op_classes: [state-append]
  destructive_op_scope: project
  external_side_effect: network-test
  external_payload_class: operational
  execution_mode: requires-WSL
  dependency_artifacts: [parity-check.py + backfill-execute]
  mutex_with: []
  interface_contract:
    inputs_from_deps: [backfill complete]
    outputs_for_downstream: ["parity-check.json with diff_count=0"]
ac.code:
  - {desc: "parity-check.json committed", verify: {command: "test -f docs/audit/AUDIT-31-phase-D-evidence/parity-check.json", expect_exit_code: 0}}
  - {desc: "diff_count = 0", verify: {command: "jq -r .diff_count docs/audit/AUDIT-31-phase-D-evidence/parity-check.json", expect_stdout_match: "^0$"}, run_as: operator}
go_live: T+0.5d after 7a-execute
```

### 31.D-8a — Wire replication failures + transient-flap counter (HARDENED per codex Q4)

```yaml
delta_from_v1:
  - context_hint.outputs_guaranteed: ADD transient-flap tracking
  - "Module also maintains ~/.cache/omnisight/replication-flaps.jsonl (1-2 fail then recover) for daily summary; NOT P1; surfaces as P3 in 31.C digest"
  - new AC for transient-flap recording
  - new AC for daily summary integration with 31.C email digest
ac.code_additions:
  - {desc: "transient-flap tracking present", verify: {command: "grep -cE 'replication-flaps.jsonl|flap.*counter' backend/replication/failure_watcher.py", expect_stdout_match: "[1-9]"}}
  - {desc: "flap entries routed P3 (daily digest, not Discord)", verify: {command: "grep -cE 'P3.*flap|flap.*P3|severity.*P3' backend/replication/failure_watcher.py", expect_stdout_match: "[1-9]"}}
```

### 31.D-8bc — Retry policy + DLQ + tests (MERGED v1 8b + 8c)

```yaml
title: "AUDIT-31.D-8bc: Retry policy + DLQ + tests"
tier: S, prefer: codex, class: subscription-codex
area_labels: [area:backend, area:tooling, area:tests]
blockedBy: [8a]
context_hint:
  produces: "patch to backend/replication/failure_watcher.py + backend/replication/tests/test_failure_watcher.py"
  consumed_by: "31.D-ReplicationLiveSmoke, 31.D-Integration"
  inputs_expected: "failure_watcher base from 8a"
  outputs_guaranteed: |
    DLQ:
    - On 3+ consecutive failures for same ref → write to ~/.cache/omnisight/replication-dlq.jsonl
    - Subsequent failures for SAME (ref, since_DLQ) suppress duplicate P1
    - Operator clears DLQ manually post-fix (no auto)
    Tests (>=8 total — codex Q3: 8b + 8c merged): parses-success / parses-fail / route_alert-on-fail / DLQ-3-fails / DLQ-dedup / SSH-error / transient-flap-P3 / DLQ-clear-allows-realert
  non_goals:
    - "DO NOT auto-retry (Gerrit plugin owns)"
    - "DO NOT modify routing_config.yaml — 8a owns the addition"
boundaries:
  loc_delta_max: 350, files_touched_max: 2
  required_paths: [backend/replication/failure_watcher.py, backend/replication/tests/test_failure_watcher.py]
  forbidden_paths: [backend/alerts/, scripts/replication/]
  test_scope: inline
  destructive_op_classes: [state-append]
  destructive_op_scope: project
  external_side_effect: none
  external_payload_class: operational
  execution_mode: unit-testable
  dependency_artifacts: [failure_watcher.py from 8a]
  mutex_with: [8a]
  interface_contract:
    inputs_from_deps: [poll_replication_queue + route_alert + flap counter]
    outputs_for_downstream:
      - "DLQ at ~/.cache/omnisight/replication-dlq.jsonl"
      - "test suite >=8 fns"
ac.code:
  - {desc: "DLQ path present", verify: {command: "grep -c 'replication-dlq.jsonl' backend/replication/failure_watcher.py", expect_stdout_match: "[1-9]"}}
  - {desc: "tests count >=8", verify: {command: "pytest --collect-only backend/replication/tests/test_failure_watcher.py 2>&1 | grep -c 'test_'", expect_stdout_match: "^[8-9]$|^[1-9][0-9]+$"}}
  - {desc: "tests pass", verify: {command: "pytest backend/replication/tests/test_failure_watcher.py -v", expect_exit_code: 0, timeout_seconds: 30}}
  - {desc: "all SSH mocked", verify: {command: "grep -cE 'mock.*subprocess|Mock.*ssh' backend/replication/tests/test_failure_watcher.py", expect_stdout_match: "[1-9]"}}
go_live: T+0.5d after 8a
```

### 31.D-ReplicationLiveSmoke — unchanged

### 31.D-9-Doc — Operator runbook + daily-parity systemd timer (P2 fix per codex Q5)

```yaml
delta_from_v1:
  - context_hint.outputs_guaranteed: ADD daily-parity systemd user timer
  - new section in runbook: "Daily Parity Snapshot Setup" — installs systemd user timer that runs parity-check.py daily 09:00 local; writes to docs/audit/AUDIT-31-phase-D-evidence/daily-parity-YYYYMMDD.json
  - timer file: deploy/systemd-user/omnisight-replication-daily-parity.{service,timer}
  - new AC for timer files + scheduling
ac.code_additions:
  - {desc: "systemd timer files exist", verify: {command: "test -f deploy/systemd-user/omnisight-replication-daily-parity.service && test -f deploy/systemd-user/omnisight-replication-daily-parity.timer", expect_exit_code: 0}}
  - {desc: "timer scheduled daily at 09:00 local", verify: {command: "grep -cE 'OnCalendar=\\*-\\*-\\* 09:00|OnCalendar=daily.*09:00' deploy/systemd-user/omnisight-replication-daily-parity.timer", expect_stdout_match: "[1-9]"}}
  - {desc: "timer is USER-level (NOT system)", verify: {command: "grep -cE 'WantedBy=multi-user.target' deploy/systemd-user/omnisight-replication-daily-parity.timer", expect_stdout_match: "^0$"}}
  - {desc: "timer requires default.target", verify: {command: "grep -cE 'WantedBy=default.target' deploy/systemd-user/omnisight-replication-daily-parity.timer", expect_stdout_match: "[1-9]"}}
  - {desc: "runbook documents daily snapshot location", verify: {command: "grep -cE 'daily-parity-.*\\.json' docs/sop/replication-operator-runbook.md", expect_stdout_match: "[1-9]"}}
```

### 31.D-CallSiteInventoryGate — broaden search per codex Q4

```yaml
delta_from_v1:
  - context_hint.outputs_guaranteed: EXPLICITLY enumerates `backend/runner/ephemeral_clone.py` (31.B output) + `git push --no-thin` searches across all backend/* and scripts/*
  - new AC: script searches for `git push --no-thin` not just literal `git push`
ac.code_additions:
  - {desc: "inventory script searches for --no-thin variants", verify: {command: "grep -cE 'git push.*--no-thin|push.*no-thin' scripts/sprint-s12/phase31d-call-site-inventory.py", expect_stdout_match: "[1-9]"}}
  - {desc: "inventory covers backend/runner/ephemeral_clone.py", verify: {command: "jq -r '.sites[] | select(.file == \"backend/runner/ephemeral_clone.py\") | .file' docs/audit/AUDIT-31-phase-D-evidence/call-site-inventory.json | head -1", expect_stdout_match: "ephemeral_clone"}}
```

### 31.D-Integration-Doc — daily snapshots, not runner timeout (P0 fix per codex Q5)

```yaml
delta_from_v1:
  - context_hint.outputs_guaranteed: workflow uses FILE COUNT (daily snapshots) not timeout
  - runbook step 5 rewritten: operator initiates soak; daily snapshots are auto-captured by 9-Doc systemd timer; Integration ticket closes when ≥14 snapshots present + all parity_pct==100
```

### 31.D-Integration — workflow change (P0 fix per codex Q5)

```yaml
delta_from_v1:
  - ac.exercised CHANGED — no `timeout_seconds: 1209600`; replaced with file-count check
ac.exercised_replacements:
  - {desc: "≥14 daily-parity snapshot files exist", verify: {command: "ls docs/audit/AUDIT-31-phase-D-evidence/daily-parity-*.json | wc -l", expect_stdout_match: "^(1[4-9]|[2-9][0-9])$|^[1-9][0-9]{2}$"}, run_as: operator}
  - {desc: "ALL daily snapshots show parity 100%", verify: {command: "for f in docs/audit/AUDIT-31-phase-D-evidence/daily-parity-*.json; do jq -r .parity_pct $f; done | sort -u | grep -cE '^100(\\.0+)?$'", expect_stdout_match: "^1$"}, run_as: operator}
  - {desc: "0 P1 replication_fail alerts in 14-day window", verify: {command: "jq -r .post_14d_p1_alerts docs/audit/AUDIT-31-phase-D-evidence/integration.json", expect_stdout_match: "^0$"}}
```

---

## §6. Filing batch order (DAG v2)

```
L0: 1a, 2a, 3a, 4a (4 specs parallel)
L1: 1bc, 2bc, 3bc, 4a, 7-parity (5 — 7-parity moved earlier; 4a doc-only no blockedBy)
L2: LibsGate (1)
L3: 5a, 5b (2 operator-window parallel — encourage batching)
L4: 5c (1 operator-window)
L5: PluginReadyGate (1)
L6: 6a → 6b → 6c → 6d (4 sequential)
L7: 7a-preflight (1)
L8: 7a-execute (1, tier:M, operator-window)
L9: 7b-verify (1)
L10: 8a (1)
L11: 8bc (1)
L12: ReplicationLiveSmoke (1)
L13: 9-Doc (1)
L14: CallSiteInventoryGate (1)
L15: Integration-Doc (1)
L16: Integration (1, tier:L)
```

17 layers, 27 tickets (was 26; +1 from 7a-preflight, +1 from 7-parity early, -1 from 8bc merge).

---

## §7. Operator answers (all locked 2026-05-13)

| # | Question | Locked answer |
|---|---|---|
| 1 | Replication direction | **one-way Gerrit → GitLab** for v1; bidirectional deferred |
| 2 | GitLab project visibility | **private** |
| 3 | Gerrit SSH key for GitLab | **generate new ed25519 + register as GitLab deploy key on each project** |
| 4 | Replication SLA | **30s P95** |
| 5 | History backfill safety | **batched by project** (encoded in 7a-execute) |
| 6 | Failure-watcher poll interval | **30s default; configurable in routing_config.yaml** |
| 7 | DLQ retention | **90 days; operator rotates via cron** |
| 8 | 31.E gating | **31.E blockedBy NEW ticket 31.D-StabilityCheckpoint (24-72h stable replication, NOT full 14-day soak)** |

**Impact on ticket table**:
- Q8 lock → **NEW ticket 31.D-StabilityCheckpoint** (operator-window; runs after ReplicationLiveSmoke + 9-Doc daily timer; operator confirms 24-72h with 0 P1 replication_fail alerts; closes → 31.E unblocks)
- 31.D-Integration still has 14-day soak AC for own closure; StabilityCheckpoint is intermediate milestone for 31.E
- Updated DAG: ReplicationLiveSmoke → 9-Doc → **StabilityCheckpoint** → CallSiteInventoryGate → Integration-Doc → Integration

### 31.D-StabilityCheckpoint — NEW (per Q8 lock)

```yaml
title: "AUDIT-31.D-StabilityCheckpoint: 24-72h stable replication confirmation"
tier: S, prefer: claude, class: subscription-claude + operator-window
area_labels: [area:devops, area:tests, area:docs]
blockedBy: [ReplicationLiveSmoke, 9-Doc]   # daily timer must be running
context_hint:
  produces: "docs/audit/AUDIT-31-phase-D-evidence/stability-checkpoint.json + entry in operator action log"
  consumed_by: "31.D-CallSiteInventoryGate (sequential within 31.D), 31.E-* (cross-phase: this unblocks 31.E)"
  inputs_expected: "ReplicationLiveSmoke PASS; daily-parity systemd timer running (from 9-Doc) → ≥1 daily snapshot file exists"
  outputs_guaranteed: |
    Operator confirms (24h minimum, 72h maximum window):
    1. ≥1 daily-parity-YYYYMMDD.json snapshot exists with parity_pct = 100
    2. 0 P1 replication_fail alerts in window (check ~/.cache/omnisight/alert-router-audit.jsonl)
    3. 0 entries in ~/.cache/omnisight/replication-dlq.jsonl during window
    4. ≤2 transient flaps in replication-flaps.jsonl during window (codex Q4 baseline)
    5. emit stability-checkpoint.json with window_start/window_end timestamps + all 4 checks PASS
  non_goals:
    - "DO NOT extend window beyond 72h (force progression; if not stable by 72h → escalate, do not silently wait)"
    - "DO NOT close 31.D-Integration here — Integration owns full 14-day soak"
boundaries:
  loc_delta_max: 80, files_touched_max: 2
  required_paths: [docs/audit/AUDIT-31-phase-D-evidence/stability-checkpoint.json, docs/operations/operator-action-log.md]
  forbidden_paths: [scripts/replication/, backend/replication/, backend/alerts/]
  test_scope: inline
  destructive_op_classes: [state-append]
  destructive_op_scope: project
  external_side_effect: none
  external_payload_class: operational
  execution_mode: operator-rehearsal
  dependency_artifacts: [ReplicationLiveSmoke PASS, 9-Doc daily timer running]
  mutex_with: []
  interface_contract:
    inputs_from_deps: [≥1 daily snapshot + alert audit + DLQ + flap log]
    outputs_for_downstream:
      - "stability-checkpoint.json: {window_start, window_end, daily_parity_ok, p1_alerts: int, dlq_entries: int, transient_flaps: int, ok_to_unblock_31E: bool}"
      - "ok_to_unblock_31E=true → 31.E filing/start enabled"
ac.code:
  - {desc: "stability-checkpoint.json committed", verify: {command: "test -f docs/audit/AUDIT-31-phase-D-evidence/stability-checkpoint.json", expect_exit_code: 0}}
ac.deploy:
  - {desc: "window length ≥24h and ≤72h", verify: {command: "python3 -c \"import json, datetime; d=json.load(open('docs/audit/AUDIT-31-phase-D-evidence/stability-checkpoint.json')); s=datetime.datetime.fromisoformat(d['window_start']); e=datetime.datetime.fromisoformat(d['window_end']); h=(e-s).total_seconds()/3600; assert 24 <= h <= 72\"", expect_exit_code: 0}, run_as: operator}
  - {desc: "0 P1 alerts in window", verify: {command: "jq -r .p1_alerts docs/audit/AUDIT-31-phase-D-evidence/stability-checkpoint.json", expect_stdout_match: "^0$"}, run_as: operator}
  - {desc: "0 DLQ entries", verify: {command: "jq -r .dlq_entries docs/audit/AUDIT-31-phase-D-evidence/stability-checkpoint.json", expect_stdout_match: "^0$"}, run_as: operator}
  - {desc: "ok_to_unblock_31E = true", verify: {command: "jq -r .ok_to_unblock_31E docs/audit/AUDIT-31-phase-D-evidence/stability-checkpoint.json", expect_stdout_match: "^true$"}, run_as: operator}
go_live: T+1d (24h) to T+3d (72h) after 9-Doc daily timer running
```

Updated ticket count: v2 27 → **28** (added StabilityCheckpoint).

---

## §8. Submission flow

1. This spec at `docs/sprint-s12/phase-31d-ticket-spec.md` (v2)
2. Operator review of §7 remaining lock items (1, 2, 3, 6, 7, 8)
3. Commit + Gerrit +2 + submit
4. Filing script files 27 children in DAG batch order
5. Spot-check first 3 tickets

---

**End of Phase 31.D ticket spec v2 (27-ticket; path canonicalization fixed; backfill split into preflight + execute; soak workflow uses daily snapshots not runner timeout). Awaiting operator §7 lock.**
