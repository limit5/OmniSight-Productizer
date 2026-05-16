---
id: SPRINT-S12-PHASE-31C-SPEC
version: v2 (post codex review)
title: Sprint S12 Phase 31.C — Operator Notifier · Ticket Spec
scope: Alert severity taxonomy + Discord (P0/P1 real-time) + Email digest (P2/P3 batch) + dedup + volume cap + ack workflow + wiring into runner/Gerrit/pickup failures + call-site inventory audit
status: Draft (2026-05-13)
related:
  - ADR-0023 §3.6 (Discord webhook P0/P1 + Email digest P2/P3)
  - Phase 31.A v2 + Phase 31.B v2 (schemas reused: boundaries + context_hint + mutex_with + destructive_op_classes + destructive_op_scope)
  - 31.A-2a (cred YAML includes discord-webhook-p0/p1.url + smtp-app-password)
  - 31.B-6cd (detect_stuck_runners — cross-phase dependency for 31.C-6d)
  - Existing modules: backend/notifications.py (1325 LOC), backend/chatops/discord.py (162 LOC), backend/agents/jira_dispatch.py:notify_operator (L386)
  - 31.C v1 (pre codex review, superseded)
  - Codex independent review (2026-05-13, captured /tmp/phase31c-codex-review-final.txt)
---

# Sprint S12 Phase 31.C · Operator Notifier — Ticket Spec v2

## §0. v2 Changelog (vs v1 — driven by codex independent review)

**P0 BLOCKING fixes:**

| Item | v1 problem | v2 fix |
|---|---|---|
| 8b verify BUG | grep accepted `multi-user.target` OR `default.target` — equivalent to allowing forbidden system-level systemd | v2: rejects `multi-user.target`; requires `default.target` |
| Schema gap | `destructive_op_scope` only models filesystem/systemd blast radius — does NOT model "data leaves the machine" (Discord POST, SMTP send) | v2: adds `external_side_effect:` + `external_payload_class:` fields (orthogonal dimension to destructive_op_scope) |
| Volume cap was §7 open question | Without cap, bad deploy → Discord storm → operator disables alerts → 31.C white-elephant | v2: §7 Q7 closed → NEW ticket **31.C-4d alert_volume_cap.py + tests** |

**P1 STRUCTURAL fixes:**

| v1 | v2 | Codex reason |
|---|---|---|
| 5a + 5b | **5ab MERGED** | router behavior without tests = easy structural "done" but wrong |
| 9a + 9b | **9ab MERGED** | ack state schema is persistence-sensitive; needs tests in same atomic |
| 8a | **8a hardened** (add inline tests + log-capture secret check) | SMTP failure handling + secret safety |
| 6b/6c/6d/7a/7b grep verify | **Add behavior smoke verify** (monkeypatch route_alert to raise + assert legacy fallback still prints) | grep proves call exists, NOT preserved behavior |
| Secret check via grep | **Strengthened**: log-capture test + canonical-path linter | grep misses `logger.info("webhook=%s", url)`, exception reprs, dict dumps |

**P2 NEW tickets:**

| ID | Title | Reason |
|---|---|---|
| **31.C-4d** | alert_volume_cap.py + tests (50/day default Discord cap; configurable per-channel; cap-hit → digest fallback) | §7 Q7 lock-in |
| **31.C-CallSiteInventoryGate** | Audit ALL existing notification call sites (notify_operator, backend/notifications.py, backend/chatops/discord.py, runner exits, Gerrit fails, stuck-runner detection); mark each: legacy / wrapped / out-of-scope; gate Integration | Codex Q4: "spec can pass synthetic smokes while real incidents bypass alerts" |

**P3 MUTEX + cross-phase fixes:**

| Ticket | v1 | v2 |
|---|---|---|
| 31.C-7b mutex_with | `[]` | `[6a]` (both touch auto-runner-jira.py) |
| 31.C-6d cross-phase dep | prose-only note | Explicit `blockedBy: [31.C-6c, 31.B-6cd-external]` + preflight `test -f backend/runner/coordinator_heartbeat.py` AC |
| 31.C-9ab non-goals | "Discord reaction stub" | Strengthened: "no socket / no webhook endpoint / no Discord API client import" |

**Ticket count**: v1 28 → v2 **28** (net zero: 2 mergers - 2; 2 new tickets + 2)

---

## §1. Pre-flight reading (unchanged from v1)

1. ADR-0023 §3.6
2. Phase 31.B v2 §3 + §3.2.1 (mutex + destructive_op schemas)
3. Phase 31.C v2 §3.4 (NEW: external_side_effect schema — read before per-child specs)
4. `backend/notifications.py` (1325 LOC existing)
5. `backend/chatops/discord.py` (162 LOC existing)
6. `backend/agents/jira_dispatch.py:notify_operator` (L386)
7. SOP-S12-TICKET-DECOMP

---

## §2. Sub-META definition (unchanged from v1)

[See v1 §2 — sub-META label set + Go-Live target T+3-4wk C4 unchanged]

---

## §3. Schemas

Reuses Phase 31.A v2 §3 (boundaries: + context_hint:) + Phase 31.B v2 §3.1/§3.2/§3.2.1 (mutex_with + destructive_op_classes + destructive_op_scope project/system rule).

### §3.4 NEW: `external_side_effect:` schema (per codex Q5)

`destructive_op_scope` only models filesystem/systemd blast radius. It does NOT model "data leaves the machine" — sending to Discord, SMTP, external HTTP. That is a DIFFERENT safety dimension. Phase 31.C adds:

