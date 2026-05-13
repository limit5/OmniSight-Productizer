---
id: SPRINT-S12-PHASE-31G-SPEC
version: v2 (post codex review)
title: Sprint S12 Phase 31.G — Prometheus + Grafana + Alertmanager · Ticket Spec
scope: stack deploy + /metrics endpoints + dashboards + AM→31.C bridge with auth + port preflight + resource budgets + network-localhost schema
status: Draft (2026-05-13)
related:
  - ADR-0023 §3.4
  - 31.F-StabilityCheckpoint (cross-phase blocker — operator §7 Q8 lock)
  - Phase 31.A v2 + 31.B v2 + 31.C v2 + 31.D v2 + 31.E v2 + 31.F v2
  - 31.G v1 (pre codex review, superseded)
  - Codex independent review (2026-05-13, /tmp/phase31g-codex-review-final.txt)
---

# Sprint S12 Phase 31.G · Observability Stack — Ticket Spec v2

## §0. v2 Changelog (vs v1 — driven by codex independent review)

**P0 BLOCKING fixes:**

| Item | v1 problem | v2 fix |
|---|---|---|
| **7a/7e parallel-edit race** | both modify `backend/ci/failure_watcher.py` despite table claiming different files | v2: **7e.mutex_with: [7a]** + explicit `blockedBy: [7a, ...]` for 7e (7a must close first) |
| **Port allocation no preflight** | ports 8001-8006 hardcoded; never checked for occupancy | v2: **NEW LibsGate AC `PortPreflight`** — `ss -ltnp` + Python socket-bind check 8001-8006 free + record evidence; LibsGate fails if any occupied |
| **9a AM-bridge no auth** | webhook endpoint accepts any POST | v2: **9a hardened** — `127.0.0.1` bind, shared `WEBHOOK_TOKEN` header (mandatory), payload schema validation (Alertmanager format), payload size cap (1 MB), reject + log + 401 on missing/bad token |
| **Alert duplication risk** | AM retries + 31.C overlap → double Discord alerts | v2: 9a explicit **dedupe key** (Alertmanager `fingerprint` if present, else SHA1 of `alertname+area+startsAt+labels-sorted`) + **resolved-notification policy** (route as P3 recovery; NOT suppressed silently) |

**P1 STRUCTURAL fixes:**

| Item | v1 problem | v2 fix |
|---|---|---|
| 4bc metric_exporter ACs sparse | could be implemented inconsistently | v2: **5 expanded ACs** (idempotent same-name re-reg / reject same-name+diff-label / `127.0.0.1` bind default / no-server-at-import / prometheus_client dep declared) |
| 7a-7e grep-only verification | doesn't prove metric registered or scrape works | v2: **each adds behavior smoke unit test** — exercise module path, assert `omnisight_*` sample appears in registry |
| 9b too large (5 rules + AM config) | threshold mistakes operationally expensive | v2: **split into 9b-config (AM routing) + 9c-rules (5 prometheus rules files)**; per-rule promtool test required |
| StackSmoke dashboards_rendered_count | count can pass while wrong dashboards render | v2: **named dashboard evidence** — 5 dashboard UIDs/titles enumerated, each with ≥1 non-empty panel query result |

**P2 OPERATIONAL fixes:**

| Item | v1 problem | v2 fix |
|---|---|---|
| WSL resource pressure unbounded | stack adds RAM/CPU on top of existing services | v2: **resource budget** in ADR-0028 + 1a/2a/3a (memory limits in compose, Prometheus retention/storage cap, no Grafana plugin install by default); **StackReadyGate** adds `docker stats --no-stream` evidence + `dockerd` memory under 2GB total |
| 9a "respects AM silences" misleading | AM normally suppresses BEFORE webhook delivery | v2: reword **"does not bypass Alertmanager silencing; only bridges notifications received from AM"** |
| AM retries + resolved-notification behavior undefined | implementers diverge | v2: 9a explicit policy — `firing` → P1/P2/P3 per severity; `resolved` → P3 recovery event (NOT silently suppress); retry-idempotent via dedupe key |

