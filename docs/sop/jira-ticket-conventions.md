# JIRA Ticket Conventions for OmniSight

**Status**: Draft (2026-05-06) — pending operator review + 3 example ticket UI verification.

**Authority**: Once accepted, this SOP governs every ticket in the OP project. Violations are caught by CI (drift guards, prereq audit, scheduler tests). Amendments require a META ticket (label `meta:governance`) plus operator +1.

**Purpose**: Replace `TODO.md` as the source of truth for actionable work. Defines ticket structure, dependencies, lifecycle, scheduler, and how runners (`auto-runner-codex.py`, future `auto-runner-claude.py`) pick up tickets safely.

**Related**:
- ADR 0001 — Five-branch Git Flow (where work lands)
- ADR 0003 — Gerrit Code Review (where review happens)
- ADR 0004 — Per-agent JIRA identity (which bot picks up)
- ADR 0005 — Tier S/M/L/X authority (escalation gates)
- ADR 0007 — Multi-Provider Subscription Orchestrator (consumer of `class:` labels)
- ADR 0008 — Agent RPG Class & Skill Leveling (shares `agent_class` schema)

---

## §1 Hierarchy

Three levels:

```
Epic     = Wave        e.g. "MP.W1 — Backend orchestrator + quota tracker"
Story    = Item        e.g. "MP.W1.1 — provider_orchestrator.py central registry"
Sub-task = (rare)      used for in-flight split, see §6
```

**Why three levels (not Initiative-Epic-Story)**:
- JIRA Initiative is a Premium-tier feature
- Priority namespace (MP / RPG / FX2 / BP / etc.) lives in Component field, not as a separate hierarchy level
- Epic = Wave maps cleanly to the existing `### MP.W1 — ...` TODO.md structure

**Why Story = Item (not Sub-task)**:
- Sub-tasks have reduced JIRA features (no own Epic Link, fewer custom fields, less flexible workflow)
- Item-level work needs full ticket features (linked PRs, comments, attachments, full status workflow)
- Sub-task reserved for in-flight split (§6) when a parent Story is discovered to be too large

**Naming**:
- Epic Summary: `<task_id> — <one-line goal>` e.g. `MP.W1 — Backend orchestrator + quota tracker`
- Story Summary: `<task_id> — <one-line goal>` e.g. `MP.W1.1 — provider_orchestrator.py central registry`
- Sub-task Summary: `<parent_task_id>.<split_index> — <split goal>` e.g. `MP.W1.1.a — orchestrator ABC + register hook`

---

## §2 Required Fields + Labels

### Required JIRA fields

| Field | Value source | Example |
|---|---|---|
| Summary | Manual, follows §1 naming | `MP.W1.1 — provider_orchestrator.py central registry` |
| Description | Markdown, follows §3/§9 templates | (see §9 + Appendix A) |
| Issue Type | `Story` (default) / `Epic` (Wave) / `Sub-task` (split only) | `Story` |
| Component | Single value, see §18 | `MP` |
| Epic Link | Parent Wave's ticket key | `OP-104` |
| Fix Version | Single value, see §4 | `v0.4.0` |
| Reporter | Operator's account | `nanakusa-sora` |
| Assignee | Empty (TODO) → bot account (In Progress) | `codex-bot@omnisight.local` |
| Status | Workflow state, see §10 | `TODO` |

### Required labels (every ticket)

| Label | Format | Example | Purpose |
|---|---|---|---|
| Class | `class:<agent_class>` | `class:api-anthropic` | Runner JQL filter (§16) |
| Tier | `tier:<S\|M\|L\|X>` | `tier:M` | Authority + estimation (§3) |
| Area | `area:<domain>` (multi) | `area:backend` `area:db` | Prompt injection (§5) |

### Optional labels

| Label | Format | Use |
|---|---|---|
| Mutex | `mutex:<resource-id>` | Resource conflict prevention (§8 type c) |
| Audit batch | `audit:<YYYY-MM-DD>` | Audit-origin tickets (§4) |
| Incident | `incident:<YYYY-MM-DD>-<slug>` | Incident-response (§4) |
| Tech debt | `tech-debt:<period>` | Burn-down batch tagging (§4) |
| Compliance | `compliance:<framework-clause>` | e.g. `compliance:soc2-cc6.1` |
| Meta sub-type | `meta:<kind>` | Only on `Component=META` (§17) |
| Drift signal | `drift:<over-run\|over-tier>` | Auto-applied by drift detector (§14) |

---

## §3 Tier ↔ Story Points 升級路徑

Tier captures both authority class (per ADR 0005) and rough size. Hour mapping is informal but consistent — used by drift detector (§14) to compute `actual / target_upper_bound` ratio.

| tier | hour range | typical scope | escalation |
|---|---|---|---|
| `tier:S` | 1–3 hour | small fix, config tweak, one test | bot self-pickup |
| `tier:M` | 4–12 hour | one module + tests | bot self-pickup |
| `tier:L` | 1–3 day | cross-module / schema migration | bot pickup, +1 human pre-merge |
| `tier:X` | > 3 day | epic-level, multi-area | **must be split before pickup** OR human assignment first |