```yaml
external_side_effect: none | network-test | network-production | email | discord
external_payload_class: synthetic | operational | secret-adjacent   # optional; defaults to operational
```

**Enum semantics**:

| Value | Definition | OP-1042 hook rule |
|---|---|---|
| `none` | Pure local; no network call (or fully-mocked tests) | Auto-allowed |
| `network-test` | Hits external endpoint but in test/dev scope (mocked OR routed to test webhook URL) | Auto-allowed |
| `network-production` | Hits production HTTP endpoint with operational data | REQUIRES `class:operator-rehearsal` OR `class:operator-window` OR `class:operator-prepare-only` |
| `email` | Real SMTP send (any recipient is human-visible) | REQUIRES operator-* class |
| `discord` | Real Discord webhook POST to a channel humans watch | REQUIRES operator-* class |

`external_payload_class` modifies severity of the rule:
- `synthetic`: test data only — operator-* class STILL required for production endpoints, but escalation lower
- `operational`: real incident data — default
- `secret-adjacent`: payload may contain partial secrets / sensitive ticket context — adds requirement that secret-redaction tests pass

This is ORTHOGONAL to `destructive_op_scope`. A ticket can be `destructive_op_scope: none` AND `external_side_effect: discord` — that means "doesn't touch filesystem badly, but DOES post to a channel humans see".

### §3.5 Updated `boundaries:` template

Per-ticket boundaries section now carries (Phase 31.C and later):

```yaml
boundaries:
  loc_delta_max: int
  files_touched_max: int
  required_paths: [str, ...]
  forbidden_paths: [str, ...]
  non_goals: [str, ...]
  interface_contract:
    inputs_from_deps: [str, ...]
    outputs_for_downstream: [str, ...]
  test_scope: defer-to-integration | inline | none
  destructive_op_classes: [enum, ...]
  destructive_op_scope: project | system | none
  external_side_effect: none | network-test | network-production | email | discord
  external_payload_class: synthetic | operational | secret-adjacent    # optional
  dependency_artifacts: [str, ...]
  execution_mode: structural-only | unit-testable | requires-WSL | operator-rehearsal
  mutex_with: [str, ...]
  on_scope_creep: file-followup
  scope_summary_max_chars: 500
```

---

## §4. Children — overview table (28 tickets v2)

| Layer | # | ID | Title | Tier | Prefer | blockedBy |
|---|---|---|---|---|---|---|
| L0 | 1 | 31.C-1a | Alert severity taxonomy spec (P0/P1/P2/P3) | S | claude | — |
| L0 | 2 | 31.C-2a | Discord webhook routing spec | S | claude | — |
| L0 | 3 | 31.C-3a | Email digest spec | S | claude | — |
| L0 | 4 | 31.C-4a | Alert dedup + volume-cap spec (UPDATED — covers 4d) | S | claude | — |
| L1 | 5 | 31.C-1bc | alert_severity.py + tests | S | claude | 1a |
| L1 | 6 | 31.C-2bc | discord_notifier.py + tests (hardened secret check) | S | codex | 2a |
| L1 | 7 | 31.C-3bc | email_digest.py + tests | S | codex | 3a |
| L1 | 8 | 31.C-4bc | alert_dedup.py + tests | S | codex | 4a |
| L1 | 9 | 31.C-4d | **alert_volume_cap.py + tests (NEW)** | S | codex | 4a |
| L2-Gate | 10 | 31.C-LibsGate | Cross-library contract integration check | S | claude | 1bc, 2bc, 3bc, 4bc, 4d |
| L3 | 11 | 31.C-5ab | **alert_router.py + tests (MERGED v1 5a+5b)** | S | claude | LibsGate |
| L3 | 12 | 31.C-5c | Routing config YAML (event class → severity matrix) | S | claude | 1a, 5ab |
| L4 | 13 | 31.C-6a | Feature flag RUNNER_ALERTS_ENABLED in auto-runner-jira.py + jira_dispatch.py | S | claude | 5ab |
| L5 | 14 | 31.C-6b | Preserve legacy notify_operator path under flag=0 (hardened verify) | S | claude | 6a |
| L6-Gate | 15 | 31.C-LegacyNotifySmoke | Legacy flag=0 notification path one-event smoke | S | claude | 6b |
| L7 | 16 | 31.C-6c | Wire alert_router into jira_dispatch.py:notify_operator (flag=1, behavior smoke) | S | claude | LegacyNotifySmoke, 5c |
| L8 | 17 | 31.C-6d | Wire alert to stuck-runner detect (cross-phase 31.B-6cd) | S | claude | 6c |
| L8 | 18 | 31.C-7a | Wire alert into Gerrit push failures (behavior smoke) | S | claude | 6c |
| L8 | 19 | 31.C-7b | Wire alert into runner exit anomalies (behavior smoke) | S | claude | 6c |
| L9 | 20 | 31.C-8a | SMTP client config (hardened: inline tests + secret log-capture) | S | codex | 3bc |
| L9 | 21 | 31.C-8b | Digest user systemd timer (**FIX verify: reject multi-user.target**) | S | codex | 8a |
| L10-Gate | 22 | 31.C-AlertSmoke | Send test P0 alert → verify Discord receipt | S | claude | 6c, 6d, 7a, 7b |
| L10-Gate | 23 | 31.C-DigestSmoke | Trigger test digest → verify email receipt | S | claude | 8a, 8b |
| L11 | 24 | 31.C-9ab | **ack_workflow.py + tests (MERGED v1 9a+9b; strengthened non-goals)** | S | claude | 5c |
| L12-Gate | 25 | 31.C-CallSiteInventoryGate | **NEW: Audit existing notification call sites; mark legacy/wrapped/out-of-scope** | S | claude | AlertSmoke, DigestSmoke, 9ab |
| L13 | 26 | 31.C-10-Doc | Operator notifier runbook | S | claude | CallSiteInventoryGate |
| L14 | 27 | 31.C-Integration-Doc | E2E alert lifecycle runbook | S | claude | 10-Doc |
| L15 | 28 | 31.C-Integration | **E2E alert lifecycle test** | **L** | claude | ALL 1-27 |

