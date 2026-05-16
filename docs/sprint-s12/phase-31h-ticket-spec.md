---
id: SPRINT-S12-PHASE-31H-SPEC
version: v2 (post codex review)
title: Sprint S12 Phase 31.H — Prod WSL Systemd + Backup + Canary · Ticket Spec
scope: prod-unit subset + backup orchestrator + BackupDRDrill + canary deploy + prod observability wiring + GPG safety + RPO decision
status: Draft (2026-05-13)
related:
  - ADR-0023 §3, §10
  - 31.G-StabilityCheckpoint (cross-phase blocker)
  - Phase 31.A-31.G all v2 (schemas reused)
  - 31.H v1 (pre codex review, superseded)
  - Codex independent review (2026-05-13, /tmp/phase31h-codex-review-final.txt)
---

# Sprint S12 Phase 31.H · Prod WSL Systemd + Backup + Canary — Ticket Spec v2

## §0. v2 Changelog (vs v1 — driven by codex independent review)

**P0 BLOCKING fixes:**

| Item | v1 problem | v2 fix |
|---|---|---|
| **Backup restore never actually tested** | BackupSmoke only `pg_restore --list` + mocked network; real restore never proven | v2: **NEW ticket 31.H-BackupDRDrill** between BackupSmoke and Integration — actual decrypt + restore to sandbox Postgres + query contract |
| **Canary auto-promote without staging** | shallow smoke + auto-promote = reactive rollback only | v2: 3bc + 9-Doc add **first-cycle operator-confirmation** (manual promote for first cycle; auto-promote unlocks after operator-approved baseline) |
| **5a decision fork dangerous** | "provision OR accept" as branching impl ticket → downstream silent assumption risk | v2: 5a becomes **decision-gate ticket** with locked field `prod_host_mode: existing-as-is | provision-ubuntu-24.04`; downstream tickets consume |
| **6a/6b not cleanly parallel** | both touch prod compose | v2: **6b.blockedBy: [6a]** + **mutex_with: [6a]** (codex Q3: backup is safety net before canary) |
| **GPG private-key safety undocumented** | public-key encryption only; private-key loss → all backups unrecoverable | v2: **2a + 9-Doc add explicit GPG private-key safety section** (escrow + rotation + loss procedure + fingerprint) |
| **No WAL archiving / RPO undecided** | pg_dump = 24h RPO; never made explicit | v2: **NEW §7 Q9 — RPO target** (24h snapshot accepted vs WAL archiving required; if WAL → new ticket added) |

**P1 STRUCTURAL fixes:**

| Item | v1 problem | v2 fix |
|---|---|---|
| 2bc too dense | 9 operations / 400 LOC budget; retention "non-goal" contradicts phase goal | v2: 2bc **retention is in-scope** (lifecycle deletion logic post-30d); Verify AC upgraded to "actual `pg_restore` into mock DB + ≥1 query returns row" |
| 3bc compression risk | "use ops abstraction" undefined; promote = destructive | v2: 3a/3bc **define ops abstraction**: previous-image record + compose lock file + dry-run mode + idempotent rollback |
| 5c evidence weak | only installed_count + units_started=0 | v2: + `systemd-analyze verify` + dependency closure + path existence + ownership/mode + uninstall manifest ACs |
| Operator-window 8 tickets ≠ 8 sessions | 33% operator ratio looks heavy | v2: tickets stay separate but add **`session_group: prod-host-install | prod-smoke | post-soak`** annotation; 9-Doc + filing batch order document grouping |
| Integration soak schema too rough | "≥14 backups + ≥1 canary + 0 silent" too easy to satisfy | v2: **8-field schema** (backup count + last DR drill ref + canary cycle result + prod P1/P2 incidents + scrape freshness + alert delivery proof + soak duration + restore-readiness boolean) |

**P2 OPERATIONAL safety:**

- BackupSmoke + CanarySmoke add safe-revert ACs (cleanup / destination-prefix isolation / compose lock / rollback verification / alert cleanup)
- Cross-phase blocker preflight: machine-checkable schema (source_key + evidence_file + commit_sha + pass_fail)

