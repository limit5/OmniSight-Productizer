---
id: SPRINT-S12-PHASE-31E-SPEC
version: v2 (post codex review)
title: Sprint S12 Phase 31.E — GitLab CI Wave 1 + Wave 2 · Ticket Spec
scope: GitLab Runner + ci.yml generator (full Wave 1 content) + lint+unit job + build+push job + CI failure alerts + queue monitor + GHA enforcement
status: Draft (2026-05-13)
related:
  - ADR-0023 §15
  - Phase 31.D-StabilityCheckpoint (cross-phase gate; 24-72h stable replication)
  - Phase 31.A v2 + 31.B v2 + 31.C v2 + 31.D v2 (all schemas reused)
  - 31.A-2 cred YAML (gitlab-*, ghcr-pull-token)
  - 31.C-Integration (alert pipeline)
  - 31.E v1 (pre codex review, superseded)
  - Codex independent review (2026-05-13, /tmp/phase31e-codex-review-final.txt)
---

# Sprint S12 Phase 31.E · GitLab CI — Ticket Spec v2

## §0. v2 Changelog (vs v1 — driven by codex independent review)

**P0 BLOCKING fixes:**

| Item | v1 problem | v2 fix |
|---|---|---|
| §4 header count | wrote "24 tickets" but table had 26 | v2: corrected to actual count |
| **9b boundary contradiction** | `execution_mode: structural-only` BUT `external_side_effect: network-production` + `destructive_op_scope: system` — internal contradiction | v2: 9b is now **structural+project+none** (only edits .gitlab-ci.yml); real GHCR push happens in CIWave2Smoke (operator-rehearsal) |
| **7b path ambiguity** | required_paths `.gitlab-ci.yml` but actually targets sora-bridge clone — boundary check unable to validate | v2: required_paths explicitly `~/sora-bridge/.gitlab-ci.yml` (operator workspace path) |
| **6a scope** | marked `project` but registers with shared GitLab admin | v2: `destructive_op_scope: system` (acknowledges shared-state mutation) |

**P1 STRUCTURAL fixes:**

| v1 | v2 |
|---|---|
| 8a + 8b separate | **8ab MERGED** — Wave 1 lint+unit job content (codex: share image/cache/rules; split invites parallel YAML edits) |
| 10a + 10b separate | **10ab MERGED** — CI failure watcher + tests in one atomic |
| 1bc generator emits skeleton; 8a/8b patch | **1bc generator emits FULL Wave 1 content from project profile** (per codex: skeleton+patch model weakens generator value); 7a/7b become thin wrappers (call generator + commit) |
| Mutex_with only; blockedBy doesn't serialize | **Explicit blockedBy chain**: 7a → 8ab → 9a → 9b → CIWave2Smoke; mutex_with lists fixed (all .gitlab-ci.yml-editors mutually listed) |

**P2 NEW tickets (hardening per codex Q4):**

| ID | Title | Reason |
|---|---|---|
| **31.E-QueueMonitor** | Daily runner queue/disk/cache metrics + alert when queue P95 > threshold | Single-host bottleneck; codex Q4 |
| **31.E-GHAEnforcementJob** | GitLab CI job that fails if `.github/workflows/*.yml` changes without explicit `legacy:` annotation | "LEGACY-NO-MIGRATE" classification needs enforcement, not just label (codex Q4) |

**P3 SLA reality fix:**

- Wave 1 P95 SLA: v1 said **<10 min** universally → v2 says **<15 min initial deployment**; <10 min aspirational AFTER caching proven (per codex Q4)
- Integration AC for P95 uses 15 min boundary; aspirational improvement tracked as Phase 31.K follow-up

**P4 secret-leak detection enhancement:**

- 3bc ci-yml-validator extended: rejects `.gitlab-ci.yml` containing `env`, `printenv`, `set -x` debug output, `cat /proc/self/environ`, `echo $TOKEN_PATTERN`, `docker login -p` (per codex Q4)

**Ticket count**: v1 26 → v2 **26** (mergers -2; new -P2 +2; net zero, better-shaped)

---

## §1, §2, §3 — unchanged from v1

[See v1 §1/§2/§3]

---

## §4. Children — overview table (26 tickets v2)