**Routing tally**: codex 7, claude 21.

**Critical path**: 1a → 1bc → LibsGate → 5ab → 5c → 6a → 6b → LegacyNotifySmoke → 6c → (6d/7a/7b parallel) → AlertSmoke → CallSiteInventoryGate → 10-Doc → Integration-Doc → Integration. ~14 sequential hops.

---

## §5. Per-child specs (v2 deltas)

For brevity, v2 shows only the deltas from v1 per ticket. Tickets unchanged from v1 are listed by title + "(unchanged)". See v1 spec history in git for full text.

Universal `context_hint.phase_goal`: "Phase 31.C: classified alerts (P0/P1/P2/P3) routed to Discord/Email with dedup + volume cap + ack; runner stops silently swallowing failures."

Universal new fields (all tickets): `external_side_effect:` + `external_payload_class:` per §3.4.

---

### 31.C-1a, 2a, 3a (specs): unchanged structure; all get `external_side_effect: none`

### 31.C-4a — Alert dedup + volume-cap spec (UPDATED)

```yaml
delta_from_v1:
  - title changed: "Alert dedup spec" → "Alert dedup + volume-cap spec"
  - context_hint.outputs_guaranteed: ADD volume-cap rules — default Discord 50/day, per-channel configurable, cap-hit → fall back to email digest with `[CAP-HIT]` prefix
  - boundaries.external_side_effect: none
  - new AC: "documents 50/day default + per-channel override"
ac.code_additions:
  - {desc: "documents default 50/day cap", verify: {command: "grep -cE '50/day|50 alerts.*day' docs/sop/alert-dedup-spec.md", expect_stdout_match: "[1-9]"}}
  - {desc: "documents cap-hit fallback to digest", verify: {command: "grep -ciE 'cap.*hit.*digest|cap.*hit.*fall.*back' docs/sop/alert-dedup-spec.md", expect_stdout_match: "[1-9]"}}
```

### 31.C-1bc — alert_severity.py + tests

```yaml
delta_from_v1:
  - boundaries.external_side_effect: none
  - boundaries.external_payload_class: operational
```

### 31.C-2bc — discord_notifier.py + tests (HARDENED secret check)

```yaml
delta_from_v1:
  - boundaries.external_side_effect: network-test (tests mock httpx; production calls happen in AlertSmoke/Integration only)
  - boundaries.external_payload_class: secret-adjacent
  - non_goals strengthened: "DO NOT import from backend/chatops/discord.py except via documented helpers per 2a spec"
  - new AC: log-capture secret-safety test (not just grep)
ac.code_additions:
  - {desc: "test captures stderr/stdout during error path and asserts webhook URL substring is absent", verify: {command: "grep -cE 'def test_.*log.*no.*secret|def test_.*secret.*not.*in.*log|caplog.*assert.*not in' backend/alerts/tests/test_discord_notifier.py", expect_stdout_match: "[1-9]"}}
  - {desc: "canonical-path linter rule present", verify: {command: "grep -cE '\\.config/omnisight/discord-webhook-p[01]\\.url' backend/alerts/discord_notifier.py", expect_stdout_match: "^[2-9]$|^[1-9][0-9]+$"}}
```

### 31.C-3bc — email_digest.py + tests

```yaml
delta_from_v1:
  - boundaries.external_side_effect: none (buffers only; sender is 8a)
  - boundaries.external_payload_class: operational
```

### 31.C-4bc — alert_dedup.py + tests

```yaml
delta_from_v1:
  - boundaries.external_side_effect: none
  - context_hint.non_goals adds: "DO NOT implement volume cap — 4d owns; dedup is per-incident-class, cap is per-channel-per-day"
  - interface_contract.outputs_for_downstream: explicit "should_alert returns True only if NOT a dedup hit; volume cap is a SEPARATE gate downstream"
```

### 31.C-4d — alert_volume_cap.py + tests (NEW PER A 案)

