---
id: SPRINT-S12-PHASE-31I-SPEC
version: v2 (post codex review)
title: Sprint S12 Phase 31.I — Prod Backend Unhealthy Container Fix · Ticket Spec
scope: audit + healthcheck baseline + Pilot fix + cascade-safe per-container fixes + per-fix evidence + N=0 branch + retro material
status: Draft (2026-05-13)
related:
  - ADR-0023 §1
  - 31.H-StabilityCheckpoint (cross-phase blocker)
  - 31.F-StabilityCheckpoint (cosign verify on canary fix deploys)
  - Phase 31.A-31.H v2 (schemas reused)
  - 31.I v1 (pre codex review, superseded)
  - Codex independent review (2026-05-13, /tmp/phase31i-codex-review-final.txt)
---

# Sprint S12 Phase 31.I · Prod Unhealthy Fix — Ticket Spec v2

## §0. v2 Changelog (vs v1 — driven by codex independent review)

**P0 BLOCKING fixes:**

| Item | v1 problem | v2 fix |
|---|---|---|
| **AuditReviewGate AC too weak** | didn't verify JIRA key per decision / boundary inheritance / 31.F-31.H blockers | v2: 5 NEW ACs (every decision → JIRA key; every dynamic ticket inherits 7-Template boundaries; every dynamic ticket carries `class:operator-window` + `external_side_effect: network-production`; every dynamic ticket has 31.F + 31.H blockers; dynamic-cap N≤8 enforced) |
| **Pre-filed examples with TBD = dangerous** | could be picked up early / pass schema while operationally meaningless | v2: examples **blockedBy AuditReviewGate** + **no-TBD AC** (description must not contain literal `TBD` / `<CONTAINER_NAME>` / `XXX`) |
| **Ticket count inconsistency** | prompt vs spec table vs text disagreed | v2: §0 + §4 statement uniform — **18 fixed + 2 examples + 1 Pilot + Integration = 22 fixed; plus N dynamic (cap 8)** |
| **Fix cascade ignored** | no dependency-aware ordering | v2: AuditReviewGate output adds `depends_on / blast_radius / fix_order` per container; dynamic 7-* tickets touching dependent services are blockedBy `depends_on` upstream tickets (filing-script enforces) |
| **First canary on broken prod risky** | dynamic 7-* could parallel-fire on first run | v2: **NEW 31.I-7-Pilot ticket** — lowest-blast container; manual hold + explicit success-criteria; ALL other dynamic 7-* blockedBy Pilot |
| **Retro material too late** | 9-Doc compiles at end; lessons lost during execution | v2: each dynamic 7-* (and 7-Example/Pilot) emits `docs/audit/AUDIT-31-phase-I-evidence/7-<container>-fix.json` with root_cause / attempted_fix / rollback_result / lesson — 9-Doc compiles these (NOT reconstructs) |
| **N=0 case undefined** | what happens to examples if audit shows zero unhealthy | v2: AuditReviewGate explicit N=0 branch — pre-filed examples + Pilot CANCELLED (closed as N/A); phase continues to baseline + verification + retro note "N=0 at audit" |
| **Hypothesis no validation** | root_cause_hypothesis unproven before fix | v2: dynamic 7-* AC requires 4-field hypothesis structure (symptom + evidence_refs + expected_healthcheck_change + falsification_observation); if fix doesn't fix → ticket stops + file follow-up (NOT iterate blindly) |

**P1 STRUCTURAL fixes:**

| Item | v1 problem | v2 fix |
|---|---|---|
| 6b blockedBy parallel with 6a (mutex_with only) | mutex advisory; /healthz satisfies compose healthchecks | v2: **6b.blockedBy: [6a]** strict sequential |
| 8b title mismatch | overview "Verification tests" / detailed "Daily health-verify systemd timer" | v2: standardized — **"Daily health-verify systemd timer + alert wiring"** |
| 6b required_paths `[backend/]` too broad | "narrow at filing" was soft note | v2: filing-script HARD REJECTS 6b if required_paths still `[backend/]`; operator + claude collaborate on specific file list pre-filing |

**P2 SCHEMA additions:**

```yaml
external_side_effect: none | network-localhost | network-test | network-production | email | discord | jira-write   # NEW
```