| Layer | # | ID | Title | Tier | Prefer | Class | blockedBy |
|---|---|---|---|---|---|---|---|
| L0 | 1 | 31.E-1a | CI Wave taxonomy spec | S | claude | claude | — |
| L0 | 2 | 31.E-2a | GitLab Runner architecture spec | S | claude | claude | — |
| L0 | 3 | 31.E-3a | ci.yml schema spec | S | claude | claude | — |
| L0 | 4 | 31.E-4a | ADR-0026 (GitLab CI only) | S | claude | claude | — |
| L0 | 5 | 31.E-5a | GHA inventory spec | S | claude | claude | — |
| L1 | 6 | 31.E-1bc | ci-yml-generator (emits FULL Wave 1 content) + tests | S | codex | codex + operator-prepare-only | 1a, 3a |
| L1 | 7 | 31.E-2bc | gitlab-runner-config-generator + tests | S | codex | codex + operator-prepare-only | 2a |
| L1 | 8 | 31.E-3bc | ci-yml-validator + tests (+ secret-leak detection) | S | codex | codex | 3a |
| L1 | 9 | 31.E-5bc | gha-inventory + tests | S | claude | claude | 5a |
| L2-Gate | 10 | 31.E-LibsGate | Cross-lib + secret-leak validator agreement | S | claude | claude | 1bc, 2bc, 3bc, 5bc, 4a |
| L3 | 11 | 31.E-6a | **OPERATOR**: Install + register GitLab Runner (scope:system fixed) | S | claude | claude + operator-window | LibsGate, 31.D-StabilityCheckpoint-external |
| L3-Gate | 12 | 31.E-RunnerReadyGate | Runner picks up test job (with orphan-ref cleanup) | S | claude | claude | 6a |
| L4 | 13 | 31.E-7a | Generate Wave 1 ci.yml for Productizer (thin wrapper) | S | codex | codex + operator-prepare-only | RunnerReadyGate, 1bc |
| L4 | 14 | 31.E-7b | Generate Wave 1 ci.yml for sora-bridge (operator workspace path FIXED) | S | codex | codex + operator-prepare-only | RunnerReadyGate, 1bc |
| L5 | 15 | 31.E-8ab | **MERGED v1 8a+8b**: Wave 1 lint + unit job content + smoke verify | S | codex | codex | 7a |
| L5-Gate | 16 | 31.E-CIWave1Smoke | Push to feature/* → CI <15 min P95 | S | claude | claude + operator-rehearsal | 8ab, 7b |
| L6 | 17 | 31.E-9a | Wave 2 docker build job | S | codex | codex | CIWave1Smoke, 8ab |
| L6 | 18 | 31.E-9b | Wave 2 image push job (yaml-edit ONLY; real push at CIWave2Smoke per codex) | S | codex | codex + operator-prepare-only | 9a |
| L6-Gate | 19 | 31.E-CIWave2Smoke | Push to main → Wave 2 builds + pushes image (real GHCR side effect) | S | claude | claude + operator-rehearsal | 9b |
| L7 | 20 | 31.E-10ab | **MERGED v1 10a+10b**: CI failure watcher + tests | S | claude | claude | CIWave2Smoke, 31.C-Integration-external |
| L8 | 21 | 31.E-QueueMonitor | **NEW**: Daily runner queue/disk/cache metrics + alert | S | codex | codex | 6a, 10ab |
| L8 | 22 | 31.E-GHAEnforcementJob | **NEW**: CI job rejects new/modified .github/workflows/* without legacy: annotation | S | codex | codex | 5bc, 8ab |
| L9 | 23 | 31.E-11-Doc | Operator runbook | S | claude | claude | 10ab, QueueMonitor |
| L10 | 24 | 31.E-CallSiteInventoryGate | One-time audit (continuous enforcement is GHAEnforcementJob's job) | S | claude | claude | 11-Doc, 5bc, GHAEnforcementJob |
| L11 | 25 | 31.E-Integration-Doc | E2E runbook | S | claude | claude | CallSiteInventoryGate |
| L12 | 26 | 31.E-Integration | E2E 14-day soak + 50+ pipelines + 2 failure injections | L | claude | claude + operator-window | ALL 1-25 |

**Routing tally**: codex 11, claude 15.

**Operator-window**: 6a, Integration (2 tickets).

**Critical path**: 1a → 1bc → LibsGate → 6a → RunnerReadyGate → 7a → 8ab → CIWave1Smoke → 9a → 9b → CIWave2Smoke → 10ab → QueueMonitor → 11-Doc → CallSiteInventoryGate → Integration-Doc → Integration. ~16 sequential hops.

**Explicit blockedBy chain on .gitlab-ci.yml editors** (codex Q3 fix): 7a → 8ab → 9a → 9b → CIWave2Smoke. No two tickets editing .gitlab-ci.yml in parallel.

---

## §5. Per-child specs (v2 deltas)

### 31.E-1a, 2a, 3a, 4a, 5a — unchanged

### 31.E-1bc — generator emits FULL Wave 1 content (codex Q3 fix)

```yaml
delta_from_v1:
  - context_hint.outputs_guaranteed: REWRITTEN — emits FULL Wave 1 content (lint + unit jobs WITH SCRIPTS, not stubs). Template wave1.yml.j2 includes per-project profile (Productizer: ruff+mypy+prettier; sora-bridge: ruff+mypy only).
  - non_goals UPDATED: "DO NOT emit Wave 2 — extension at 9a"; "DO NOT include skeleton/TODO placeholders — full content required"
  - tests: per-project profile rendering correct
ac.code_additions:
  - {desc: "generated ci.yml has lint+unit job scripts filled in (NOT TODO)", verify: {command: "python3 -c 'import sys; sys.path.insert(0,\"scripts/ci\"); from ci_yml_generator import generate_ci_yml; out = generate_ci_yml(\"omnisight-productizer\", 1, \"/tmp/out.yml\"); assert \"TODO\" not in out; assert \"ruff check\" in out; assert \"pytest backend\" in out'", expect_exit_code: 0}}
  - {desc: "per-project profile (Productizer vs sora-bridge differ)", verify: {command: "python3 -c 'import sys; sys.path.insert(0,\"scripts/ci\"); from ci_yml_generator import generate_ci_yml; p = generate_ci_yml(\"omnisight-productizer\", 1, \"/tmp/p.yml\"); s = generate_ci_yml(\"sora-bridge\", 1, \"/tmp/s.yml\"); assert p != s'", expect_exit_code: 0}}
```

### 31.E-3bc — ci-yml-validator + secret-leak detection (codex Q4 fix)

```yaml
delta_from_v1:
  - context_hint.outputs_guaranteed: ADDS secret-leak pattern detection
  - validator REJECTS .gitlab-ci.yml containing: `env`, `printenv`, `set -x`, `cat /proc/self/environ`, `echo $TOKEN_PATTERN`, `docker login -p`, unmasked token-like literals
  - tests >=9 (was 6; +3 for secret-leak patterns)
ac.code_additions:
  - {desc: "validator rejects `env` command", verify: {command: "grep -cE 'env\\b|printenv|set -x|/proc/self/environ' scripts/ci/ci-yml-validator.py", expect_stdout_match: "^[3-9]$|^[1-9][0-9]+$"}}
  - {desc: "validator rejects `docker login -p` (password in argv)", verify: {command: "grep -c 'docker login.*-p' scripts/ci/ci-yml-validator.py", expect_stdout_match: "[1-9]"}}
  - {desc: "tests count >=9 (added 3 secret-leak tests)", verify: {command: "pytest --collect-only scripts/ci/tests/test_ci_yml_validator.py 2>&1 | grep -c 'test_'", expect_stdout_match: "^[9-9]$|^[1-9][0-9]+$"}}
```

### 31.E-LibsGate — add secret-leak validator agreement check

```yaml
delta_from_v1:
  - context_hint.outputs_guaranteed: (7th check) validator's secret-leak detection rejects a synthetic bad-ci.yml fixture
  - blockedBy unchanged: [1bc, 2bc, 3bc, 5bc, 4a]
ac.code_additions:
  - {desc: "secret-leak fixture rejected by validator", verify: {command: "echo 'lint: { script: [echo $GHCR_PUSH_TOKEN] }' > /tmp/bad-ci.yml && python3 scripts/ci/ci-yml-validator.py /tmp/bad-ci.yml | jq -r .passed", expect_stdout_match: "^false$"}}
```

### 31.E-6a — destructive_op_scope FIX (codex Q5)

```yaml
delta_from_v1:
  - boundaries.destructive_op_scope: **system** (was project — codex Q5: GitLab admin registration mutates shared state)
  - class already operator-window; satisfies §3.2.1 + §3.4 rules for system + network-production
```

### 31.E-RunnerReadyGate — orphan-ref cleanup AC (apply 31.D pattern)

```yaml
delta_from_v1:
  - context_hint.outputs_guaranteed: ADD orphan-ref cleanup verification (test branch must not leak on either Gerrit or GitLab)
ac.code_additions:
  - {desc: "test branch cleaned on Gerrit", verify: {command: "ssh -p 29418 admin@sora.services 'gerrit query --format JSON branch:feature/runner-readiness-test* status:open' | grep -c runner-readiness-test", expect_stdout_match: "^0$"}, run_as: operator}
  - {desc: "test branch cleaned on GitLab", verify: {command: "git ls-remote ssh://git@sora.services:49154/omnisight/omnisight-productizer 'refs/heads/feature/runner-readiness-test*' | wc -l", expect_stdout_match: "^0$"}, run_as: operator}
```

### 31.E-7a — thin wrapper (per codex Q3 fix)

```yaml
delta_from_v1:
  - context_hint.outputs_guaranteed: REWRITTEN — "Run generator (1bc emits FULL content); commit output as .gitlab-ci.yml; pass validator (3bc)"
  - non_goals UPDATED: "DO NOT add job content manually — 1bc owns content via template; if missing, fix 1bc"
  - mutex_with: [7b, 8ab, 9a, 9b]   (was [8a, 8b, 9a]; added 9b explicitly + 7b coordination)
ac.code_replacements:
  - {desc: "ci.yml has Wave 1 jobs filled (NOT TODO)", verify: {command: "grep -c 'TODO' .gitlab-ci.yml", expect_stdout_match: "^0$"}}
  - {desc: "passes validator including secret-leak", verify: {command: "python3 scripts/ci/ci-yml-validator.py .gitlab-ci.yml | jq -r .passed", expect_stdout_match: "^true$"}}
```

### 31.E-7b — sora-bridge path FIX (codex Q2 fix)

```yaml
delta_from_v1:
  - boundaries.required_paths: **`~/sora-bridge/.gitlab-ci.yml`** (was `.gitlab-ci.yml` — ambiguous; v2 explicit operator workspace path)
  - boundaries.forbidden_paths: ADDS `.gitlab-ci.yml` (this repo's; 7b must NOT touch Productizer's ci.yml)
  - context_hint.consumed_by: clarified that sora-bridge ci.yml needs equivalent of 8ab content (also via 1bc generator template; profile=sora-bridge)
ac.code_replacements:
  - {desc: "sora-bridge ci.yml has content per template profile", verify: {command: "test -f ~/sora-bridge/.gitlab-ci.yml && grep -c TODO ~/sora-bridge/.gitlab-ci.yml", expect_stdout_match: "^0$"}, run_as: operator}
  - {desc: "NO modification to THIS repo's .gitlab-ci.yml", verify: {command: "git -C ~/work/sora/OmniSight-Productizer diff --name-only HEAD~1 HEAD .gitlab-ci.yml | wc -l", expect_stdout_match: "^0$"}}
```

### 31.E-8ab — MERGED v1 8a+8b (codex Q3)

```yaml
title: "AUDIT-31.E-8ab: Wave 1 lint + unit job content + smoke verify"
tier: S, prefer: codex, class: subscription-codex
area_labels: [area:ci, area:tooling, area:tests]
blockedBy: [7a]   # NOTE: 7a (NOT just blockedBy 7a; 8ab MUST close before 9a starts per chain)
context_hint:
  produces: "Patch to .gitlab-ci.yml — lint job (ruff+mypy+prettier) + unit job (pytest+coverage); both with rules + tags per 1bc generator output (this ticket VERIFIES generator output AND adds project-specific tweaks if needed)"
  consumed_by: "31.E-CIWave1Smoke, 31.E-9a (Wave 2 chain)"
  inputs_expected: "1bc generator emitted full Wave 1 content into .gitlab-ci.yml via 7a"
  outputs_guaranteed: |
    Lint job:
    - ruff check . + mypy backend/ + (frontend prettier --check; skip if pnpm missing)
    - image python:3.12-slim
    Unit job:
    - pytest backend/ --cov=backend --cov-report=xml --tb=short --maxfail=20
    - artifacts coverage.xml
    - TIME BUDGET: 8 min
    Both jobs match per-project profile from 1bc.
    If 1bc generator output looks right (validator passes), 8ab may be a no-op shipping ticket; otherwise small fixes to job blocks here.
  non_goals:
    - "DO NOT add Wave 2 build/push — 9a/9b own"
    - "DO NOT modify scripts/ci/ — generator/validator owned by 1bc/3bc"
    - "DO NOT modify ruff.toml / pyproject.toml / .prettierrc / pytest.ini (use existing)"
    - "DO NOT modify backend/requirements.txt"
boundaries:
  loc_delta_max: 100, files_touched_max: 1
  required_paths: [.gitlab-ci.yml]
  forbidden_paths: [scripts/ci/, ruff.toml, pyproject.toml, .prettierrc, pytest.ini, backend/requirements.txt]
  test_scope: defer-to-integration
  destructive_op_classes: []
  destructive_op_scope: project
  external_side_effect: none
  external_payload_class: operational
  execution_mode: structural-only
  dependency_artifacts: [.gitlab-ci.yml post-7a]
  mutex_with: [7a, 7b, 9a, 9b]
  interface_contract:
    inputs_from_deps: [generator-emitted .gitlab-ci.yml]
    outputs_for_downstream: ["lint + unit jobs ready for Wave 1 smoke"]
ac.code:
  - {desc: "lint job has ruff + mypy", verify: {command: "grep -cE 'ruff check|mypy backend' .gitlab-ci.yml", expect_stdout_match: "^[2-9]$|^[1-9][0-9]+$"}}
  - {desc: "unit job has pytest --cov", verify: {command: "grep -cE 'pytest backend.*--cov' .gitlab-ci.yml", expect_stdout_match: "[1-9]"}}
  - {desc: "passes validator including secret-leak", verify: {command: "python3 scripts/ci/ci-yml-validator.py .gitlab-ci.yml | jq -r .passed", expect_stdout_match: "^true$"}}
go_live: T+0.5d after 7a
```

### 31.E-CIWave1Smoke — SLA boundary update (codex Q4)

```yaml
delta_from_v1:
  - blockedBy: [8ab, 7b]   (was [8a, 8b, 7b]; 8a+8b merged)
  - context_hint.outputs_guaranteed: SLA target **<15 min initial** (was 10 min); <10 min aspirational note in evidence
  - emit two metrics: total_seconds (queued+exec) AND exec_seconds (job duration only) for codex Q4
ac.code_replacements:
  - {desc: "total < 900s (15 min) for initial deployment", verify: {command: "jq -r .total_seconds docs/audit/AUDIT-31-phase-E-evidence/ci-wave1-smoke.json", expect_stdout_match: "^([0-9]|[1-9][0-9]|[1-8][0-9]{2}|900)$"}, run_as: operator}
  - {desc: "exec_seconds metric also emitted (aspirational <10 min tracking)", verify: {command: "jq -r .exec_seconds docs/audit/AUDIT-31-phase-E-evidence/ci-wave1-smoke.json", expect_stdout_match: "^[0-9]+$"}, run_as: operator}
```

### 31.E-9a — Wave 2 docker build job (chain fix)

```yaml
delta_from_v1:
  - blockedBy: [CIWave1Smoke, 8ab]   (was [CIWave1Smoke]; explicit chain per codex Q3)
  - mutex_with: [7a, 7b, 8ab, 9b]
```

### 31.E-9b — Wave 2 push (BOUNDARY CONTRADICTION FIXED — codex Q2/Q5)

```yaml
delta_from_v1:
  - boundaries.external_side_effect: **none** (was network-production — contradicted structural-only)
  - boundaries.destructive_op_scope: **project** (was system)
  - boundaries.execution_mode: structural-only (unchanged; now consistent)
  - context_hint.non_goals strengthened: "DO NOT actually push images here — pure yaml-edit. Real GHCR push side-effect captured in CIWave2Smoke (operator-rehearsal)"
  - class unchanged: subscription-codex + operator-prepare-only
  - mutex_with: [7a, 7b, 8ab, 9a]
explanatory_comment: |
  v1 had 9b labeled structural-only (yaml edit) AND network-production+system (live push). Contradiction.
  v2: 9b ONLY edits .gitlab-ci.yml (structural). The REAL push happens when CIWave2Smoke (operator-rehearsal) actually triggers the pipeline on main and the runner executes the job. CIWave2Smoke carries the network-production + system labels.
```

### 31.E-CIWave2Smoke — now carries the network-production label (codex Q5)

```yaml
delta_from_v1:
  - boundaries.external_side_effect: network-production (already had; no change; just confirming this is the side-effect site, not 9b)
  - context_hint.non_goals: ADD "first real GHCR push from CI runner happens here, NOT in 9b"
```

### 31.E-10ab — MERGED v1 10a+10b (codex Q3)

```yaml
title: "AUDIT-31.E-10ab: CI failure watcher + tests"
tier: S, prefer: claude, class: subscription-claude
area_labels: [area:backend, area:ci, area:tooling, area:tests]
blockedBy: [CIWave2Smoke, 31.C-Integration-external]
context_hint:
  produces: "backend/ci/failure_watcher.py + backend/ci/tests/test_failure_watcher.py + tiny patch to backend/alerts/routing_config.yaml (event classes: ci_pipeline_fail_{main,develop,feature})"
  consumed_by: "31.E-QueueMonitor (reuses GitLab API surface), 31.E-Integration"
  inputs_expected: "31.C alert pipeline + GitLab CI API token"
  outputs_guaranteed: |
    Module polls GitLab CI API for failed pipelines every 60s.
    Emits route_alert per branch:
    - main → P1
    - develop → P2
    - feature/* → P3 (digest)
    Dedup via 31.C-4bc (event_class includes branch + project).
    Tests >=7 (was 6 in v1 10b; +1 for branch classification edge case main-vs-release-*).
  non_goals:
    - "DO NOT modify alert_router"
    - "DO NOT auto-retry pipelines"
    - "DO NOT add monitoring metrics — QueueMonitor owns daily metrics"
boundaries:
  loc_delta_max: 400, files_touched_max: 3
  required_paths: [backend/ci/failure_watcher.py, backend/ci/tests/test_failure_watcher.py, backend/alerts/routing_config.yaml]
  forbidden_paths: [scripts/ci/, .gitlab-ci.yml, backend/alerts/alert_router.py, backend/alerts/alert_severity.py, backend/alerts/alert_dedup.py]
  test_scope: inline
  destructive_op_classes: [state-append]
  destructive_op_scope: project
  external_side_effect: network-production
  external_payload_class: operational
  execution_mode: unit-testable
  dependency_artifacts: [31.C alert pipeline + GitLab API token]
  mutex_with: []
  interface_contract:
    inputs_from_deps: [route_alert + GitLab API]
    outputs_for_downstream:
      - "ci_pipeline_fail_{main,develop,feature} routing entries"
      - "failure_watcher polling 60s"
      - "tests >=7 pass"
ac.code:
  - {desc: "module + fn importable", verify: {command: "python3 -c 'from backend.ci.failure_watcher import poll_ci_pipelines'", expect_exit_code: 0}}
  - {desc: "3 branch classes routed", verify: {command: "grep -cE 'ci_pipeline_fail_(main|develop|feature)' backend/alerts/routing_config.yaml", expect_stdout_match: "^[3-9]$"}}
  - {desc: "tests count >=7", verify: {command: "pytest --collect-only backend/ci/tests/test_failure_watcher.py 2>&1 | grep -c 'test_'", expect_stdout_match: "^[7-9]$|^[1-9][0-9]+$"}}
  - {desc: "tests pass", verify: {command: "pytest backend/ci/tests/test_failure_watcher.py -v", expect_exit_code: 0, timeout_seconds: 30}}
go_live: T+1d after CIWave2Smoke + 31.C-Integration
```

### 31.E-QueueMonitor — NEW (codex Q4 fix)

```yaml
title: "AUDIT-31.E-QueueMonitor: Daily runner queue / disk / cache metrics + alerting"
tier: S, prefer: codex, class: subscription-codex
area_labels: [area:backend, area:ci, area:tooling]
blockedBy: [6a, 10ab]
context_hint:
  produces: "backend/ci/queue_monitor.py + deploy/systemd-user/omnisight-ci-queue-monitor.{service,timer} + backend/ci/tests/test_queue_monitor.py"
  consumed_by: "operator daily alerts via 31.C; 31.E-Integration"
  inputs_expected: "Runner active (6a); alert routing live (10ab)"
  outputs_guaranteed: |
    Module polls daily (via systemd USER timer 08:00 local):
    - pending_jobs_count (from GitLab API)
    - queue_wait_p95_seconds (over last 24h)
    - runner_status (active/offline)
    - disk_free_gb (on dev WSL)
    - docker_cache_size_gb
    Writes daily JSONL to ~/.cache/omnisight/ci-queue-metrics.jsonl.
    Emits route_alert when:
    - queue_wait_p95 > 600s (10 min) for develop/main → P2
    - queue_wait_p95 > 1800s (30 min) for feature → P3
    - disk_free < 5 GB → P1 (host fillup risk)
    - runner offline > 5 min → P1
    Tests >=5: parses-GitLab-API / classifies-severity-by-threshold / disk-low-P1 / runner-offline-P1 / JSONL-append-atomic.
  non_goals:
    - "DO NOT modify alert_router"
    - "DO NOT manage docker cache cleanup (separate concern)"
    - "DO NOT auto-restart runner — operator-only via runbook"
boundaries:
  loc_delta_max: 350, files_touched_max: 4
  required_paths: [backend/ci/queue_monitor.py, deploy/systemd-user/omnisight-ci-queue-monitor.service, deploy/systemd-user/omnisight-ci-queue-monitor.timer, backend/ci/tests/test_queue_monitor.py]
  forbidden_paths: [scripts/ci/, .gitlab-ci.yml, backend/alerts/alert_router.py, backend/ci/failure_watcher.py]
  test_scope: inline
  destructive_op_classes: [state-append, systemd-control]
  destructive_op_scope: project
  external_side_effect: network-production
  external_payload_class: operational
  execution_mode: unit-testable
  dependency_artifacts: [6a runner active, backend/alerts/alert_router.py]
  mutex_with: []
  interface_contract:
    inputs_from_deps: [GitLab API + route_alert]
    outputs_for_downstream:
      - "daily ci-queue-metrics.jsonl"
      - "P1/P2/P3 alerts on threshold breach"
ac.code:
  - {desc: "module importable", verify: {command: "python3 -c 'from backend.ci.queue_monitor import poll_queue_metrics'", expect_exit_code: 0}}
  - {desc: "timer files exist + USER systemd", verify: {command: "test -f deploy/systemd-user/omnisight-ci-queue-monitor.timer && grep -cE 'WantedBy=default.target' deploy/systemd-user/omnisight-ci-queue-monitor.timer", expect_stdout_match: "[1-9]"}}
  - {desc: "NO multi-user.target", verify: {command: "grep -cE 'WantedBy=multi-user.target' deploy/systemd-user/omnisight-ci-queue-monitor.timer", expect_stdout_match: "^0$"}}
  - {desc: "tests count >=5", verify: {command: "pytest --collect-only backend/ci/tests/test_queue_monitor.py 2>&1 | grep -c 'test_'", expect_stdout_match: "^[5-9]$|^[1-9][0-9]+$"}}
  - {desc: "tests pass", verify: {command: "pytest backend/ci/tests/test_queue_monitor.py -v", expect_exit_code: 0, timeout_seconds: 30}}
go_live: T+1d after 6a + 10ab
```

### 31.E-GHAEnforcementJob — NEW (codex Q4 fix)

```yaml
title: "AUDIT-31.E-GHAEnforcementJob: CI job that rejects new/modified .github/workflows/*.yml without explicit legacy: annotation"
tier: S, prefer: codex, class: subscription-codex
area_labels: [area:ci, area:tooling]
blockedBy: [5bc, 8ab]
context_hint:
  produces: "Patch to .gitlab-ci.yml (Productizer) — add `gha-enforcement` job in lint stage + scripts/ci/gha-enforcement-check.py"
  consumed_by: "31.E-CallSiteInventoryGate (one-shot replaced by this continuous enforcement)"
  inputs_expected: "gha-inventory (5bc); Wave 1 ci.yml deployed (8ab)"
  outputs_guaranteed: |
    GitLab CI job runs on every push touching .github/workflows/*.yml:
    Script checks each modified GHA workflow file has a frontmatter comment matching: `# legacy: <PARITY-EXISTS|LEGACY-NO-MIGRATE|UNCLEAR>`
    If missing OR new file added without classification: job fails with P1 message guiding operator to update gha-inventory.
    Rules: $CI_PIPELINE_SOURCE == "merge_request_event" AND $CI_COMMIT_BRANCH matches feature/*
  non_goals:
    - "DO NOT auto-classify (operator decides)"
    - "DO NOT remove GHA files — 31.K"
    - "DO NOT modify gha-inventory.py — 5bc owns"
boundaries:
  loc_delta_max: 250, files_touched_max: 2
  required_paths: [.gitlab-ci.yml, scripts/ci/gha-enforcement-check.py]
  forbidden_paths: [.github/workflows/, scripts/ci/gha-inventory.py, scripts/ci/ci-yml-generator.py, scripts/ci/ci-yml-validator.py]
  test_scope: defer-to-integration
  destructive_op_classes: []
  destructive_op_scope: project
  external_side_effect: none
  external_payload_class: operational
  execution_mode: structural-only
  dependency_artifacts: [scripts/ci/gha-inventory.py, .gitlab-ci.yml post-8ab]
  mutex_with: [7a, 7b, 8ab, 9a, 9b]   # all touch .gitlab-ci.yml
  interface_contract:
    inputs_from_deps: [gha-inventory + ci.yml]
    outputs_for_downstream:
      - "gha-enforcement CI job in lint stage"
      - "scripts/ci/gha-enforcement-check.py rejects unclassified GHA changes"
ac.code:
  - {desc: "gha-enforcement job present", verify: {command: "grep -cE '^gha-enforcement:|gha-enforcement-check' .gitlab-ci.yml", expect_stdout_match: "[1-9]"}}
  - {desc: "check script exists", verify: {command: "test -x scripts/ci/gha-enforcement-check.py", expect_exit_code: 0}}
  - {desc: "rejects unclassified change (test synthetic)", verify: {command: "echo 'on: push' > /tmp/test-gha.yml && python3 scripts/ci/gha-enforcement-check.py --file /tmp/test-gha.yml | grep -c 'missing legacy'", expect_stdout_match: "[1-9]"}}
go_live: T+1d after 5bc + 8ab
```

### 31.E-11-Doc — Operator runbook (queue + GHA enforcement added)

```yaml
delta_from_v1:
  - new sections: "Queue depth monitoring" + "Adding/modifying .github/workflows (legacy: annotation required)"
  - blockedBy: [10ab, QueueMonitor]
```

### 31.E-CallSiteInventoryGate — one-time audit (continuous = GHAEnforcementJob)

```yaml
delta_from_v1:
  - context_hint.consumed_by: GHAEnforcementJob NOW provides continuous coverage; this gate is one-shot baseline
  - blockedBy: [11-Doc, 5bc, GHAEnforcementJob]   (was [11-Doc, 5bc]; depends on GHAEnforcementJob existing)
```

### 31.E-Integration — daily snapshot AC pattern (codex Q4 + 31.D pattern)

```yaml
delta_from_v1:
  - context_hint.outputs_guaranteed: REWRITTEN — P95 W1 < **900s (15 min)** (was 600s); aspirational <600s tracked separately
  - integration.json new field `aspirational_p95_w1_seconds` (tracks improvement toward <10 min target)
ac.integration_replacements:
  - {desc: "P95 W1 < 900s (15 min initial)", verify: {command: "jq -r .p95_w1_seconds docs/audit/AUDIT-31-phase-E-evidence/integration.json", expect_stdout_match: "^([0-9]|[1-9][0-9]|[1-8][0-9]{2}|900)$"}}
  - {desc: "aspirational metric also recorded", verify: {command: "jq -r .aspirational_p95_w1_seconds docs/audit/AUDIT-31-phase-E-evidence/integration.json", expect_stdout_match: "^[0-9]+$"}}
  - {desc: "P95 W1+W2 < 1800s", verify: {command: "jq -r .p95_w1w2_seconds docs/audit/AUDIT-31-phase-E-evidence/integration.json", expect_stdout_match: "^([0-9]|[1-9][0-9]{1,2}|1[0-7][0-9]{2}|1800)$"}}
```

---

## §6. Filing batch order (DAG v2)

```
L0: 1a, 2a, 3a, 4a, 5a (5 specs parallel)
L1: 1bc, 2bc, 3bc, 5bc (4 libs parallel)
L2: LibsGate (1)
L3: 6a (1 operator-window; cross-phase 31.D-StabilityCheckpoint)
L4: RunnerReadyGate (1)
L5: 7a, 7b (2 parallel)
L6: 8ab (1, MERGED)
L7: CIWave1Smoke (1 operator-rehearsal)
L8: 9a (1)
L9: 9b (1, FIXED structural-only)
L10: CIWave2Smoke (1 operator-rehearsal)
L11: 10ab (1, MERGED)
L12: QueueMonitor, GHAEnforcementJob (2 parallel NEW)
L13: 11-Doc (1)
L14: CallSiteInventoryGate (1)
L15: Integration-Doc (1)
L16: Integration (1, tier:L)
```

17 layers, 26 tickets.

---

## §7. Operator answers (all locked 2026-05-13)

| # | Question | Locked answer |
|---|---|---|
| 1 | GitLab Runner host | **dev WSL initial**; revisit dedicated host at 31.J prod cutover |
| 2 | GHCR vs GitLab Container Registry | **GHCR for 31.E**; **GitLab CR migration moves to 31.F** (cosign + CR migration coupled cleanly) |
| 3 | Pipeline timeouts | **W1 P95 <15 min initial** / <10 min aspirational; W2 +20 min; revisit when false-positive timeout >5% |
| 4 | CI concurrency | **5 concurrent jobs per runner** (configurable in runner config) |
| 5 | Failure alert dedup window | **10 min** (matches 31.C-4bc default) |
| 6 | GHA decommission timing | 31.E does audit + enforce. **Actual removal in Phase 31.K, after 31.E-Integration PASS + 24-72h stable** (operator window) |
| 7 | Integration soak | **14 days** (parity with 31.C/31.D; daily snapshot file-count AC) |
| 8 | GitLab CI variables provisioning | **manual via GitLab UI for v1**; API automation deferred to 31.K |

---

## §8. Submission flow

1. This spec at `docs/sprint-s12/phase-31e-ticket-spec.md` (v2)
2. Operator §7 lock (8 items)
3. Commit + Gerrit +2 + submit
4. Filing script files 26 children
5. Spot-check first 3

---

**End of Phase 31.E ticket spec v2 (26-ticket; 9b boundary fixed; 8a+8b/10a+10b merged; generator emits full Wave 1; new QueueMonitor + GHAEnforcementJob; SLA realistic <15 min). Awaiting operator §7 lock.**
