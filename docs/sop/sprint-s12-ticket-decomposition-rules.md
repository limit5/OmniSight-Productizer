---
id: SOP-S12-TICKET-DECOMP
title: Sprint S12 (Bedrock) Ticket Decomposition Rules
scope: Sprint S12 (Foundation Rebuild per ADR-0023) — applies to ~250 tickets across 11 sub-METAs
status: Draft (2026-05-13)
authors: claude main-session (synthesised from 2026-05-08 to 2026-05-13 rescue work) + operator review
related:
  - ADR-0023 (Foundation Rebuild — implementation contract)
  - ADR-0021 (Release Pipeline Coordinator)
  - OP-1042 (filing-time JIRA label validation hook — primary enforcement vehicle)
  - feedback_4_ac_discipline.md (memory — 4-AC discipline + Go-Live target)
  - feedback_type_meta_routing.md (memory — type:meta routing trap)
  - feedback_jira_issuelink_direction.md (memory — L-OP-870 inward=blocker)
---

# Sprint S12 Bedrock — Ticket Decomposition Rules

## §1. Why this document exists

Sprint S12 (per ADR-0023) will produce **~250 tickets across 11 sub-phases over 18-26 weeks**. The previous wave (Sprint S0 "Foundation 2026-05-06") shipped ADR-0001 + ADR-0002 as documents but the implementation never landed; the result was 13 cross-cutting failures discovered 2026-05-13 (see ADR-0023 §1.1 + Appendix A).

S0 vs S12 difference: S0 specified WHAT (architecture intent). S12 must specify HOW (executable contract). **A ticket whose AC could be misread by codex-cli, claude-cli, or a future operator IS a ticket that will be misread.**

Specifically, the past 5 days of rescue work (2026-05-08 to 2026-05-13) surfaced **5 incident patterns** + **2 meta-patterns** that this document formalises into hard rules. Every Sprint S12 ticket spec MUST conform; OP-1042 (filing-time validation hook) enforces at filing time.

This is not optional advice. It is the anchor that prevents Sprint S12 from becoming another Sprint S0.

---

## §2. Pattern 1 — Multi-area AC must reverse-audit area labels

### 2.1 What goes wrong

When an AC references a file path that's in an area the ticket doesn't declare, the runner enforces its area-boundary discipline and refuses to make the change. This produces a `[codex-blocked]: out-of-area dependency` revert. Examples observed:

| Incident | AC referenced | Ticket areas | Result |
|---|---|---|---|
| OP-989 (29b Sprint F infra) | `docs/sop/3d-memory-architecture.md` | `area:devops, area:backend` | codex bail with out-of-area; ticket stuck for hours |
| OP-1016 (29a-1 deployment audit baseline) | `docs/audit/2026-05-13-deployment-audit-baseline.md` | `area:devops, area:tooling` | same pattern; 4 revert cycles before operator unblocked |
| OP-1029 (29d-1 Caddy) | docs/sop/cross-host-portability-staging.md | `area:devops` only | codex partial-complete + revert |

Root cause: AC was written by humans in prose form; ticket labels were tagged separately. No automated check linked the two.

### 2.2 The rule

> **Filing-time validation hook (extends OP-1042)**: every ticket's AC text MUST be scanned for file path references. Each path maps to an `area:*` label per the table below. The ticket's label set MUST contain every implied area, OR the AC text MUST be rephrased to defer those changes to a child ticket. **Otherwise filing is rejected.**

### 2.3 Path → area mapping (canonical)

| Path prefix | Maps to area |
|---|---|
| `docs/`, `*.md` at repo root | `area:docs` |
| `backend/`, `*.py` at repo root | `area:backend` |
| `backend/tests/`, `tests/` | `area:tests` |
| `app/`, `components/`, `hooks/`, `*.tsx`, `*.ts` (frontend) | `area:frontend` |
| `deploy/systemd/`, `deploy/k8s/`, `deploy/helm/`, `*.service`, `*.timer`, `Dockerfile*`, `docker-compose*.yml` | `area:devops` |
| `scripts/` (top-level), `*.sh`, `*.py` under `scripts/` | `area:tooling` |
| `.gitlab-ci.yml`, `.github/workflows/*` | `area:ci` (NEW for Sprint S12) |
| `backend/security/`, `backend/auth/`, `backend/credentials/`, cosign material | `area:security` |
| `backend/alembic/`, `*.sql`, DB schemas | `area:db` |
| `gerrit/`, Gerrit config | `area:gerrit` |
| `.config/omnisight/*.env` (operator creds) | `area:devops + class:operator-prepare-only` |