**Ticket count**: v1 24 → v2 **25** (+ BackupDRDrill)

---

## §1, §2 — unchanged from v1

---

## §3. Schemas — unchanged structurally; adds `session_group` ticket-metadata field (not boundaries schema)

`session_group:` annotation on operator-window tickets (NEW; non-boundary; for batching purposes only):

```yaml
session_group: prod-host-install | prod-smoke | post-soak
```

- `prod-host-install`: 5a, 5b, 5c, ProdHostReadyGate (single operator session)
- `prod-smoke`: BackupSmoke, CanarySmoke, BackupDRDrill (single rehearsal session)
- `post-soak`: StabilityCheckpoint, Integration (passive observation)

Filing-script batches tickets within same session_group sequentially within a planned operator window.

---

## §4. Children — overview table (25 tickets v2)

| Layer | # | ID | Title | Tier | Prefer | Class | blockedBy | session_group |
|---|---|---|---|---|---|---|---|---|
| L0 | 1-4 | 31.H-1a..4a | specs (2a + GPG safety section) | S | claude | claude | — | — |
| L1 | 5 | 31.H-1bc | prod-systemd-units.txt + tests | S | claude | claude | 1a | — |
| L1 | 6 | 31.H-2bc | backup-orchestrator + retention + actual restore verify | S | codex | codex | 2a | — |
| L1 | 7 | 31.H-3bc | canary-runner + ops abstraction (lock + dry-run + immutable previous-image) | S | codex | codex | 3a | — |
| L2-Gate | 8 | 31.H-LibsGate | + GPG private-key safety doc verified + ops abstraction defined | S | claude | claude | 1bc, 2bc, 3bc, 4a | — |
| L3 | 9 | 31.H-5a | **DECISION GATE**: operator locks `prod_host_mode` | S | claude | claude + operator-window | LibsGate, 31.G-StabilityCheckpoint-external | **prod-host-install** |
| L3 | 10 | 31.H-5b | **OPERATOR**: install prod cred dir | S | claude | claude + operator-window | 5a | **prod-host-install** |
| L4 | 11 | 31.H-5c | **OPERATOR**: install 50+ systemd units + systemd-analyze verify | S | claude | claude + operator-window | 5b, 1bc | **prod-host-install** |
| L4-Gate | 12 | 31.H-ProdHostReadyGate | gate (units verified) | S | claude | claude + operator-rehearsal | 5c | **prod-host-install** |
| L5 | 13 | 31.H-6a | backup orchestrator → off-host wiring | S | codex | codex + operator-prepare-only | ProdHostReadyGate, 2bc | — |
| L6 | 14 | 31.H-6b | canary runner → GitLab CR + cosign verify (sequential after 6a) | S | codex | codex | **6a**, 3bc, 31.F-StabilityCheckpoint-external | — |
| L7 | 15 | 31.H-7a | prod /metrics → 31.G | S | codex | codex + operator-prepare-only | 6a, 6b, 31.G-StabilityCheckpoint-external | — |
| L7 | 16 | 31.H-7b | prod alerts → 31.C routing_config | S | claude | claude | 6a, 6b, 31.C-Integration-external | — |
| L8-Gate | 17 | 31.H-BackupSmoke | + cleanup + isolation + no-overwrite real-backup-names ACs | S | claude | claude + operator-rehearsal | 6a, 7a | **prod-smoke** |
| L8-Gate | 18 | 31.H-CanarySmoke | + previous-image capture + compose lock + dry-run + rollback verify | S | claude | claude + operator-rehearsal | 6b, 7b | **prod-smoke** |
| L9-Gate | 19 | 31.H-BackupDRDrill | **NEW** — actual restore to sandbox Postgres + query contract | S | claude | claude + operator-rehearsal | BackupSmoke | **prod-smoke** |
| L10 | 20 | 31.H-8a | prod event_classes routing config | S | claude | claude | 7b | — |
| L10 | 21 | 31.H-8b | routing tests | S | claude | claude | 8a | — |
| L11 | 22 | 31.H-9-Doc | prod runbook + GPG safety + canary-first-cycle-manual policy | S | claude | claude | BackupSmoke, CanarySmoke, BackupDRDrill, 8b | — |
| L12 | 23 | 31.H-CallSiteInventoryGate | audit (incl. cross-phase blocker preflight schema) | S | claude | claude | 9-Doc | — |
| L13 | 24 | 31.H-StabilityCheckpoint | 24-72h → unblocks 31.I | S | claude | claude + operator-window | CallSiteInventoryGate | **post-soak** |
| L14 | 25 | 31.H-Integration | 14-day soak (8-field schema) tier:L | L | claude | claude + operator-window | StabilityCheckpoint | **post-soak** |