**P3 SCHEMA additions (extends 31.C v2 §3.4 + 31.F v2 §3.9):**

```yaml
external_side_effect: none | network-localhost | network-test | network-production | email | discord
                                  ^^^^^^^^^^^^^^^^^^^ NEW
scope_components: [host-cred-dir | host-fs-project | ... | localhost-listener]
                                                            ^^^^^^^^^^^^^^^^ NEW
```

`network-localhost` rule (filing hook):
- Listener binds 127.0.0.1 ONLY; no egress; not externally reachable
- Auto-allowed (no operator-* class needed)
- Filing-time check: ticket description/AC explicitly states 127.0.0.1 binding

`localhost-listener` scope_components value (filing hook):
- ticket creates a new HTTP/socket listener on localhost
- LibsGate / StackReadyGate must verify port preflight before this ticket starts
- If listener can be exposed via --expose-host-debug or similar, MUST also have auth mechanism documented

**Cross-phase preflight (carried forward from 31.F v2 §0; v2 makes explicit):**

> Filing script does JQL/Gerrit preflight on every `*-external` blocker: assert blocker ticket Closed AND its submit-SHA known. Evidence written to filing artifact. Applies to 6a, 7e, 9a in this phase.

**Ticket count**: v1 29 → v2 **30** (9b → 9b-config + 9c-rules: +1)

---

## §1, §2 — unchanged from v1

---

## §3. Schemas (extended)

Reuses all prior + adds:
- `external_side_effect: network-localhost` (between `none` and `network-test`)
- `scope_components: localhost-listener` value

[Full schema rules in §0 above.]

---

## §4. Children — overview table (30 tickets v2)

[Same table as v1 §4 with these changes:]

| Layer | # | ID | Change |
|---|---|---|---|
| L5 | 20 | 31.G-7e | **mutex_with: [7a]**; **blockedBy adds: 7a** (sequential, NOT parallel) |
| L7 | 23 | 31.G-9a | Hardened (auth + dedupe + resolved policy) |
| L7-split | 24 | 31.G-9b-config | **SPLIT v1 9b**: AM routing config only |
| L7-split | 25 | 31.G-9c-rules | **NEW SPLIT from v1 9b**: 5 Prometheus rules files + per-rule promtool tests |
| L8 | 26 | 31.G-StackSmoke | Strengthened named-dashboard evidence + retries/resolved verify |
| L11 | 27 | 31.G-LibsGate | NEW `PortPreflight` AC + container_memory_budget AC |

All other tickets unchanged in structure but layered numbers shift by +1 from 9b split.

Full updated table:

| Layer | # | ID | Title | Tier | Prefer | Class | blockedBy |
|---|---|---|---|---|---|---|---|
| L0 | 1-5 | 31.G-1a..5a | specs (unchanged) | S | claude | claude | — |
| L1 | 6 | 31.G-1bc | Prometheus deploy (resource budget added) | S | codex | codex + operator-prepare-only | 1a |
| L1 | 7 | 31.G-2bc | Grafana deploy (no plugin install default) | S | codex | codex + operator-prepare-only | 2a |
| L1 | 8 | 31.G-3bc | Alertmanager deploy | S | codex | codex + operator-prepare-only | 3a |
| L1 | 9 | 31.G-4bc | metric_exporter (5 expanded ACs) | S | codex | codex | 5a |
| L1 | 10 | 31.G-5bc | dashboard JSONs | S | claude | claude | 2a, 5a |
| L2-Gate | 11 | 31.G-LibsGate | + PortPreflight + memory budget AC | S | claude | claude | 1bc, 2bc, 3bc, 4bc, 5bc, 4a |
| L3 | 12-14 | 31.G-6a/b/c | operator-window deploys (unchanged) | S | claude | claude + operator-window | LibsGate (+ 31.F-StabilityCheckpoint-external for 6a) |
| L4-Gate | 15 | 31.G-StackReadyGate | + container memory budget < 2GB AC | S | claude | claude | 6a, 6b, 6c |
| L5 | 16 | 31.G-7a | CI metrics (+ behavior smoke unit test) | S | codex | codex | StackReadyGate, 4bc |
| L5 | 17 | 31.G-7b | replication metrics (+ smoke) | S | codex | codex | StackReadyGate, 4bc |
| L5 | 18 | 31.G-7c | runner metrics (+ smoke) | S | codex | codex | StackReadyGate, 4bc |
| L5 | 19 | 31.G-7d | alert metrics (+ smoke) | S | codex | codex | StackReadyGate, 4bc |
| L6 | 20 | 31.G-7e | signing/verify metrics (mutex_with 7a; **blockedBy adds 7a**) | S | codex | codex | 7a, StackReadyGate, 4bc, 31.F-StabilityCheckpoint-external |
| L7 | 21 | 31.G-8a | Prometheus scrape config | S | codex | codex + operator-prepare-only | 7a, 7b, 7c, 7d, 7e |
| L7 | 22 | 31.G-8b | Grafana provisioning | S | codex | codex + operator-prepare-only | 8a, 5bc |
| L8 | 23 | 31.G-9a | AM→31.C bridge (auth + dedupe + resolved policy) | S | claude | claude | 8a, 31.C-Integration-external |
| L9 | 24 | 31.G-9b-config | **SPLIT**: AM routing config only | S | claude | claude | 9a |
| L9 | 25 | 31.G-9c-rules | **NEW SPLIT**: 5 Prometheus rules + per-rule promtool tests | S | claude | claude | 9a |
| L10-Gate | 26 | 31.G-StackSmoke | named dashboard evidence + resolved-notification verify | S | claude | claude + operator-rehearsal | 9b-config, 9c-rules, 8b |
| L11 | 27 | 31.G-10-Doc | runbook (+ AM silencing wording) | S | claude | claude | StackSmoke |
| L12 | 28 | 31.G-CallSiteInventoryGate | audit (unchanged) | S | claude | claude | 10-Doc |
| L13 | 29 | 31.G-StabilityCheckpoint | 24-72h → unblocks 31.H + 31.I | S | claude | claude + operator-window | CallSiteInventoryGate |
| L14 | 30 | 31.G-Integration | 14-day soak (tier:L) | L | claude | claude + operator-window | StabilityCheckpoint |

---

## §5. Per-child specs (v2 deltas)

### 31.G-1a, 2a, 3a — add resource budget section

```yaml
delta_from_v1:
  - outputs_guaranteed: ADD "Resource budget (Prometheus 512MB; Grafana 512MB; Alertmanager 256MB); 30-day Prometheus storage cap 10GB; NO Grafana plugin install by default"
  - ADR-0028 (4a) reflects same budgets
ac.code_additions:
  - {desc: "doc has resource budget section", verify: {command: "grep -ciE 'resource budget|memory limit|storage cap' docs/sop/{prometheus,grafana,alertmanager}-deploy-spec.md", expect_stdout_match: "[1-9]"}}
```

### 31.G-4bc — 5 expanded ACs (codex Q2)