### 2.4 Enforcement implementation

In `file_jira_ticket.py` `validate_areas_match_description()`:

```python
# Strict mode (Sprint S12+):
def validate_areas_match_description_strict(areas: set[str], description_text: str) -> list[str]:
    referenced = scan_paths_to_areas(description_text)
    missing = referenced - areas
    if not missing:
        return []
    # Sprint S12: hard reject (no --force escape)
    raise ValidationError(
        f"AC references paths in {sorted(missing)} but ticket labels only "
        f"include {sorted(areas)}. Add labels OR split docs/tests changes into "
        f"a child ticket. (Sprint S12 strict mode; see SOP-S12-TICKET-DECOMP §2)."
    )
```

The OP-1042 (lint hook) implementation already has `is_canonical_label()`. Extend with this audit. Test cases: file synthetic tickets matching each rescue pattern from §2.1; assert filing rejected.

### 2.5 Escape hatch

A ticket may include AC referring to area X without label area:X **only if** the AC explicitly defers that work to a child:

```markdown
### 1. Code AC
- [ ] `backend/agents/foo.py`: new function bar()
- [ ] `docs/sop/foo-runbook.md` updated  ← deferred: see ticket OP-NNNN (area:docs child)
```

The hook recognises the literal pattern `deferred: see ticket OP-NNNN` and allows the AC to pass.

---

## §3. Pattern 2 — Sibling tickets need an Integration ticket

### 3.1 What goes wrong

Sibling tickets (same parent META, similar tier) each ship "their part" but the chain between them is never end-to-end-verified until production breaks. Examples:

| Incident chain | Per-ticket status | Real outcome |
|---|---|---|
| OP-1015/1019/1026/1028 (Missing tree) | All 4 marked 公開済み individually | Push chain to Gerrit silently failed for 90+ min; 7 incidents back-to-back |
| AUDIT-29 Phase 31.E migration (hypothetical) | 21 per-job migration tickets all green | Could ship 21 GitLab CI jobs that individually pass but collectively fail parity (artifact names diverged, required-check semantics drifted) |
| OP-693 + OP-694 + OP-1042 (label hygiene cluster) | each shipped | Coordinator + lint-hook + schema doc; if no integration test the chain doesn't fire on a real malformed ticket |

Root cause: 4-AC discipline (Code/Deploy/Integration/Exercised) is checked PER TICKET. But "Integration AC" of one ticket can be a false-positive when the integration spans multiple tickets.

### 3.2 The rule

> Every group of sibling sub-tickets (same parent META, doing related work) MUST have **one explicit `integration-*` ticket** that does ONLY the end-to-end verification of the group. The integration ticket:
> - **tier:M or higher** (cannot be tier:S — integration scope is too holistic)
> - blockedBy ALL siblings in the group
> - parentMeta same as siblings
> - AC focuses on: real chain execution + evidence + drift detection
> - Closes the sibling group's parent sub-META gate

### 3.3 What the integration ticket looks like

For each sibling group, the integration ticket follows this template:

```
Title: integration-{group-name}: end-to-end verify {N} siblings + parity report
Labels:
  - tier:M (minimum)
  - type:integration  (new label, distinguishes from feature/bug/cleanup)
  - area:{union of sibling areas}
  - capability:enable=run_tests (typically)
  - class:subscription-{claude|codex} (prefer claude for verification work)
parentMeta: <sibling parent>
blockedBy: <ALL siblings>

### Scope
Run the {group-name} end-to-end chain in real environment + emit evidence
file at `docs/audit/AUDIT-31-phase-{X}-evidence/integration-{group-name}.json`.

### 1. Code AC
- [ ] `tests/integration/test_{group-name}.py` exists
- [ ] Test exercises the FULL chain (not mock); pytest exit 0
  verify:
    command: pytest tests/integration/test_{group-name}.py -v 2>&1
    expect_exit_code: 0
    expect_stdout_match: "PASSED.*test_{group-name}"
    timeout_seconds: 600

### 2. Deploy AC
- [ ] All sibling artefacts (containers/scripts/services/yaml) deployed
- [ ] systemctl/docker/file evidence per sibling captured in evidence JSON
  verify:
    command: scripts/sprint-s12-evidence-collect.sh phase-{X}
    expect_exit_code: 0
    expect_stdout_match: "evidence_complete: true"

### 3. Integration AC
- [ ] End-to-end real-traffic test (or scheduled job run with success signal)
  verify:
    command: scripts/run-integration-{group-name}.sh
    expect_exit_code: 0

### 4. Exercised AC
- [ ] >=24h sustained green observation
- [ ] No `[verify-failed]` events from any sibling's verify hook
  verify:
    command: jq '.failures' /var/log/sprint-s12/verify-events.jsonl | grep -c "{group-name}"
    expect_stdout_match: "^0$"
    expect_exit_code: 0

### Go-Live target
{T+1d after all siblings closed}
```