**Drift signals** (when to add tiers):
- Many tickets at one tier with wide actual-hour spread → split that tier (e.g. add `tier:XS` < 1 hour for trivial fixes if `tier:S` accumulates 30-min items)
- Many tier:X tickets that resist splitting → consider `tier:XL` for genuine epics

**Future Story Points migration path**:
- Story Points become useful when Sprint cadence is adopted (capacity planning per Sprint)
- Until then: tier-only
- Migration: 1 SP = 1 hour of `tier:M` mid-range = one mechanical SQL update
- The hour-range mapping above must stay updated so the conversion stays trivial

---

## §4 Fix Version 軸 + Label 軸正交設計

Two orthogonal axes:

**Fix Version (single, ships-with semantics)**:

| Type | Examples | Use |
|---|---|---|
| SemVer feature | `v0.4.0`, `v0.5.0`, `v1.0.0` | Planned feature releases |
| SemVer patch | `v0.4.1`, `v0.4.2` | Hotfix / patch within major.minor |
| `backlog` | (literal value) | Not yet scheduled |
| `incident` | (literal value) | Incident-response, transient until upgraded to SemVer |

**Origin labels (multiple, why-this-exists semantics)**:

| Label | Use |
|---|---|
| `audit:2026-05-03` | Output of a deep-audit cycle (e.g. FX) |
| `audit:2026-05-06` | Different audit cycle (e.g. FX2) |
| `incident:2026-05-06-oauth-401` | Specific incident response |
| `tech-debt:q2-2026` | Quarterly tech-debt burn-down |
| `compliance:soc2-cc6.1` | Compliance-framework-driven |

**Mental model**:
- Fix Version answers *"what does this ship with?"*
- Origin label answers *"why does this exist?"*

A ticket can have one Fix Version + multiple origin labels. Example: a ticket created during incident response that ALSO ships in v0.4.0:
- `Fix Version: v0.4.0`
- `labels: incident:2026-05-06-oauth-401, audit:2026-05-06`

Industry reference: Atlassian's own JAC / JRA public projects use this orthogonal pattern.

---

## §5 Component + Area prompt injection

### Component (single = priority namespace)

See §18 for full list. Examples: `MP`, `RPG`, `FX2`, `BP`, `META`.

### Area (multiple = work domains)

| Label | Domain |
|---|---|
| `area:backend` | Python / FastAPI / SQLAlchemy / Postgres |
| `area:frontend` | React / Next.js / Tailwind |
| `area:devops` | Docker / Synology / CI / deployment |
| `area:tests` | Test framework, fixtures, CI gates |
| `area:db` | Migrations, schema design, RLS |
| `area:docs` | ADRs, SOPs, README, retrospectives |
| `area:security` | Auth, secrets, KS envelope, OAuth |
| `area:embedded` | HD priority hardware/firmware verification |
| `area:tooling` | Runner / scripts / CLI helpers |

### Prompt injection at runner pickup

When runner picks up `OP-1234` it constructs:

```
You are working on JIRA ticket OP-1234.

Component: MP — Multi-Provider Subscription Orchestrator
Areas: backend, db
Tier: M (4–12 hour scope)

Stay strictly within these boundaries. Do NOT introduce changes to:
  - frontend (no React / Tailwind / TSX edits)
  - docs (no markdown / ADR edits unless explicitly required by AC)
  - security (no auth / token / secret edits)
  - embedded
  - devops
  - tooling

If you find that completing this ticket requires touching an out-of-area
domain, halt, comment on the ticket, and transition back to TODO with
a discovered-dependency note (see §11).
```

The negation list = full `area:*` taxonomy minus declared areas. Mechanical, schema-driven prompt — no operator hand-curation, no drift between ticket label and prompt context. This is how Component + Area become *goal anchors* and not just plan-view filters.

---

## §6 Sub-task split rules + authority preservation

Sub-tasks are reserved for in-flight split: a Story is picked up, the agent discovers it exceeds its tier, proposes splitting before continuing.

### Allowed split conditions

| Condition | Example |
|---|---|
| Parent actual scope > tier upper bound | `tier:M` ticket discovered to be 14-hour work |
| Agent context window > 70% with work remaining | Small-context model (e.g. local-llm-qwen) hits limit |
| Multi-file / multi-area work warrants separate review | One ticket touches 5 unrelated files; reviewer prefers 5 separate diffs |

### Forbidden split conditions

| Condition | Why forbidden |
|---|---|
| Make ticket "look smaller" for metric optics | Gaming the system |
| Bypass `tier:X` human-approval gate | `tier:X` parent → 4× `tier:M` sub-tasks would dodge ADR 0005 authority |
| Reroute work from `class:api-anthropic` to `class:subscription-codex` | Class assignment is intentional; split cannot change it |

