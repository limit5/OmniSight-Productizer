---
id: SPRINT-S12-PHASE-31A-SPEC
version: v2 (post-codex review)
title: Sprint S12 Phase 31.A — Dev WSL Ubuntu-26.04 One-Button Provisioning · Ticket Spec
scope: Phase 31.A only (Dev WSL provisioning); 37 atomic tickets (36 × tier:S + 1 × tier:L)
status: Draft (2026-05-13)
related:
  - ADR-0023 (Foundation Rebuild) §4 31.A
  - SOP-S12-TICKET-DECOMP (5 patterns + 2 meta-patterns + 10 anti-patterns)
  - v1 of this spec (13-ticket version, superseded)
  - Codex independent review (2026-05-13, captured at /tmp/phase31a-codex-review-output.txt)
---

# Sprint S12 Phase 31.A · Dev WSL Ubuntu-26.04 One-Button Provisioning — Ticket Spec v2

## §0. v2 Changelog (vs v1)

**Operator decision (A 案 post codex review):**
- v1 had 13 tickets (mix S/M/L). v2 has **37 tickets (36 × tier:S + 1 × tier:L Integration)**.
- v2 absorbs codex's 4 ticket mergers, drops 1 invalid dependency, expands range-notation blockedBy to explicit links.
- v2 introduces **`boundaries:` schema** (10 fields) on every ticket.
- v2 introduces **`context_hint:` block** (6 lines) on every tier:S ticket — addresses codex's "I'd lose the big picture on ticket #23 in isolation" risk.
- v2 answers all 8 open questions from v1 §5.

**v1 → v2 ticket merges (4):**
| v1 IDs | v2 ID | Codex reason |
|---|---|---|
| 4b + 4c | 4b | symlink/state init needs launcher.sh content |
| 5c + 5d | 5c | head verify needs runner alembic invocation knowledge |
| 9a + 9b | 9a | paused guard needs session naming knowledge |
| 12b + 12c | 12b | caching needs assertion shape knowledge |

**v1 → v2 dependency fixes:**
| Change | Reason |
|---|---|
| Dropped `6a → 5d` | Cognee doesn't need backend DB migration |
| Added `5a → 1a + docker-group-ready check (inline in 5a AC)` | docker-compose needs daemon access |
| Expanded `11a blockedBy:1-10` → 12 explicit links | JIRA doesn't accept range notation |
| Strong boundaries on 11c | Codex flagged "extreme drift risk on state-machine logic" |

---

## §1. Pre-flight reading order

1. ADR-0023 §4 31.A (1 page)
2. SOP-S12-TICKET-DECOMP §2-§6 (the 5 patterns + OP-1042 hook)
3. Existing `scripts/setup-dev-env.sh` (213 LOC baseline; to be **renamed to `.deprecated.sh` + leave redirect message**)
4. This spec §3 (boundaries) + §4 (context hint) before §6 (per-child specs)

---

## §2. Sub-META definition (unchanged from v1)

```yaml
key:        Sprint S12 — Phase 31.A (sub-META)
summary:    "AUDIT-31.A: Dev WSL Ubuntu-26.04 one-button provisioning"
type:       Story (ストーリー per AUDIT-27 locale alias)
sprint:     "S12: Bedrock" (sprint id 52)
parentMeta: Sprint S12 META
fixVersion: v0.5.0-rc2
labels:
  - area:devops, area:tooling, area:docs, area:tests
  - tier:L (sub-META is roll-up; children carry their own tier)
  - type:meta
  - agent:auto
  - scope:sprint-s12, phase:31.A
  - adr:0023
  - "capability:enable=gerrit_push, capability:enable=code_edit, capability:enable=jira_update, capability:enable=run_lint, capability:enable=run_tests"
```

**Scope statement**: From a clean Ubuntu-26.04 WSL, run `scripts/setup-dev-env-full.sh` once → working dev environment in <30 min with no manual prompts beyond cred secret values.

**Sub-META AC** (Pattern 2 — closes only when all 37 children close):
1. Code: master orchestrator + 10 component scripts + validation suite all on develop
2. Deploy: 37 children all closed
3. Integration: 31.A-Integration ticket closes
4. Exercised: 24-h post-Integration health snapshot stays green

**Go-Live**: T+4-5 wk (C4) / 5-6 wk (C2) / 7-8 wk (C1)

**Capacity routing**: 22 tickets to codex, 15 to claude (orchestration, docs, judgement-heavy creds, tests)

---

## §3. `boundaries:` schema (NEW, applies to every child)

Every ticket carries a `boundaries:` block in its JIRA description (NOT as labels). Filing-time hook (OP-1042) validates YAML schema. Runtime AC enforces.

### §3.1 Schema fields

```yaml
boundaries:
  loc_delta_max: int          # tripwire warn level; hard stop = 2× this; see §3.3
  files_touched_max: int       # hard cap on files modified
  required_paths: [str, ...]   # whitelist — ticket must exclusively touch these
  forbidden_paths: [str, ...]  # paths that MUST NOT be touched even within an allowed area
  non_goals: [str, ...]        # 2-4 explicit "this ticket does NOT do X" bullets
  interface_contract:
    inputs_from_deps: [str, ...]      # filenames / env vars / state-keys this ticket consumes
    outputs_for_downstream: [str, ...]  # filenames / env vars / state-keys this ticket guarantees
  test_scope: enum             # defer-to-integration | inline | none
  destructive_ops_allowed: bool
  dependency_artifacts: [str, ...]  # explicit files/state-keys prior tickets produced
  execution_mode: enum         # structural-only | unit-testable | requires-WSL | operator-rehearsal
  on_scope_creep: file-followup  # constant; documents the rule
  scope_summary_max_chars: 500   # constant
```

### §3.2 Field rationale (codex-driven)

- `required_paths` + `non_goals` = primary drift control (codex Q4 finding: most useful)
- `interface_contract` = solves the cross-ticket contract risk (codex Q5 finding: "riskiest area is not implementation size; it's cross-ticket contracts")
- `destructive_ops_allowed: false` default on every devops/provisioning ticket
- `execution_mode` distinguishes structural validation (grep for command presence) from real execution (`requires-WSL`) — most 31.A tickets are `structural-only` because real run is in Integration ticket
- `dependency_artifacts` makes implicit deps explicit (prevents codex from "patching missing dependency locally" Q4 anti-pattern)
- **Removed `primary_area`** — codex Q4: redundant with `required_paths`

### §3.3 LOC tripwire semantics

`loc_delta_max` is a TRIPWIRE (warn), not a hard ceiling:
- Diff ≤ `loc_delta_max` → green
- `loc_delta_max` < diff ≤ 2× `loc_delta_max` → yellow warn in CI; reviewer judgement
- Diff > 2× `loc_delta_max` → hard fail; runner must file follow-up + revert

Shell scripts with `set -euo pipefail` + dry-run + error handlers legitimately consume 60-100 LOC. So budgets are set generously (~80 for typical S, ~120-200 for state-machine S like 11a/11c/11f and YAML-heavy S like 2a).

### §3.4 Boundary AC verify block (auto-generated)

Every ticket AC includes a synthetic boundary check:

```yaml
- desc: "Boundary: files_touched_max enforcement"
  verify:
    command: "git diff --name-only HEAD~1 HEAD -- $(yq '.boundaries.required_paths | join(\" \")' DESCRIPTION_YAML) | wc -l"
    expect_stdout_match: "^[0-{files_touched_max}]$"
- desc: "Boundary: forbidden_paths untouched"
  verify:
    command: "git diff --name-only HEAD~1 HEAD -- $(yq '.boundaries.forbidden_paths | join(\" \")' DESCRIPTION_YAML) | wc -l"
    expect_stdout_match: "^0$"
- desc: "Boundary: loc_delta tripwire"
  verify:
    command: "git diff --stat HEAD~1 HEAD | tail -1 | awk '{print $4+$6}'"
    expect_stdout_match: "^[0-9]{1,3}$"  # ≤ 2× loc_delta_max
```

OP-1042 schema YAML extension (separate ticket) generates these verifies from the `boundaries:` block at filing time.

---

## §4. `context_hint:` block schema (NEW, applies to every tier:S child)

Codex Q5 finding: "If I picked up ticket #23 in isolation, I'd know the local action but not the overall provisioning story." Fix: 6-line preamble in every tier:S ticket.

### §4.1 Schema

```yaml
context_hint:
  phase_goal: |
    "Fresh Ubuntu-26.04 WSL → run scripts/setup-dev-env-full.sh once → working dev env <30min"
  this_ticket_produces: "what file/state this ticket guarantees"
  consumed_by: "which downstream ticket(s) read this output"
  inputs_expected: "what this ticket reads from prior tickets (filenames/env/state-keys)"
  outputs_guaranteed: "format-specific guarantee for downstream consumption"
  non_goals: ["explicit 2-4 bullets of what this ticket does NOT do"]
```

### §4.2 Why duplicate `non_goals` in both context_hint and boundaries?

`context_hint.non_goals` is the human-facing "why this is out of scope" narrative.
`boundaries.non_goals` is the same content reformatted for the filing-time hook.

OP-1042 hook will check: every `context_hint.non_goals` item is mirrored in `boundaries.non_goals` (or vice versa). Single source of truth via post-filing reconciliation.

---

## §5. Children — overview table (37 tickets)

DAG dependency layers shown. Filing batch order = layer order.

| Layer | # | ID | Title | Tier | Prefer | blockedBy |
|---|---|---|---|---|---|---|
| L0 | 1 | 31.A-1a | apt base packages install | S | codex | — |
| L0 | 2 | 31.A-2a | Cred YAML schema definition | S | claude | — |
| L0 | 3 | 31.A-8a | dev-subset.txt curation (25/84 list) | S | claude | — |
| L1 | 4 | 31.A-1b | nvm + Node LTS install | S | codex | 1a |
| L1 | 5 | 31.A-1c | pnpm + Python venv | S | codex | 1a |
| L1 | 6 | 31.A-2b | Cred script — read YAML + prompt operator | S | claude | 2a |
| L1 | 7 | 31.A-5a | Dev Postgres compose definition | S | codex | 1a |
| L1 | 8 | 31.A-6a | Cognee compose + freeze network/env conventions | S | codex | 1a |
| L1 | 9 | 31.A-6b | Neo4j compose (uses 6a conventions) | S | codex | 1a |
| L1 | 10 | 31.A-10a | Cross-WSL probe script | S | codex | 1a |
| L2 | 11 | 31.A-2c | SSH keypair generation (3 bots) | S | claude | 2b |
| L2 | 12 | 31.A-2d | chmod 600 hardening pass | S | codex | 2b |
| L2 | 13 | 31.A-5b | Auto-gen password + env file | S | codex | 5a |
| L2 | 14 | 31.A-6c | Graphiti compose (uses 6a conventions) | S | codex | 6a, 6b |
| L2 | 15 | 31.A-7 | GHCR docker login | S | codex | 2b |
| L2 | 16 | 31.A-10b | Cross-WSL operator runbook (doc) | S | claude | 10a |
| L3 | 17 | 31.A-2e | Cred-provisioning unit test | S | claude | 2b, 2c, 2d |
| L3 | 18 | 31.A-3a | Productizer clone (Gerrit) | S | codex | 1a, 2c |
| L3 | 19 | 31.A-3b | sora-bridge clone (Gerrit) | S | codex | 1a, 2c |
| L3 | 20 | 31.A-5c | Alembic upgrade runner + head verify (MERGED) | S | codex | 5b |
| L3 | 21 | 31.A-6d | --expose-host-debug flag impl | S | codex | 6a, 6b, 6c |
| L4 | 22 | 31.A-3c | remote + git config setup | S | codex | 3a, 3b |
| L5 | 23 | 31.A-4a | Launcher root dir creation | S | codex | 3c |
| L5 | 24 | 31.A-8b | systemd installer (symlink + daemon-reload) | S | codex | 8a, 3c |
| L6 | 25 | 31.A-4b | launcher.sh template + symlink + state init (MERGED) | S | codex | 4a |
| L6 | 26 | 31.A-8c | enable-not-start safety logic | S | codex | 8b |
| L7 | 27 | 31.A-9a | tmux session creation + paused-state guard (MERGED) | S | codex | 4b, 8c |
| L8 | 28 | 31.A-11a | Master script skeleton + arg parse + state-file schema lock | S | claude | 1a,1b,1c,2b,3c,4b,5c,6d,7,8c,9a,10a |
| L9 | 29 | 31.A-11b | --dry-run mode | S | claude | 11a |
| L9 | 30 | 31.A-11c | --resume mode (reads locked state schema) | S | claude | 11a |
| L9 | 31 | 31.A-11d | --validate mode delegate | S | claude | 11a |
| L9 | 32 | 31.A-11e | --rollback mode | S | claude | 11a |
| L10 | 33 | 31.A-11f | Orchestration sequence wiring | S | claude | 11a, 11b, 11c, 11d, 11e |
| L11 | 34 | 31.A-12a | pytest skeleton + integration mark | S | claude | 11f |
| L11 | 35 | 31.A-Integration-Doc | E2E test runbook (doc) | S | claude | 11f |
| L12 | 36 | 31.A-12b | Per-step assertion functions + result caching (MERGED) | S | claude | 12a |
| L13 | 37 | 31.A-Integration | **E2E fresh-WSL provision test** | **L** | claude | ALL 1-36 |