```yaml
delta_from_v1:
  - context_hint.outputs_guaranteed: REWRITTEN — explicit semantics:
    1. get_or_create_* is IDEMPOTENT: same-name + same-labels → returns existing instance
    2. get_or_create_* REJECTS same-name + different-labels (raises ValueError)
    3. serve_metrics(port, host='127.0.0.1') — host DEFAULT 127.0.0.1; explicit override required for any other binding
    4. NO server start at module import time; serve_metrics() must be called explicitly
    5. Module declares prometheus_client dependency in backend/requirements.txt (separate small PR if not already present)
  - tests >=12 (was 8; +4 for new semantics)
ac.code_additions:
  - {desc: "same-name+same-labels returns same instance (idempotent)", verify: {command: "grep -cE 'def test_.*idempotent|def test_.*same.*labels.*returns.*same' backend/observability/tests/test_metric_exporter.py", expect_stdout_match: "[1-9]"}}
  - {desc: "same-name+diff-labels raises ValueError", verify: {command: "grep -cE 'def test_.*reject.*diff.*labels|def test_.*conflict.*labels' backend/observability/tests/test_metric_exporter.py", expect_stdout_match: "[1-9]"}}
  - {desc: "serve_metrics default host=127.0.0.1", verify: {command: "grep -cE 'host:\\s*str\\s*=\\s*[\\\"\\']127.0.0.1' backend/observability/metric_exporter.py", expect_stdout_match: "[1-9]"}}
  - {desc: "no server-at-import test", verify: {command: "grep -cE 'def test_.*no.*server.*import|def test_.*import.*does_not_start' backend/observability/tests/test_metric_exporter.py", expect_stdout_match: "[1-9]"}}
  - {desc: "prometheus_client in backend/requirements.txt", verify: {command: "grep -c 'prometheus.client\\|prometheus-client' backend/requirements.txt", expect_stdout_match: "[1-9]"}}
```

### 31.G-LibsGate — add PortPreflight + memory budget (codex P0/Q4)

```yaml
delta_from_v1:
  - context_hint.outputs_guaranteed: ADD checks (now 8 total instead of 6):
    7. PortPreflight: ports 8001, 8002, 8003, 8004, 8005, 8006 ALL free (no current listener)
    8. metric_exporter dependency check (prometheus_client importable)
ac.code_additions:
  - {desc: "PortPreflight 8001-8006 all free", verify: {command: "for p in 8001 8002 8003 8004 8005 8006; do ss -ltnp 2>/dev/null | grep -c \":$p \" && exit 1 || true; done; echo OK", expect_exit_code: 0}}
  - {desc: "libs-gate.json has 8+ check fields", verify: {command: "python3 -c \"import json; d=json.load(open('docs/audit/AUDIT-31-phase-G-evidence/libs-gate.json')); checks=[k for k in d.keys() if k not in ('git_sha','timestamp')]; assert len(checks) >= 8\"", expect_exit_code: 0}}
```

### 31.G-6a/6b/6c — unchanged (already operator-window)

### 31.G-StackReadyGate — add memory budget AC (codex P2)

```yaml
delta_from_v1:
  - context_hint.outputs_guaranteed: ADD "Resource budget verification via `docker stats --no-stream`: total stack memory (prometheus + grafana + alertmanager) < 2 GB; record evidence"
ac.code_additions:
  - {desc: "stack memory < 2GB", verify: {command: "docker stats --no-stream --format '{{.MemUsage}}' omnisight-prometheus omnisight-grafana omnisight-alertmanager | awk -F'/' '{gsub(/MiB|GiB/, \"\"); sum += $1} END {if (sum < 2048) print \"OK\"; else print \"FAIL\"}'", expect_stdout_match: "^OK$"}, run_as: operator}
```

### 31.G-7a — add behavior smoke unit test (codex Q3)

```yaml
delta_from_v1:
  - new AC: per-module behavior smoke test
ac.code_additions:
  - {desc: "smoke unit test asserts omnisight_ci_* sample in registry post-wiring", verify: {command: "grep -cE 'def test_.*ci.*metric.*registered|def test_.*omnisight_ci_' backend/ci/tests/test_failure_watcher.py backend/ci/tests/test_queue_monitor.py 2>/dev/null | awk -F: '{sum+=$2} END {if (sum >= 1) print \"OK\"; else print \"FAIL\"}'", expect_stdout_match: "^OK$"}}
```

### 31.G-7b/7c/7d — same delta pattern as 7a (smoke unit test per module)

### 31.G-7e — mutex + blockedBy fix + smoke (codex P0)