```yaml
title: "AUDIT-31.C-4d: alert_volume_cap.py + tests"
tier: S, prefer: codex, class: subscription-codex
area_labels: [area:backend, area:tooling, area:tests]
blockedBy: [4a]
context_hint:
  produces: "backend/alerts/alert_volume_cap.py + backend/alerts/tests/test_alert_volume_cap.py"
  consumed_by: "31.C-LibsGate, 31.C-5ab (router uses cap as final gate before send)"
  inputs_expected: "4a spec covers cap rules"
  outputs_guaranteed: |
    Module: under_cap(channel: str, severity: Severity) -> bool;
    record_send(channel: str) -> None.
    Storage: ~/.cache/omnisight/alert-volume-state.json (24h rolling window + flock + atomic temp+rename).
    Defaults: Discord 50/day, Email 200/day, configurable via routing_config.yaml.
    On cap-hit: under_cap returns False; router falls back to email digest with [CAP-HIT] prefix.
    Tests: >=6 covering first-of-day pass + cap-boundary pass + over-cap reject + per-channel independent + 24h window expiry + atomic state.
  non_goals:
    - "DO NOT modify alert_dedup — 4bc owns per-incident dedup"
    - "DO NOT send anywhere — pure gate function"
    - "DO NOT modify routing_config.yaml — 5c owns"
boundaries:
  loc_delta_max: 300, files_touched_max: 2
  required_paths: [backend/alerts/alert_volume_cap.py, backend/alerts/tests/test_alert_volume_cap.py]
  forbidden_paths: [backend/alerts/alert_dedup.py, backend/alerts/alert_router.py, backend/alerts/alert_severity.py, backend/alerts/discord_notifier.py, backend/alerts/email_digest.py, backend/alerts/routing_config.yaml]
  test_scope: inline
  destructive_op_classes: [state-append]
  destructive_op_scope: project
  external_side_effect: none
  external_payload_class: operational
  execution_mode: unit-testable
  dependency_artifacts: [docs/sop/alert-dedup-spec.md]
  mutex_with: []
  interface_contract:
    inputs_from_deps: [cap rules from 4a]
    outputs_for_downstream:
      - "under_cap(channel: str, severity: Severity) -> bool"
      - "record_send(channel: str) -> None"
      - "state at ~/.cache/omnisight/alert-volume-state.json (24h rolling + flock + atomic)"
ac.code:
  - {desc: "module + 2 fns importable", verify: {command: "python3 -c 'from backend.alerts.alert_volume_cap import under_cap, record_send'", expect_exit_code: 0}}
  - {desc: "default Discord cap = 50/day", verify: {command: "grep -cE 'discord.*=.*50|50.*discord' backend/alerts/alert_volume_cap.py", expect_stdout_match: "[1-9]"}}
  - {desc: "uses flock + atomic write", verify: {command: "grep -cE 'fcntl.*flock|os\\.replace' backend/alerts/alert_volume_cap.py", expect_stdout_match: "^[2-9]$|^[1-9][0-9]+$"}}
  - {desc: "tests count >=6", verify: {command: "pytest --collect-only backend/alerts/tests/test_alert_volume_cap.py 2>&1 | grep -c 'test_'", expect_stdout_match: "^[6-9]$|^[1-9][0-9]+$"}}
  - {desc: "tests pass", verify: {command: "pytest backend/alerts/tests/test_alert_volume_cap.py -v", expect_exit_code: 0, timeout_seconds: 30}}
go_live: T+1d after 4a
```

### 31.C-LibsGate — Cross-library contract integration check (UPDATED)

```yaml
delta_from_v1:
  - blockedBy adds: [4d]
  - boundaries.external_side_effect: none
  - new AC: script embeds commit_sha + git status at run time (codex Q3: "artifact JSON can pass schema without proving script ran against current code")
ac.code_additions:
  - {desc: "libs-gate.json includes git_sha of the commit script ran on", verify: {command: "python3 -c \"import json; d=json.load(open('docs/audit/AUDIT-31-phase-C-evidence/libs-gate.json')); assert 'git_sha' in d and len(d['git_sha']) == 40\"", expect_exit_code: 0}}
  - {desc: "libs-gate.json includes volume_cap_compat field", verify: {command: "python3 -c \"import json; d=json.load(open('docs/audit/AUDIT-31-phase-C-evidence/libs-gate.json')); assert 'volume_cap_compat' in d\"", expect_exit_code: 0}}
```

### 31.C-5ab — alert_router.py + tests (MERGED v1 5a+5b)

```yaml
title: "AUDIT-31.C-5ab: alert_router.py + tests"
tier: S, prefer: claude, class: subscription-claude
area_labels: [area:backend, area:tooling, area:tests]
blockedBy: [LibsGate]
context_hint:
  produces: "backend/alerts/alert_router.py + backend/alerts/tests/test_alert_router.py (MERGED — codex Q3: router behavior without tests is too easy to ship structurally 'done' but wrong)"
  consumed_by: "31.C-5c (config), 31.C-6c (wired into jira_dispatch.py)"
  inputs_expected: "All 4 libs (severity + discord + email + dedup) + 4d (volume cap) from LibsGate"
  outputs_guaranteed: |
    route_alert(event_class, module, ticket_key, message, context) flow:
    1. classify_event → Severity
    2. should_alert (dedup) → bool; if False, log suppressed + return
    3. under_cap(channel, severity) → bool; if False, fall back to email digest with [CAP-HIT] prefix
    4. By channel: discord → post_alert; email → accumulate
    5. record_send(channel) on success
    6. Emit alert-router-audit.jsonl per call
    Tests >=10: covers P0/P1/P2/P3 routing, dedup hit, 429 fallback, cap-hit fallback, unknown-event default, audit emission, error handling.
  non_goals:
    - "DO NOT modify the 5 alert libs"
    - "DO NOT add routing config — 5c owns YAML"
    - "DO NOT wire into runner — 6c owns"
boundaries:
  loc_delta_max: 500, files_touched_max: 2
  required_paths: [backend/alerts/alert_router.py, backend/alerts/tests/test_alert_router.py]
  forbidden_paths: [backend/alerts/alert_severity.py, backend/alerts/discord_notifier.py, backend/alerts/email_digest.py, backend/alerts/alert_dedup.py, backend/alerts/alert_volume_cap.py, backend/notifications.py, backend/agents/jira_dispatch.py]
  test_scope: inline
  destructive_op_classes: [state-append]
  destructive_op_scope: project
  external_side_effect: network-test   # tests mock discord/SMTP
  external_payload_class: operational
  execution_mode: unit-testable
  dependency_artifacts: [all 5 alert libs from LibsGate]
  mutex_with: [5c]
  interface_contract:
    inputs_from_deps: [classify_event, should_alert, under_cap, post_alert, accumulate, record_send]
    outputs_for_downstream:
      - "route_alert(event_class, module, ticket_key, message, context) -> RouterDecision"
      - "audit JSONL at ~/.cache/omnisight/alert-router-audit.jsonl"
      - "tests suite >=10 fns including cap-hit fallback"
ac.code:
  - {desc: "module + route_alert importable", verify: {command: "python3 -c 'from backend.alerts.alert_router import route_alert'", expect_exit_code: 0}}
  - {desc: "uses all 5 alert libs", verify: {command: "grep -cE 'from backend\\.alerts\\.(alert_severity|discord_notifier|email_digest|alert_dedup|alert_volume_cap)' backend/alerts/alert_router.py", expect_stdout_match: "^[5-9]$"}}
  - {desc: "cap-hit fallback path present", verify: {command: "grep -cE '\\[CAP-HIT\\]|under_cap.*False' backend/alerts/alert_router.py", expect_stdout_match: "[1-9]"}}
  - {desc: "tests count >=10", verify: {command: "pytest --collect-only backend/alerts/tests/test_alert_router.py 2>&1 | grep -c 'test_'", expect_stdout_match: "^(1[0-9]|[2-9][0-9])$|^[1-9][0-9]{2}$"}}
  - {desc: "tests pass", verify: {command: "pytest backend/alerts/tests/test_alert_router.py -v", expect_exit_code: 0, timeout_seconds: 60}}
go_live: T+1d after LibsGate
```