**Operator-window** (8 tickets, batched into 3 sessions): see `session_group`.

**Critical path**: 1a → 1bc → LibsGate → 5a → 5b → 5c → ProdHostReadyGate → 6a → 6b → 7a/7b → BackupSmoke → CanarySmoke → BackupDRDrill → 8a → 8b → 9-Doc → CallSiteInventoryGate → StabilityCheckpoint → Integration. ~19 hops.

---

## §5. Per-child specs (v2 deltas)

### 31.H-1a, 3a, 4a — unchanged

### 31.H-2a — Prod backup architecture spec (+ GPG private-key safety per codex P0)

```yaml
delta_from_v1:
  - outputs_guaranteed: ADD new section "GPG private-key safety":
    - Private-key OWNER: operator (sora); fingerprint recorded in docs/security/gpg-public-key.pem header
    - Offline escrow: paper printout in safe + USB encrypted backup (operator process; not in repo)
    - Passphrase: separate from key file (operator-only; never in cred dir)
    - Restore-host procedure: import private key on restoration host (offline first; never on prod itself)
    - Rotation: annual + emergency; old key retained 1y for legacy backup restore
    - Loss procedure: if private key lost → all encrypted backups unrecoverable → URGENT operator manual re-snapshot with new key + invalidate old encrypted set
  - retention now in scope (NOT non-goal); 30-day rolling deletion specified
ac.code_additions:
  - {desc: "GPG private-key safety section present", verify: {command: "grep -ciE 'private.key.*safety|escrow|loss procedure' docs/sop/prod-backup-architecture.md", expect_stdout_match: "^[3-9]$|^[1-9][0-9]+$"}}
  - {desc: "retention 30-day rolling documented", verify: {command: "grep -ciE '30.day.*rolling|retention.*30' docs/sop/prod-backup-architecture.md", expect_stdout_match: "[1-9]"}}
```

### 31.H-3a — Canary deployment spec (+ ops abstraction + first-cycle manual)

```yaml
delta_from_v1:
  - outputs_guaranteed: ADD "First-cycle policy: First canary cycle after deploy REQUIRES operator manual confirm before promote. After 5 successful auto-cycles + 7-day stable, operator may un-gate via runbook procedure."
  - outputs_guaranteed: ADD "Ops abstraction (used by 3bc):
    - prod_compose_lock(): file-based mutex at ~/.cache/omnisight/prod-compose.lock
    - record_previous_image(image_ref): writes to ~/.cache/omnisight/prod-image-history.jsonl
    - swap_image_tag(new_ref, prev_ref): atomic tag swap in compose file with rollback path
    - dry_run mode: prints commands; no real compose mutation"
ac.code_additions:
  - {desc: "first-cycle manual promote documented", verify: {command: "grep -ciE 'first.cycle.*manual|operator.*confirm.*before.*promote' docs/sop/canary-deployment-spec.md", expect_stdout_match: "[1-9]"}}
  - {desc: "ops abstraction documented", verify: {command: "grep -cE 'prod_compose_lock|record_previous_image|swap_image_tag|dry_run' docs/sop/canary-deployment-spec.md", expect_stdout_match: "^[3-9]$|^[1-9][0-9]+$"}}
```

### 31.H-1bc — unchanged

### 31.H-2bc — actual restore verify + retention (codex P0/P1)