**Routing tally**: codex 22, claude 15.

**Critical path (sequential)**: 1a → 2b → 2c → 3a → 3c → 4a → 4b → 9a → 11a → 11b → 11c → 11d → 11e → 11f → 12a → 12b → Integration-Doc → Integration. ~17 sequential hops. With C4, much parallelism in L0-L7; critical path bottleneck is L8-L13.

---

## §6. Per-child specs

Universal labels (omitted per-ticket to save lines):
```
type:feature
scope:sprint-s12, phase:31.A
agent:auto
capability:enable=gerrit_push, capability:enable=code_edit, capability:enable=jira_update, capability:enable=run_lint, capability:enable=run_tests
parentMeta: 31.A
```

Universal `context_hint.phase_goal`: "Fresh Ubuntu-26.04 WSL → run scripts/setup-dev-env-full.sh once → working dev env <30min"

Universal `boundaries`: `on_scope_creep: file-followup`, `scope_summary_max_chars: 500`

Universal AC patterns:
- Deploy AC: "Merged to develop via Gerrit" (always)
- Integration AC: "Deferred to 31.A-Integration" (always except Integration ticket itself)
- Exercised AC: "Deferred to 31.A-Integration" (always except Integration ticket itself)

Per-ticket specs below show only the deltas.

---

### 31.A-1a — apt base packages install

```yaml
title: "AUDIT-31.A-1a: apt base packages install"
tier: S, prefer: codex
class: subscription-codex
area_labels: [area:devops, area:tooling]
blockedBy: []
context_hint:
  produces: "scripts/setup-dev-env-full/01-base-tools.sh (apt-only installer)"
  consumed_by: "31.A-1b, 31.A-1c, 31.A-11f (orchestrator wires as step 01)"
  inputs_expected: "(first in chain)"
  outputs_guaranteed: "13 apt packages installed; re-run is no-op (idempotent)"
  non_goals:
    - "DO NOT install nvm/Node — 31.A-1b owns"
    - "DO NOT install pnpm/Python venv — 31.A-1c owns"
    - "DO NOT touch master script (setup-dev-env-full.sh) — 31.A-11a owns"
boundaries:
  loc_delta_max: 80
  files_touched_max: 1
  required_paths: [scripts/setup-dev-env-full/01-base-tools.sh]
  forbidden_paths: [scripts/setup-dev-env-full.sh, scripts/setup-dev-env-full/02-*, scripts/setup-dev-env-full/0[3-9]-*]
  test_scope: defer-to-integration
  destructive_ops_allowed: false
  execution_mode: structural-only
  dependency_artifacts: []
  interface_contract:
    inputs_from_deps: []
    outputs_for_downstream: [scripts/setup-dev-env-full/01-base-tools.sh executable + idempotent]
ac.code:
  - desc: script exists + executable
    verify: {command: "test -x scripts/setup-dev-env-full/01-base-tools.sh", expect_exit_code: 0}
  - desc: installs canonical 13-package list
    verify: {command: "grep -cE 'apt-get install -y' scripts/setup-dev-env-full/01-base-tools.sh", expect_stdout_match: "[1-9]"}
  - desc: NO nvm/pnpm/venv (those are 1b/1c)
    verify: {command: "grep -cE 'nvm install|npm install -g pnpm|python3 -m venv' scripts/setup-dev-env-full/01-base-tools.sh", expect_stdout_match: "^0$"}
  - desc: --dry-run shows SKIP for already-installed
    verify: {command: "bash scripts/setup-dev-env-full/01-base-tools.sh --dry-run 2>&1 | grep -c SKIP", expect_stdout_match: "[1-9]"}
go_live: T+0.5d
```

### 31.A-1b — nvm + Node LTS install

```yaml
tier: S, prefer: codex, class: subscription-codex
area_labels: [area:devops, area:tooling]
blockedBy: [1a]
context_hint:
  produces: "scripts/setup-dev-env-full/01-node.sh"
  consumed_by: "31.A-11f orchestrator step 01b"
  inputs_expected: "apt base from 1a (curl, git available)"
  outputs_guaranteed: "nvm installed in ~/.nvm; Node LTS pinned; usable via `nvm use --lts`"
  non_goals:
    - "DO NOT install pnpm — 1c owns"
    - "DO NOT hardcode Node version (use --lts dynamic)"
    - "DO NOT modify 01-base-tools.sh"
boundaries:
  loc_delta_max: 60
  files_touched_max: 1
  required_paths: [scripts/setup-dev-env-full/01-node.sh]
  forbidden_paths: [scripts/setup-dev-env-full/01-base-tools.sh, scripts/setup-dev-env-full.sh]
  test_scope: defer-to-integration
  destructive_ops_allowed: false
  execution_mode: structural-only
  dependency_artifacts: [scripts/setup-dev-env-full/01-base-tools.sh]
  interface_contract:
    inputs_from_deps: [apt-provisioned curl/git]
    outputs_for_downstream: [~/.nvm/nvm.sh exists, `nvm current` returns lts/*]
ac.code:
  - desc: script exists + executable; uses `nvm install --lts`
    verify: {command: "grep -c 'nvm install --lts' scripts/setup-dev-env-full/01-node.sh", expect_stdout_match: "[1-9]"}
decision_points:
  - id: dp_lts_major_change
    trigger: "nvm ls-remote --lts | tail -1"
    pause_if: "major version > current pin"
    operator_label: needs-operator-decision-node-major-bump
go_live: T+0.5d
```

### 31.A-1c — pnpm + Python venv

```yaml
tier: S, prefer: codex, class: subscription-codex
area_labels: [area:devops, area:tooling]
blockedBy: [1a]
context_hint:
  produces: "scripts/setup-dev-env-full/01-pnpm-venv.sh"
  consumed_by: "31.A-11f orchestrator step 01c"
  inputs_expected: "apt base from 1a; Node from 1b (for pnpm via npm)"
  outputs_guaranteed: "pnpm installed globally; ~/.venv/omnisight-dev Python venv ready"
  non_goals:
    - "DO NOT install backend Python deps — that's part of repo-clone (3a) or master orchestrator (11f)"
    - "DO NOT modify 01-base-tools.sh / 01-node.sh"
boundaries:
  loc_delta_max: 60
  files_touched_max: 1
  required_paths: [scripts/setup-dev-env-full/01-pnpm-venv.sh]
  forbidden_paths: [scripts/setup-dev-env-full/01-base-tools.sh, scripts/setup-dev-env-full/01-node.sh]
  test_scope: defer-to-integration
  execution_mode: structural-only
  dependency_artifacts: [01-base-tools.sh, 01-node.sh]
  interface_contract:
    inputs_from_deps: [Node available]
    outputs_for_downstream: ["pnpm in PATH", "~/.venv/omnisight-dev/bin/python exists"]
ac.code:
  - desc: installs pnpm + creates venv
    verify: {command: "grep -cE 'npm install -g pnpm|python3 -m venv ~/.venv/omnisight-dev' scripts/setup-dev-env-full/01-pnpm-venv.sh", expect_stdout_match: "^[2-9]$"}
go_live: T+0.5d
```

### 31.A-2a — Cred YAML schema definition

```yaml
tier: S, prefer: claude, class: subscription-claude
area_labels: [area:devops, area:security]
blockedBy: []
context_hint:
  produces: "scripts/setup-dev-env-full/02-creds.template.yaml (full 16+ cred catalog)"
  consumed_by: "31.A-2b (reader), 31.A-2c (SSH gen), 31.A-2e (test)"
  inputs_expected: "(first in chain)"
  outputs_guaranteed: "valid YAML with 16+ entries; each entry has filename / format / description / operator_prompt"
  non_goals:
    - "DO NOT write any executable code — schema-only ticket"
    - "DO NOT implement the reader script — 2b owns"
    - "DO NOT generate SSH keys here — 2c owns"
boundaries:
  loc_delta_max: 200
  files_touched_max: 1
  required_paths: [scripts/setup-dev-env-full/02-creds.template.yaml]
  forbidden_paths: [scripts/setup-dev-env-full/02-creds.sh, scripts/setup-dev-env-full/02-ssh-keys.sh]
  test_scope: defer-to-integration
  execution_mode: structural-only
  destructive_ops_allowed: false
  dependency_artifacts: []
  interface_contract:
    inputs_from_deps: []
    outputs_for_downstream: ["YAML schema fields: filename, format, description, operator_prompt, sensitivity"]
ac.code:
  - desc: YAML parses + has >=16 cred entries
    verify: {command: "python3 -c \"import yaml; d = yaml.safe_load(open('scripts/setup-dev-env-full/02-creds.template.yaml')); assert len(d['credentials']) >= 16\"", expect_exit_code: 0}
  - desc: every entry has required fields
    verify: {command: "python3 -c \"import yaml; d = yaml.safe_load(open('scripts/setup-dev-env-full/02-creds.template.yaml')); assert all({'filename','format','description','operator_prompt'} <= set(c.keys()) for c in d['credentials'])\"", expect_exit_code: 0}
  - desc: includes new S12 creds (gitlab-*, discord-webhook-*, cosign-*, smtp-app-password)
    verify: {command: "grep -cE 'gitlab-(claude|codex)-token|discord-webhook-p[01]|cosign-private-key|smtp-app-password' scripts/setup-dev-env-full/02-creds.template.yaml", expect_stdout_match: "^[5-9]$|^1[0-9]$"}
go_live: T+0.5d
```

### 31.A-2b — Cred script (read YAML + prompt operator)

```yaml
tier: S, prefer: claude, class: subscription-claude + operator-prepare-only
area_labels: [area:devops, area:security]
blockedBy: [2a]
context_hint:
  produces: "scripts/setup-dev-env-full/02-creds.sh (interactive prompt loop)"
  consumed_by: "31.A-7 (GHCR), 31.A-11f (orchestrator), operator at 31.A-Integration"
  inputs_expected: "02-creds.template.yaml schema (16+ entries)"
  outputs_guaranteed: "~/.config/omnisight/*.{env,token,url,key} files written with operator-provided values"
  non_goals:
    - "DO NOT generate SSH keys here — 31.A-2c owns"
    - "DO NOT chmod 600 here — 31.A-2d owns"
    - "DO NOT print secret values to stdout EVER"
    - "DO NOT add unit test inline — 31.A-2e owns"
boundaries:
  loc_delta_max: 120
  files_touched_max: 1
  required_paths: [scripts/setup-dev-env-full/02-creds.sh]
  forbidden_paths: [scripts/setup-dev-env-full/02-creds.template.yaml, scripts/setup-dev-env-full/02-ssh-keys.sh, scripts/setup-dev-env-full/02-chmod-hardening.sh, scripts/setup-dev-env-full/tests/test_creds_provisioning.py]
  test_scope: defer-to-integration
  execution_mode: structural-only
  destructive_ops_allowed: false
  dependency_artifacts: [scripts/setup-dev-env-full/02-creds.template.yaml]
  interface_contract:
    inputs_from_deps: [02-creds.template.yaml entries]
    outputs_for_downstream: ["~/.config/omnisight/<filename> per template entries"]
ac.code:
  - desc: script exists; reads YAML
    verify: {command: "grep -cE 'yq |python3.*yaml.safe_load' scripts/setup-dev-env-full/02-creds.sh", expect_stdout_match: "[1-9]"}
  - desc: prompts operator (uses `read -s` for secrets)
    verify: {command: "grep -c 'read -s' scripts/setup-dev-env-full/02-creds.sh", expect_stdout_match: "[1-9]"}
  - desc: NEVER echos secret values
    verify: {command: "grep -cE 'echo.*\\$(TOKEN|PASSWORD|KEY|SECRET)' scripts/setup-dev-env-full/02-creds.sh", expect_stdout_match: "^0$"}
decision_points:
  - id: dp_existing_cred_files
    trigger: "ls ~/.config/omnisight/*.env 2>/dev/null | wc -l"
    pause_if: ">= 1"
    operator_label: needs-operator-decision-existing-creds
go_live: T+1d after 2a
```

### 31.A-2c — SSH keypair generation (3 bots)