### 31.C-5c — Routing config YAML (UPDATED blockedBy)

```yaml
delta_from_v1:
  - blockedBy: [1a, 5ab] (was [1a, 5a])
  - boundaries.external_side_effect: none
  - mutex_with: [5ab]
```

### 31.C-6a — Feature flag (unchanged structure)

```yaml
delta_from_v1:
  - blockedBy: [5ab] (was [5a])
  - boundaries.external_side_effect: none
  - mutex_with: [6b, 6c, 6d, 7a, 7b]
```

### 31.C-6b — Preserve legacy notify_operator path (HARDENED verify)

```yaml
delta_from_v1:
  - new AC: behavior smoke (NOT just grep) — synthetic test that with ALERTS_ENABLED=0, calling notify_operator() produces the SAME output as pre-31.C
  - boundaries.external_side_effect: none
  - boundaries.external_payload_class: operational
ac.code_additions:
  - {desc: "behavior smoke: ALERTS_ENABLED=0 path produces identical output to pre-31.C reference capture", verify: {command: "pytest backend/agents/tests/test_notify_operator_flag0_smoke.py -v", expect_exit_code: 0, timeout_seconds: 30}}
```

### 31.C-LegacyNotifySmoke — (unchanged; already operator-rehearsal)

```yaml
delta_from_v1:
  - boundaries.external_side_effect: none (smoke uses synthetic event; mocked Discord)
  - boundaries.external_payload_class: synthetic
```

### 31.C-6c — Wire alert_router into jira_dispatch.py:notify_operator (HARDENED behavior smoke)

```yaml
delta_from_v1:
  - boundaries.external_side_effect: network-test
  - boundaries.external_payload_class: operational
  - new AC: behavior smoke that monkeypatches route_alert to raise; asserts legacy path STILL runs (fallback works)
ac.code_additions:
  - {desc: "behavior smoke: route_alert raises → legacy fallback prints", verify: {command: "pytest backend/agents/tests/test_notify_operator_fallback_smoke.py::test_route_alert_raises_falls_back -v", expect_exit_code: 0, timeout_seconds: 30}}
```

### 31.C-6d — Wire alert to stuck-runner detect (CROSS-PHASE EXPLICIT)

```yaml
delta_from_v1:
  - blockedBy: [6c, 31.B-6cd-external]   (was [6c]; cross-phase explicit)
  - new AC: preflight test -f backend/runner/coordinator_heartbeat.py PASS (proves 31.B-6cd produced the file)
  - boundaries.external_side_effect: network-test
  - boundaries.external_payload_class: operational
  - mutex_with: [external/31.B-6cd]   # documentary; coordinator enforces at filing
ac.code_additions:
  - {desc: "preflight: coordinator_heartbeat.py exists (31.B-6cd output)", verify: {command: "test -f backend/runner/coordinator_heartbeat.py && grep -c 'detect_stuck_runners' backend/runner/coordinator_heartbeat.py", expect_stdout_match: "[1-9]"}}
  - {desc: "behavior smoke: stuck-runner detection triggers route_alert (mocked)", verify: {command: "pytest backend/runner/tests/test_heartbeat_alerts_smoke.py -v", expect_exit_code: 0, timeout_seconds: 30}}
```

### 31.C-7a — Wire alert into Gerrit push failures (HARDENED)

```yaml
delta_from_v1:
  - boundaries.external_side_effect: network-test
  - mutex_with: [6a, 6b, 6c, 6d, 7b]
  - new AC: behavior smoke — synthetic Gerrit push fail → route_alert called with `gerrit_push_fail` event
ac.code_additions:
  - {desc: "behavior smoke", verify: {command: "pytest backend/agents/tests/test_gerrit_push_alert_smoke.py -v", expect_exit_code: 0, timeout_seconds: 30}}
```

### 31.C-7b — Wire alert into runner exit anomalies (MUTEX FIX + HARDENED)