```yaml
delta_from_v1:
  - context_hint.outputs_guaranteed: ADD "Retention: lifecycle script deletes off-host artifacts older than 30 days (configurable). Verify: NOT just `pg_restore --list`; tests actually decrypt + restore to ephemeral Postgres container + run `SELECT count(*) FROM <known-table>` query contract."
  - tests >=9 (was 7; +2 for retention + actual-restore)
ac.code_additions:
  - {desc: "retention lifecycle implemented", verify: {command: "grep -cE 'def.*purge_old|retention.*30.*days|lifecycle' scripts/setup-prod-env/backup-orchestrator.py", expect_stdout_match: "[1-9]"}}
  - {desc: "test does actual pg_restore (not just --list)", verify: {command: "grep -cE 'def test_.*actual.*restore|def test_.*restore.*sandbox|pg_restore [^-]' scripts/setup-prod-env/tests/test_backup_orchestrator.py", expect_stdout_match: "[1-9]"}}
```

### 31.H-3bc — ops abstraction + first-cycle confirm (codex P0/P1)

```yaml
delta_from_v1:
  - context_hint.outputs_guaranteed: ADD "Uses ops abstraction from 3a:
    - prod_compose_lock() before any tag swap (mutex)
    - record_previous_image() called BEFORE swap (rollback target captured)
    - swap_image_tag() atomic with rollback path
    - dry_run mode: --dry-run flag prints commands, no real mutation
    - First-cycle policy: refuses auto-promote unless ~/.cache/omnisight/canary-first-cycle-approved.flag exists (operator creates after manual review)"
  - tests >=10 (was 7; +3 for lock + dry-run + first-cycle-refusal)
ac.code_additions:
  - {desc: "prod_compose_lock used before swap", verify: {command: "grep -cE 'prod_compose_lock|compose.lock' scripts/setup-prod-env/canary-runner.py", expect_stdout_match: "[1-9]"}}
  - {desc: "first-cycle gate present", verify: {command: "grep -cE 'canary-first-cycle-approved.flag|first.cycle.*manual' scripts/setup-prod-env/canary-runner.py", expect_stdout_match: "[1-9]"}}
  - {desc: "--dry-run flag", verify: {command: "grep -cE '\\-\\-dry-run' scripts/setup-prod-env/canary-runner.py", expect_stdout_match: "[1-9]"}}
  - {desc: "test for first-cycle refusal", verify: {command: "grep -cE 'def test_.*first.cycle.*refuse|def test_.*no.*flag.*refuse' scripts/setup-prod-env/tests/test_canary_runner.py", expect_stdout_match: "[1-9]"}}
```

### 31.H-LibsGate — + GPG safety doc verified + ops abstraction defined

```yaml
delta_from_v1:
  - context_hint.outputs_guaranteed: ADD checks (now 9):
    8. GPG private-key safety section exists in 2a doc + 9-Doc references it
    9. Ops abstraction interface documented in 3a doc; 3bc implements all 4 functions
ac.code_additions:
  - {desc: "all 9 checks PASS", verify: {command: "jq -r '. | to_entries | map(select(.value==false)) | length' docs/audit/AUDIT-31-phase-H-evidence/libs-gate.json", expect_stdout_match: "^0$"}}
  - {desc: "GPG safety verify", verify: {command: "jq -r .gpg_safety_documented docs/audit/AUDIT-31-phase-H-evidence/libs-gate.json", expect_stdout_match: "^true$"}}
  - {desc: "ops abstraction verify", verify: {command: "jq -r .ops_abstraction_implemented docs/audit/AUDIT-31-phase-H-evidence/libs-gate.json", expect_stdout_match: "^true$"}}
```

### 31.H-5a — DECISION GATE (codex P0)