```yaml
tier: S, prefer: claude, class: subscription-claude + operator-prepare-only
area_labels: [area:devops, area:security]
blockedBy: [2b]
context_hint:
  produces: "scripts/setup-dev-env-full/02-ssh-keys.sh (ed25519 gen for claude-bot, codex-bot, merger-bot)"
  consumed_by: "31.A-3a/3b (Gerrit clone), operator at Integration (upload pubkey to Gerrit)"
  inputs_expected: "operator confirmed cred dir exists from 2b"
  outputs_guaranteed: "~/.ssh/id_ed25519_{claude,codex,merger}-bot{,.pub} (6 files, mode 600/644)"
  non_goals:
    - "DO NOT modify ~/.ssh/config — that's part of 3c"
    - "DO NOT auto-upload pubkeys to Gerrit — operator-only at Integration"
    - "DO NOT generate keys for any other bot/user identities"
boundaries:
  loc_delta_max: 50
  files_touched_max: 1
  required_paths: [scripts/setup-dev-env-full/02-ssh-keys.sh]
  forbidden_paths: [scripts/setup-dev-env-full/02-creds.sh, scripts/setup-dev-env-full/02-chmod-hardening.sh]
  test_scope: defer-to-integration
  execution_mode: structural-only
  destructive_ops_allowed: false
  dependency_artifacts: []
  interface_contract:
    inputs_from_deps: []
    outputs_for_downstream: ["~/.ssh/id_ed25519_<bot>{,.pub} (3 keypairs)"]
ac.code:
  - desc: generates 3 ed25519 keys with correct naming
    verify: {command: "grep -cE 'ssh-keygen -t ed25519.*id_ed25519_(claude|codex|merger)-bot' scripts/setup-dev-env-full/02-ssh-keys.sh", expect_stdout_match: "^[3-9]$"}
  - desc: idempotent (skips if key already exists)
    verify: {command: "grep -cE 'test -f.*id_ed25519' scripts/setup-dev-env-full/02-ssh-keys.sh", expect_stdout_match: "[1-9]"}
go_live: T+0.5d after 2b
```

### 31.A-2d — chmod 600 hardening pass

```yaml
tier: S, prefer: codex, class: subscription-codex
area_labels: [area:devops, area:security]
blockedBy: [2b]
context_hint:
  produces: "scripts/setup-dev-env-full/02-chmod-hardening.sh"
  consumed_by: "31.A-11f orchestrator step 02d"
  inputs_expected: "Files exist in ~/.config/omnisight/ (from 2b) and ~/.ssh/id_ed25519_*-bot (from 2c)"
  outputs_guaranteed: "All cred + SSH private key files chmod 600; pub keys chmod 644"
  non_goals:
    - "DO NOT generate any new files — chmod-only pass"
    - "DO NOT modify 02-creds.sh or 02-ssh-keys.sh"
boundaries:
  loc_delta_max: 40
  files_touched_max: 1
  required_paths: [scripts/setup-dev-env-full/02-chmod-hardening.sh]
  forbidden_paths: [scripts/setup-dev-env-full/02-creds.sh, scripts/setup-dev-env-full/02-ssh-keys.sh, scripts/setup-dev-env-full/02-creds.template.yaml]
  test_scope: defer-to-integration
  execution_mode: structural-only
  destructive_ops_allowed: false
  dependency_artifacts: [02-creds.sh outputs, 02-ssh-keys.sh outputs]
  interface_contract:
    inputs_from_deps: ["~/.config/omnisight/*", "~/.ssh/id_ed25519_*-bot*"]
    outputs_for_downstream: ["all files mode 0600 (private) or 0644 (public)"]
ac.code:
  - desc: chmods cred files to 600
    verify: {command: "grep -cE 'chmod 600.*\\.config/omnisight' scripts/setup-dev-env-full/02-chmod-hardening.sh", expect_stdout_match: "[1-9]"}
  - desc: chmods SSH private keys to 600
    verify: {command: "grep -cE 'chmod 600.*id_ed25519' scripts/setup-dev-env-full/02-chmod-hardening.sh", expect_stdout_match: "[1-9]"}
go_live: T+0.5d after 2b
```

### 31.A-2e — Cred-provisioning unit test

```yaml
tier: S, prefer: claude, class: subscription-claude
area_labels: [area:tests, area:devops]
blockedBy: [2b, 2c, 2d]
context_hint:
  produces: "scripts/setup-dev-env-full/tests/test_creds_provisioning.py"
  consumed_by: "31.A-12a (pytest skeleton), CI"
  inputs_expected: "02-creds.sh / 02-ssh-keys.sh / 02-chmod-hardening.sh all exist + executable"
  outputs_guaranteed: ">=4 pytest test functions covering: YAML parsing, cred write, SSH key gen, chmod"
  non_goals:
    - "DO NOT modify any of 02-* component scripts"
    - "DO NOT add an integration test — that's 31.A-Integration"
boundaries:
  loc_delta_max: 150
  files_touched_max: 1
  required_paths: [scripts/setup-dev-env-full/tests/test_creds_provisioning.py]
  forbidden_paths: [scripts/setup-dev-env-full/02-creds.sh, scripts/setup-dev-env-full/02-ssh-keys.sh, scripts/setup-dev-env-full/02-chmod-hardening.sh, scripts/setup-dev-env-full/02-creds.template.yaml]
  test_scope: inline
  execution_mode: unit-testable
  destructive_ops_allowed: false
  dependency_artifacts: [02-creds.sh, 02-ssh-keys.sh, 02-chmod-hardening.sh]
  interface_contract:
    inputs_from_deps: [3 scripts above]
    outputs_for_downstream: [pytest collected with `pytest.mark.unit`]
ac.code:
  - desc: pytest collects >=4 test functions
    verify: {command: "pytest --collect-only scripts/setup-dev-env-full/tests/test_creds_provisioning.py 2>&1 | grep -c 'test_'", expect_stdout_match: "^[4-9]$|^[1-9][0-9]+$"}
  - desc: tests pass
    verify: {command: "pytest scripts/setup-dev-env-full/tests/test_creds_provisioning.py -v", expect_exit_code: 0, timeout_seconds: 60}
go_live: T+1d after 2b/2c/2d
```

### 31.A-3a — Productizer clone (Gerrit)

```yaml
tier: S, prefer: codex, class: subscription-codex
area_labels: [area:devops, area:tooling]
blockedBy: [1a, 2c]
context_hint:
  produces: "scripts/setup-dev-env-full/03-clone-productizer.sh"
  consumed_by: "31.A-3c (remote config), 31.A-11f"
  inputs_expected: "git installed (1a); SSH keys present (2c)"
  outputs_guaranteed: "~/work/sora/OmniSight-Productizer cloned from ssh://claude-bot@sora.services:29418/omnisight/OmniSight-Productizer; HEAD at develop"
  non_goals:
    - "DO NOT clone sora-bridge — 3b owns"
    - "DO NOT configure remotes / git user — 3c owns"
    - "DO NOT clone from GitHub (per L-OP-247: Gerrit is canonical)"
boundaries:
  loc_delta_max: 50
  files_touched_max: 1
  required_paths: [scripts/setup-dev-env-full/03-clone-productizer.sh]
  forbidden_paths: [scripts/setup-dev-env-full/03-clone-sora-bridge.sh, scripts/setup-dev-env-full/03-git-remote-config.sh]
  test_scope: defer-to-integration
  execution_mode: structural-only
  destructive_ops_allowed: false
  dependency_artifacts: [01-base-tools.sh (git), 02-ssh-keys.sh (claude-bot key)]
  interface_contract:
    inputs_from_deps: [git, ~/.ssh/id_ed25519_claude-bot]
    outputs_for_downstream: ["~/work/sora/OmniSight-Productizer/.git", "HEAD on develop"]
ac.code:
  - desc: uses Gerrit URL (not github.com)
    verify: {command: "grep -c 'ssh://claude-bot@sora.services:29418/omnisight/OmniSight-Productizer' scripts/setup-dev-env-full/03-clone-productizer.sh", expect_stdout_match: "[1-9]"}
  - desc: NO github.com clone URL
    verify: {command: "grep -cE 'git clone.*github.com' scripts/setup-dev-env-full/03-clone-productizer.sh", expect_stdout_match: "^0$"}
  - desc: checks out develop
    verify: {command: "grep -c 'git checkout develop\\|git -C.*checkout develop' scripts/setup-dev-env-full/03-clone-productizer.sh", expect_stdout_match: "[1-9]"}
decision_points:
  - id: dp_existing_clone
    trigger: "test -d ~/work/sora/OmniSight-Productizer/.git && echo exists"
    pause_if: "exists"
    operator_label: needs-operator-decision-existing-repo
go_live: T+0.5d after 1a + 2c
```

### 31.A-3b — sora-bridge clone (Gerrit)

```yaml
tier: S, prefer: codex, class: subscription-codex
area_labels: [area:devops, area:tooling]
blockedBy: [1a, 2c]
context_hint:
  produces: "scripts/setup-dev-env-full/03-clone-sora-bridge.sh"
  consumed_by: "31.A-3c, 31.A-8b (systemd unit symlink source path is sora-bridge)"
  inputs_expected: "git installed (1a); SSH keys present (2c)"
  outputs_guaranteed: "~/sora-bridge cloned from Gerrit; HEAD at develop"
  non_goals:
    - "DO NOT clone Productizer — 3a owns"
    - "DO NOT configure remotes — 3c owns"
boundaries:
  loc_delta_max: 50
  files_touched_max: 1
  required_paths: [scripts/setup-dev-env-full/03-clone-sora-bridge.sh]
  forbidden_paths: [scripts/setup-dev-env-full/03-clone-productizer.sh]
  test_scope: defer-to-integration
  execution_mode: structural-only
  destructive_ops_allowed: false
  dependency_artifacts: [01-base-tools.sh, 02-ssh-keys.sh]
  interface_contract:
    inputs_from_deps: [git, claude-bot SSH key]
    outputs_for_downstream: ["~/sora-bridge/.git", "deploy/systemd/*.service exists"]
ac.code:
  - desc: uses Gerrit URL
    verify: {command: "grep -cE 'ssh://claude-bot@sora.services:29418/.*sora-bridge' scripts/setup-dev-env-full/03-clone-sora-bridge.sh", expect_stdout_match: "[1-9]"}
go_live: T+0.5d after 1a + 2c
```

### 31.A-3c — remote + git config setup

```yaml
tier: S, prefer: codex, class: subscription-codex
area_labels: [area:devops, area:tooling]
blockedBy: [3a, 3b]
context_hint:
  produces: "scripts/setup-dev-env-full/03-git-remote-config.sh"
  consumed_by: "31.A-4a (launcher roots reference repo), runner pickups"
  inputs_expected: "Productizer + sora-bridge cloned (from 3a/3b)"
  outputs_guaranteed: "origin = Gerrit; mirror = github.com (read-only); user.name/user.email per operator+bot identity; ~/.ssh/config maps host→key"
  non_goals:
    - "DO NOT re-clone repos — 3a/3b own clone"
    - "DO NOT push any commits"
boundaries:
  loc_delta_max: 100
  files_touched_max: 1
  required_paths: [scripts/setup-dev-env-full/03-git-remote-config.sh]
  forbidden_paths: [scripts/setup-dev-env-full/03-clone-productizer.sh, scripts/setup-dev-env-full/03-clone-sora-bridge.sh]
  test_scope: defer-to-integration
  execution_mode: structural-only
  destructive_ops_allowed: false
  dependency_artifacts: [03-clone-productizer.sh, 03-clone-sora-bridge.sh]
  interface_contract:
    inputs_from_deps: [Productizer .git, sora-bridge .git]
    outputs_for_downstream: ["git remote -v shows origin@gerrit + mirror@github (Productizer)", "~/.ssh/config has sora.services entries"]
ac.code:
  - desc: sets origin to Gerrit, mirror to GitHub
    verify: {command: "grep -cE 'git remote.*(set-url origin ssh://.*sora.services|add mirror https://github.com)' scripts/setup-dev-env-full/03-git-remote-config.sh", expect_stdout_match: "^[2-9]$"}
  - desc: configures user.name + user.email
    verify: {command: "grep -cE 'git config (user.name|user.email)' scripts/setup-dev-env-full/03-git-remote-config.sh", expect_stdout_match: "^[2-9]$"}
  - desc: ~/.ssh/config wiring per bot identity
    verify: {command: "grep -cE 'IdentityFile.*id_ed25519_(claude|codex|merger)-bot' scripts/setup-dev-env-full/03-git-remote-config.sh", expect_stdout_match: "[1-9]"}
go_live: T+0.5d after 3a + 3b
```

### 31.A-4a — Launcher root dir creation