```yaml
delta_from_v1:
  - mutex_with: [6a]   (was []; codex Q3: both touch auto-runner-jira.py)
  - boundaries.external_side_effect: network-test
  - new AC: behavior smoke — synthetic uncaught exception → route_alert called with `runner_exit_anomaly` + exception re-raised
ac.code_additions:
  - {desc: "behavior smoke", verify: {command: "pytest backend/tests/test_runner_exit_alert_smoke.py -v", expect_exit_code: 0, timeout_seconds: 30}}
```

### 31.C-8a — SMTP client config (HARDENED: inline tests + secret log-capture)

```yaml
delta_from_v1:
  - title: "SMTP client config + tests" (now bundles tests inline)
  - required_paths: [backend/alerts/smtp_sender.py, backend/alerts/tests/test_smtp_sender.py]  (was just smtp_sender.py)
  - boundaries.files_touched_max: 2 (was 1)
  - boundaries.external_side_effect: email (CAUTION: function actually opens SMTP socket — tests MUST mock)
  - boundaries.external_payload_class: secret-adjacent
  - boundaries.test_scope: inline (was defer-to-integration)
  - new ACs: log-capture secret-safety test; SMTP failure handling test
ac.code_additions:
  - {desc: "test file exists; pytest collects >=5 tests", verify: {command: "pytest --collect-only backend/alerts/tests/test_smtp_sender.py 2>&1 | grep -c 'test_'", expect_stdout_match: "^[5-9]$|^[1-9][0-9]+$"}}
  - {desc: "log-capture secret-safety test: capture stderr/stdout during error; assert smtp-app-password literal NOT present", verify: {command: "grep -cE 'def test_.*secret.*not.*log|def test_.*log.*no.*password|caplog.*not in' backend/alerts/tests/test_smtp_sender.py", expect_stdout_match: "[1-9]"}}
  - {desc: "tests pass", verify: {command: "pytest backend/alerts/tests/test_smtp_sender.py -v", expect_exit_code: 0, timeout_seconds: 60}}
  - {desc: "tests use mock SMTP (no real socket)", verify: {command: "grep -cE 'mock.*smtplib|Mock.*SMTP|aiosmtpd' backend/alerts/tests/test_smtp_sender.py", expect_stdout_match: "[1-9]"}}
```

### 31.C-8b — Digest user systemd timer (**P0 verify BUG FIXED**)

```yaml
delta_from_v1:
  - boundaries.external_side_effect: none (timer is just file)
  - boundaries.external_payload_class: operational
  - FIX: AC verify "USER systemd" was bugged in v1 (accepted multi-user.target OR default.target — equivalent to allowing forbidden system level)
  - v2: split into 2 verifies: (1) MUST have default.target, (2) MUST NOT have multi-user.target
ac.code_replacements:
  - {desc: "REQUIRES default.target", verify: {command: "grep -cE 'WantedBy=default.target' deploy/systemd-user/omnisight-email-digest.timer", expect_stdout_match: "[1-9]"}}
  - {desc: "FORBIDS multi-user.target (system-level)", verify: {command: "grep -cE 'WantedBy=multi-user.target' deploy/systemd-user/omnisight-email-digest.timer", expect_stdout_match: "^0$"}}
```

### 31.C-AlertSmoke — Send test P0 alert (external_side_effect updated)

```yaml
delta_from_v1:
  - boundaries.external_side_effect: discord
  - boundaries.external_payload_class: synthetic
  - already has operator-rehearsal class — meets §3.4 rule ✓
  - new AC: uses discord-webhook-test.url if present, else fallback to webhook-p1.url with [TEST] prefix (per v1 §7 Q8)
ac.code_additions:
  - {desc: "smoke prefers discord-webhook-test.url if present", verify: {command: "grep -cE 'discord-webhook-test.url|webhook-test\\.url' scripts/sprint-s12/phase31c-alert-smoke.sh", expect_stdout_match: "[1-9]"}}
```

### 31.C-DigestSmoke — Trigger test digest (external_side_effect updated)

```yaml
delta_from_v1:
  - boundaries.external_side_effect: email
  - boundaries.external_payload_class: synthetic
  - already has operator-rehearsal class — meets §3.4 rule ✓
```

### 31.C-9ab — ack_workflow.py + tests (MERGED v1 9a+9b; STRENGTHENED non-goals)