### 3.4 When to NOT have integration ticket

A solo sub-ticket (no siblings, just one work-unit) doesn't need a separate integration ticket. The work-unit's own 4-AC Integration section is sufficient.

But: **if you find yourself wanting to split a "do X" into 2+ sub-tickets, you must also create the integration ticket**. The split + integration is the unit, not the split alone.

### 3.5 Anti-pattern: "We'll add the integration ticket later"

This pattern was observed in AUDIT-29: tickets shipped, then a follow-up "verify" was deferred. The follow-up never happened because the parent META closed first. **The integration ticket goes in at filing time, blockedBy is enforced, parent META cannot close until integration ticket closes.**

---

## §4. Pattern 3 — AC verify blocks must be machine-executable

### 4.1 What goes wrong

AC bullets written in English prose ("tests pass", "service deployed", "alert fires") leave codex/claude to infer how to verify. Inferred verifications drift from reality. Examples:

| Incident | AC bullet (English) | What codex actually did | Gap |
|---|---|---|---|
| OP-995 (29h auto-archive) | "Bridge transitions 公開済み → Archived after retention window" | Wrote function code; ran 1 unit test on synthetic data | Never verified the real bridge daemon picked up new code path |
| OP-1015/1019/1026 Missing tree | "Push to Gerrit refs/for/develop" | Ran `git push`; got "Missing tree" stderr; misread as race | No post-push verify (e.g., `gerrit query change-id` to confirm change exists) |
| OP-1029 Caddy path-rule | "Caddy reloaded with new config" | Wrote config file; reloaded service | Never `curl`'d the path to verify routing actually changed |

Root cause: ACs were not given exact verification commands; runners filled the gap with their own judgment which was insufficient.

### 4.2 The rule

> Every AC bullet that has a runtime expectation MUST include a `verify:` block. Filing-time hook rejects tickets where any AC bullet lacks `verify:` (except `verify: deferred-to-operator` explicit waiver).

### 4.3 Verify block schema

```yaml
verify:
  command: <shell command, must be runnable in the runner's CWD>
  expect_exit_code: <integer, default 0>
  expect_stdout_match: <regex OR exact string OR jsonpath-with-expected-value>
  expect_stderr_absent: <regex of stderr that should NOT appear; optional>
  timeout_seconds: <integer, default 60>
  run_as: <runner | operator | runner-then-operator; default runner>
```

### 4.4 Examples

**Trivial**:
```markdown
- [ ] `scripts/deployment-audit.sh` returns 0 fatal rows
  verify:
    command: scripts/deployment-audit.sh
    expect_exit_code: 0
    expect_stdout_match: "fatal: 0"
    timeout_seconds: 60
```

**Multi-step (use && in command)**:
```markdown
- [ ] Backend container reports `migration: ok` in /readyz
  verify:
    command: docker exec omnisight-productizer-backend-a-1 curl -sf http://localhost:8000/readyz | jq -r '.checks.migrations.ok'
    expect_exit_code: 0
    expect_stdout_match: "^true$"
    timeout_seconds: 30
```

**Jsonpath**:
```markdown
- [ ] Gerrit change merged + Patchset accepted
  verify:
    command: ssh -i ~/.config/omnisight/gerrit-claude-bot-ed25519 -p 29418 claude-bot@sora.services gerrit query --format=JSON change:${CHANGE_ID}
    expect_stdout_match: '"status":"MERGED"'
    expect_exit_code: 0
    timeout_seconds: 30
```

**Deferred to operator**:
```markdown
- [ ] Cosign keypair generated + private key stored as GitLab CI variable
  verify: deferred-to-operator
  (rationale: requires operator's GitLab CI admin access; runner cannot verify)
```

### 4.5 Verify execution contract

After task work completes, runner runs each `verify:` block in order:
1. If `run_as: runner` and verify passes → mark AC bullet done
2. If `run_as: runner` and verify fails → ticket NOT closed; revert to To Do + comment `[verify-failed] {bullet}: {actual_output}`
3. If `run_as: operator` → AC bullet stays unchecked; operator must comment `[operator-verified] {bullet}` to close
4. If `run_as: runner-then-operator` → runner runs verify first; then operator confirms (e.g., for destructive operations)

### 4.6 Enforcement