### Authority preservation rule

> **The maximum `tier` of any sub-task is bounded by parent `tier`.** Aggregate workload of all sub-tasks must approximately equal parent workload (drift detector §14 catches if total actual hours diverge > 50% from sum of sub-task targets). Split cannot dilute authority gates.

Example: `tier:X` parent split into 3 sub-tasks → each sub-task is `tier:X` (or lower). Parent's pickup gate (human assignment first) cascades to all sub-tasks.

### Procedure

1. Agent comments on parent: `[runner-split-proposal]` with proposed sub-task list
2. Operator / O7 reviewer accepts or amends
3. Sub-tasks created (auto-inherit parent's `class:`, `area:`, `mutex:`; `tier:` ≤ parent)
4. Parent transitions back to `TODO` with status note (sub-tasks become the work)
5. Each sub-task picked up independently per §10 workflow

---

## §7 Documentation taxonomy (HANDOFF.md replacement)

`HANDOFF.md` is **frozen as of 2026-05-06**. Future entries split by purpose:

| Need | Destination |
|---|---|
| Per-ticket resolution log | JIRA ticket Resolution field + final comment + linked Gerrit patchset |
| Cross-agent handoff | JIRA ticket comment with @-mention of receiving bot/operator |
| Lesson learned (process / pitfall) | `docs/sop/lessons-learned.md` (markdown, version-controlled) |
| Multi-ticket retrospective | `docs/retrospectives/YYYY-MM-DD-<slug>.md` (one file per cycle / incident) |
| Onboarding / wiki | Confluence (links to ADR / SOP / lessons-learned / recent retrospectives) |

`HANDOFF.md` itself stays in repo as historical archive (~9.9 MB). Header marks it deprecated. Existing references in commit messages stay valid (file not deleted).

CLAUDE.md L1 rules updated:

```
When completing a task:
  - Update the JIRA ticket: Resolution field + final comment with what
    was done / why
  - If a generalisable lesson emerged: append to docs/sop/lessons-learned.md
    (one entry per lesson, dated, with situation/fix/verification)
  - If a cross-ticket / cross-Phase retrospective is warranted: open
    docs/retrospectives/YYYY-MM-DD-<slug>.md and link from META ticket
HANDOFF.md is FROZEN as of 2026-05-06. Do not append.
```

---

## §8 Dependencies — 6-type taxonomy

| Type | Definition | JIRA mechanism | Enforcement layer |
|---|---|---|---|
| **(a) Hard blocker** | B literally cannot run until A is Done | Issue link `is blocked by` | Workflow validator (§10) |
| **(b) Soft prereq** | Better after A but B can start with mock | Issue link `relates to` + description note | Hint only, runner reads but doesn't gate |
| **(c) Mutex** | A and B touch same resource (file, DB row, alembic chain head) | Label `mutex:<resource-id>` | Runner pre-pickup check (§16) |
| **(d) Schema lock-in** | B imports / depends on A's contract (enum, schema, function signature) | Description YAML `schema_locks` + cross-ADR reference | CI drift guard test |
| **(e) Live-state requires** | Repo / runtime state must be specific value (alembic head, feature flag, deployed file) | Description YAML `live_state_requires` | Runner pre-pickup live check (§13) |
| **(f) External / human action** | Requires human intervention (DNS, secret rotation, GCP project setup) | Status `Waiting for External` + assignee = operator | Status filter (bot JQL excludes this status) |

### Why 6 types matter

Mixing them produces ambiguous semantics. "Relates to BP.B" — is it (a), (b), or (d)? Without the distinction, runner cannot decide whether to gate, hint, or schema-check. This taxonomy is the foundation for the 4 enforcement layers in §10.

---

## §9 Prerequisites YAML schema

Every Story description MUST contain a `## Prerequisites` section with a YAML block. Empty sections are still required (machine-readable signal that the author considered dependencies).

````markdown
## Prerequisites

```yaml
blocks_on: []                       # (a) hard blockers
soft_prereqs: []                    # (b) hints
mutex_with: []                      # (c) shared resources
schema_locks: []                    # (d) upstream contracts
live_state_requires: []             # (e) repo / runtime state
external_blockers: []               # (f) human action gates
```
````

### Filled example

```yaml
blocks_on:
  - OP-1234   # MP.W0.1 schema must be Done before this can compile

soft_prereqs:
  - OP-1240   # ideally after MP.W2 baseline data lands

mutex_with:
  - mutex:backend/auth.py
  - mutex:alembic-chain-head

schema_locks:
  - source_priority: BP.B
    target: backend/agents/guild_registry.py
    contract: enum_value_list
    drift_guard_test: tests/test_guild_registry_byte_equal.py

live_state_requires:
  - alembic_head: "0198"
  - feature_flag: "OMNISIGHT_MP_ENABLED=false"
  - file_exists: "backend/agents/provider_orchestrator.py"
  - command_succeeds: "python3 -c 'from backend.agents import guild_registry'"

external_blockers:
  - operator_action: "GCP project setup per docs/operations/byog-setup.md"
  - approval_required: tier:X    # ADR 0005 authority gate
```

### Why YAML in Markdown

- Machine-parseable (runner / CI consume via PyYAML)
- Human-readable in JIRA UI
- Diff-friendly in comment history
- No custom JIRA field required (free-tier compatible)

### Field reference

- `blocks_on`: list of JIRA ticket keys. Workflow validator (§10) gates pickup.
- `soft_prereqs`: list of ticket keys. No gating, just hint.
- `mutex_with`: list of `mutex:<resource-id>` strings. Runner queries siblings (§16).
- `schema_locks`: list of objects with `source_priority`, `target`, `contract`, `drift_guard_test`. CI test must exist and pass.
- `live_state_requires`: list of single-key objects, one per check kind (§13).
- `external_blockers`: list of objects with `operator_action` (free text) or `approval_required` (tier label).

---

## §10 Workflow states + 4-layer enforcement

### State machine

```
   TODO ──pickup──> In Progress ──push──> Under Review ──+2──> Approved ──merge──> Published ──release──> Archived
                          │                     │
                          └──changes-requested──┴───changes-requested──> In Progress
                          │
                          └──split-proposed──> TODO (sub-tasks created)
                          │
                          └──discovered-dep──> TODO (with comment)
                          │
                          └──auto-revert (zombie)──> TODO (per §15)

   TODO ──external-block──> Waiting for External ──unblock──> TODO
```

### State definitions

| State | Meaning | Enter condition |
|---|---|---|
| TODO | Ready for pickup | Created, or returned from In Progress |
| In Progress | Bot has picked up, working | Runner sets assignee = self |
| Under Review | Code on Gerrit / PR open | Runner pushes commits |
| Approved | +2 received (1 human + 1 AI per ADR 0003) | Reviewer transition |
| Published | Merged to develop / main per ADR 0001 | Gerrit submit hook |
| Archived | Release shipped or manually closed | Operator transition |
| Waiting for External | Human action required (§8 type f) | Operator transition |

### 4-layer dependency enforcement

| Layer | Catches | Mechanism |
|---|---|---|
| 1. JIRA workflow validator | Hard blocker (a) | `is blocked by` issue not Done → cannot transition TODO → In Progress |
| 2. Runner pre-pickup check | Mutex (c), Live-state (e) | Runner queries DB / runs commands before transitioning; aborts on mismatch |
| 3. CI drift guard | Schema lock-in (d) | PR-merge gate runs `tests/test_*_byte_equal.py` etc. |
| 4. Status routing | External (f) | Bot's pickup JQL excludes `status = "Waiting for External"` |

Each layer is independent — defense-in-depth. If layer 1 misses (e.g. a `blocks_on` entry was forgotten), layer 2 may still catch via live-state mismatch. Soft prereq (b) is intentionally not enforced; it's a hint only.

### Workflow validator config (JIRA)

In Atlassian Cloud workflow editor:
1. Edit OP project workflow
2. Add transition `TODO → In Progress`
3. Add validator: `Issue Link Status` — fails if any `is blocked by` link target's status ∉ {`Done`, `Published`, `Archived`}
4. Save + publish workflow

Test by attempting transition on a ticket with an open `is blocked by` link — should fail with operator-visible error.

---

## §11 Discovered-dependency protocol

When agent finds a dependency mid-execution that wasn't in Prerequisites block:

| Discovery type | Agent self-add allowed? | Procedure |
|---|---|---|
| `relates_to` (pure reference) | ✓ | Add link directly |
| `soft_prereqs` | ✓ | Add to YAML, comment notes the find |
| `live_state_requires` missing entry | ✓ | Add to YAML + comment + auto-retry on next polling cycle if state matches |
| `mutex_with` missing entry | ✗ | Comment proposal, transition back to TODO, operator confirms |
| `blocks_on` (hard) | ✗ | Comment proposal, transition back to TODO, operator + O7 confirm |
| `schema_locks` missing entry | ✗ | Comment proposal + ADR amendment may be needed |

### Comment template

```
[runner-discovered-dependency] @<operator-handle>

While executing this ticket I discovered a dependency not declared in
the Prerequisites block:

Type: <blocks_on | mutex_with | schema_locks>
Target: <ticket key | resource ID | source priority>
Evidence: <file path:line | error trace | log link>

Per ticket-conventions §11, this dependency type cannot be added by
runner unilaterally. Transitioning back to TODO. Please review,
amend Prerequisites block, then I will re-pickup on next polling cycle.

—— auto-runner-codex.py @ <git-sha>
```

### Why hard categories cannot be self-added

`blocks_on` / `mutex_with` / `schema_locks` change execution semantics and other-ticket pickup logic. Allowing AI self-add = authority leakage, analogous to sub-task split bypassing tier:X (§6). The principle is consistent: AI may *propose* changes that affect other agents' work, but humans confirm before they take effect.

---

## §12 Cycle detection

A `blocks_on` graph cycle is fatal: A blocks B blocks C blocks A → all three permanently locked.

### Audit script

`scripts/jira_prereq_audit.py` (operator-runnable, read-only):

- Fetch all tickets in OP project
- Build directed graph from `blocks_on` links + Prerequisites YAML
- DFS for cycles → report
- Detect broken refs (target ticket doesn't exist / Archived)
- Detect stale schema_locks (referenced ADR is Superseded)
- Output: human-readable report + machine-readable JSON

```
$ python3 scripts/jira_prereq_audit.py
=== Prereq audit report (2026-05-06) ===
Tickets scanned: 191

[ERR] Cycle detected:
  OP-104 → OP-108 → OP-115 → OP-104

[WARN] Broken reference:
  OP-130 blocks_on: OP-9999 (does not exist)

[WARN] Stale schema_lock:
  OP-145 references ADR-0003 contract, but ADR-0003 is Superseded by ADR-0009

Cycles: 1 / Broken: 1 / Stale: 1 / Healthy: 188
```

### CI contract test

`backend/tests/test_jira_prereq_integrity.py`:

- Calls audit script in JSON mode
- Fails CI if `cycles > 0` or `broken_refs > 0`
- Stale schema_locks → warning, not fail (transition period)

PR that introduces a cycle is blocked at merge time.

---

## §13 Live-state check engine

Generic dispatcher for `live_state_requires` checks. Replaces the hardcoded `_current_alembic_head()` probe in `auto-runner-codex.py`.

### Module: `backend/agents/live_state_check.py`

```python
@dataclass(frozen=True)
class CheckResult:
    passed: bool
    detail: str

CHECK_KINDS: dict[str, Callable] = {
    "alembic_head":     _check_alembic_head,
    "feature_flag":     _check_feature_flag,
    "file_exists":      _check_file_exists,
    "command_succeeds": _check_command_succeeds,
    "db_row_count":     _check_db_row_count,
    "deployed_version": _check_deployed_version,
    # add more by registering here
}

def evaluate(requirements: list[dict]) -> list[CheckResult]:
    """Dispatch each requirement to its handler. Order-independent."""
```

### Built-in check kinds

| Kind | Argument | Pass condition |
|---|---|---|
| `alembic_head` | `"0198"` (string) | `alembic heads` output equals argument |
| `feature_flag` | `"OMNISIGHT_MP_ENABLED=false"` | env / DB feature flag matches |
| `file_exists` | `"path/to/file"` | file exists at repo root |
| `command_succeeds` | shell command string | exit code 0 |
| `db_row_count` | `{"table": "users", "min": 1}` | row count satisfies range |
| `deployed_version` | `"v0.4.0"` | running service reports matching version |

### Adding a new check kind

1. Implement handler in `live_state_check.py`
2. Register in `CHECK_KINDS` dict
3. Add unit test in `tests/test_live_state_check.py`
4. Document in this section

No need to amend ticket conventions doc itself — handlers are pluggable.

### Behavior on failure

Runner pre-pickup:
- All requirements pass → proceed to assign + transition
- Any fail → comment ticket with check result, leave in TODO

Comment template:

```
[runner-live-state-fail]

Pre-pickup live-state check failed; not picking up.

  - alembic_head: expected "0198", found "0197"

This ticket will be retried on next polling cycle. If the live state
will not change (e.g. expected value is stale), please amend the
Prerequisites block.

—— auto-runner-codex.py @ <git-sha>
```

---

## §14 Tier drift retrospective trigger

When ticket transitions to `Published`, the drift detector computes:

```
ratio = actual_hours / tier_target_upper_bound

tier:S target_upper = 3
tier:M target_upper = 12
tier:L target_upper = 72   (3 days × 24)
tier:X target_upper = ∞    (already escalated, no drift signal)
```

`actual_hours` = wall time between transition `TODO → In Progress` and `Under Review → Approved`, minus pause time. `Under Review → In Progress` (changes-requested) cycles count as work; queue time doesn't.

### Drift thresholds

| Ratio | Outcome |
|---|---|
| 0.5 ≤ ratio ≤ 1.5 | Normal — no action |
| ratio > 1.5 | **Over-run** — auto-create retrospective ticket |
| ratio < 0.5 | **Over-tier** — auto-create retrospective ticket |
| ratio > 3.0 | **Severe over-run** — also flag parent epic for review |

### Retrospective ticket auto-create

Triggered by `scripts/jira_drift_detector.py` (daily cron):

```yaml
Component: META
Issue Type: Story
Summary: "Retro [drift:over-run 2.4x]: OP-1234 MP.W1.1 orchestrator"
Labels: meta:retrospective, drift:over-run, tier:S
Linked: caused-by OP-1234

Description: |
  ## Retrospective for OP-1234

  ### Drift signal
  - Tier target: tier:M (≤ 12 hour)
  - Actual: 29 hour
  - Ratio: 2.4x over target

  ## Required structured fields
  (Source-ticket agent fills first pass; reviewer +1 to Approve)

  ```yaml
  situation: |
    # what was the ticket supposed to be?
  divergence: |
    # what actually happened, factually (not interpretation)?
  root_cause: |
    # 5-why or equivalent. Surface symptoms don't count.
  contributing: |
    # secondary factors (context window? undocumented dep? new area?)
  concrete_fix: |
    # specific change to convention / tooling / SOP.
    # vague answers like "be more careful" are auto-rejected.
  verification: |
    # how will we know the fix worked?
    # typically: next similar ticket's drift ratio
  ```
```

### Retrospective ticket workflow rules

- Source agent (whoever was assignee on triggering ticket) gets initial assignment
- Cannot transition to `Approved` without 1 +1 from non-source agent (no self-review)
- Cannot transition to `Approved` if any structured field is empty / matches `(?i)be more careful|pay attention|try harder`
- **Blocks pickup of next-similar ticket** (same `class:` + same `area:`) until retro is `Published` — enforced by runner pre-pickup check, with comment explaining the block

This last rule is the teeth: lessons must be ingested before the same agent attempts a similar ticket, otherwise drift repeats.

---

## §15 In Progress SLA — observation-then-threshold

### Phase 0 (now → 4 weeks): observation

- Runner emits `time_in_progress` metric to `metrics/jira_ticket_lifecycle.jsonl`
- Each entry: `{ticket: OP-1234, in_progress_started_at, in_progress_ended_at, transitions: [...], commits: [...], comments: [...]}`
- No hard enforcement during observation
- **Soft warnings at intuitive breakpoints**:
  - 7 days In Progress + no new commit/comment → comment "been In Progress 7d, still active?"
  - 14 days → @-mention operator
  - 30 days → hard auto-revert: transition to TODO + comment "auto-reverted, please re-pickup"

### Phase 1 (after 4 weeks): operator threshold review

- Operator reads `metrics/jira_ticket_lifecycle.jsonl`, computes p50 / p90 / p95
- Sets hard threshold = p95 × 1.5 in `config/ticket_lifecycle_sla.yaml`:

```yaml
# config/ticket_lifecycle_sla.yaml
phase: 1   # 0 = observation, 1 = enforced
hard_revert_days: 21
soft_warning_days: 7
mention_at_days: 14
zombie_archive_multiplier: 2.0   # > 2× hard_revert → archive + retrospective
```

### Phase 2 (steady state): enforce

- Beyond `hard_revert_days` → automatic revert to TODO
- Beyond `hard_revert_days × zombie_archive_multiplier` (clear zombie) → transition to `Archived` + flag as abandoned
- Abandoned tickets require META retrospective before archiving (use cron, not silent)

### Defining "no progress"

Effective progress signals (any one resets the clock):
- New commit referencing `OP-XXXX` in message
- JIRA comment from non-bot account
- JIRA field modification (description / labels / etc.)

Non-effective signals (don't reset):
- Runner pickup heartbeat
- Status check from monitoring
- Mechanical retry comment

---

## §16 Cross-priority scheduler weights

Runner pickup is NOT pure FIFO. Score each candidate; pick highest score that passes pre-pickup checks.

### Score formula

```
score = priority_weight                              # base
      + min(downstream_blocked × 5, 30)              # unblock_score (cap)
      + (10 / max(days_to_fix_version, 1))           # deadline_pressure
      + (log10(days_since_created + 1) × 3)          # age_bonus
      − (50 if mutex_in_progress_elsewhere else 0)   # mutex_pressure
```

### Config: `config/scheduler_weights.yaml`

```yaml
# Operator-tunable. Runner re-reads on every dispatch loop.
priority_weights:
  MP: 100
  RPG: 80
  META: 90       # retrospective + lessons must not starve
  FX2: 60
  BP: 40
  WP: 40
  KS: 50
  Q: 30
  # default for un-listed Component:
  default: 50

bonuses:
  per_downstream_unblock: 5
  max_unblock_bonus: 30
  deadline_pressure_coefficient: 10
  age_bonus_coefficient: 3

penalties:
  mutex_in_progress: 50
```

### Runner dispatch loop

```python
candidates = jira_query(jql_for_class)  # top-50 by JIRA's ORDER BY priority DESC, created ASC
scored = [(score(t, weights), t) for t in candidates]
scored.sort(reverse=True)
for s, ticket in scored:
    if pre_pickup_checks(ticket):  # mutex + live-state + blocker
        return pickup(ticket)
# all candidates failed checks → idle, retry next polling cycle (default 30s)
```

### Pickup JQL (per agent_class)

```sql
project = OP
  AND status = "TODO"
  AND assignee is EMPTY
  AND labels = "class:subscription-codex"     -- or class:api-anthropic, etc.
  AND status != "Waiting for External"
ORDER BY priority DESC, created ASC
```

### Why each component

| Component | Why included |
|---|---|
| `priority_weight` | Operator's strategic priority |
| `unblock_score` | A ticket unblocking 5 others is more valuable than one unblocking 0 |
| `deadline_pressure` | Closer to fix_version target → urgency |
| `age_bonus` | Prevents starvation of low-priority tickets |
| `mutex_pressure` | Avoids parallel work on same resource |

### Phase 0 / Phase 1 / Phase 2

Same observation-then-tune pattern as §15:
- Phase 0 (4 weeks): use defaults, log dispatch decisions
- Phase 1: operator reviews dispatch log, tunes weights
- Phase 2: weights stable, contract test pins them

### Tests

`backend/tests/test_scheduler.py`:
- Score calculation determinism (same input → same score)
- Weight YAML schema validation
- Dispatch decision golden test (fixture tickets → expected pickup)
- **Starvation regression**: a `priority_weight: 30` ticket created 30 days ago must eventually win over a `priority_weight: 100` ticket created today

---

## §17 META Component

### Purpose

`META` Component is for process work: retrospectives, lessons-learned, governance changes, tooling improvements, ticket-system iteration.

### Sub-types via labels

| Label | Use |
|---|---|
| `meta:retrospective` | Auto-created by drift detector (§14) or manually for incidents |
| `meta:lessons-learned` | Adding entry to `docs/sop/lessons-learned.md` |
| `meta:tooling` | Runner / scripts / helpers improvement |
| `meta:governance` | ADR amendments, SOP rewrites, CLAUDE.md changes |
| `meta:onboarding` | Confluence wiki / onboarding doc work |
| `meta:audit` | New audit cycle planning + execution |

### Inherits all conventions

META tickets follow ALL ticket conventions:
- Tier (most retrospectives are `tier:S`, governance amendments often `tier:M`)
- Class (typically `class:subscription-claude` for narrative work, `class:subscription-codex` for tooling)
- Area (`area:docs` typical; `area:tooling` for runner work)
- Prerequisites YAML
- Lifecycle workflow (§10)

### Special workflow rules

- **Retrospective tickets** (`meta:retrospective`):
  - Cannot transition `Approved` without +1 from non-source agent (no self-review)
  - Cannot transition `Approved` with empty / vague structured fields (§14)
  - Block similar-class+area pickup until Published

- **Governance tickets** (`meta:governance`):
  - `Approved` requires operator +1 explicitly (cannot be 2× AI bot +1)
  - Linked ADR / SOP file change must include the ticket key in commit message

### Priority weight

`META: 90` (per §16) — retrospectives and lessons-learned must not be starved by feature work. Per ADR 0008, agents level up partly through teaching others (RPG.W12.6); META work is the operational manifestation of that.

---

## §18 Component complete list

### Active priority namespaces

```
MP        Multi-Provider Subscription Orchestrator
RPG       Agent Class & Skill Leveling System
FX2       Audit Findings Fix v2 (2026-05-06 deep audit)
FX        Audit Findings Fix (2026-05-03, mostly Done)
BP        Blueprint V2 Enterprise Multi-Agent Software Factory
WP        Warp-inspired Patterns
KS        Multi-Tenant Secret Management
KS-early  Auth Hardening early phase
KS-rest   Auth Hardening rest phase
Q         Multi-Device Parity
Z         LLM Provider Observability
ZZ        Claude-Code-Style Agent Observability
Y         Tenant Ops & Multi-Project Hierarchy
Y-prep    Gerrit + JIRA Integration prep
N         Dependency Governance
O         Enterprise Event-Driven Multi-Agent Orchestration
W         Web Platform Vertical
P         Mobile App Vertical
X         Pure Software Application Vertical
L         Bootstrap Wizard
BS        Bootstrap Vertical-Aware Setup
AS        Auth & Security Shared Library
CL        Commercial Launch
L4        Beyond-Commercial Excellence
L5        Category-Defining R&D
AB        Anthropic API + Batch Mode
FS        Full-Stack Web Application Generation
SC        Security Compliance
HD        Hardware Design Verification
HE        Hardware Engineering Expansion (deferred)
G         Ops / Reliability
H         Host-aware Coordinator
V         Visual Design Loop + Workspace Architecture
R         Enterprise Watchdog & Disaster Recovery
S2        Security Hardening Phase 2
D         L4 Layer B (per-product skill packs)
E         L4 Layer C (software tracks)
F         (legacy meta — new META work uses META Component below)
T         Billing & Payment Gateway
J         Multi-session Single-user Hardening
S         Shared Auth/Session Foundation
I         Multi-tenancy Foundation
M         Resource Hard Isolation
```

### Meta namespace

```
META  Process / retrospective / lessons-learned / governance / tooling / onboarding
```

### Adding a new Component

1. Open META ticket with `meta:governance` label
2. Justify why a new top-level priority is needed (vs fitting into existing one)
3. Operator + 1 reviewer +1
4. Update this section (§18) and `config/scheduler_weights.yaml`
5. Drift guard test (`tests/test_jira_component_list.py`) compares this list to JIRA's actual Component list, fails on divergence

---

## Appendix A — Worked example: MP.W1.1 in JIRA

```
─────────────────────────────────────────────────────────────
[OP-XXXX] MP.W1.1 — provider_orchestrator.py central registry
─────────────────────────────────────────────────────────────
Type: Story            Component: MP            Status: TODO
Epic: MP.W1            Fix Version: v0.4.0      Priority: High
Assignee: <empty>      Reporter: nanakusa-sora
Labels: class:api-anthropic, tier:M, area:backend, area:db

Description:

## Goal
Central registry of `ProviderAdapter` + routing + circuit breaker.
Foundation for cap-aware multi-provider dispatch (per ADR-0007).

## Acceptance Criteria
- [ ] `ProviderAdapter` ABC defined with register / dispatch / health hooks
- [ ] Anthropic + OpenAI subscription adapter shells wired
- [ ] Circuit breaker trips on 5 consecutive 429
- [ ] All CI green (drift guards, contract tests)

## Files / Paths
- backend/agents/provider_orchestrator.py (new, ~250 LOC)
- backend/tests/test_provider_orchestrator.py (new, ~30 test)

## Spec references
- ADR-0007 §Architecture: Provider Orchestrator
- TODO.md MP.W1.1 (will be Archived after JIRA migration)

## Prerequisites

```yaml
blocks_on:
  - OP-XXX1   # MP.W0 agent_class schema (Done check via workflow validator)

soft_prereqs: []

mutex_with:
  - mutex:alembic-chain-head

schema_locks: []

live_state_requires:
  - alembic_head: "0198"
  - command_succeeds: "python3 -c 'import yaml; yaml.safe_load(open(\"config/agent_class_schema.yaml\"))'"

external_blockers: []
```

## Definition of Done
- [ ] feature/OP-XXXX-mp-w1-1-orchestrator branch
- [ ] Tests pass locally + CI
- [ ] Gerrit Code-Review +2 (1 human + 1 AI per ADR-0003)
- [ ] Commit message contains `[OP-XXXX]`
- [ ] Merge to `develop` (per ADR-0001)

## Runner notes
- agent_class hint: api-anthropic
- tier: M (4–12 hour scope)
- worktree: claude-work
─────────────────────────────────────────────────────────────
```

---

## Appendix B — Worked example: META retrospective

```
─────────────────────────────────────────────────────────────
[OP-YYYY] Retro [drift:over-run 2.4x]: OP-1234 MP.W1.1 orchestrator
─────────────────────────────────────────────────────────────
Type: Story            Component: META          Status: TODO
Epic: <none>           Fix Version: v0.4.0      Priority: High
Assignee: claude-bot   Reporter: drift-detector
Labels: meta:retrospective, drift:over-run, tier:S, area:docs
Linked: caused-by OP-1234

Description:

## Retrospective for OP-1234

### Drift signal (auto-filled)
- Tier target: tier:M (≤ 12 hour)
- Actual: 29 hour
- Ratio: 2.4x over target

## Required structured fields

```yaml
situation: |
  # Source agent fills.
divergence: |
  # ...
root_cause: |
  # ...
contributing: |
  # ...
concrete_fix: |
  # Required: specific change to convention / tooling / SOP.
  # Vague answers auto-rejected.
verification: |
  # ...
```

## Prerequisites

```yaml
blocks_on: []
soft_prereqs:
  - OP-1234   # the ticket this retro is about
mutex_with: []
schema_locks: []
live_state_requires: []
external_blockers:
  - approval_required: non-source-agent-plus-one
```
─────────────────────────────────────────────────────────────
```

---

## Acceptance for this SOP

This document is **Draft** as of 2026-05-06. Acceptance requires:

1. Operator review of all 18 sections + appendices
2. 3 example tickets opened in JIRA OP project (one MP, one RPG, one META retrospective)
3. UI verification: each example renders correctly in JIRA list view, board view, and detail view
4. Initial `config/scheduler_weights.yaml`, `config/ticket_lifecycle_sla.yaml` committed
5. Skeleton (signatures only) for `scripts/jira_prereq_audit.py`, `scripts/jira_drift_detector.py`, `backend/agents/live_state_check.py`, `backend/agents/scheduler.py`
6. Skeleton tests for `backend/tests/test_jira_prereq_integrity.py`, `backend/tests/test_scheduler.py`
7. CLAUDE.md L1 amendment for HANDOFF.md replacement (§7)
8. `docs/sop/lessons-learned.md` created with seed entries (lesson #1-6 from governance migration plan)

After acceptance, status transitions to `Accepted`. Future amendments require META `meta:governance` ticket per §17.