```yaml
title: "AUDIT-31.H-5a: **OPERATOR DECISION GATE**: lock prod_host_mode"
tier: S, prefer: claude, class: subscription-claude + operator-window
area_labels: [area:docs, area:devops, area:prod]
blockedBy: [LibsGate, 31.G-StabilityCheckpoint-external]
session_group: prod-host-install
context_hint:
  produces: "docs/audit/AUDIT-31-phase-H-evidence/prod-host-mode.json + entry in operator action log"
  consumed_by: "31.H-5b, 31.H-5c, all downstream tickets MUST consume locked value"
  inputs_expected: "LibsGate PASS; 31.G-StabilityCheckpoint closed"
  outputs_guaranteed: |
    Operator locks ONE of:
    1. `existing-as-is`: prod runs on current dev host (Ubuntu-22.04 or whatever exists); 5b/5c apply to that host
    2. `provision-ubuntu-24.04`: operator provisions new Ubuntu-24.04 WSL; 5b/5c apply there
    Output: prod-host-mode.json with {mode, justification, host_identifier, locked_timestamp}
    Downstream tickets MUST verify this field exists + locked before proceeding (cross-ticket preflight).
  non_goals:
    - "DO NOT perform any install — 5b/5c own"
    - "DO NOT change mode after lock (requires NEW META + retro)"
boundaries:
  loc_delta_max: 80, files_touched_max: 2
  required_paths: [docs/audit/AUDIT-31-phase-H-evidence/prod-host-mode.json, docs/operations/operator-action-log.md]
  forbidden_paths: [scripts/setup-prod-env/, deploy/]
  test_scope: defer-to-integration
  destructive_op_classes: []
  destructive_op_scope: none
  external_side_effect: none
  external_payload_class: operational
  execution_mode: operator-rehearsal
  dependency_artifacts: [LibsGate, 31.G-StabilityCheckpoint]
  mutex_with: []
  interface_contract:
    inputs_from_deps: [LibsGate evidence]
    outputs_for_downstream:
      - "prod-host-mode.json with locked field"
      - "host_identifier (WSL distro name)"
ac.code:
  - {desc: "prod-host-mode.json with locked mode", verify: {command: "jq -r .mode docs/audit/AUDIT-31-phase-H-evidence/prod-host-mode.json | grep -cE '^(existing-as-is|provision-ubuntu-24.04)$'", expect_stdout_match: "^1$"}, run_as: operator}
  - {desc: "host_identifier recorded", verify: {command: "jq -r .host_identifier docs/audit/AUDIT-31-phase-H-evidence/prod-host-mode.json | grep -cE 'Ubuntu-(2[2-9]|3[0-9])\\.[0-9]+|[a-z-]+'", expect_stdout_match: "[1-9]"}, run_as: operator}
  - {desc: "locked_timestamp present", verify: {command: "jq -r .locked_timestamp docs/audit/AUDIT-31-phase-H-evidence/prod-host-mode.json | grep -cE '^[0-9]{4}-[0-9]{2}-[0-9]{2}T'", expect_stdout_match: "[1-9]"}, run_as: operator}
go_live: T+1d after LibsGate + 31.G-StabilityCheckpoint (operator window)
```

### 31.H-5b, 5c — read locked prod_host_mode

```yaml
shared_delta_from_v1:
  - context_hint.inputs_expected: ADD "prod_host_mode locked at 5a (must read + use)"
  - new AC: ticket execution begins by reading prod-host-mode.json and confirming mode before proceeding
ac.code_additions:
  - {desc: "5b/5c reads prod-host-mode.json + confirms before install", verify: {command: "jq -r .read_prod_host_mode docs/audit/AUDIT-31-phase-H-evidence/{5b,5c}-deploy.json | sort -u", expect_stdout_match: "existing-as-is|provision-ubuntu-24.04"}, run_as: operator}
```

### 31.H-5c — + systemd-analyze + dependency closure + uninstall manifest (codex P1)

```yaml
delta_from_v1:
  - new ACs per codex Q2:
ac.code_additions:
  - {desc: "systemd-analyze verify passes on installed units", verify: {command: "jq -r .systemd_analyze_verify_clean docs/audit/AUDIT-31-phase-H-evidence/5c-deploy.json", expect_stdout_match: "^true$"}, run_as: operator}
  - {desc: "dependency closure verified (Requires/After resolves)", verify: {command: "jq -r .dependency_closure_clean docs/audit/AUDIT-31-phase-H-evidence/5c-deploy.json", expect_stdout_match: "^true$"}, run_as: operator}
  - {desc: "path existence verified (each ExecStart binary exists)", verify: {command: "jq -r .all_exec_paths_exist docs/audit/AUDIT-31-phase-H-evidence/5c-deploy.json", expect_stdout_match: "^true$"}, run_as: operator}
  - {desc: "uninstall manifest written for rollback", verify: {command: "test -f docs/audit/AUDIT-31-phase-H-evidence/5c-uninstall-manifest.txt", expect_exit_code: 0}, run_as: operator}
```