OP-1042 hook parses AC at filing time. Each AC bullet:
- Has `verify:` block → pass
- Has `verify: deferred-to-operator` + rationale → pass
- Else → reject filing

---

## §5. Pattern 4 — Operator-decision branchpoints must be in spec

### 5.1 What goes wrong

When a ticket's work might encounter a state that requires operator judgment, codex/claude either:
(a) make the call themselves (sometimes wrong — e.g., OP-1016 wanted to `alembic upgrade head` while 0203 had a duplicate)
(b) bail with `[discovered-dependency]` and the operator must triage

Both are sub-optimal. (a) is risky; (b) requires operator attention but the operator doesn't have the diagnostic info codex collected.

### 5.2 The rule

> Tickets with potentially-branching execution MUST include a `decision_points:` block in spec that:
> - Names each decision point (id)
> - Defines the condition trigger
> - Lists branches with (a) condition match, (b) action (continue/pause/escalate/file-followup)
> - Provides exact diagnostic command for runner to emit

### 5.3 Decision-point schema

```yaml
decision_points:
  - id: dp1
    name: alembic-head-resolution
    trigger:
      command: cd backend && alembic heads 2>&1
      pattern_match: ".*more than once.*"
    branches:
      - name: duplicate-revision-known
        match: "0203 is present more than once"
        action: pause
        operator_label: needs-operator-decision-alembic-0203
        diagnostic_command: |
          cd backend && python3 /tmp/alembic-dump2.py
        rationale: "0203 duplicate is a known cluster — operator decides which file keeps the revision (per OP-1046)"
      - name: novel-multi-head
        match: ".*more than once.*" # but NOT 0203
        action: file-followup
        followup_ticket_spec:
          summary: "alembic multi-head investigation"
          area: db
          tier: M
        rationale: "novel multi-head; runner shouldn't unilaterally resolve"
      - name: clean
        action: continue
```

### 5.4 What happens at runtime

Runner picks up ticket → executes `decision_points[].trigger.command` → matches branches:
- `action: continue` → proceed with main task
- `action: pause` → add `needs-operator-decision-{name}` label + emit diagnostic_command output to JIRA comment + revert ticket to To Do
- `action: escalate` → notify operator via operator_notifier P0 + halt
- `action: file-followup` → POST followup ticket per spec; blockedBy: this ticket; pause this ticket pending followup

### 5.5 Enforcement

For tickets in Sprint S12 categories with known branchpoints (per ADR-0023 §3 architecture):
- All Phase 31.B tickets: must address chicken-and-egg deploy branchpoint
- All Phase 31.D tickets: must address replication state branchpoint (existing/missing)
- All Phase 31.E ci.yml migrations: must address parity branchpoint
- All Phase 31.F cosign tickets: must address legacy-image branchpoint
- All Phase 31.I prod health: must address rolling-restart safety branchpoint

OP-1042 hook validates: tickets in these phases without decision_points block are rejected (unless explicit waiver `decision_points: none-needed: <rationale>`).

---

## §6. Pattern 5 — Functional ticket pairs with Activation ticket

### 6.1 What goes wrong

"Code written + Gerrit pushed + merged to develop" feels like done. But the artefact only matters if it's installed + enabled + observed firing. Examples observed:

| Functional shipped | Activation gap | Outcome |
|---|---|---|
| 84 systemd unit files in `deploy/systemd/` | Only 25 installed on host (`systemctl --user list-units`) | "shipped but not deployed" anti-pattern (AUDIT-23) |
| Prometheus alert rules (`deploy/observability/prometheus/alerts.yml`) | No prometheus container running | 0 alerts fire ever |
| cosign keyless config in CI workflows | GitHub OIDC issuer hardcoded; never produced real sig | Signature chain entirely fictional |

Root cause: ticket spec stops at "code merged"; nobody owns the activation.

### 6.2 The rule

> Every functional ticket whose deliverable is a deployable artefact (systemd unit / container / cron / alert rule / script that must run / config that must be loaded) MUST have a paired **activation ticket**:
> - Activation ticket blockedBy: functional ticket
> - Activation ticket parentMeta: same as functional ticket
> - Activation ticket scope: install + enable + observe firing
> - Activation ticket fixVersion: same as functional ticket
> - Functional ticket's parent sub-META does NOT close until activation ticket closes

### 6.3 Activation ticket template