```yaml
title: "AUDIT-31.C-9ab: ack_workflow.py + tests"
tier: S, prefer: claude, class: subscription-claude
area_labels: [area:backend, area:tooling, area:tests]
blockedBy: [5c]
context_hint:
  produces: "backend/alerts/ack_workflow.py + backend/alerts/tests/test_ack_workflow.py (MERGED — codex Q3: ack state schema persistence-sensitive)"
  consumed_by: "31.C-CallSiteInventoryGate, 31.C-10-Doc"
  inputs_expected: "routing_config from 5c"
  outputs_guaranteed: |
    Module:
    - ack_via_jira_comment(ticket_key, alert_id) → bool
    - ack_via_discord_reaction(message_id, reaction) → raises NotImplementedError with "Phase 31.K — local stub only; no socket, no webhook endpoint, no Discord API client import"
    - list_unacked(severity_filter, age_seconds) → List[AlertId]
    State: ~/.cache/omnisight/alert-ack-state.json (flock + atomic).
    Tests >=8: jira-ack-success / unknown-id / stale / list-empty / list-finds / Discord-reaction-raises-with-31.K-hint / Discord-stub-no-network-imports / atomic-state.
  non_goals:
    - "DO NOT implement Discord reaction listener — Phase 31.K"
    - "DO NOT import any socket / websocket / http server library"
    - "DO NOT import discord.py / discord-related API SDK"
    - "DO NOT modify alert_router — ack is downstream"
boundaries:
  loc_delta_max: 400, files_touched_max: 2
  required_paths: [backend/alerts/ack_workflow.py, backend/alerts/tests/test_ack_workflow.py]
  forbidden_paths: [backend/alerts/alert_router.py, backend/alerts/discord_notifier.py, backend/alerts/email_digest.py, backend/alerts/alert_severity.py, backend/alerts/alert_dedup.py, backend/alerts/alert_volume_cap.py]
  test_scope: inline
  destructive_op_classes: [state-append]
  destructive_op_scope: project
  external_side_effect: none
  external_payload_class: operational
  execution_mode: unit-testable
  dependency_artifacts: [backend/alerts/routing_config.yaml]
  mutex_with: []
  interface_contract:
    inputs_from_deps: [requires_ack rules from routing_config]
    outputs_for_downstream:
      - "ack_via_jira_comment / ack_via_discord_reaction (stub) / list_unacked"
      - "ack state at ~/.cache/omnisight/alert-ack-state.json"
ac.code:
  - {desc: "module + 3 fns importable", verify: {command: "python3 -c 'from backend.alerts.ack_workflow import ack_via_jira_comment, ack_via_discord_reaction, list_unacked'", expect_exit_code: 0}}
  - {desc: "Discord reaction stub raises NotImplementedError with 31.K hint", verify: {command: "python3 -c 'from backend.alerts.ack_workflow import ack_via_discord_reaction;\\ntry:\\n    ack_via_discord_reaction(1, 2)\\nexcept NotImplementedError as e:\\n    assert \"31.K\" in str(e)' || python3 -c 'from backend.alerts.ack_workflow import ack_via_discord_reaction; ack_via_discord_reaction(1, 2)' 2>&1 | grep -c '31.K'", expect_stdout_match: "[1-9]"}}
  - {desc: "NO socket / websocket / discord SDK imports", verify: {command: "grep -cE '^import (socket|websocket|discord)|^from (socket|websocket|discord)' backend/alerts/ack_workflow.py", expect_stdout_match: "^0$"}}
  - {desc: "tests count >=8", verify: {command: "pytest --collect-only backend/alerts/tests/test_ack_workflow.py 2>&1 | grep -c 'test_'", expect_stdout_match: "^[8-9]$|^[1-9][0-9]+$"}}
  - {desc: "tests pass", verify: {command: "pytest backend/alerts/tests/test_ack_workflow.py -v", expect_exit_code: 0, timeout_seconds: 30}}
go_live: T+1d after 5c
```

### 31.C-CallSiteInventoryGate — Audit existing notification call sites (NEW PER A 案)