### 31.H-6a, 6b — sequential (codex P0)

```yaml
6b_delta_from_v1:
  - blockedBy: [**6a**, 3bc, 31.F-StabilityCheckpoint-external]   (was [ProdHostReadyGate, 3bc, ...])
  - mutex_with: [6a]   (NEW per codex Q3)
```

### 31.H-BackupSmoke — + safe-revert ACs

```yaml
delta_from_v1:
  - new ACs per codex Q5:
ac.code_additions:
  - {desc: "smoke uses destination prefix `smoke-test-` (isolation from real backups)", verify: {command: "grep -cE 'smoke.test.|destination_prefix.*smoke' scripts/sprint-s12/phase31h-backup-smoke.sh", expect_stdout_match: "[1-9]"}}
  - {desc: "smoke cleans up its synthetic artifacts post-run", verify: {command: "grep -cE 'cleanup|rm.*smoke.test|aws s3 rm.*smoke.test' scripts/sprint-s12/phase31h-backup-smoke.sh", expect_stdout_match: "[1-9]"}}
  - {desc: "smoke does NOT touch real backup namespace", verify: {command: "jq -r .smoke_did_not_overwrite_real_backups docs/audit/AUDIT-31-phase-H-evidence/backup-smoke.json", expect_stdout_match: "^true$"}, run_as: operator}
```

### 31.H-CanarySmoke — + safe-revert ACs

```yaml
delta_from_v1:
  - new ACs per codex Q5:
ac.code_additions:
  - {desc: "smoke uses dry-run + non-serving canary variant first", verify: {command: "grep -cE '\\-\\-dry-run|non-serving' scripts/sprint-s12/phase31h-canary-smoke.sh", expect_stdout_match: "[1-9]"}}
  - {desc: "previous-image captured before any swap", verify: {command: "jq -r .previous_image_captured docs/audit/AUDIT-31-phase-H-evidence/canary-smoke.json", expect_stdout_match: "^true$"}, run_as: operator}
  - {desc: "rollback verified (one promote-then-rollback path exercised)", verify: {command: "jq -r .rollback_path_verified docs/audit/AUDIT-31-phase-H-evidence/canary-smoke.json", expect_stdout_match: "^true$"}, run_as: operator}
  - {desc: "compose lock acquired", verify: {command: "jq -r .compose_lock_acquired docs/audit/AUDIT-31-phase-H-evidence/canary-smoke.json", expect_stdout_match: "^true$"}, run_as: operator}
```

### 31.H-BackupDRDrill — NEW (codex P0)