`jira-write` semantics:
- Ticket files / mutates JIRA tickets (other than ticket's own state changes)
- Auto-allowed (NOT operator-* required) since JIRA writes are part of governance, not prod state
- Applies to AuditReviewGate (files dynamic 7-* tickets)

7-Template filing-script enforcement (codex Q5):
- Locally render full ticket payload pre-POST
- Reject if: missing boundaries / context_hint / blockers / operator-window class / rollback AC / contains literal "TBD"/"XXX"/"<...>"
- Hard-reject if dynamic-cap N>8 (operator must split to new META)

§6/§8 cross-phase preflight rule (applies to ALL dynamic 7-*, not just 3bc):
- Every dynamic 7-* filing script validates 31.F-StabilityCheckpoint Closed + 31.H-StabilityCheckpoint Closed before file
- Evidence written to dynamic ticket description (filing artifact)

**Ticket count**: v1 21 → v2 **22** fixed (+ Pilot) plus N≤8 dynamic plus 2 example placeholders.

---

## §1, §2 — unchanged structure

---

## §3. Schemas (extended)

Reuses all prior + adds:
- `external_side_effect: jira-write` (above)

---

## §4. Children — overview table (22 fixed + 2 examples + N dynamic v2)

| Layer | # | ID | Title | Tier | Prefer | Class | blockedBy |
|---|---|---|---|---|---|---|---|
| L0 | 1-4 | 31.I-1a..4a | specs (3a adds hypothesis + falsification fields; 4a unchanged) | S | claude | claude | — |
| L1 | 5 | 31.I-1bc | container-health-auditor + tests | S | codex | codex | 1a |
| L1 | 6 | 31.I-2bc | healthcheck-validator + tests | S | codex | codex | 2a |
| L1 | 7 | 31.I-3bc | recovery-orchestrator + tests (cross-phase 31.H) | S | codex | codex | 3a, 31.H-StabilityCheckpoint-external |
| L2-Gate | 8 | 31.I-LibsGate | unchanged | S | claude | claude | 1bc, 2bc, 3bc, 4a |
| L3 | 9 | 31.I-5a | operator audit (unchanged) | S | claude | claude + operator-window | LibsGate |
| L4-Gate | 10 | 31.I-AuditReviewGate | **HARDENED**: + JIRA key per decision + boundary inheritance verify + N≤8 cap + depends_on/blast_radius/fix_order fields + N=0 branch | S | claude | claude + operator-window | 5a |
| L5 | 11 | 31.I-6a | healthcheck baseline → compose (unchanged) | S | codex | codex + operator-prepare-only | AuditReviewGate, 2bc |
| L5 | 12 | 31.I-6b | /healthz endpoints (**now blockedBy 6a**; strict required_paths narrowing) | S | codex | codex + operator-prepare-only | **6a**, 2bc |
| L6 | 13 | 31.I-7-Template | + filing-script validation pre-POST + no-TBD enforcement | S | claude | claude | AuditReviewGate, 3a |
| L7 | 14 | 31.I-7-Pilot | **NEW**: lowest-blast first canary; manual hold; ALL other dynamic 7-* blockedBy this | S | claude | claude + operator-window | 7-Template, 6a, 6b, 31.F-StabilityCheckpoint-external |
| L8 | 15 | 31.I-7-Example-1 | + no-TBD AC + blockedBy AuditReviewGate AND Pilot | S | claude/codex | depends + operator-window | AuditReviewGate, **Pilot**, 7-Template |
| L8 | 16 | 31.I-7-Example-2 | + no-TBD AC + blockedBy AuditReviewGate AND Pilot | S | claude/codex | depends + operator-window | AuditReviewGate, **Pilot**, 7-Template |
| L8 | N | 31.I-7-{Container}* | **DYNAMIC** (cap N≤8): filed post-AuditReviewGate; each blockedBy Pilot + depends_on-upstream-fixes | S | claude/codex | depends + operator-window | Pilot, 7-Template, depends_on |
| L9 | 17 | 31.I-8a | verification framework (unchanged) | S | codex | codex | 7-Template |
| L9 | 18 | 31.I-8b | **renamed**: Daily health-verify systemd timer + alert wiring | S | codex | codex + operator-prepare-only | 8a |
| L10 | 19 | 31.I-9-Doc | **NOW compiles from per-fix JSONs** (not reconstructs from scratch) | S | claude | claude | 8b, ALL fix tickets closed |
| L11 | 20 | 31.I-CallSiteInventoryGate | unchanged | S | claude | claude | 9-Doc |
| L12 | 21 | 31.I-StabilityCheckpoint | unchanged | S | claude | claude + operator-window | CallSiteInventoryGate |
| L13 | 22 | 31.I-Integration | unchanged tier:L | L | claude | claude + operator-window | StabilityCheckpoint |

**Total fixed**: 22 (incl. Pilot, examples, Integration). Plus 0-8 dynamic per audit results.

**Routing tally** (fixed only): codex 6, claude 16.

**Critical path**: 1a → 1bc → LibsGate → 5a → AuditReviewGate → 6a → 6b → 7-Template → **Pilot** → 7-Example-1/2 + dynamic 7-* → 8a → 8b → 9-Doc → CallSiteInventoryGate → StabilityCheckpoint → Integration. ~15 hops + Pilot adds 1.

---

## §5. Per-child specs (v2 deltas)

### 31.I-1a, 2a, 4a, 1bc, 2bc, LibsGate, 5a, 6a — unchanged

### 31.I-3a — adds hypothesis + falsification field requirements

```yaml
delta_from_v1:
  - outputs_guaranteed: ADD "Template per-container fix REQUIRES 4-field hypothesis block: {symptom: str, evidence_refs: [str], expected_healthcheck_change: str, falsification_observation: str}"
  - non_goals strengthened: "DO NOT skip hypothesis validation; if post-fix observation matches falsification → ticket STOPS + file follow-up (no blind iteration)"
ac.code_additions:
  - {desc: "hypothesis 4-field block documented", verify: {command: "grep -ciE 'symptom|evidence_refs|expected_healthcheck_change|falsification_observation' docs/sop/per-container-fix-template.md", expect_stdout_match: "^[4-9]$|^[1-9][0-9]+$"}}
```

### 31.I-3bc — unchanged (already cross-phase 31.H)

### 31.I-AuditReviewGate — HARDENED (codex P0)

```yaml
delta_from_v1:
  - context_hint.outputs_guaranteed: REWRITTEN — extended decision schema:
    Per inventory entry: {decision, jira_key (if filed), rationale, depends_on: [container_names], blast_radius: low|medium|high, fix_order: int}
    AuditReviewGate enforces:
    - Every file-fix decision produced a JIRA key (queryable)
    - Every filed dynamic 7-* ticket carries: class:operator-window, external_side_effect: network-production, blockers [Pilot, 31.F-StabilityCheckpoint, 31.H-StabilityCheckpoint, depends_on-upstream-keys]
    - Cap: file-fix count N ≤ 8 (if >8 → escalate / split to new META)
    - N=0 branch: explicit "N=0" decision → Pilot + Example-1/2 marked Cancelled (operator records); phase continues to 8a/8b/9-Doc/Integration without dynamic
    - depends_on serialization: dynamic tickets touching upstream container blockedBy their upstream fix ticket
  - boundaries.external_side_effect: **jira-write** (codex Q5)
ac.code_additions:
  - {desc: "every file-fix decision has JIRA key", verify: {command: "python3 -c \"import json; fl=json.load(open('docs/audit/AUDIT-31-phase-I-evidence/fix-list.json')); filing = [d for d in fl['decisions'] if d.get('decision') == 'file-fix-ticket']; assert all(d.get('jira_key') for d in filing)\"", expect_exit_code: 0}, run_as: operator}
  - {desc: "N <= 8 (cap enforced)", verify: {command: "python3 -c \"import json; fl=json.load(open('docs/audit/AUDIT-31-phase-I-evidence/fix-list.json')); filing = [d for d in fl['decisions'] if d.get('decision') == 'file-fix-ticket']; assert len(filing) <= 8\"", expect_exit_code: 0}, run_as: operator}
  - {desc: "depends_on / blast_radius / fix_order present per entry", verify: {command: "python3 -c \"import json; fl=json.load(open('docs/audit/AUDIT-31-phase-I-evidence/fix-list.json')); assert all('depends_on' in d and 'blast_radius' in d and 'fix_order' in d for d in fl['decisions'])\"", expect_exit_code: 0}, run_as: operator}
  - {desc: "N=0 branch handled (if no file-fix decisions → Pilot/Examples marked Cancelled)", verify: {command: "python3 -c \"import json; fl=json.load(open('docs/audit/AUDIT-31-phase-I-evidence/fix-list.json')); n = sum(1 for d in fl['decisions'] if d.get('decision') == 'file-fix-ticket'); if n == 0: assert fl.get('n_zero_branch_cancelled_pilot_and_examples') == True\"", expect_exit_code: 0}, run_as: operator}
```

### 31.I-6a — + boundary note

```yaml
delta_from_v1:
  - boundaries.non_goals: ADD "project-scope BOUNDARY until merge; any actual container restart/recreate/deploy must be a SEPARATE operator-window system-scope ticket (dynamic 7-* owns)"
```

### 31.I-6b — blockedBy 6a + strict required_paths narrowing (codex P1)

```yaml
delta_from_v1:
  - blockedBy: [**6a**, 2bc]   (was [AuditReviewGate, 2bc])
  - required_paths: filing-script HARD-REJECTS if `backend/` (broad); operator + claude must specify narrow list at filing
  - boundaries.non_goals: + boundary note same as 6a
  - mutex_with removed (no longer needed since sequential)
ac.code_additions:
  - {desc: "required_paths NOT just `[backend/]` (narrowed list required)", verify: {command: "python3 -c \"import yaml; t=yaml.safe_load(open('jira/AUDIT-31.I-6b.yaml')); paths=t['boundaries']['required_paths']; assert paths != ['backend/']; assert all('backend/' in p and p != 'backend/' for p in paths)\"", expect_exit_code: 0}, run_as: operator}
```

### 31.I-7-Template — + filing-script validation (codex Q5)

```yaml
delta_from_v1:
  - context_hint.outputs_guaranteed: ADD "Filing script `file-fix-ticket.py` does LOCAL VALIDATION before POST:
    - Reject if `<TBD>` / `<CONTAINER_NAME>` / `XXX` literal anywhere in title/description/AC
    - Reject if missing: boundaries / context_hint / mutex_with / decision_points / 4-field hypothesis
    - Reject if missing blockers: Pilot, 31.F-StabilityCheckpoint, 31.H-StabilityCheckpoint, depends_on upstream
    - Reject if missing rollback AC
    - Reject if class not operator-window
    - Reject if cumulative dynamic-fix count would exceed N=8"
ac.code_additions:
  - {desc: "filing script has all 7 validation checks", verify: {command: "grep -cE 'no-TBD|TBD|missing.*boundaries|missing.*blockers|missing.*rollback|operator-window|N.*<=.*8|hypothesis' scripts/setup-prod-env/file-fix-ticket.py", expect_stdout_match: "^[6-9]$|^[1-9][0-9]+$"}}
  - {desc: "filing script rejects synthetic bad ticket fixture", verify: {command: "echo '{\"title\":\"TBD\",\"description\":\"<CONTAINER_NAME>\"}' | python3 scripts/setup-prod-env/file-fix-ticket.py --dry-run 2>&1 | grep -ci 'reject\\|error\\|invalid'", expect_stdout_match: "[1-9]"}}
```

### 31.I-7-Pilot — NEW (codex P0)

```yaml
title: "AUDIT-31.I-7-Pilot: First dynamic canary fix on lowest-blast container; manual hold + gates remaining 7-*"
tier: S, prefer: claude, class: subscription-claude + operator-window
area_labels: [area:devops, area:backend, area:prod]
blockedBy: [7-Template, 6a, 6b, 31.F-StabilityCheckpoint-external, 31.H-StabilityCheckpoint-external]
context_hint:
  produces: "docs/audit/AUDIT-31-phase-I-evidence/7-pilot-fix.json + entry in operator action log"
  consumed_by: "ALL other dynamic 7-* tickets (blockedBy Pilot); 9-Doc retro material"
  inputs_expected: "AuditReviewGate locked + lowest-blast container identified in fix-list"
  outputs_guaranteed: |
    Operator-managed first real broken-prod canary:
    1. Select lowest-blast container per fix-list (blast_radius=low)
    2. Manual hold points (operator confirms before each):
       a. backup confirmed (last 31.H BackupDRDrill within 7d)
       b. previous image recorded via 31.H abstraction
       c. cosign verify on fix image (31.F adapter)
       d. one-container swap (via 31.H canary; first-cycle manual)
       e. ≥4 consecutive healthy probes over 2 min
       f. dependency smoke check (consumers of this container still responding)
       g. rollback dry-run executed + verified
    3. Emit pilot-fix.json: {container_name, blast_radius_proven_low, manual_hold_passes_count, rollback_dry_run_ok, total_minutes, lesson_for_next_dynamic_fixes}
    4. Operator marks "ready-for-parallel-dynamic" flag → unblocks 7-Example/dynamic 7-*
  non_goals:
    - "DO NOT pick medium/high blast container as Pilot"
    - "DO NOT skip manual hold points (this is FIRST real broken-prod canary)"
    - "DO NOT unblock other dynamic 7-* until pilot-fix.json shows all manual_hold_passes"
boundaries:
  loc_delta_max: 100, files_touched_max: 2
  required_paths: [docs/audit/AUDIT-31-phase-I-evidence/7-pilot-fix.json, docs/operations/operator-action-log.md]
  forbidden_paths: [scripts/setup-prod-env/, deploy/, Dockerfile, docker-compose.yml]
  test_scope: defer-to-integration
  destructive_op_classes: []
  destructive_op_scope: system   # real prod container swap
  external_side_effect: network-production
  external_payload_class: operational
  execution_mode: operator-rehearsal
  dependency_artifacts: [scripts/setup-prod-env/recovery-orchestrator.py, scripts/setup-prod-env/canary-runner.py (31.H), backend/security/cosign_verify_adapter.py (31.F)]
  mutex_with: []
  interface_contract:
    inputs_from_deps: [fix-list locked + recovery infra ready]
    outputs_for_downstream:
      - "pilot-fix.json with ready-for-parallel-dynamic flag"
      - "all 7 manual_hold checkpoints PASSED"
ac.code:
  - {desc: "pilot-fix.json committed", verify: {command: "test -f docs/audit/AUDIT-31-phase-I-evidence/7-pilot-fix.json", expect_exit_code: 0}}
ac.deploy:
  - {desc: "all 7 manual_hold checkpoints PASSED", verify: {command: "jq -r .manual_hold_passes_count docs/audit/AUDIT-31-phase-I-evidence/7-pilot-fix.json", expect_stdout_match: "^[7-9]$|^[1-9][0-9]+$"}, run_as: operator}
  - {desc: "blast_radius_proven_low", verify: {command: "jq -r .blast_radius_proven_low docs/audit/AUDIT-31-phase-I-evidence/7-pilot-fix.json", expect_stdout_match: "^true$"}, run_as: operator}
  - {desc: "rollback dry-run OK", verify: {command: "jq -r .rollback_dry_run_ok docs/audit/AUDIT-31-phase-I-evidence/7-pilot-fix.json", expect_stdout_match: "^true$"}, run_as: operator}
  - {desc: "ready-for-parallel-dynamic flag", verify: {command: "jq -r .ready_for_parallel_dynamic docs/audit/AUDIT-31-phase-I-evidence/7-pilot-fix.json", expect_stdout_match: "^true$"}, run_as: operator}
go_live: T+2d after 7-Template + 6a + 6b
```

### 31.I-7-Example-1/2 — + no-TBD AC + blockedBy Pilot

```yaml
shared_delta:
  - blockedBy: [AuditReviewGate, **Pilot**, 7-Template]   (was [7-Template, 6a])
  - new AC: no-TBD literal in description/AC; container_name field filled at AuditReviewGate (NOT pre-spec)
  - AC: per-fix evidence emitted to docs/audit/AUDIT-31-phase-I-evidence/7-{name}-fix.json with 4-field hypothesis + root_cause + attempted_fix + rollback_result + lesson
  - N=0 case: ticket Cancelled (per AuditReviewGate v2 branch)
ac.code_additions:
  - {desc: "no `<TBD>`/`<CONTAINER_NAME>`/`XXX` literals in title or description at filing", verify: {command: "python3 -c \"import json; t=json.load(open('jira/AUDIT-31.I-7-Example-1.json')); txt = t['title'] + t['description']; assert '<TBD>' not in txt and '<CONTAINER_NAME>' not in txt and 'XXX' not in txt\"", expect_exit_code: 0}, run_as: operator}
  - {desc: "per-fix evidence emitted with 4-field hypothesis", verify: {command: "python3 -c \"import json; d=json.load(open('docs/audit/AUDIT-31-phase-I-evidence/7-example-1-fix.json')); assert all(k in d for k in ['root_cause','attempted_fix','rollback_result','lesson','hypothesis_validated'])\"", expect_exit_code: 0}, run_as: operator}
```

### 31.I-8a — unchanged

### 31.I-8b — renamed (codex P1)

```yaml
delta_from_v1:
  - title: "AUDIT-31.I-8b: Daily health-verify systemd timer + alert wiring"   (was inconsistent)
  - context_hint.outputs_guaranteed: clarify both timer install AND alert routing (no functional change; doc/title fix only)
```

### 31.I-9-Doc — compiles per-fix evidence (codex P0)

```yaml
delta_from_v1:
  - context_hint.outputs_guaranteed: REWRITTEN — "9-Doc COMPILES retro material from individual `7-<container>-fix.json` files (NOT reconstructs from scratch). Pulls root_cause + attempted_fix + rollback_result + lesson per fix. Identifies recurring patterns via cross-fix analysis."
  - blockedBy: ALL fix tickets closed (Pilot + Examples + dynamic) — runner pickup must verify
ac.code_additions:
  - {desc: "retro material references each fix's evidence JSON", verify: {command: "for f in docs/audit/AUDIT-31-phase-I-evidence/7-*-fix.json; do name=$(basename $f .json); grep -c $name docs/retrospectives/2026-*-31I-unhealthy-fix-input.md || exit 1; done; echo OK", expect_stdout_match: "^OK$"}}
  - {desc: "9-Doc identifies ≥1 recurring pattern (cross-fix)", verify: {command: "grep -ciE 'recurring|pattern|cross.fix' docs/retrospectives/2026-*-31I-unhealthy-fix-input.md", expect_stdout_match: "[1-9]"}}
```

### 31.I-CallSiteInventoryGate, StabilityCheckpoint, Integration — minor updates

```yaml
StabilityCheckpoint_delta:
  - blockedBy: [CallSiteInventoryGate, Pilot, ALL dynamic 7-* closed OR N=0 branch confirmed]
Integration_delta:
  - new field in integration.json: pilot_fix_ref (records Pilot ticket key + outcome); n_dynamic_fixes_executed; dependency_cascade_handled (bool — if depends_on serialization actually occurred)
```

---

## §6. Filing batch order (DAG v2 — includes filing-time preflight rule)

**Cross-phase filing preflight rule (applies to ALL dynamic 7-* tickets + 7-Pilot)**: filing script verifies 31.F-StabilityCheckpoint + 31.H-StabilityCheckpoint Closed (with commit SHAs) BEFORE filing. Evidence written to ticket description.

```
L0: 1a, 2a, 3a, 4a
L1: 1bc, 2bc, 3bc
L2: LibsGate
L3: 5a (operator)
L4: AuditReviewGate (operator + jira-write; files dynamic 7-*)
L5: 6a
L6: 6b (sequential after 6a per codex)
L7: 7-Template
L8: 7-Pilot (NEW — first real broken-prod canary)
L9: 7-Example-1, 7-Example-2, ALL dynamic 7-* (parallel ONLY for independent containers; dependent sequenced per depends_on)
L10: 8a
L11: 8b
L12: 9-Doc (compiles from per-fix JSONs; blockedBy all closed)
L13: CallSiteInventoryGate
L14: StabilityCheckpoint (24-72h)
L15: Integration (tier:L 7-day soak)
```

16 layers, 22 fixed + 2 examples + N dynamic (N≤8) tickets.

---

## §7. Operator answers (all locked 2026-05-13)

| # | Question | Locked answer |
|---|---|---|
| 1 | Audit inventory richness | **YES** — 5a script auto-fetches 31.G Prometheus metric history per container (last 7d) |
| 2 | Pre-filed examples container names | **TBD at AuditReviewGate** (real containers vary; v2 enforces no-TBD-at-filing AC) |
| 3 | Dynamic ticket cap | **N≤8 per Phase 31.I batch**; spillover → new META |
| 4 | "Healthy" criterion | **≥4 consecutive probes / 2 min for fix close**; long-term tracked via 8b daily timer |
| 5 | Failure-injection target | **Lowest-blast-radius service** selected at AuditReviewGate (same container as Pilot if possible) |
| 6 | Retro material format | `docs/retrospectives/2026-XX-XX-31I-unhealthy-fix-input.md` (per project SOP) |
| 7 | 31.J gating | **blockedBy 31.I-StabilityCheckpoint** (24-72h pattern) |
| 8 | Soak duration | **7 days** (incremental on 31.H 14-day stability; rc2 schedule benefits) |

---

## §8. Submission flow

1. Spec at `docs/sprint-s12/phase-31i-ticket-spec.md` (v2)
2. Operator §7 lock
3. Commit + Gerrit +2 + submit
4. Filing script files 22 fixed + 2 examples (Pilot + dynamic deferred until post-AuditReviewGate)
5. Spot-check first 3
6. **Filing-script preflight rule (NEW)**: for every dynamic 7-* + Pilot, verify 31.F-StabilityCheckpoint + 31.H-StabilityCheckpoint Closed before POST

---

**End of Phase 31.I ticket spec v2 (22 fixed + 2 examples + N≤8 dynamic + Integration; Pilot first; hypothesis validation; per-fix evidence; jira-write schema; cascade-safe). Awaiting operator §7 lock.**