```
Title: activate-{component}: install + enable + verify firing
Labels:
  - tier:M (typically; activation is not trivial)
  - type:activation (new label)
  - area:devops (typically; or area:{relevant})
  - class:subscription-{claude|codex} OR class:operator-prepare-only (if destructive)
parentMeta: <same as functional ticket>
blockedBy: <functional ticket>

### Scope
Take the functional artefact from <functional ticket> and:
1. Install it on the target host (per deploy/<artefact-path>)
2. Enable it (systemctl enable / docker compose up / cron register)
3. Verify it fires once (synthetic trigger if possible)
4. Observe N hours of healthy operation

### 1. Code AC
- [ ] `scripts/activate-{component}.sh` exists
  verify:
    command: test -x scripts/activate-{component}.sh
    expect_exit_code: 0

### 2. Deploy AC
- [ ] Artefact installed on host (per location)
  verify:
    command: systemctl --user is-enabled {unit} OR docker ps --filter ...
    expect_exit_code: 0
    expect_stdout_match: "enabled" OR "Up"
- [ ] artefact enabled (auto-start on boot if applicable)
  verify: ...

### 3. Integration AC
- [ ] One end-to-end firing event observed (synthetic trigger)
  verify:
    command: scripts/test-trigger-{component}.sh && journalctl --user -u {component} --since="1 min ago" | grep "expected-event"
    expect_exit_code: 0

### 4. Exercised AC
- [ ] >=N hours uptime (per component criticality)
- [ ] >=M firing events observed (per ticket spec, ≥1)
  verify:
    command: journalctl --user -u {component} --since="N hours ago" | wc -l
    expect_stdout_match: "[1-9][0-9]*"  (at least 1 event)

### Go-Live target
T+{N}d after functional ticket close
```

### 6.4 What's NOT an activation candidate

Some functional tickets don't need an activation pair:
- Pure documentation (no artefact to enable)
- Refactoring (existing system already runs; nothing to activate)
- Test-only changes (run via CI/tests, no deploy)
- Tickets explicitly marked `activation: not-applicable: <rationale>` (waiver)

### 6.5 Enforcement

OP-1042 hook: any ticket with these label combos REQUIRES an activation child:
- `area:devops` + `type:feature`
- `area:devops` + creates new file in `deploy/systemd/` or `deploy/observability/`
- `type:feature` + creates new file matching `*.service|*.timer|*.yaml` under `deploy/`

Filing without activation child → rejected; OR explicit waiver line in spec.

---

## §7. Meta-pattern A — ADR-anchored spec review before filing

### 7.1 Lesson from this conversation cycle

The pattern observed 2026-05-13:
- Operator + claude designed AUDIT-29 META + 14 children
- ADR-0021 (Coordinator) accepted before tickets filed
- AUDIT-29 batch filed via batch script (28 tickets)
- Immediately discovered: ~5 of the 28 had label errors (type:meta routing trap, missing area, missing capability)
- 2 rounds of fix scripts to repair

Root cause: filing was a single-step bulk action; no per-ticket peer review.

### 7.2 The rule

> Every Sprint S12 sub-phase (31.A, 31.B, ..., 31.K) ticket batch MUST go through:
> 1. Spec drafting → `docs/sprint-s12/phase-{X}-ticket-spec.md` (or similar)
> 2. Gerrit PR for the spec doc (operator + 1 other reviewer required)
> 3. `+2` lock → spec doc merged to develop
> 4. THEN ticket-filing script runs against spec doc
> 5. Filing-time OP-1042 hook validates each ticket
> 6. Post-filing: smoke-check the first 3 tickets actually picked up correctly by one runner

This is slower than "design + file in one session". That's the point. The 5-day cleanup from S0's quick filing was more expensive than the friction of slower filing.

### 7.3 Spec doc template

Each sub-phase's spec doc structure:
```
# Phase 31.X Ticket Spec — {phase title}

## Sub-META definition
{summary, AC, Go-Live, areas}

## Children
{table of children: id, title, type, areas, tier, deps}

## Per-child specs
{each child has: scope, 4-AC, decision_points if any, verify blocks, dependencies}

## Integration ticket
{if siblings present; per §3}

## Activation ticket(s)
{if functional deliverables present; per §6}

## Open questions for operator review
{anything spec-level that needs operator decision before filing}
```

---

## §8. Meta-pattern B — Runner preference hints

### 8.1 Observation

Claude and codex have different strengths:

| Strength | Claude | Codex |
|---|---|---|
| Backend / scripts / build automation | strong | strongest |
| Frontend (Next.js / React) | strong | strong |
| Documentation / lesson docs | strongest | strong |
| ADR / architecture writing | strongest | strong |
| Test writing (especially edge cases) | strong | strong |
| Migration / rebase / merge resolution | mid | strongest |
| Cross-area judgment / scope decisions | strongest | mid |
| Pure code-edit + commit + push cycle | strong | strongest |