```yaml
tier: S, prefer: codex, class: subscription-codex
area_labels: [area:devops, area:tooling]
blockedBy: [3c]
context_hint:
  produces: "scripts/setup-dev-env-full/04-launcher-roots-mkdir.sh"
  consumed_by: "31.A-4b (template), 31.A-9a (tmux), 31.A-11f"
  inputs_expected: "Repos cloned + git configured (3c)"
  outputs_guaranteed: "4 dirs: ~/runner-claude-1, ~/runner-claude-2, ~/runner-codex-1, ~/runner-codex-2 (empty shells)"
  non_goals:
    - "DO NOT add launcher.sh content — 4b owns"
    - "DO NOT create symlinks or state files — 4b owns"
    - "DO NOT use git worktree (per ADR-0023 §3.5 v2: ephemeral clones at pickup, NOT worktrees)"
boundaries:
  loc_delta_max: 40
  files_touched_max: 1
  required_paths: [scripts/setup-dev-env-full/04-launcher-roots-mkdir.sh]
  forbidden_paths: [scripts/setup-dev-env-full/04-launcher-roots-template.sh]
  test_scope: defer-to-integration
  execution_mode: structural-only
  destructive_ops_allowed: false
  dependency_artifacts: [03-git-remote-config.sh outputs]
  interface_contract:
    inputs_from_deps: [Productizer repo + git config]
    outputs_for_downstream: [4 dirs at ~/runner-{claude,codex}-{1,2}]
ac.code:
  - desc: creates exactly 4 dirs with correct names
    verify: {command: "grep -cE 'mkdir -p.*runner-(claude|codex)-[12]$' scripts/setup-dev-env-full/04-launcher-roots-mkdir.sh", expect_stdout_match: "^[4-9]$"}
  - desc: NO git worktree command
    verify: {command: "grep -cE 'git worktree' scripts/setup-dev-env-full/04-launcher-roots-mkdir.sh", expect_stdout_match: "^0$"}
go_live: T+0.5d after 3c
```

### 31.A-4b — launcher.sh template + symlink + state init (MERGED v1 4b+4c)

```yaml
tier: S, prefer: codex, class: subscription-codex
area_labels: [area:devops, area:tooling]
blockedBy: [4a]
context_hint:
  produces: "scripts/setup-dev-env-full/04-launcher-roots-content.sh"
  consumed_by: "31.A-9a (tmux uses launcher.sh), 31.A-11f"
  inputs_expected: "4 launcher root dirs (from 4a)"
  outputs_guaranteed: "each dir contains: launcher.sh (executable tmux script), symlink to canonical auto-runner-jira.py, ~/.cache/omnisight/state-{instance}.json initial, empty metrics-out.jsonl"
  non_goals:
    - "DO NOT create the dirs — 4a owns"
    - "DO NOT touch backend/agents/auto_runner_jira.py (the symlink TARGET)"
    - "DO NOT start tmux sessions — 9a owns"
boundaries:
  loc_delta_max: 100
  files_touched_max: 1
  required_paths: [scripts/setup-dev-env-full/04-launcher-roots-content.sh]
  forbidden_paths: [scripts/setup-dev-env-full/04-launcher-roots-mkdir.sh, backend/agents/auto_runner_jira.py, scripts/setup-dev-env-full/09-tmux-bootstrap.sh]
  test_scope: defer-to-integration
  execution_mode: structural-only
  destructive_ops_allowed: false
  dependency_artifacts: [04-launcher-roots-mkdir.sh (4 dirs)]
  interface_contract:
    inputs_from_deps: [~/runner-{claude,codex}-{1,2} dirs]
    outputs_for_downstream: ["launcher.sh in each dir", "symlink to canonical auto-runner-jira.py", "initial state.json"]
ac.code:
  - desc: writes launcher.sh in each of 4 roots
    verify: {command: "grep -cE 'launcher\\.sh' scripts/setup-dev-env-full/04-launcher-roots-content.sh", expect_stdout_match: "^[4-9]$|^[1-9][0-9]+$"}
  - desc: creates symlinks to auto-runner-jira.py
    verify: {command: "grep -cE 'ln -sf.*auto[_-]runner[_-]jira' scripts/setup-dev-env-full/04-launcher-roots-content.sh", expect_stdout_match: "[1-9]"}
  - desc: initializes state.json in ~/.cache/omnisight/
    verify: {command: "grep -cE 'state-(claude|codex)-[12]\\.json' scripts/setup-dev-env-full/04-launcher-roots-content.sh", expect_stdout_match: "^[4-9]$"}
go_live: T+0.5d after 4a
```

### 31.A-5a — Dev Postgres compose definition

```yaml
tier: S, prefer: codex, class: subscription-codex
area_labels: [area:devops, area:db]
blockedBy: [1a]
context_hint:
  produces: "deploy/dev/postgres/docker-compose.yml + scripts/setup-dev-env-full/05-postgres-compose.sh"
  consumed_by: "31.A-5b (env file), 31.A-6a (memory stack DB), 31.A-11f"
  inputs_expected: "docker.io + docker-compose-v2 from 1a; docker daemon group membership"
  outputs_guaranteed: "compose definition for omnisight-dev-postgres container; uses NAT-internal port 5432; NO host port mapping (per port plan: NAT primary)"
  non_goals:
    - "DO NOT generate password or env file — 31.A-5b owns"
    - "DO NOT run alembic — 31.A-5c owns"
    - "DO NOT add host port mapping (use NAT; --expose-host-debug is 6d)"
    - "DO NOT collide with prod-WSL omnisight-pg-primary or staging-postgres-1"
boundaries:
  loc_delta_max: 80
  files_touched_max: 2
  required_paths: [deploy/dev/postgres/docker-compose.yml, scripts/setup-dev-env-full/05-postgres-compose.sh]
  forbidden_paths: [deploy/postgres/, scripts/setup-dev-env-full/05-postgres-creds.sh, scripts/setup-dev-env-full/05-alembic-upgrade.sh]
  test_scope: defer-to-integration
  execution_mode: structural-only
  destructive_ops_allowed: false
  dependency_artifacts: [01-base-tools.sh]
  interface_contract:
    inputs_from_deps: [docker daemon ready, docker group membership]
    outputs_for_downstream: ["container omnisight-dev-postgres definition", "compose project name `omnisight-dev`"]
ac.code:
  - desc: compose has postgres service
    verify: {command: "grep -cE 'image: postgres:|container_name: omnisight-dev-postgres' deploy/dev/postgres/docker-compose.yml", expect_stdout_match: "^[2-9]$"}
  - desc: NO host port mapping (NAT-only)
    verify: {command: "grep -cE '\\- \"[0-9]+:5432\"' deploy/dev/postgres/docker-compose.yml", expect_stdout_match: "^0$"}
  - desc: docker group membership check in sh
    verify: {command: "grep -cE 'getent group docker|groups.*docker' scripts/setup-dev-env-full/05-postgres-compose.sh", expect_stdout_match: "[1-9]"}
go_live: T+0.5d after 1a
```

### 31.A-5b — Auto-gen password + env file

```yaml
tier: S, prefer: codex, class: subscription-codex
area_labels: [area:devops, area:security, area:db]
blockedBy: [5a]
context_hint:
  produces: "scripts/setup-dev-env-full/05-postgres-creds.sh"
  consumed_by: "31.A-5c (alembic uses DSN), 31.A-6a (memory stack uses DB), 31.A-11f"
  inputs_expected: "compose file at deploy/dev/postgres/docker-compose.yml (from 5a)"
  outputs_guaranteed: "~/.config/omnisight/dev-postgres.env with POSTGRES_PASSWORD (auto-gen, 32 chars); ~/.config/omnisight/audit-db.env with full DSN"
  non_goals:
    - "DO NOT prompt operator for password (auto-gen via openssl)"
    - "DO NOT modify compose file — 5a owns"
    - "DO NOT run alembic — 5c owns"
boundaries:
  loc_delta_max: 60
  files_touched_max: 1
  required_paths: [scripts/setup-dev-env-full/05-postgres-creds.sh]
  forbidden_paths: [deploy/dev/postgres/docker-compose.yml, scripts/setup-dev-env-full/05-postgres-compose.sh, scripts/setup-dev-env-full/05-alembic-upgrade.sh]
  test_scope: defer-to-integration
  execution_mode: structural-only
  destructive_ops_allowed: false
  dependency_artifacts: [deploy/dev/postgres/docker-compose.yml]
  interface_contract:
    inputs_from_deps: [compose project name `omnisight-dev`]
    outputs_for_downstream: [~/.config/omnisight/dev-postgres.env, ~/.config/omnisight/audit-db.env]
ac.code:
  - desc: auto-generates 32-char password
    verify: {command: "grep -cE 'openssl rand -hex 32|head -c 32 /dev/urandom' scripts/setup-dev-env-full/05-postgres-creds.sh", expect_stdout_match: "[1-9]"}
  - desc: writes both env files
    verify: {command: "grep -cE '\\.config/omnisight/(dev-postgres|audit-db)\\.env' scripts/setup-dev-env-full/05-postgres-creds.sh", expect_stdout_match: "^[2-9]$"}
  - desc: DSN uses NAT-internal hostname
    verify: {command: "grep -cE 'postgresql://omnisight.*@omnisight-dev-postgres' scripts/setup-dev-env-full/05-postgres-creds.sh", expect_stdout_match: "[1-9]"}
decision_points:
  - id: dp_existing_env
    trigger: "test -f ~/.config/omnisight/dev-postgres.env && echo exists"
    pause_if: "exists"
    operator_label: needs-operator-decision-existing-dev-postgres-env
go_live: T+0.5d after 5a
```

### 31.A-5c — Alembic upgrade runner + head verify (MERGED v1 5c+5d)

```yaml
tier: S, prefer: codex, class: subscription-codex
area_labels: [area:devops, area:db, area:backend]
blockedBy: [5b]
context_hint:
  produces: "scripts/setup-dev-env-full/05-alembic-upgrade.sh"
  consumed_by: "31.A-11f orchestrator step 05c, backend container at runtime"
  inputs_expected: "Postgres compose + DSN env (from 5a/5b)"
  outputs_guaranteed: "Postgres container running; alembic at single head matching backend/alembic/versions/; psql connect works"
  non_goals:
    - "DO NOT modify compose or env scripts — 5a/5b own"
    - "DO NOT add memory stack — 6a/6b/6c own"
    - "DO NOT run if multi-head detected (decision_point → operator pause)"
boundaries:
  loc_delta_max: 100
  files_touched_max: 1
  required_paths: [scripts/setup-dev-env-full/05-alembic-upgrade.sh]
  forbidden_paths: [deploy/dev/postgres/docker-compose.yml, scripts/setup-dev-env-full/05-postgres-compose.sh, scripts/setup-dev-env-full/05-postgres-creds.sh, backend/alembic/versions/]
  test_scope: defer-to-integration
  execution_mode: structural-only
  destructive_ops_allowed: true
  dependency_artifacts: [docker-compose.yml, dev-postgres.env, audit-db.env]
  interface_contract:
    inputs_from_deps: [compose ready, DSN env]
    outputs_for_downstream: [container `up`, alembic_version at single head, psql works]
ac.code:
  - desc: starts container via compose up
    verify: {command: "grep -cE 'docker compose.*up -d' scripts/setup-dev-env-full/05-alembic-upgrade.sh", expect_stdout_match: "[1-9]"}
  - desc: runs alembic upgrade head
    verify: {command: "grep -cE 'alembic.*upgrade head' scripts/setup-dev-env-full/05-alembic-upgrade.sh", expect_stdout_match: "[1-9]"}
  - desc: head-count verification
    verify: {command: "grep -cE 'alembic heads.*wc -l|alembic heads.*grep -c' scripts/setup-dev-env-full/05-alembic-upgrade.sh", expect_stdout_match: "[1-9]"}
decision_points:
  - id: dp_multi_head
    trigger: "cd backend && alembic heads 2>&1 | grep -c head"
    pause_if: ">= 2"
    operator_label: needs-operator-decision-alembic-multi-head
go_live: T+1d after 5b
```

### 31.A-6a — Cognee compose + freeze network/env conventions