```yaml
title: "AUDIT-31.C-CallSiteInventoryGate: Notification call-site inventory audit"
tier: S, prefer: claude, class: subscription-claude
area_labels: [area:tests, area:docs, area:backend]
blockedBy: [AlertSmoke, DigestSmoke, 9ab]
context_hint:
  produces: "scripts/sprint-s12/phase31c-call-site-inventory.py + docs/audit/AUDIT-31-phase-C-evidence/call-site-inventory.json + docs/audit/AUDIT-31-phase-C-evidence/call-site-inventory.md"
  consumed_by: "Gates entry to Integration (codex Q4: 'spec can pass synthetic smokes while real incidents bypass alerts')"
  inputs_expected: "All wiring done (6c/6d/7a/7b); both smokes PASS"
  outputs_guaranteed: |
    Script enumerates ALL call sites of:
    - jira_dispatch.notify_operator (L386)
    - backend/notifications.py:* (all 1325 LOC public surface)
    - backend/chatops/discord.py:* (all 162 LOC public surface)
    - Any direct `print(` in runner exit paths (auto-runner-jira.py + jira_dispatch.py)
    - Direct `webhook` / `smtp` string in any backend/* module
    Each call site classified by reviewer (claude):
    - `legacy-preserved`: intentionally on old path (flag=0 fallback)
    - `wrapped-by-alert-router`: ticket 6c/6d/7a/7b wired this site through new router
    - `out-of-scope`: explicitly NOT a 31.C concern (e.g., manual operator-only utility scripts)
    Each classification justified in inventory.md.
    JSON output: {site, file, line, classification, justification}.
    GATE PASS: every site is one of 3 valid classifications. No "unknown" sites.
  non_goals:
    - "DO NOT modify any code — audit-only"
    - "DO NOT re-classify by automated grep alone; claude reads each site"
boundaries:
  loc_delta_max: 300, files_touched_max: 3
  required_paths: [scripts/sprint-s12/phase31c-call-site-inventory.py, docs/audit/AUDIT-31-phase-C-evidence/call-site-inventory.json, docs/audit/AUDIT-31-phase-C-evidence/call-site-inventory.md]
  forbidden_paths: [backend/alerts/, auto-runner-jira.py, backend/agents/jira_dispatch.py, backend/notifications.py, backend/chatops/discord.py]
  test_scope: inline
  destructive_op_classes: [state-append]
  destructive_op_scope: project
  external_side_effect: none
  external_payload_class: operational
  execution_mode: unit-testable
  dependency_artifacts: [post-wiring jira_dispatch.py + auto-runner-jira.py]
  mutex_with: []
  interface_contract:
    inputs_from_deps: [post-wiring system]
    outputs_for_downstream:
      - "call-site-inventory.json with every site classified"
      - "call-site-inventory.md human-readable summary"
      - "GATE PASS = 0 unknown sites"
ac.code:
  - {desc: "script exists", verify: {command: "test -x scripts/sprint-s12/phase31c-call-site-inventory.py", expect_exit_code: 0}}
  - {desc: "inventory JSON has every site classified", verify: {command: "python3 -c \"import json; d=json.load(open('docs/audit/AUDIT-31-phase-C-evidence/call-site-inventory.json')); assert all(s['classification'] in ('legacy-preserved','wrapped-by-alert-router','out-of-scope') for s in d['sites'])\"", expect_exit_code: 0}}
  - {desc: "ZERO unclassified sites", verify: {command: "python3 -c \"import json; d=json.load(open('docs/audit/AUDIT-31-phase-C-evidence/call-site-inventory.json')); unknown = [s for s in d['sites'] if s['classification'] not in ('legacy-preserved','wrapped-by-alert-router','out-of-scope')]; assert len(unknown) == 0\"", expect_exit_code: 0}}
  - {desc: "inventory.md has justification per site", verify: {command: "wc -l docs/audit/AUDIT-31-phase-C-evidence/call-site-inventory.md | awk '{print $1}'", expect_stdout_match: "^[1-9][0-9]+$|^[1-9][0-9]{2}$"}}
go_live: T+2d after AlertSmoke + DigestSmoke + 9ab
```

### 31.C-10-Doc — Operator notifier runbook

```yaml
delta_from_v1:
  - blockedBy: [CallSiteInventoryGate]  (was [AlertSmoke, DigestSmoke, 9b])
  - boundaries.external_side_effect: none
  - runbook now also documents volume-cap behavior + cap-hit recovery procedure
```

### 31.C-Integration-Doc — (unchanged structure; references CallSiteInventoryGate)

```yaml
delta_from_v1:
  - boundaries.external_side_effect: none
  - runbook references CallSiteInventoryGate evidence
```

### 31.C-Integration — E2E alert lifecycle test (external_side_effect updated)

```yaml
delta_from_v1:
  - boundaries.external_side_effect: network-production
  - boundaries.external_payload_class: operational
  - already has operator-rehearsal — meets §3.4 rule ✓
  - new AC: integration.json includes cap_hit_seen field (proves volume cap exercised at least once)
ac.code_additions:
  - {desc: "integration.json: cap_hit_seen field present", verify: {command: "python3 -c \"import json; d=json.load(open('docs/audit/AUDIT-31-phase-C-evidence/integration.json')); assert 'cap_hit_seen' in d\"", expect_exit_code: 0}}
```

---

## §6. Filing batch order (DAG v2)

```
L0: 1a, 2a, 3a, 4a (4 specs)
L1: 1bc, 2bc, 3bc, 4bc, 4d (5 libs — NEW 4d added)
L2: LibsGate (1)
L3: 5ab, 8a (2 — merged router + tests; SMTP+tests)
L4: 5c, 6a, 8b (3)
L5: 6b, 9ab (2 — merged ack+tests)
L6: LegacyNotifySmoke (1)
L7: 6c (1)
L8: 6d, 7a, 7b (3 parallel)
L9: AlertSmoke, DigestSmoke (2 parallel gates)
L10: CallSiteInventoryGate (1 — NEW gate)
L11: 10-Doc (1)
L12: Integration-Doc (1)
L13: Integration (1, tier:L)
```

14 layers, 28 tickets.

---

## §7. Operator answers (all locked 2026-05-13)

| # | Question | Locked answer |
|---|---|---|
| 1 | Alert events also as JIRA comments | **YES** — P0/P1 auto-post comment on originating JIRA ticket; P2/P3 stay digest-only |
| 2 | Webhook URL rotation strategy | **restart-only for v1**; SIGHUP reload deferred to Phase 31.K |
| 3 | Email digest frequency | **daily-only** (08:00 local; hourly option deferred to Phase 31.K) |
| 4 | Silencing rules in routing_config.yaml — who edits | **operator-only via Gerrit +2 from non-AI reviewer** (runner refuses edits to this file even with operator-prepare-only) |
| 5 | META vs feature severity | **same rules** (META failures naturally cascade to downstream events) |
| 6 | Recipients format | **1+ individual addresses, one per line in `~/.config/omnisight/email-recipients.txt`**; distribution lists handled externally |
| 7 | ~~Volume cap~~ | **CLOSED** → real ticket 31.C-4d (default 50/day Discord, 200/day Email; configurable per-channel) |
| 8 | Integration test channel | **discord-webhook-test.url if present, else [TEST] prefix on -p1** |

**Impact on per-child specs**:
- Answer 1: 5c routing_config.yaml entries for P0/P1 include `also_post_jira_comment: true`
- Answer 2: 6a feature flag init reads URLs at module load; no SIGHUP handler
- Answer 4: 5c required_paths includes `routing_config.yaml`; OP-1042 hook adds rule that editing routing_config.yaml requires operator-* class label
- Answer 6: 3a spec documents single-address-per-line format; 8a smtp_sender reads recipients per documented format

---

## §8. Submission flow

1. This spec at `docs/sprint-s12/phase-31c-ticket-spec.md` (v2)
2. Operator review of §7 remaining open questions (1-6 + 8); lock answers
3. Commit to feature branch `feature/sprint-s12-phase-31c-spec-v2`
4. Push to Gerrit refs/for/develop
5. Operator + 1 reviewer +2
6. Submit (merge to develop)
7. Filing script files 28 children in DAG batch order
8. Post-filing spot-check first 3 filed tickets

---

**End of Phase 31.C ticket spec v2 (28-ticket atomic + boundaries + external_side_effect dimension + volume cap + call-site inventory gate). Awaiting operator §7 answers (1-6 + 8).**