```yaml
delta_from_v1:
  - boundaries.mutex_with: [7a]   (NEW — both touch failure_watcher.py)
  - blockedBy: [7a, StackReadyGate, 4bc, 31.F-StabilityCheckpoint-external]   (NEW — 7a sequential before 7e)
  - new AC: smoke test for omnisign_signing_* metric registered
ac.code_additions:
  - {desc: "smoke unit test asserts omnisight_signing_* in registry", verify: {command: "grep -cE 'def test_.*omnisight_signing_|def test_.*sign.*metric.*registered' backend/ci/tests/test_failure_watcher.py backend/security/tests/test_cosign_verify_adapter.py 2>/dev/null | awk -F: '{sum+=$2} END {if (sum >= 1) print \"OK\"; else print \"FAIL\"}'", expect_stdout_match: "^OK$"}}
  - {desc: "7e diff does NOT remove omnisight_ci_ metrics from 7a", verify: {command: "git diff HEAD~1 HEAD backend/ci/failure_watcher.py | grep -cE '^-.*omnisight_ci_'", expect_stdout_match: "^0$"}}
```

### 31.G-8a, 8b — unchanged

### 31.G-9a — HARDENED (codex P0)

```yaml
delta_from_v1:
  - context_hint.outputs_guaranteed: REWRITTEN — explicit security + dedupe + resolved policy:
    1. HTTP endpoint :8006/alertmanager-webhook binds 127.0.0.1 ONLY (NEVER 0.0.0.0)
    2. REQUIRES header `X-Webhook-Token: $AM_BRIDGE_TOKEN` (env var; matched against shared secret from operator-managed cred file ~/.config/omnisight/am-bridge-token)
    3. Missing/wrong token → 401 + structured log entry (NO token value logged)
    4. Payload size cap: 1 MB (413 if exceeds)
    5. Payload schema validation: must parse as Alertmanager webhook format (`receiver`, `alerts: List[...]`, etc.) — reject if malformed
    6. "Does NOT bypass AM silencing; only bridges notifications received from AM" (codex Q4 wording fix)
    7. Dedupe key derivation: per-alert `fingerprint` if Alertmanager provides it, else SHA1(`alertname` + `area` + `startsAt` + sorted(`labels`))
    8. Resolved notification policy: AM alert with `status=resolved` → call route_alert with event_class derived from alertname suffixed `_resolved`; severity P3 (recovery event)
    9. Retries: AM may retry webhook; dedupe key ensures idempotent route_alert (31.C-4bc handles)
  - boundaries.external_side_effect: network-localhost (was network-test — codex P3 schema)
  - boundaries.scope_components: [host-fs-project, localhost-listener]
  - tests >=8 (was 6; +2 for auth + dedupe)
ac.code_additions:
  - {desc: "binds 127.0.0.1 ONLY", verify: {command: "grep -cE '127\\.0\\.0\\.1|localhost' backend/observability/alertmanager_bridge.py", expect_stdout_match: "[1-9]"}}
  - {desc: "NO 0.0.0.0 bind", verify: {command: "grep -cE 'host\\s*=\\s*[\\\"\\']0\\.0\\.0\\.0|bind.*0\\.0\\.0\\.0' backend/observability/alertmanager_bridge.py", expect_stdout_match: "^0$"}}
  - {desc: "X-Webhook-Token header validated", verify: {command: "grep -cE 'X-Webhook-Token|AM_BRIDGE_TOKEN' backend/observability/alertmanager_bridge.py", expect_stdout_match: "[1-9]"}}
  - {desc: "401 on missing/bad token", verify: {command: "grep -cE '401|HTTPException.*401|Unauthorized' backend/observability/alertmanager_bridge.py", expect_stdout_match: "[1-9]"}}
  - {desc: "payload size cap", verify: {command: "grep -cE 'size.*1.*MB|1_000_000|1048576|413' backend/observability/alertmanager_bridge.py", expect_stdout_match: "[1-9]"}}
  - {desc: "dedupe key derivation", verify: {command: "grep -cE 'fingerprint|dedupe.*key|sha1.*alertname' backend/observability/alertmanager_bridge.py", expect_stdout_match: "[1-9]"}}
  - {desc: "resolved → _resolved event_class P3", verify: {command: "grep -cE 'status.*resolved|_resolved.*P3' backend/observability/alertmanager_bridge.py", expect_stdout_match: "[1-9]"}}
  - {desc: "auth test", verify: {command: "grep -cE 'def test_.*missing_token.*401|def test_.*bad_token.*401' backend/observability/tests/test_alertmanager_bridge.py", expect_stdout_match: "[1-9]"}}
  - {desc: "dedupe test", verify: {command: "grep -cE 'def test_.*dedupe|def test_.*retry.*idempotent' backend/observability/tests/test_alertmanager_bridge.py", expect_stdout_match: "[1-9]"}}
```