```yaml
tier: S, prefer: codex, class: subscription-codex
area_labels: [area:devops, area:backend]
blockedBy: [1a]
context_hint:
  produces: "deploy/dev/memory-stack/docker-compose.yml (cognee) + .env.template + scripts/setup-dev-env-full/06-cognee.sh"
  consumed_by: "31.A-6b/6c (use same network), 31.A-6d (debug expose)"
  inputs_expected: "docker daemon + group from 1a"
  outputs_guaranteed: "Cognee in docker network `omnisight-dev-memory`; env var prefixes COGNEE_*/NEO4J_*/GRAPHITI_* frozen for downstream"
  non_goals:
    - "DO NOT add Neo4j service — 6b owns (uses same network defined here)"
    - "DO NOT add Graphiti service — 6c owns"
    - "DO NOT add host port mapping (NAT-only; debug expose is 6d)"
    - "DO NOT depend on Postgres — Cognee uses own data stores"
boundaries:
  loc_delta_max: 120
  files_touched_max: 3
  required_paths: [deploy/dev/memory-stack/docker-compose.yml, deploy/dev/memory-stack/.env.template, scripts/setup-dev-env-full/06-cognee.sh]
  forbidden_paths: [scripts/setup-dev-env-full/06-neo4j.sh, scripts/setup-dev-env-full/06-graphiti.sh, scripts/setup-dev-env-full/06-expose-host.sh]
  test_scope: defer-to-integration
  execution_mode: structural-only
  destructive_ops_allowed: false
  dependency_artifacts: [01-base-tools.sh]
  interface_contract:
    inputs_from_deps: [docker daemon ready]
    outputs_for_downstream:
      - "compose project: omnisight-dev-memory"
      - "docker network: omnisight-dev-memory (driver bridge)"
      - "service: cognee (container_name omnisight-dev-cognee)"
      - "env var prefixes frozen: COGNEE_*, NEO4J_*, GRAPHITI_*"
ac.code:
  - desc: compose has cognee + named network
    verify: {command: "grep -cE 'cognee:|networks:.*omnisight-dev-memory' deploy/dev/memory-stack/docker-compose.yml", expect_stdout_match: "^[2-9]$"}
  - desc: .env.template defines 3 canonical prefixes
    verify: {command: "grep -cE '^(COGNEE|NEO4J|GRAPHITI)_' deploy/dev/memory-stack/.env.template", expect_stdout_match: "^[3-9]$|^[1-9][0-9]+$"}
  - desc: NO host port mapping
    verify: {command: "grep -cE '\\- \"[0-9]+:[0-9]+\"' deploy/dev/memory-stack/docker-compose.yml", expect_stdout_match: "^0$"}
go_live: T+0.5d after 1a
```

### 31.A-6b — Neo4j compose (uses 6a conventions)

```yaml
tier: S, prefer: codex, class: subscription-codex
area_labels: [area:devops, area:backend]
blockedBy: [1a, 6a]
context_hint:
  produces: "patch to deploy/dev/memory-stack/docker-compose.yml (add neo4j service)"
  consumed_by: "31.A-6c (Graphiti needs Neo4j), 31.A-11f"
  inputs_expected: "deploy/dev/memory-stack/docker-compose.yml exists (from 6a); .env.template has NEO4J_* prefix (from 6a)"
  outputs_guaranteed: "neo4j service added; uses omnisight-dev-memory network; NAT-internal ports only"
  non_goals:
    - "DO NOT modify network definition (frozen by 6a)"
    - "DO NOT add Graphiti — 6c owns"
    - "DO NOT add host port mapping"
    - "DO NOT touch .env.template if NEO4J_* prefix already there"
boundaries:
  loc_delta_max: 40
  files_touched_max: 1
  required_paths: [deploy/dev/memory-stack/docker-compose.yml]
  forbidden_paths: [deploy/dev/memory-stack/.env.template, scripts/setup-dev-env-full/06-cognee.sh, scripts/setup-dev-env-full/06-graphiti.sh]
  test_scope: defer-to-integration
  execution_mode: structural-only
  destructive_ops_allowed: false
  dependency_artifacts: [deploy/dev/memory-stack/docker-compose.yml (from 6a)]
  interface_contract:
    inputs_from_deps: [omnisight-dev-memory network, NEO4J_* env prefix]
    outputs_for_downstream: [neo4j container reachable at omnisight-dev-neo4j:7687 inside network]
ac.code:
  - desc: neo4j service added
    verify: {command: "grep -cE '^\\s+neo4j:' deploy/dev/memory-stack/docker-compose.yml", expect_stdout_match: "[1-9]"}
  - desc: reuses omnisight-dev-memory network
    verify: {command: "yq '.services.neo4j.networks[]' deploy/dev/memory-stack/docker-compose.yml", expect_stdout_match: "omnisight-dev-memory"}
  - desc: NO host port mapping for neo4j
    verify: {command: "yq '.services.neo4j.ports // [] | length' deploy/dev/memory-stack/docker-compose.yml", expect_stdout_match: "^0$"}
go_live: T+0.5d after 6a
```

### 31.A-6c — Graphiti compose (uses 6a conventions)

```yaml
tier: S, prefer: codex, class: subscription-codex
area_labels: [area:devops, area:backend]
blockedBy: [6a, 6b]
context_hint:
  produces: "patch to deploy/dev/memory-stack/docker-compose.yml (add graphiti service)"
  consumed_by: "31.A-6d (debug expose), 31.A-11f"
  inputs_expected: "compose has cognee + neo4j (6a/6b); GRAPHITI_* env prefix frozen"
  outputs_guaranteed: "graphiti service added; depends_on neo4j; same network"
  non_goals:
    - "DO NOT modify cognee or neo4j services"
    - "DO NOT add host port mapping"
boundaries:
  loc_delta_max: 40
  files_touched_max: 1
  required_paths: [deploy/dev/memory-stack/docker-compose.yml]
  forbidden_paths: [scripts/setup-dev-env-full/06-cognee.sh, scripts/setup-dev-env-full/06-neo4j.sh, scripts/setup-dev-env-full/06-expose-host.sh, deploy/dev/memory-stack/.env.template]
  test_scope: defer-to-integration
  execution_mode: structural-only
  destructive_ops_allowed: false
  dependency_artifacts: [docker-compose.yml after 6a + 6b]
  interface_contract:
    inputs_from_deps: [network omnisight-dev-memory, neo4j service]
    outputs_for_downstream: [graphiti container reachable at omnisight-dev-graphiti:<port>]
ac.code:
  - desc: graphiti service added
    verify: {command: "yq '.services.graphiti.container_name' deploy/dev/memory-stack/docker-compose.yml", expect_stdout_match: "omnisight-dev-graphiti"}
  - desc: depends_on neo4j
    verify: {command: "yq '.services.graphiti.depends_on[]' deploy/dev/memory-stack/docker-compose.yml", expect_stdout_match: "neo4j"}
go_live: T+0.5d after 6a + 6b
```

### 31.A-6d — --expose-host-debug flag impl

```yaml
tier: S, prefer: codex, class: subscription-codex
area_labels: [area:devops, area:backend]
blockedBy: [6a, 6b, 6c]
context_hint:
  produces: "scripts/setup-dev-env-full/06-expose-host.sh + deploy/dev/memory-stack/docker-compose.host-expose.yml (overlay)"
  consumed_by: "31.A-11a master script (--expose-host-debug flag), operator if Windows GUI tools needed"
  inputs_expected: "Memory stack compose complete (6a/b/c)"
  outputs_guaranteed: "Overlay compose mapping non-conflicting host ports (65432 for postgres, 17474/17687 for neo4j); script applies overlay only when --expose-host-debug set"
  non_goals:
    - "DO NOT modify base docker-compose.yml (overlay, opt-in)"
    - "DO NOT bind to ports <1024"
    - "DO NOT expose to 0.0.0.0 (use 127.0.0.1 only — WSL host only, not LAN)"
boundaries:
  loc_delta_max: 80
  files_touched_max: 2
  required_paths: [scripts/setup-dev-env-full/06-expose-host.sh, deploy/dev/memory-stack/docker-compose.host-expose.yml]
  forbidden_paths: [deploy/dev/memory-stack/docker-compose.yml, scripts/setup-dev-env-full/06-cognee.sh, scripts/setup-dev-env-full/06-neo4j.sh, scripts/setup-dev-env-full/06-graphiti.sh]
  test_scope: defer-to-integration
  execution_mode: structural-only
  destructive_ops_allowed: false
  dependency_artifacts: [base compose ready]
  interface_contract:
    inputs_from_deps: [memory-stack compose with services cognee/neo4j/graphiti]
    outputs_for_downstream: ["overlay activated via `docker compose -f base.yml -f overlay.yml up`"]
ac.code:
  - desc: overlay binds 127.0.0.1 only
    verify: {command: "grep -cE '127.0.0.1:[0-9]+:[0-9]+' deploy/dev/memory-stack/docker-compose.host-expose.yml", expect_stdout_match: "[3-9]"}
  - desc: NO ports <1024
    verify: {command: "grep -oE '127.0.0.1:[0-9]+:' deploy/dev/memory-stack/docker-compose.host-expose.yml | awk -F: '{print $2}' | awk '$1 < 1024'", expect_stdout_match: "^$"}
  - desc: script gates on --expose-host-debug flag
    verify: {command: "grep -c 'expose-host-debug' scripts/setup-dev-env-full/06-expose-host.sh", expect_stdout_match: "[1-9]"}
go_live: T+0.5d after 6a + 6b + 6c
```

### 31.A-7 — GHCR docker login

```yaml
tier: S, prefer: codex, class: subscription-codex + operator-prepare-only
area_labels: [area:devops, area:security]
blockedBy: [2b]
context_hint:
  produces: "scripts/setup-dev-env-full/07-ghcr-login.sh"
  consumed_by: "31.A-11f, runner image pulls at pickup time"
  inputs_expected: "~/.config/omnisight/ghcr-pull-token from 2b"
  outputs_guaranteed: "~/.docker/config.json has ghcr.io auth entry; `docker pull ghcr.io/...` succeeds"
  non_goals:
    - "DO NOT generate the token — operator provides via 2b"
    - "DO NOT pull any images (just login)"
boundaries:
  loc_delta_max: 30
  files_touched_max: 1
  required_paths: [scripts/setup-dev-env-full/07-ghcr-login.sh]
  forbidden_paths: [scripts/setup-dev-env-full/02-creds.sh]
  test_scope: defer-to-integration
  execution_mode: structural-only
  destructive_ops_allowed: false
  dependency_artifacts: [~/.config/omnisight/ghcr-pull-token (from 2b)]
  interface_contract:
    inputs_from_deps: [ghcr-pull-token file]
    outputs_for_downstream: [~/.docker/config.json with ghcr.io auth]
ac.code:
  - desc: reads token from canonical path
    verify: {command: "grep -c '\\.config/omnisight/ghcr-pull-token' scripts/setup-dev-env-full/07-ghcr-login.sh", expect_stdout_match: "[1-9]"}
  - desc: uses docker login --password-stdin (no token in argv)
    verify: {command: "grep -cE 'docker login.*ghcr\\.io.*--password-stdin|--password-stdin.*docker login' scripts/setup-dev-env-full/07-ghcr-login.sh", expect_stdout_match: "[1-9]"}
go_live: T+0.5d after 2b
```

### 31.A-8a — dev-subset.txt curation (25/84 list)

```yaml
tier: S, prefer: claude, class: subscription-claude
area_labels: [area:devops, area:docs]
blockedBy: []
context_hint:
  produces: "scripts/setup-dev-env-full/08-systemd-units.dev-subset.txt (canonical 20-30 unit filenames)"
  consumed_by: "31.A-8b (installer reads list), 31.A-Integration (verify enabled count)"
  inputs_expected: "operator + claude review 84 units in ~/sora-bridge/deploy/systemd/ and decide dev relevance"
  outputs_guaranteed: "newline-separated 20-30 unit filenames; comments explain dev relevance; excludes prod-only (canary/backup/omnisight-compose-prod)"
  non_goals:
    - "DO NOT install any units — 8b owns"
    - "DO NOT modify unit definitions in sora-bridge"
    - "DO NOT include any unit with prod-only intent"
boundaries:
  loc_delta_max: 80
  files_touched_max: 1
  required_paths: [scripts/setup-dev-env-full/08-systemd-units.dev-subset.txt]
  forbidden_paths: [scripts/setup-dev-env-full/08-systemd-install.sh, scripts/setup-dev-env-full/08-systemd-enable.sh]
  test_scope: defer-to-integration
  execution_mode: structural-only
  destructive_ops_allowed: false
  dependency_artifacts: []
  interface_contract:
    inputs_from_deps: []
    outputs_for_downstream: ["newline-separated unit filenames"]
ac.code:
  - desc: file has 20-30 entries
    verify: {command: "grep -vE '^#|^$' scripts/setup-dev-env-full/08-systemd-units.dev-subset.txt | wc -l", expect_stdout_match: "^(2[0-9]|30)$"}
  - desc: NO prod-only units
    verify: {command: "grep -cE 'omnisight-compose-prod|backup-daily|backup-hourly|canary-' scripts/setup-dev-env-full/08-systemd-units.dev-subset.txt", expect_stdout_match: "^0$"}
  - desc: each entry ends in .service or .timer
    verify: {command: "grep -cE '\\.(service|timer)$' scripts/setup-dev-env-full/08-systemd-units.dev-subset.txt", expect_stdout_match: "^(2[0-9]|30)$"}
go_live: T+1d after sub-META starts
```

### 31.A-8b — systemd installer (symlink + daemon-reload)