```yaml
title: "AUDIT-31.H-BackupDRDrill: Actual backup restore to sandbox Postgres + query contract"
tier: S, prefer: claude, class: subscription-claude + operator-rehearsal
area_labels: [area:tests, area:devops, area:prod, area:backend]
blockedBy: [BackupSmoke]
session_group: prod-smoke
context_hint:
  produces: "scripts/sprint-s12/phase31h-backup-dr-drill.sh + docs/audit/AUDIT-31-phase-H-evidence/dr-drill.json"
  consumed_by: "31.H-Integration (Integration AC includes last_dr_drill_ref); 31.J disaster recovery rehearsal"
  inputs_expected: "BackupSmoke PASS; encrypted backup snapshot exists at off-host destination; GPG private key accessible to operator on isolated restore host"
  outputs_guaranteed: |
    Operator-witnessed:
    1. Operator selects 1 backup from yesterday's daily timer output
    2. Decrypt with GPG private key (on offline/isolated restore host; NOT prod)
    3. Spin up ephemeral Postgres container (sandbox)
    4. Run `pg_restore --dbname=sandbox_db <archive>` to actually load data
    5. Run query contract: `SELECT COUNT(*) FROM <key-table>` returns row count > 0
    6. Repeat for ≥1 memory-stack artifact (Neo4j cypher import OR Cognee data dir load)
    7. Tear down sandbox; record dr-drill.json
    8. If ANY step fails → file follow-up ticket; do NOT proceed to Integration
  non_goals:
    - "DO NOT restore to prod (sandbox only)"
    - "DO NOT keep private key on restore host post-drill (offline only)"
boundaries:
  loc_delta_max: 250, files_touched_max: 2
  required_paths: [scripts/sprint-s12/phase31h-backup-dr-drill.sh, docs/audit/AUDIT-31-phase-H-evidence/dr-drill.json]
  forbidden_paths: [scripts/setup-prod-env/, deploy/]
  test_scope: inline
  destructive_op_classes: [state-append]
  destructive_op_scope: project   # sandbox is ephemeral local
  external_side_effect: network-test
  external_payload_class: secret-adjacent   # uses GPG private key briefly
  execution_mode: operator-rehearsal
  dependency_artifacts: [BackupSmoke PASS, real backup snapshot at off-host]
  mutex_with: []
  interface_contract:
    inputs_from_deps: [working backup snapshot]
    outputs_for_downstream:
      - "dr-drill.json: {snapshot_date, decrypt_ok, restore_ok, query_count, memory_stack_ok, total_minutes}"
      - "PASS unblocks 9-Doc + Integration"
ac.code:
  - {desc: "script exists", verify: {command: "test -x scripts/sprint-s12/phase31h-backup-dr-drill.sh", expect_exit_code: 0}}
ac.deploy:
  - {desc: "decrypt + restore + query all PASS", verify: {command: "jq -r '. | (.decrypt_ok and .restore_ok and (.query_count > 0) and .memory_stack_ok)' docs/audit/AUDIT-31-phase-H-evidence/dr-drill.json", expect_stdout_match: "^true$"}, run_as: operator}
  - {desc: "drill duration recorded", verify: {command: "jq -r .total_minutes docs/audit/AUDIT-31-phase-H-evidence/dr-drill.json", expect_stdout_match: "^[0-9]+$"}, run_as: operator}
go_live: T+2d after BackupSmoke
```

### 31.H-9-Doc — + GPG safety + first-cycle policy + DR drill cadence

```yaml
delta_from_v1:
  - new sections:
    - "GPG private-key safety procedures" (per 2a)
    - "Canary first-cycle manual policy + un-gate process" (5 auto-cycles + 7-day stable → operator un-gates)
    - "Backup DR drill cadence" (recommend quarterly after rc2; ad-hoc when GPG/Postgres versions change)
ac.code_additions:
  - {desc: "3 new sections covered", verify: {command: "grep -cE '^##.*GPG.*safety|^##.*first.cycle|^##.*DR.*drill' docs/sop/prod-operator-runbook.md", expect_stdout_match: "^[3-9]$|^[1-9][0-9]+$"}}
```

### 31.H-CallSiteInventoryGate — + cross-phase blocker preflight schema

```yaml
delta_from_v1:
  - context_hint.outputs_guaranteed: ADD "Cross-phase blocker preflight: for each *-external blocker in this phase, emit JSON {source_key, expected_evidence_file, commit_sha, pass_fail}; all must pass before Integration files"
ac.code_additions:
  - {desc: "cross-phase preflight evidence has 4-field schema", verify: {command: "python3 -c \"import json; d=json.load(open('docs/audit/AUDIT-31-phase-H-evidence/cross-phase-preflight.json')); assert all('source_key' in e and 'expected_evidence_file' in e and 'commit_sha' in e and 'pass_fail' in e for e in d['blockers'])\"", expect_exit_code: 0}}
  - {desc: "all blockers pass_fail=PASS", verify: {command: "jq -r '[.blockers[] | select(.pass_fail != \"PASS\")] | length' docs/audit/AUDIT-31-phase-H-evidence/cross-phase-preflight.json", expect_stdout_match: "^0$"}}
```

### 31.H-Integration — 8-field schema (codex Q3)