### 31.G-9b-config — SPLIT from v1 9b (codex Q3)

```yaml
title: "AUDIT-31.G-9b-config: Alertmanager routing config (route → bridge webhook)"
tier: S, prefer: claude, class: subscription-claude
area_labels: [area:devops, area:observability]
blockedBy: [9a]
context_hint:
  produces: "Patch to deploy/observability/alertmanager/alertmanager.yml — route to bridge webhook URL with auth header"
  consumed_by: "31.G-9c-rules (uses these AM receivers), 31.G-StackSmoke"
  inputs_expected: "Bridge endpoint :8006 + token from 9a"
  outputs_guaranteed: |
    alertmanager.yml:
    - route → webhook_configs → http://host.docker.internal:8006/alertmanager-webhook
    - http_config: { authorization: { type: 'X-Webhook-Token', credentials: '$AM_BRIDGE_TOKEN_FROM_ENV' } } (operator provides via compose env)
    - send_resolved: true (resolved events forwarded; bridge routes as P3 per 9a)
    - group_wait/group_interval per spec from 3a (5 min default, per-severity override possible later)
  non_goals:
    - "DO NOT add rules — 9c-rules owns"
    - "DO NOT modify bridge — 9a owns"
boundaries:
  loc_delta_max: 80, files_touched_max: 1
  required_paths: [deploy/observability/alertmanager/alertmanager.yml]
  forbidden_paths: [deploy/observability/prometheus/rules/, backend/observability/alertmanager_bridge.py]
  test_scope: defer-to-integration
  destructive_op_classes: []
  destructive_op_scope: project
  external_side_effect: none
  external_payload_class: secret-adjacent
  execution_mode: structural-only
  dependency_artifacts: [bridge from 9a]
  mutex_with: [9c-rules]
  interface_contract:
    inputs_from_deps: [bridge :8006 + token]
    outputs_for_downstream: ["AM routes to bridge with auth + resolved-forwarded"]
ac.code:
  - {desc: "webhook URL configured", verify: {command: "grep -cE 'alertmanager-webhook' deploy/observability/alertmanager/alertmanager.yml", expect_stdout_match: "[1-9]"}}
  - {desc: "send_resolved: true", verify: {command: "grep -cE 'send_resolved:\\s*true' deploy/observability/alertmanager/alertmanager.yml", expect_stdout_match: "[1-9]"}}
  - {desc: "auth header configured", verify: {command: "grep -cE 'X-Webhook-Token|authorization' deploy/observability/alertmanager/alertmanager.yml", expect_stdout_match: "[1-9]"}}
  - {desc: "amtool check-config passes", verify: {command: "amtool check-config deploy/observability/alertmanager/alertmanager.yml", expect_exit_code: 0}}
go_live: T+0.5d after 9a
```

### 31.G-9c-rules — NEW SPLIT (codex Q3)