```yaml
tier: S, prefer: codex, class: subscription-codex
area_labels: [area:devops]
blockedBy: [8a, 3c]
context_hint:
  produces: "scripts/setup-dev-env-full/08-systemd-install.sh"
  consumed_by: "31.A-8c (enable logic), 31.A-11f"
  inputs_expected: "dev-subset.txt (8a); ~/sora-bridge cloned (3b via 3c)"
  outputs_guaranteed: "All units in dev-subset.txt symlinked to ~/.config/systemd/user/; daemon-reload run; NO units started"
  non_goals:
    - "DO NOT modify dev-subset.txt — 8a owns"
    - "DO NOT enable or start units — 8c owns"
    - "DO NOT copy units (use symlinks for develop merge propagation)"
boundaries:
  loc_delta_max: 60
  files_touched_max: 1
  required_paths: [scripts/setup-dev-env-full/08-systemd-install.sh]
  forbidden_paths: [scripts/setup-dev-env-full/08-systemd-units.dev-subset.txt, scripts/setup-dev-env-full/08-systemd-enable.sh]
  test_scope: defer-to-integration
  execution_mode: structural-only
  destructive_ops_allowed: false
  dependency_artifacts: [dev-subset.txt, ~/sora-bridge/deploy/systemd/]
  interface_contract:
    inputs_from_deps: [dev-subset.txt content, sora-bridge clone]
    outputs_for_downstream: [~/.config/systemd/user/<unit> symlinks]
ac.code:
  - desc: uses ln -sf (NOT cp)
    verify: {command: "grep -cE 'ln -sf.*sora-bridge/deploy/systemd' scripts/setup-dev-env-full/08-systemd-install.sh", expect_stdout_match: "[1-9]"}
  - desc: NO `cp` of unit files
    verify: {command: "grep -cE '^\\s*cp .*\\.service' scripts/setup-dev-env-full/08-systemd-install.sh", expect_stdout_match: "^0$"}
  - desc: runs daemon-reload
    verify: {command: "grep -c 'systemctl --user daemon-reload' scripts/setup-dev-env-full/08-systemd-install.sh", expect_stdout_match: "[1-9]"}
go_live: T+0.5d after 8a + 3c
```

### 31.A-8c — enable-not-start safety logic

```yaml
tier: S, prefer: codex, class: subscription-codex
area_labels: [area:devops]
blockedBy: [8b]
context_hint:
  produces: "scripts/setup-dev-env-full/08-systemd-enable.sh"
  consumed_by: "31.A-11f, operator at Integration (decides when to actually start runners)"
  inputs_expected: "Units symlinked + daemon-reloaded (8b)"
  outputs_guaranteed: "All dev-subset units `systemctl --user enable` (auto-start on next boot); NONE started immediately"
  non_goals:
    - "DO NOT use --now flag (operator decides start time)"
    - "DO NOT start any units immediately"
    - "DO NOT modify install script or unit list"
boundaries:
  loc_delta_max: 40
  files_touched_max: 1
  required_paths: [scripts/setup-dev-env-full/08-systemd-enable.sh]
  forbidden_paths: [scripts/setup-dev-env-full/08-systemd-install.sh, scripts/setup-dev-env-full/08-systemd-units.dev-subset.txt]
  test_scope: defer-to-integration
  execution_mode: structural-only
  destructive_ops_allowed: false
  dependency_artifacts: [dev-subset.txt content, units symlinked]
  interface_contract:
    inputs_from_deps: [units installed in ~/.config/systemd/user/]
    outputs_for_downstream: [units enabled but not started]
ac.code:
  - desc: uses systemctl --user enable (NOT enable --now)
    verify: {command: "grep -c 'systemctl --user enable' scripts/setup-dev-env-full/08-systemd-enable.sh", expect_stdout_match: "[1-9]"}
  - desc: NO --now flag
    verify: {command: "grep -cE 'systemctl.*--now' scripts/setup-dev-env-full/08-systemd-enable.sh", expect_stdout_match: "^0$"}
  - desc: NO `systemctl --user start` calls
    verify: {command: "grep -cE 'systemctl --user start' scripts/setup-dev-env-full/08-systemd-enable.sh", expect_stdout_match: "^0$"}
go_live: T+0.5d after 8b
```

### 31.A-9a — tmux session creation + paused-state guard (MERGED v1 9a+9b)

```yaml
tier: S, prefer: codex, class: subscription-codex
area_labels: [area:devops, area:tooling]
blockedBy: [4b, 8c]
context_hint:
  produces: "scripts/setup-dev-env-full/09-tmux-bootstrap.sh"
  consumed_by: "31.A-11f, operator at Integration (resumes one session to test runner pickup)"
  inputs_expected: "Launcher roots populated (4b); systemd units enabled but not started (8c)"
  outputs_guaranteed: "4 tmux sessions created in PAUSED state; ~/runner-<instance>/.paused marker file present; auto-runner-jira.py NOT running"
  non_goals:
    - "DO NOT start auto-runner-jira.py — operator decides resume time"
    - "DO NOT create the launcher roots — 4a/4b own"
    - "DO NOT enable systemd units — 8c owns"
boundaries:
  loc_delta_max: 80
  files_touched_max: 1
  required_paths: [scripts/setup-dev-env-full/09-tmux-bootstrap.sh]
  forbidden_paths: [scripts/setup-dev-env-full/04-launcher-roots-mkdir.sh, scripts/setup-dev-env-full/04-launcher-roots-content.sh, scripts/setup-dev-env-full/08-systemd-enable.sh]
  test_scope: defer-to-integration
  execution_mode: structural-only
  destructive_ops_allowed: false
  dependency_artifacts: [launcher.sh in each runner root, systemd units enabled]
  interface_contract:
    inputs_from_deps: [launcher.sh executable in each ~/runner-<instance>]
    outputs_for_downstream: [tmux ls shows 4 sessions; auto-runner-jira.py NOT in pgrep output]
ac.code:
  - desc: creates 4 named sessions
    verify: {command: "grep -cE 'tmux new-session -d -s (claude|codex)(-2)?-runner-loop\\b' scripts/setup-dev-env-full/09-tmux-bootstrap.sh", expect_stdout_match: "^4$"}
  - desc: creates .paused marker in each root
    verify: {command: "grep -cE 'touch .*runner-(claude|codex)-[12]/.paused' scripts/setup-dev-env-full/09-tmux-bootstrap.sh", expect_stdout_match: "^[4-9]$"}
  - desc: NO auto-runner-jira.py launch
    verify: {command: "grep -cE 'auto[_-]runner[_-]jira\\.py' scripts/setup-dev-env-full/09-tmux-bootstrap.sh", expect_stdout_match: "^0$"}
go_live: T+0.5d after 4b + 8c
```

### 31.A-10a — Cross-WSL probe script

```yaml
tier: S, prefer: codex, class: subscription-codex + operator-prepare-only
area_labels: [area:devops, area:tooling]
blockedBy: [1a]
context_hint:
  produces: "scripts/setup-dev-env-full/10-cross-wsl-probe.sh"
  consumed_by: "31.A-10b (runbook), operator at Integration"
  inputs_expected: "Base tools (curl/nc) from 1a"
  outputs_guaranteed: "Probe tests: ai-core URL reachable, Gerrit/GitLab DNS resolves, prod-WSL IP reachable; emits JSONL to stdout"
  non_goals:
    - "DO NOT execute the probe (operator-only at Integration per SOP §12.1)"
    - "DO NOT modify any production WSL state"
boundaries:
  loc_delta_max: 80
  files_touched_max: 1
  required_paths: [scripts/setup-dev-env-full/10-cross-wsl-probe.sh]
  forbidden_paths: [scripts/setup-dev-env-full/10-cross-wsl-runbook.md]
  test_scope: defer-to-integration
  execution_mode: structural-only
  destructive_ops_allowed: false
  dependency_artifacts: [01-base-tools.sh (curl, nc, jq)]
  interface_contract:
    inputs_from_deps: [curl, nc, jq available]
    outputs_for_downstream: [stdout JSONL with probe results per endpoint]
ac.code:
  - desc: probes ai-core URL
    verify: {command: "grep -cE 'curl.*ai-tunnel|curl.*ai-core' scripts/setup-dev-env-full/10-cross-wsl-probe.sh", expect_stdout_match: "[1-9]"}
  - desc: probes Gerrit + GitLab
    verify: {command: "grep -cE 'nc -z .*sora\\.services.*29418|nc -z .*sora\\.services.*49154' scripts/setup-dev-env-full/10-cross-wsl-probe.sh", expect_stdout_match: "^[2-9]$"}
  - desc: emits JSONL
    verify: {command: "grep -cE 'jq -nc|printf .*json' scripts/setup-dev-env-full/10-cross-wsl-probe.sh", expect_stdout_match: "[1-9]"}
go_live: T+0.5d after 1a
```

### 31.A-10b — Cross-WSL operator runbook (doc)

```yaml
tier: S, prefer: claude, class: subscription-claude
area_labels: [area:docs, area:devops]
blockedBy: [10a]
context_hint:
  produces: "docs/operations/dev-wsl-cross-wsl-runbook.md (≤200 lines)"
  consumed_by: "operator at Integration"
  inputs_expected: "10-cross-wsl-probe.sh exists (10a)"
  outputs_guaranteed: "1-page runbook: when to run probe, how to interpret results, where to commit evidence"
  non_goals:
    - "DO NOT modify the probe script — 10a owns"
    - "DO NOT add unrelated docs (only this single runbook)"
boundaries:
  loc_delta_max: 200
  files_touched_max: 1
  required_paths: [docs/operations/dev-wsl-cross-wsl-runbook.md]
  forbidden_paths: [scripts/setup-dev-env-full/10-cross-wsl-probe.sh]
  test_scope: none
  execution_mode: operator-rehearsal
  destructive_ops_allowed: false
  dependency_artifacts: [10-cross-wsl-probe.sh]
  interface_contract:
    inputs_from_deps: [probe script available]
    outputs_for_downstream: [runbook referenced from Integration ticket]
ac.code:
  - desc: doc exists ≤200 lines
    verify: {command: "wc -l docs/operations/dev-wsl-cross-wsl-runbook.md | awk '{print $1}'", expect_stdout_match: "^([0-9]|[1-9][0-9]|1[0-9]{2}|200)$"}
  - desc: references probe script
    verify: {command: "grep -c '10-cross-wsl-probe.sh' docs/operations/dev-wsl-cross-wsl-runbook.md", expect_stdout_match: "[1-9]"}
go_live: T+0.5d after 10a
```

### 31.A-11a — Master script skeleton + arg parse + state-file schema lock

```yaml
tier: S, prefer: claude, class: subscription-claude
area_labels: [area:devops, area:tooling, area:docs]
blockedBy: [1a, 1b, 1c, 2b, 3c, 4b, 5c, 6d, 7, 8c, 9a, 10a]   # 12 explicit
context_hint:
  produces: "scripts/setup-dev-env-full.sh (skeleton + arg parser + state-file schema in heredoc)"
  consumed_by: "31.A-11b/c/d/e/f (modes), 31.A-12a (validation entry)"
  inputs_expected: "All 10 component scripts exist (12 deps in blockedBy)"
  outputs_guaranteed: |
    1. master script exists
    2. arg parser handles --dry-run / --resume / --validate / --rollback / --expose-host-debug / --help (parsing only)
    3. state-file schema FROZEN as `{"version":"v1","last_completed_step":"step_NN","timestamp":"ISO8601","git_sha":"<40hex>","errors":[]}` in heredoc
    4. helpers (log_info/log_warn/log_error/write_state) defined
  non_goals:
    - "DO NOT implement --dry-run logic (11b owns)"
    - "DO NOT implement --resume logic (11c owns)"
    - "DO NOT implement --validate logic (11d owns)"
    - "DO NOT implement --rollback logic (11e owns)"
    - "DO NOT wire orchestration sequence (11f owns)"
boundaries:
  loc_delta_max: 200
  files_touched_max: 1
  required_paths: [scripts/setup-dev-env-full.sh]
  forbidden_paths: [scripts/setup-dev-env-full/]
  test_scope: defer-to-integration
  execution_mode: structural-only
  destructive_ops_allowed: false
  dependency_artifacts: [all 10 component scripts + 8a list]
  interface_contract:
    inputs_from_deps: [all component scripts exist]
    outputs_for_downstream:
      - "scripts/setup-dev-env-full.sh exists + executable"
      - "state-file schema v1 LOCKED — downstream 11c/d/e MUST conform"
      - "log_info/log_warn/log_error/write_state helpers exist"
      - "arg parser routes flags to placeholder funcs"
ac.code:
  - desc: master script exists + executable
    verify: {command: "test -x scripts/setup-dev-env-full.sh", expect_exit_code: 0}
  - desc: --help shows 5 flags
    verify: {command: "bash scripts/setup-dev-env-full.sh --help 2>&1 | grep -cE '(--dry-run|--resume|--validate|--rollback|--expose-host-debug)'", expect_stdout_match: "^[5-9]$"}
  - desc: state-file schema documented
    verify: {command: "grep -cE 'last_completed_step|schema.*v1' scripts/setup-dev-env-full.sh", expect_stdout_match: "^[2-9]$|^[1-9][0-9]+$"}
  - desc: 4 helpers defined
    verify: {command: "grep -cE '^(log_info|log_warn|log_error|write_state)\\s*\\(\\)' scripts/setup-dev-env-full.sh", expect_stdout_match: "^[4-9]$"}
go_live: T+1d after all 10 component scripts close
```