This is empirical (observed during AUDIT-29 routing). It's not rigid — both can do any ticket — but routing accuracy improves outcomes.

### 8.2 The rule

> Sprint S12 ticket spec may include `prefer:` hint:
> - `prefer:claude` — soft hint; runner JQL still gates by `class:*`
> - `prefer:codex` — soft hint; runner JQL still gates by `class:*`
> - No `prefer:` → operator chooses class:* at filing time

Class label is still authoritative (`class:subscription-claude` or `class:subscription-codex`). The `prefer:` hint is for the operator's routing decision at filing time + for the Coordinator (Phase 31.B → 29f Coordinator when live) to use as input.

### 8.3 Phase-level prefer guidance (per ADR-0023)

| Phase | Prefer | Rationale |
|---|---|---|
| 31.A Dev WSL one-button | claude | judgment-heavy + docs-heavy |
| 31.B Multi-agent runner fix | codex | backend Python; runner is codex's territory |
| 31.C Operator notifier | claude (Discord/Email design) + codex (Python implementation) | split |
| 31.D Source-of-truth replication | codex (Gerrit plugin config + ssh) | infrastructure |
| 31.E GitLab CI migration | codex (yaml + scripts) | mostly tooling |
| 31.F Image distribution + cosign | codex | scripts + crypto |
| 31.G Observability | codex (deploy) + claude (docs/runbook) | split |
| 31.H Systemd discipline | codex | infra |
| 31.I Prod health hardening | claude (judgment) + codex (scripts) | split |
| 31.J 5-env carve-out cutover | claude (judgment-heavy) | high-stakes operator-supervised |
| 31.K Doc + ADR amendment | claude | docs |

These are starting points. Operator decides final routing per-ticket.

---

## §9. Anti-patterns (from rescue history; do NOT repeat)

### 9.1 Type:meta on single-phase tickets

**Origin**: AUDIT-29 batch (OP-988 etc.) — single-phase sub-METAs filed with `type:meta`. Runner pickup treated them as roll-ups, returned read-only fallback capabilities, ticket stuck in pickup-revert loop.

**Rule**: `type:meta` ONLY for tickets that have children (true roll-ups). Single-phase tickets use `type:feature` / `type:cleanup` / `type:operator`.

OP-1042 schema enforces: `type:meta + class:subscription-* + no children` = reject.

### 9.2 Bulk-file tickets without per-ticket review

**Origin**: AUDIT-29 batch via `file_audit_29_tickets.py`. 28 tickets filed in 30 sec. 5 had label errors. 5 separate fix rounds needed.

**Rule**: Per §7. Spec doc → Gerrit review → file.

### 9.3 Migration without monitoring

**Origin**: 13 of 15 ADR-0023 cross-cutting failures involved a migration / setup that was never monitored for drift.

**Rule**: every sub-phase that touches replication / sync / mirror / scheduled-job MUST include a drift-detection ticket (Phase 31.G observability extension).

### 9.4 "Future ADR will handle" deferrals

**Origin**: ADR-0001 + ADR-0002 marked Accepted on 2026-05-04. The implementation was deferred indefinitely. 9 days later, 13 failures discovered.

**Rule**: Every Sprint S12 ADR-equivalent decision must spawn an implementation ticket at the same time the ADR is filed. No "ADR Accepted, ticket later".

### 9.5 Code edits without verify command

**Origin**: 6+ rescue incidents (OP-1015, OP-1019, OP-989, OP-995, etc.).

**Rule**: Per §4. Every AC bullet has machine-executable verify block OR explicit deferred-to-operator waiver.

### 9.6 Single AC bullet doing 3 things

**Origin**: AUDIT-29 ticket spec — "Cognee container + Neo4j systemd unit + on-host TLS config" as one bullet. Runner can't split; either does all or none.

**Rule**: One AC bullet = one verifiable assertion. If you find yourself writing "and" in an AC bullet, split it.

### 9.7 Operator-only work in runner-class ticket

**Origin**: OP-1043 (operator-supervised live migration) — early draft had `class:subscription-codex`. Would have caused runner to attempt destructive db migration unsupervised.

**Rule**: Per ADR-0023 §12. Tickets requiring operator are `class:operator` or no `class:*`. Filing-time hook checks per §12 classification table.

### 9.8 Vague "tests pass" AC

**Origin**: many rescue incidents.

**Rule**: AC bullet "tests pass" → reject. Must specify which test file, expected output, exit code.

### 9.9 No rollback procedure for destructive ops

**Origin**: OP-1046 alembic upgrade — initial AC didn't include backup procedure. Operator + claude added pg_dump-via-docker-exec last-minute.