```yaml
delta_from_v1:
  - new explicit Integration JSON schema (8 fields per codex):
    1. backup_snapshot_count: int (≥14)
    2. last_dr_drill_ref: str (BackupDRDrill ticket key)
    3. canary_cycles: List[{date, outcome (promoted/rolled_back), duration_minutes}]
    4. prod_p1_incidents: int (0 expected for healthy soak)
    5. prod_p2_incidents: int
    6. scrape_freshness: {prometheus_last_successful: timestamp, max_gap_seconds: int} (Prometheus targets staying scraped)
    7. alert_delivery_proof: {p1_alerts_sent: int, p1_alerts_received_in_discord: int}
    8. restore_readiness: bool (last_dr_drill PASS within 30 days)
ac.integration_replacements:
  - {desc: "integration.json has all 8 fields", verify: {command: "python3 -c \"import json; d=json.load(open('docs/audit/AUDIT-31-phase-H-evidence/integration.json')); assert all(k in d for k in ['backup_snapshot_count','last_dr_drill_ref','canary_cycles','prod_p1_incidents','prod_p2_incidents','scrape_freshness','alert_delivery_proof','restore_readiness'])\"", expect_exit_code: 0}}
  - {desc: "restore_readiness true (recent DR drill PASS)", verify: {command: "jq -r .restore_readiness docs/audit/AUDIT-31-phase-H-evidence/integration.json", expect_stdout_match: "^true$"}, run_as: operator}
  - {desc: "alert delivery: sent == received", verify: {command: "jq -r '.alert_delivery_proof.p1_alerts_sent == .alert_delivery_proof.p1_alerts_received_in_discord' docs/audit/AUDIT-31-phase-H-evidence/integration.json", expect_stdout_match: "^true$"}, run_as: operator}
  - {desc: "0 unexplained prod P1 incidents (failure-inject ones may count separately)", verify: {command: "jq -r .prod_p1_incidents_unexplained docs/audit/AUDIT-31-phase-H-evidence/integration.json", expect_stdout_match: "^0$"}, run_as: operator}
```

---

## §6. Filing batch order

```
L0: 1a, 2a, 3a, 4a
L1: 1bc, 2bc, 3bc
L2: LibsGate
**prod-host-install session**:
  L3: 5a (decision gate)
  L4: 5b
  L5: 5c
  L6: ProdHostReadyGate
L7: 6a (sequential before 6b)
L8: 6b
L9: 7a, 7b (parallel)
**prod-smoke session**:
  L10: BackupSmoke, CanarySmoke (parallel)
  L11: BackupDRDrill (NEW after BackupSmoke)
L12: 8a
L13: 8b
L14: 9-Doc
L15: CallSiteInventoryGate
**post-soak**:
  L16: StabilityCheckpoint
  L17: Integration (tier:L 14d)
```

18 layers, 25 tickets.

---

## §7. Operator answers (all locked 2026-05-13)

| # | Question | Locked answer |
|---|---|---|
| 1 | Prod WSL provisioning timing | **existing host as-is for rc2**; new Ubuntu-24.04 WSL deferred to post-rc2 |
| 2 | Backup destination | **SSH-mounted remote** (uses existing Gerrit SSH auth surface) |
| 3 | GPG public-key cred | **new cred slot `backup-gpg-public-key`** in 31.A-2 cred YAML (retroactive add) |
| 4 | Backup retention | **30 days** rolling (revisit if storage budget allows extension) |
| 5 | Canary promote_threshold | **10 min** initial; tune at 31.H-Integration |
| 6 | Canary auto-rollback window | **10 min** post-promote |
| 7 | Prod event_classes | `prod_backup_fail` / `prod_canary_fail` / `prod_unit_inactive` |
| 8 | 31.I gating | **blockedBy 31.H-StabilityCheckpoint** (24-72h pattern) |
| 9 | RPO target | **24h pg_dump snapshot accepted for rc2**; WAL archiving deferred to post-rc2 (explicit operator decision; new ticket if required later) |

---

## §8. Submission flow — unchanged

---

**End of Phase 31.H ticket spec v2 (25-ticket; BackupDRDrill NEW; decision-gate 5a; sequential 6a→6b; GPG private-key safety; first-cycle canary manual; 8-field Integration schema). Awaiting operator §7 lock (9 questions).**