### 31.A-11b — --dry-run mode

```yaml
tier: S, prefer: claude, class: subscription-claude
area_labels: [area:devops, area:tooling]
blockedBy: [11a]
context_hint:
  produces: "patch to scripts/setup-dev-env-full.sh — do_dry_run()"
  consumed_by: "31.A-11f (wires orchestration), Integration test"
  inputs_expected: "Master skeleton + state schema locked (11a)"
  outputs_guaranteed: "do_dry_run() iterates components in order, propagates --dry-run, NO side effects"
  non_goals:
    - "DO NOT modify other mode functions (do_resume/do_validate/do_rollback are 11c/d/e)"
    - "DO NOT modify arg parser (11a owns)"
    - "DO NOT modify state schema (locked by 11a)"
    - "DO NOT actually execute any component script"
boundaries:
  loc_delta_max: 80
  files_touched_max: 1
  required_paths: [scripts/setup-dev-env-full.sh]
  forbidden_paths: [scripts/setup-dev-env-full/]
  test_scope: defer-to-integration
  execution_mode: structural-only
  destructive_ops_allowed: false
  dependency_artifacts: [scripts/setup-dev-env-full.sh skeleton from 11a]
  interface_contract:
    inputs_from_deps: [skeleton + helpers + arg parser]
    outputs_for_downstream: [bash setup-dev-env-full.sh --dry-run produces "would..." output, exit 0]
ac.code:
  - desc: do_dry_run function exists
    verify: {command: "grep -cE '^do_dry_run\\s*\\(\\)' scripts/setup-dev-env-full.sh", expect_stdout_match: "[1-9]"}
  - desc: --dry-run prints would-be actions
    verify: {command: "bash scripts/setup-dev-env-full.sh --dry-run 2>&1 | grep -cE '(would|DRY-RUN|skip)'", expect_stdout_match: "^[1-9][0-9]*$"}
  - desc: NO real side effects
    verify: {command: "bash scripts/setup-dev-env-full.sh --dry-run 2>&1 | grep -cE '^\\+\\s+(apt-get install|docker compose up|chmod 600)'", expect_stdout_match: "^0$"}
go_live: T+0.5d after 11a
```

### 31.A-11c — --resume mode (reads locked state schema)

```yaml
tier: S, prefer: claude, class: subscription-claude
area_labels: [area:devops, area:tooling]
blockedBy: [11a]
context_hint:
  produces: "patch to scripts/setup-dev-env-full.sh — do_resume()"
  consumed_by: "31.A-11f, operator after partial failure"
  inputs_expected: "Master skeleton + state schema FROZEN by 11a (do NOT change schema)"
  outputs_guaranteed: "do_resume() reads setup-state.json (schema v1), determines last_completed_step, resumes from next step"
  non_goals:
    - "DO NOT modify state-file schema — 11a froze it"
    - "DO NOT modify do_dry_run / do_validate / do_rollback"
    - "DO NOT modify arg parser or helpers"
    - "DO NOT re-implement component scripts inline"
    - "CRITICAL — codex flagged state-machine logic as HIGH drift risk; this ticket ONLY adds resume-read+dispatch; touching any other mode = scope creep → file follow-up"
boundaries:
  loc_delta_max: 100
  files_touched_max: 1
  required_paths: [scripts/setup-dev-env-full.sh]
  forbidden_paths: [scripts/setup-dev-env-full/]
  test_scope: defer-to-integration
  execution_mode: structural-only
  destructive_ops_allowed: false
  dependency_artifacts: [scripts/setup-dev-env-full.sh skeleton from 11a]
  interface_contract:
    inputs_from_deps: [state-file schema v1 frozen; helpers exist]
    outputs_for_downstream: [bash setup-dev-env-full.sh --resume from partial state.json resumes correctly]
ac.code:
  - desc: do_resume function exists
    verify: {command: "grep -cE '^do_resume\\s*\\(\\)' scripts/setup-dev-env-full.sh", expect_stdout_match: "[1-9]"}
  - desc: reads state file via jq
    verify: {command: "grep -cE 'jq -r.*last_completed_step.*setup-state.json' scripts/setup-dev-env-full.sh", expect_stdout_match: "[1-9]"}
  - desc: handles state-file-missing case
    verify: {command: "grep -cE 'setup-state\\.json.*not.*exist|missing.*state' scripts/setup-dev-env-full.sh", expect_stdout_match: "[1-9]"}
  - desc: did NOT modify other mode funcs
    verify: {command: "git diff HEAD~1 HEAD scripts/setup-dev-env-full.sh -- | grep -cE '^[+\\-]do_(dry_run|validate|rollback)'", expect_stdout_match: "^0$"}
decision_points:
  - id: dp_state_corrupt
    trigger: "cat ~/.cache/omnisight/setup-state.json 2>&1 | jq -r .last_completed_step"
    pause_if: "null or parse error"
    operator_label: needs-operator-decision-resume-state
go_live: T+0.5d after 11a
```

### 31.A-11d — --validate mode delegate

```yaml
tier: S, prefer: claude, class: subscription-claude
area_labels: [area:devops, area:tests]
blockedBy: [11a]
context_hint:
  produces: "patch to scripts/setup-dev-env-full.sh — do_validate()"
  consumed_by: "31.A-11f, Integration validation step"
  inputs_expected: "Skeleton (11a); pytest will exist after 12a/12b but this ticket only adds delegation"
  outputs_guaranteed: "do_validate() invokes `pytest scripts/setup-dev-env-full/tests/ -v --tb=short`; exit code propagates"
  non_goals:
    - "DO NOT write the pytest tests — 12a/12b own"
    - "DO NOT modify other mode functions"
    - "DO NOT add inline assertions — pure delegation"
boundaries:
  loc_delta_max: 50
  files_touched_max: 1
  required_paths: [scripts/setup-dev-env-full.sh]
  forbidden_paths: [scripts/setup-dev-env-full/, scripts/setup-dev-env-full/tests/]
  test_scope: defer-to-integration
  execution_mode: structural-only
  destructive_ops_allowed: false
  dependency_artifacts: [scripts/setup-dev-env-full.sh skeleton]
  interface_contract:
    inputs_from_deps: [skeleton; pytest available from 1a]
    outputs_for_downstream: [bash setup-dev-env-full.sh --validate delegates to pytest]
ac.code:
  - desc: do_validate exists
    verify: {command: "grep -cE '^do_validate\\s*\\(\\)' scripts/setup-dev-env-full.sh", expect_stdout_match: "[1-9]"}
  - desc: invokes pytest on tests dir
    verify: {command: "grep -cE 'pytest.*setup-dev-env-full/tests' scripts/setup-dev-env-full.sh", expect_stdout_match: "[1-9]"}
go_live: T+0.5d after 11a
```

### 31.A-11e — --rollback mode

```yaml
tier: S, prefer: claude, class: subscription-claude
area_labels: [area:devops, area:tooling]
blockedBy: [11a]
context_hint:
  produces: "patch to scripts/setup-dev-env-full.sh — do_rollback()"
  consumed_by: "31.A-11f, operator at Integration if retry needed"
  inputs_expected: "Skeleton (11a); state-file schema (11a)"
  outputs_guaranteed: "do_rollback() reads state.json, iterates completed steps in REVERSE, calls each with --rollback if supported, else logs `manual rollback required for step X`"
  non_goals:
    - "DO NOT modify component scripts to add --rollback support (SEPARATE follow-ups if needed)"
    - "DO NOT actually rollback during ticket (structural only)"
    - "DO NOT modify other mode functions"
boundaries:
  loc_delta_max: 80
  files_touched_max: 1
  required_paths: [scripts/setup-dev-env-full.sh]
  forbidden_paths: [scripts/setup-dev-env-full/]
  test_scope: defer-to-integration
  execution_mode: structural-only
  destructive_ops_allowed: false  # rollback IS destructive, but operator-only at runtime
  dependency_artifacts: [scripts/setup-dev-env-full.sh skeleton]
  interface_contract:
    inputs_from_deps: [skeleton + state schema]
    outputs_for_downstream: [bash setup-dev-env-full.sh --rollback iterates reverse + delegates]
ac.code:
  - desc: do_rollback exists
    verify: {command: "grep -cE '^do_rollback\\s*\\(\\)' scripts/setup-dev-env-full.sh", expect_stdout_match: "[1-9]"}
  - desc: iterates state in reverse
    verify: {command: "grep -cE 'tac|tail -r|reverse' scripts/setup-dev-env-full.sh", expect_stdout_match: "[1-9]"}
  - desc: requires operator confirmation
    verify: {command: "grep -cE 'read -p|confirm' scripts/setup-dev-env-full.sh", expect_stdout_match: "[1-9]"}
go_live: T+0.5d after 11a
```

### 31.A-11f — Orchestration sequence wiring

```yaml
tier: S, prefer: claude, class: subscription-claude
area_labels: [area:devops, area:tooling]
blockedBy: [11a, 11b, 11c, 11d, 11e]
context_hint:
  produces: "patch to scripts/setup-dev-env-full.sh — main_orchestrate() calls 01-10 in DAG order, writes state after each success"
  consumed_by: "Integration ticket (operator runs end-to-end)"
  inputs_expected: "Master skeleton (11a) + 4 modes (11b/c/d/e) + state schema v1 frozen"
  outputs_guaranteed: "main_orchestrate() runs steps: 01a, 01b||01c, 02a, 02b, 02c||02d, 02e, 03a||03b, 03c, 04a, 04b, 05a, 05b, 05c, 06a, 06b||06c, 06d (if flag), 07, 08a, 08b, 08c, 09a, 10a; writes state after each"
  non_goals:
    - "DO NOT modify component scripts (1-10 own)"
    - "DO NOT change state-file schema (11a froze)"
    - "DO NOT modify mode functions (11b/c/d/e own)"
boundaries:
  loc_delta_max: 120
  files_touched_max: 1
  required_paths: [scripts/setup-dev-env-full.sh]
  forbidden_paths: [scripts/setup-dev-env-full/]
  test_scope: defer-to-integration
  execution_mode: structural-only
  destructive_ops_allowed: false
  dependency_artifacts: [skeleton + all 4 mode funcs + all 10 component scripts]
  interface_contract:
    inputs_from_deps: [all component scripts + 4 mode functions]
    outputs_for_downstream: [bash setup-dev-env-full.sh runs full provisioning end-to-end]
ac.code:
  - desc: main_orchestrate exists
    verify: {command: "grep -cE '^main_orchestrate\\s*\\(\\)' scripts/setup-dev-env-full.sh", expect_stdout_match: "[1-9]"}
  - desc: calls all 10 component scripts
    verify: {command: "grep -cE 'setup-dev-env-full/(01|02|03|04|05|06|07|08|09|10)-' scripts/setup-dev-env-full.sh", expect_stdout_match: "^[1-9][0-9]*$"}
  - desc: writes state after each step
    verify: {command: "grep -cE 'write_state.*step_' scripts/setup-dev-env-full.sh", expect_stdout_match: "^[1-9][0-9]*$"}
go_live: T+0.5d after 11a-11e
```

### 31.A-12a — pytest skeleton + integration mark