**Rule**: Every destructive operation has explicit backup + rollback procedure in spec.

### 9.10 Multi-area work without explicit area label

**Origin**: §2.1 incidents.

**Rule**: Per §2.

---

## §10. Enforcement integration with OP-1042

OP-1042 (filing-time JIRA label validation hook, currently in develop after merge per AUDIT-29) is the primary enforcement vehicle. After Sprint S12 starts, OP-1042's schema YAML at `docs/sop/jira-label-schema.yaml` MUST be extended with these rules:

```yaml
sprint_s12_rules:
  ac_area_audit:
    enabled: true
    mode: hard-reject  # was: warn-only
    path_to_area_map: see SOP-S12-TICKET-DECOMP §2.3
  verify_block_required:
    enabled: true
    mode: hard-reject
    waiver_pattern: "verify: deferred-to-operator"
  integration_ticket_required:
    enabled: true
    mode: hard-reject
    trigger: "if 2+ siblings under same parent-meta with similar area+tier"
  activation_ticket_required:
    enabled: true
    mode: hard-reject
    trigger: "if area:devops AND type:feature AND creates new .service|.timer|.yaml under deploy/"
  decision_points_required_phases:
    enabled: true
    mode: hard-reject
    phases: [31.B, 31.D, 31.E, 31.F, 31.I]
  one_assertion_per_ac_bullet:
    enabled: true
    mode: warn-then-reject  # warn first; reject if 2 warnings
    pattern: '" AND ", " and verify", "; and"'
  type_meta_only_with_children:
    enabled: true
    mode: hard-reject
    rule: "type:meta + class:subscription-* + no_children_at_filing_time = reject"
  operator_class_segregation:
    enabled: true
    mode: hard-reject
    table: see ADR-0023 §12
```

Update process: when SOP-S12-TICKET-DECOMP is reviewed + locked (operator +2), the schema YAML extension lands in same commit.

---

## §11. Examples (drawn from a sub-phase)

### 11.1 Worked example: Phase 31.B-1 (branch namespacing) ticket spec

Following all 5 patterns + 2 meta-patterns:

```markdown
# Phase 31.B-1: Branch namespacing in auto-runner-jira.py

Labels:
  area:backend, area:tests, area:tooling
  tier:M
  type:feature
  prefer:codex
  class:subscription-codex
  capability:enable=code_edit, capability:enable=run_tests, capability:enable=gerrit_push, capability:enable=jira_update, capability:enable=run_lint
  scope:sprint-s12, phase:31.B, sub:31.B-1
  blocks: 31.B-Integration

parentMeta: AUDIT-31.B
blockedBy: 31.B-0 (chicken-and-egg freeze deploy ticket)

## Scope
Modify `auto-runner-jira.py` so each runner pickup constructs a unique
branch name: `feature/{TICKET}-{INSTANCE}-pid{PID}-{EPOCH}-runner`.
Eliminates worktree branch collision (root cause of codex-1 + codex-2
race observed 2026-05-13).

## 1. Code AC
- [ ] `auto-runner-jira.py:make_pickup_branch_name(ticket_key, instance_id, pid) -> str` function exists
  verify:
    command: python3 -c "from auto_runner_jira import make_pickup_branch_name; print(make_pickup_branch_name('OP-1234', 'codex-1', 12345))"
    expect_exit_code: 0
    expect_stdout_match: "^feature/OP-1234-codex-1-pid12345-\\d{10}-runner$"
    timeout_seconds: 5

- [ ] Function output is collision-free under concurrent invocation
  verify:
    command: python3 backend/tests/test_runner_branch_namespacing.py -v
    expect_exit_code: 0
    expect_stdout_match: "test_concurrent_no_collision.*PASSED"
    timeout_seconds: 30

- [ ] Branch name format validates against schema (no path separators, length cap)
  verify:
    command: python3 backend/tests/test_runner_branch_namespacing.py::test_format_constraints -v
    expect_exit_code: 0

## 2. Deploy AC
- [ ] Modified `auto-runner-jira.py` is on develop branch
  verify:
    command: git fetch gerrit develop && git diff gerrit/develop:auto-runner-jira.py HEAD:auto-runner-jira.py | grep -c "make_pickup_branch_name"
    expect_stdout_match: "^[1-9]"
    expect_exit_code: 0

## 3. Integration AC
- [ ] After 31.B-0 freeze deploy: all 4 runners running new code use new branch format
  verify:
    command: tmux capture-pane -t codex-runner-loop -p | grep -c "feature/OP-.*-codex-.*-pid"
    expect_stdout_match: "^[1-9]"
  run_as: runner-then-operator
  rationale: "verify after operator confirms deploy"

## 4. Exercised AC
- [ ] ≥4 distinct pickups in production with new branch format observed in JSONL log
  verify:
    command: jq -r 'select(.event=="branch_created") | .branch_name' /var/log/runner/branch-events.jsonl | grep -c "^feature/OP-.*-pid"
    expect_stdout_match: "^[4-9]|^[1-9][0-9]+"
    expect_exit_code: 0

## decision_points
  - id: dp_existing_branch_collision
    name: existing-claimed-ticket-on-old-format-branch
    trigger:
      command: git -C $WORKTREE branch | grep "feature/OP-.*-runner-fresh$"
      pattern_match: ".+"
    branches:
      - name: pre-existing-claimed
        match: ".+"
        action: pause
        operator_label: needs-operator-decision-old-format-branch
        diagnostic_command: |
          git -C $WORKTREE branch | grep "feature/OP-.*-runner-fresh$"
        rationale: "old-format branches exist from pre-31.B-1; operator decides cleanup procedure"
      - name: clean
        action: continue

## Go-Live target
T+0.5d after 31.B-0 freeze deploy

## fixVersion
v0.5.0-rc2

## Notes
This ticket is sibling to 31.B-2 (ephemeral clones), 31.B-3 (cleanup), 31.B-4 (chaos tests), 31.B-5 (rollback flag).
After all five sibling tickets close, the **31.B-Integration ticket** (separate, blockedBy 31.B-1..5) executes end-to-end chaos test verifying race-free 4-runner concurrent pickup. 31.B sub-META cannot close until Integration ticket closes.
```