```yaml
title: "AUDIT-31.G-9c-rules: 5 Prometheus alerting rules files + per-rule promtool tests"
tier: S, prefer: claude, class: subscription-claude
area_labels: [area:devops, area:observability, area:tests]
blockedBy: [9a]
context_hint:
  produces: "deploy/observability/prometheus/rules/{ci,replication,runner,alerts,signing}.rules.yml + per-rule promtool unit-test fixtures"
  consumed_by: "31.G-StackSmoke"
  inputs_expected: "Bridge live (9a)"
  outputs_guaranteed: |
    5 rules files, one per dashboard area:
    - ci.rules.yml: CIPipelineP95High (>10 min for >5 min), CIFailureRateHigh (>10% over 1h)
    - replication.rules.yml: ReplicationLagHigh (>30s for >2 min), DLQGrowing (>0 entries for >5 min)
    - runner.rules.yml: RunnerStuckLong (heartbeat > 10 min), MutexRaceDetected (any race in last hour)
    - alerts.rules.yml: AlertVolumeCapNear (>80% of daily Discord cap)
    - signing.rules.yml: SignSuccessRateLow (<99% over 1h), AllowlistDaysLow (<14 days remaining)
    Each rule has:
    - alert name + expr + for + labels (severity per AM routing) + annotations (summary + runbook_url)
    Per-rule unit test in tests/test_rules.py uses promtool test rules with synthetic time-series fixture.
  non_goals:
    - "DO NOT modify AM config — 9b-config owns"
    - "DO NOT modify bridge — 9a owns"
boundaries:
  loc_delta_max: 400, files_touched_max: 6   # 5 rules + 1 test
  required_paths: [deploy/observability/prometheus/rules/ci.rules.yml, deploy/observability/prometheus/rules/replication.rules.yml, deploy/observability/prometheus/rules/runner.rules.yml, deploy/observability/prometheus/rules/alerts.rules.yml, deploy/observability/prometheus/rules/signing.rules.yml, deploy/observability/prometheus/rules/tests/test_rules.py]
  forbidden_paths: [deploy/observability/alertmanager/alertmanager.yml, backend/observability/alertmanager_bridge.py]
  test_scope: inline
  destructive_op_classes: []
  destructive_op_scope: project
  external_side_effect: none
  external_payload_class: operational
  execution_mode: unit-testable
  dependency_artifacts: [bridge from 9a]
  mutex_with: [9b-config]
  interface_contract:
    inputs_from_deps: [Prometheus + bridge live]
    outputs_for_downstream: ["5 rules files with thresholds", "per-rule promtool tests pass"]
ac.code:
  - {desc: "5 rules files exist", verify: {command: "ls deploy/observability/prometheus/rules/*.yml | wc -l", expect_stdout_match: "^[5-9]$|^[1-9][0-9]+$"}}
  - {desc: "promtool check rules passes for each", verify: {command: "for f in deploy/observability/prometheus/rules/*.yml; do promtool check rules $f || exit 1; done", expect_exit_code: 0}}
  - {desc: "promtool test rules passes (per-rule synthetic)", verify: {command: "promtool test rules deploy/observability/prometheus/rules/tests/test_rules.py", expect_exit_code: 0}}
  - {desc: "each rule has runbook_url annotation", verify: {command: "for f in deploy/observability/prometheus/rules/*.yml; do grep -c runbook_url $f | grep -q '^0$' && exit 1 || true; done; echo OK", expect_stdout_match: "^OK$"}}
go_live: T+0.5d after 9a
```

### 31.G-StackSmoke — strengthened named-dashboard + resolved verify (codex Q3)

```yaml
delta_from_v1:
  - context_hint.outputs_guaranteed: REWRITTEN evidence:
    1. synthetic alert fired → AM rule → bridge → 31.C → Discord within 5 min ✓
    2. 5 named dashboards loaded by UID (uids: ci-pipeline, replication-sla, runner-pickup, alert-volume, signing-verify); for each dashboard, ≥1 panel returns non-empty data via Grafana API query
    3. resolved notification path: synthetic alert RESOLVES → bridge routes as P3 recovery event → Discord receives
    4. dedupe path: same synthetic alert RE-FIRED within 10 min → 31.C dedups (NOT double-Discord)
  - boundaries.destructive_op_scope: mixed (codex Q5)
  - boundaries.scope_components: [host-fs-project, systemd-user, external-network-prod]
ac.code_replacements:
  - {desc: "5 named dashboards rendered (UIDs verified)", verify: {command: "jq -r '.dashboard_uids | length' docs/audit/AUDIT-31-phase-G-evidence/stack-smoke.json", expect_stdout_match: "^[5-9]$|^[1-9][0-9]+$"}, run_as: operator}
  - {desc: "each dashboard has ≥1 non-empty panel", verify: {command: "jq -r '[.dashboard_uids[] | select(.non_empty_panel_count >= 1)] | length' docs/audit/AUDIT-31-phase-G-evidence/stack-smoke.json", expect_stdout_match: "^[5-9]$|^[1-9][0-9]+$"}, run_as: operator}
  - {desc: "resolved notification routed P3", verify: {command: "jq -r .resolved_routed_as_p3 docs/audit/AUDIT-31-phase-G-evidence/stack-smoke.json", expect_stdout_match: "^true$"}, run_as: operator}
  - {desc: "dedupe prevented double-Discord", verify: {command: "jq -r .dedupe_prevented_double_discord docs/audit/AUDIT-31-phase-G-evidence/stack-smoke.json", expect_stdout_match: "^true$"}, run_as: operator}
```