```yaml
tier: S, prefer: claude, class: subscription-claude
area_labels: [area:tests, area:devops]
blockedBy: [11f]
context_hint:
  produces: "scripts/setup-dev-env-full/tests/conftest.py + tests/test_validation_suite.py (skeleton + 1 sample test)"
  consumed_by: "31.A-12b (adds per-step assertions), 31.A-11d (--validate delegates here)"
  inputs_expected: "Master orchestrator wired (11f)"
  outputs_guaranteed: "pytest skeleton with fixtures; 1 sample test for step_01 demonstrating integration mark; pyproject.toml has integration mark registered"
  non_goals:
    - "DO NOT write the 10+ per-step assertion functions — 12b owns"
    - "DO NOT modify master script — 11a-f own"
boundaries:
  loc_delta_max: 100
  files_touched_max: 3
  required_paths: [scripts/setup-dev-env-full/tests/conftest.py, scripts/setup-dev-env-full/tests/test_validation_suite.py, pyproject.toml]
  forbidden_paths: [scripts/setup-dev-env-full.sh, scripts/setup-dev-env-full/0[1-9]-, scripts/setup-dev-env-full/10-]
  test_scope: inline
  execution_mode: unit-testable
  destructive_ops_allowed: false
  dependency_artifacts: [scripts/setup-dev-env-full.sh]
  interface_contract:
    inputs_from_deps: [master script callable]
    outputs_for_downstream: [pytest skeleton; sample test passes; mark registered]
ac.code:
  - desc: conftest.py + test file exist
    verify: {command: "test -f scripts/setup-dev-env-full/tests/conftest.py && test -f scripts/setup-dev-env-full/tests/test_validation_suite.py", expect_exit_code: 0}
  - desc: pytest collects >=1 test
    verify: {command: "pytest --collect-only scripts/setup-dev-env-full/tests/test_validation_suite.py 2>&1 | grep -c 'test_'", expect_stdout_match: "[1-9]"}
  - desc: integration mark registered
    verify: {command: "grep -c 'pytest.mark.integration\\|integration:' pyproject.toml", expect_stdout_match: "[1-9]"}
go_live: T+0.5d after 11f
```

### 31.A-12b — Per-step assertion functions + result caching (MERGED v1 12b+12c)

```yaml
tier: S, prefer: claude, class: subscription-claude
area_labels: [area:tests, area:devops]
blockedBy: [12a]
context_hint:
  produces: "patch to scripts/setup-dev-env-full/tests/test_validation_suite.py — 10 step-specific test functions + result caching layer (git_sha + step keyed)"
  consumed_by: "31.A-Integration (validation step)"
  inputs_expected: "pytest skeleton + sample test (12a)"
  outputs_guaranteed: "10 test_step_<NN>_validate() functions; each does real host check (file exists, container healthy, etc.); cache writes ~/.cache/omnisight/validation-cache.json keyed on (git_sha, step) for repeat short-circuit"
  non_goals:
    - "DO NOT modify conftest.py — 12a owns"
    - "DO NOT modify master script"
    - "DO NOT add tests for steps NOT in the 10-script set"
boundaries:
  loc_delta_max: 200
  files_touched_max: 1
  required_paths: [scripts/setup-dev-env-full/tests/test_validation_suite.py]
  forbidden_paths: [scripts/setup-dev-env-full/tests/conftest.py, scripts/setup-dev-env-full.sh, scripts/setup-dev-env-full/0[1-9]-, scripts/setup-dev-env-full/10-]
  test_scope: inline
  execution_mode: unit-testable
  destructive_ops_allowed: false
  dependency_artifacts: [conftest.py from 12a]
  interface_contract:
    inputs_from_deps: [conftest fixtures, pytest infra]
    outputs_for_downstream: [10+ test functions + caching layer]
ac.code:
  - desc: >=10 test_step_ functions
    verify: {command: "grep -cE '^def test_step_' scripts/setup-dev-env-full/tests/test_validation_suite.py", expect_stdout_match: "^([1-9][0-9]|1[0-9])$"}
  - desc: all marked integration
    verify: {command: "grep -cE 'pytest.mark.integration' scripts/setup-dev-env-full/tests/test_validation_suite.py", expect_stdout_match: "^([1-9][0-9]|1[0-9])$"}
  - desc: cache layer present
    verify: {command: "grep -cE 'validation-cache\\.json' scripts/setup-dev-env-full/tests/test_validation_suite.py", expect_stdout_match: "^[2-9]$"}
  - desc: cache key includes git_sha
    verify: {command: "grep -cE 'git_sha|git rev-parse' scripts/setup-dev-env-full/tests/test_validation_suite.py", expect_stdout_match: "[1-9]"}
go_live: T+1d after 12a
```

### 31.A-Integration-Doc — E2E test runbook (doc)

```yaml
tier: S, prefer: claude, class: subscription-claude
area_labels: [area:docs, area:devops, area:tests]
blockedBy: [11f]
context_hint:
  produces: "docs/audit/AUDIT-31-phase-A-evidence/runbook.md"
  consumed_by: "31.A-Integration (operator follows this runbook)"
  inputs_expected: "Master script wired (11f)"
  outputs_guaranteed: "Step-by-step runbook: (1) wsl --terminate Ubuntu-26.04, (2) wsl --unregister + reinstall OR rm key paths, (3) start fresh WSL, (4) run setup-dev-env-full.sh, (5) wait, (6) run --validate, (7) resume one tmux session, (8) file test JIRA ticket, (9) confirm pickup, (10) commit evidence"
  non_goals:
    - "DO NOT execute any step (operator at Integration owns execution)"
    - "DO NOT modify master script or validation suite"
boundaries:
  loc_delta_max: 300
  files_touched_max: 1
  required_paths: [docs/audit/AUDIT-31-phase-A-evidence/runbook.md]
  forbidden_paths: [scripts/setup-dev-env-full.sh, scripts/setup-dev-env-full/tests/]
  test_scope: none
  execution_mode: operator-rehearsal
  destructive_ops_allowed: false
  dependency_artifacts: [scripts/setup-dev-env-full.sh]
  interface_contract:
    inputs_from_deps: [master script + 4 modes wired]
    outputs_for_downstream: [runbook ready for operator at Integration]
ac.code:
  - desc: runbook ≥10 numbered steps
    verify: {command: "grep -cE '^(##|###) Step [0-9]+|^[0-9]+\\. ' docs/audit/AUDIT-31-phase-A-evidence/runbook.md", expect_stdout_match: "^([1-9][0-9]|1[0-9])$|^[2-9][0-9]+$"}
  - desc: references wsl --terminate / --unregister
    verify: {command: "grep -cE 'wsl --terminate|wsl --unregister' docs/audit/AUDIT-31-phase-A-evidence/runbook.md", expect_stdout_match: "[1-9]"}
go_live: T+1d after 11f
```

### 31.A-Integration — **E2E fresh-WSL provision test** (TIER:L)

```yaml
title: "AUDIT-31.A-Integration: End-to-end fresh Ubuntu-26.04 WSL provisioning test"
tier: L
prefer: claude
class: subscription-claude + operator-window
area_labels: [area:devops, area:tests, area:docs, area:tooling]
type_label: type:integration   # Pattern 2 SOP §3
blockedBy: [1a, 1b, 1c, 2a, 2b, 2c, 2d, 2e, 3a, 3b, 3c, 4a, 4b, 5a, 5b, 5c, 6a, 6b, 6c, 6d, 7, 8a, 8b, 8c, 9a, 10a, 10b, 11a, 11b, 11c, 11d, 11e, 11f, 12a, 12b, Integration-Doc]   # ALL 36 siblings
context_hint:
  produces: "docs/audit/AUDIT-31-phase-A-evidence/integration.json + final commit"
  consumed_by: "Sprint S12 Phase 31.A sub-META closure"
  inputs_expected: "All 36 sibling tickets closed"
  outputs_guaranteed: |
    - operator did: wsl --terminate Ubuntu-26.04 → re-provision from scratch
    - master script ran end-to-end in <30 min target
    - --validate green (all 10+ pytest pass)
    - one tmux session resumed; runner picked up test ticket; full Gerrit cycle clean
    - 24h post-observation: 0 unhealthy containers
    - integration.json committed
  non_goals:
    - "DO NOT 'fix' a failing component script here — file follow-up + re-test"
    - "DO NOT extend Phase 31.A scope based on what's discovered"
boundaries:
  loc_delta_max: 50
  files_touched_max: 1
  required_paths: [docs/audit/AUDIT-31-phase-A-evidence/integration.json]
  forbidden_paths: [scripts/setup-dev-env-full.sh, scripts/setup-dev-env-full/, docs/audit/AUDIT-31-phase-A-evidence/runbook.md]
  test_scope: inline
  execution_mode: requires-WSL + operator-rehearsal
  destructive_ops_allowed: true  # wsl --unregister IS destructive (operator-supervised)
  dependency_artifacts: [all 36 sibling outputs + runbook.md]
  interface_contract:
    inputs_from_deps: [everything from 1a through Integration-Doc]
    outputs_for_downstream: [integration.json → sub-META closure → Sprint S12 progresses to Phase 31.B]
ac.code:
  - desc: integration.json present with schema
    verify: {command: "python3 -c \"import json; d=json.load(open('docs/audit/AUDIT-31-phase-A-evidence/integration.json')); assert all(k in d for k in ['sub_steps','overall_pass','duration_seconds','runner_pickup_test','post_24h_health'])\"", expect_exit_code: 0}
ac.deploy:
  - desc: fresh WSL run + validate green
    verify: {command: "bash scripts/setup-dev-env-full.sh --validate", expect_exit_code: 0}
    run_as: operator
  - desc: completion < 30 min
    verify: {command: "jq -r .duration_seconds docs/audit/AUDIT-31-phase-A-evidence/integration.json", expect_stdout_match: "^([0-9]|[1-9][0-9]|1[0-7][0-9]{2})$"}
    run_as: operator
ac.integration:
  - desc: 10+ sub-validations green
    verify: {command: "jq -r '[.sub_steps[]|select(.pass==false)]|length' docs/audit/AUDIT-31-phase-A-evidence/integration.json", expect_stdout_match: "^0$"}
  - desc: runner pickup cycle passed
    verify: {command: "jq -r .runner_pickup_test.passed docs/audit/AUDIT-31-phase-A-evidence/integration.json", expect_stdout_match: "^true$"}
    run_as: operator
ac.exercised:
  - desc: 24h post-observation healthy
    verify: {command: "jq -r .post_24h_health docs/audit/AUDIT-31-phase-A-evidence/integration.json", expect_stdout_match: "^healthy$"}
    run_as: operator
    timeout_seconds: 86400
decision_points:
  - id: dp_partial_validation_failure
    trigger: "jq -r '[.sub_steps[]|select(.pass==false)]|length' docs/audit/AUDIT-31-phase-A-evidence/integration.json"
    pause_if: ">= 1"
    action: file-followup
    followup_spec: "AUDIT-31.A-Integration follow-up: {N} failed sub-validations"
go_live: T+2d after all 36 siblings close (1d execute + 1d observation)
```

---

## §7. Filing batch order (DAG)

Per §5 table layers. Filing 37 tickets in 14 batches keeps blockedBy chains consistent + avoids JIRA orphan-link issues.

```
L0: 31.A-1a, 31.A-2a, 31.A-8a (3 tickets)
L1: 1b, 1c, 2b, 5a, 6a, 6b, 10a (7)
L2: 2c, 2d, 5b, 6c, 7, 10b (6)
L3: 2e, 3a, 3b, 5c, 6d (5)
L4: 3c (1)
L5: 4a, 8b (2)
L6: 4b, 8c (2)
L7: 9a (1)
L8: 11a (1)
L9: 11b, 11c, 11d, 11e (4)
L10: 11f (1)
L11: 12a, Integration-Doc (2)
L12: 12b (1)
L13: Integration (1)
```

---

## §8. v1 §5 open questions — operator answers (locked)

| # | Question | Operator answer |
|---|---|---|
| 1 | Cred provisioning UX: split essential/extended? | **No split — all 16+ in one go** |
| 2 | Dev postgres port | **65432** (host-expose mode only; NAT-internal stays 5432) |
| 3 | Memory stack ports / NAT vs external | **NAT isolation primary**; `--expose-host-debug` flag for opt-in |
| 4 | systemd dev-subset exact list | **Deferred to 31.A-8a curation pass** |
| 5 | Integration ticket Go-Live | **T+2d** (1d execute + 1d 24h observation) |
| 6 | prefer-hint enforcement | **Coordinator (Phase 31.B) auto-respects; operator override possible** |
| 7 | Validation suite caching | **Yes** — cache keyed on git_sha + step (31.A-12b owns) |
| 8 | Rename old setup-dev-env.sh | **Yes** — rename to `.deprecated.sh` + add redirect message |

---

## §9. Submission flow

1. This spec doc at `docs/sprint-s12/phase-31a-ticket-spec.md`
2. Commit to feature branch `feature/sprint-s12-phase-31a-spec-v2`
3. Push to Gerrit refs/for/develop
4. Operator + 1 reviewer required +2
5. Submit (merge to develop)
6. THEN run filing script `scripts/file_sprint_s12_phase_31a_tickets.py` (to be written under separate ticket)
7. Filing script reads each ticket spec block, validates against OP-1042 schema (extended with `boundaries:` + `context_hint:` per §3/§4), files in DAG batch order (§7), comments on sub-META 31.A linking 37 children
8. Post-filing spot-check: first 3 filed tickets reviewed for label + boundary correctness

---

**End of Phase 31.A ticket spec v2 (37-ticket atomic + boundaries). Awaiting operator review + Gerrit +2. Tickets NOT filed until this doc lands in develop.**