### 11.2 What this example demonstrates

- Pattern 1: AC has explicit path references → all referenced areas in labels (backend + tests + tooling)
- Pattern 2: sibling structure (B-1..5) + explicit "31.B-Integration ticket" sibling
- Pattern 3: every AC bullet has machine-executable verify block; one has run_as: runner-then-operator
- Pattern 4: decision_points block for the existing-branch-collision scenario
- Pattern 5: not applicable (this is functional change, no separate "activate" — runner deploys via 31.B-0 freeze ticket)
- Meta-A: spec drafted here; Gerrit PR + +2 required before ticket file
- Meta-B: `prefer:codex` hint

---

## §12. Open questions / future

1. **Auto-extraction of verify commands from existing tests**. Many `verify:` blocks are running pytest. Tool to auto-generate verify blocks from `pytest --co` output?
2. **Decision-point execution model**. Runner needs decision-point grammar parser. Implementation as part of 31.B's auto-runner-jira.py refactor?
3. **Activation ticket auto-generation**. When functional ticket spec landed, auto-generate activation child spec? Risky because activation specifics vary.
4. **Integration ticket scope ambiguity**. How many siblings before an integration ticket is required? 2? 3? More? §3.4 says "wherever you split do X into 2+ tickets" — needs refinement.
5. **Cross-phase integration tickets**. Some integration tests span multiple sub-phases (e.g., "release pipeline integration" spans 31.E + 31.F + 31.G + 31.I). How to scope these?
6. **prefer: hint vs operator override**. Operator may override prefer-hint per pickup. Should that be logged for retrospective?

These get resolved as Sprint S12 progresses; this doc is updated.

---

## Appendix A — Incident-to-pattern map

For traceability:

| Incident | Pattern | Lesson |
|---|---|---|
| OP-989 docs-out-of-area revert | Pattern 1 | AC text reverse-audits area labels |
| OP-1016 alembic 0203 duplicate | Pattern 4 | decision_points required for db-domain |
| OP-1015/1019/1026/1028 Missing tree | Pattern 3 + sub-phase Integration ticket | Post-push verify; integration end-to-end |
| OP-995/1013/1029/1033 capability fail | Pattern 1 (capability is a kind of area) | Lint hook requires full capability set for tier:S |
| OP-988 type:meta routing | Anti-pattern 9.1 | Type:meta only for roll-ups |
| AUDIT-29 batch 5/28 label errors | Meta-A | Pre-filing Gerrit review |
| AUDIT-23 (84 unit, 25 installed) | Pattern 5 | Activation ticket pairs |
| OP-1046 alembic upgrade backup | Anti-pattern 9.9 | Explicit rollback in spec |
| OP-1042 lint hook | Enforcement | This doc is the input |
| L-OP-247 repo topology drift | Anti-pattern 9.4 | No "future ADR" deferral |

---

**End of SOP-S12-TICKET-DECOMP draft. Awaits operator review + Gerrit +2.**