### 31.G-10-Doc — wording fix (codex P2)

```yaml
delta_from_v1:
  - section "Silencing an alert in AM UI" REPHRASED: "Silencing happens via Alertmanager UI; AM suppresses silenced notifications BEFORE delivery to bridge. Bridge does NOT independently bypass silences."
ac.code_additions:
  - {desc: "wording does NOT use 'respects silences' (misleading)", verify: {command: "grep -ciE 'AM suppresses.*before.*delivery|does NOT.*independently bypass' docs/sop/observability-operator-runbook.md", expect_stdout_match: "[1-9]"}}
```

### 31.G-CallSiteInventoryGate, StabilityCheckpoint, Integration — unchanged from v1 structure

---

## §6. Filing batch order (DAG v2)

```
L0: 1a, 2a, 3a, 4a, 5a (5 specs)
L1: 1bc, 2bc, 3bc, 4bc, 5bc (5 deploy artifacts)
L2: LibsGate (+ PortPreflight + memory budget AC)
L3: 6a, 6b, 6c (3 operator-window parallel)
L4: StackReadyGate (+ docker stats AC)
L5: 7a, 7b, 7c, 7d (4 parallel; 7e separated)
L6: 7e (NEW LAYER — sequential after 7a per mutex_with fix)
L7: 8a
L8: 8b
L9: 9a (hardened with auth + dedupe + resolved)
L10: 9b-config, 9c-rules (2 parallel — split from v1 9b)
L11: StackSmoke
L12: 10-Doc
L13: CallSiteInventoryGate
L14: StabilityCheckpoint
L15: Integration (tier:L)
```

16 layers, 30 tickets.

---

## §7. Operator answers (all locked 2026-05-13)

| # | Question | Locked answer |
|---|---|---|
| 1 | Grafana admin password storage | **NEW cred file `grafana-admin-password`** in ~/.config/omnisight/ (retroactive add to 31.A-2 cred YAML) |
| 2 | Prometheus retention | **30 days** default; revisit at 31.G-Integration with disk metrics |
| 3 | Alertmanager grouping wait | **5 min default**; per-severity override left to future rules update |
| 4 | Dashboard auto-import vs manual | **Auto-import baseline**; operator edits saved as forks (don't overwrite committed JSON) |
| 5 | /metrics endpoint auth | **localhost-only plaintext for dev WSL**; revisit at 31.J prod cutover |
| 6 | AM→31.C severity mapping | **critical→P1 / warning→P2 / info→P3** (documented in 3a) |
| 7 | Stack image version updates | **LTS pin now**; quarterly review + per-CVE upgrade ticket |
| 8 | 31.H + 31.I gating | **blockedBy 31.G-StabilityCheckpoint** (24-72h; same as 31.E→31.D and 31.G→31.F) |

---

## §8. Submission flow (unchanged)

---

**End of Phase 31.G ticket spec v2 (30-ticket; auth-hardened bridge + port preflight + 9b split + network-localhost schema + resource budgets). Awaiting operator §7 lock.**
