---
id: SPRINT-S12G-A-V2-SPEC
version: v1.4 (draft 2026-05-14 — post round-2 codex review touch-up)
title: Sprint S12.G G.A-v2 — Runtime Defense Contract · Ticket Spec
scope: 5-dimension defense contract (error / exception / exit / recovery / rescue) + 5 gap families (⑤⑥⑦⑧⑨ from 2026-05-14 incident) + schema additions + plugin extensions + Prometheus alert rule wiring
status: Draft 2026-05-14 — v1.4 (post round-2 codex amendment review touch-up: SD-1 evidence commands rewritten with git-show pinned refs + real result counts; AutoRedeploy tier:S → M + boundary block; Reproduce-401 boundary block; Family ⑤/⑦/⑩ heading counts + effort estimates corrected; Family ⑥ image-head multi-head fail-build invariant; Family ⑨ punt names META-POST-RC2-CROSS-STACK-HYGIENE placeholder; v2-⑧-4a-Doc explicit "Productizer cannot detect last forced shutdown" section; required_area_labels filing-time materialize rule). **70 tickets total**; v1.0 → v1.1 (SD-1) → v1.2 (Family ⑩) → v1.3 (codex P0/P1) → v1.4 (codex round-2 caveats); all 7 design Qs locked; codex round-2 verdict was **file-ready-with-conditional**, conditional fixes now applied.
related:
  - ADR-0023 Foundation Rebuild
  - ADR-0033 Governance Engine + Operator Authority
  - ADR-0034 Override Review Lifecycle + Separation of Duties
  - docs/retrospectives/2026-05-14-host-reboot-image-db-drift.md (incident → 5 gaps)
  - sprint-s12g-governance-engine-spec.md (parent: G.A-v0 + G.A-v1 ship before this)
  - phase-31i-ticket-spec.md (31.I-2bc healthcheck-validator may consume this contract)
  - phase-31g-ticket-spec.md (31.G AM rules co-owned with this spec)
---

# Sprint S12.G G.A-v2 · Runtime Defense Contract — Ticket Spec v1.4 (draft)

## §0. Origin (audit findings 2026-05-14)

This sub-phase was created on 2026-05-14 in direct response to the host-reboot
incident documented in `docs/retrospectives/2026-05-14-host-reboot-image-db-drift.md`.

A coverage audit against the just-committed S12 / S12.G plan (`f0274e7c`)
surfaced 5 gaps the existing 11 phases + G.A-v0/v1 do **not** cover. A parallel
4-stream code audit established the current implementation state of each gap.

| # | Gap (operator-named) | Audited root cause | File:line evidence |
|---|---|---|---|
| ⑤ | Shipped-but-not-deployed runtime detector | `scripts/deployment-audit.sh` does NOT exist; daily audit cron does NOT exist; backend has no `/version` endpoint exposing image SHA / build time; AUDIT-29a-3 CI hook never implemented (memory was aspirational) | `scripts/` (file absent); `backend/main.py:435` only sets FastAPI `version="0.1.0"` metadata |
| ⑥ | Image-vs-DB alembic head drift | Detection layer ~80 % wired (gauge fed, /metrics exposes it). Missing: AM alert rule, auto-`alembic upgrade head` on container start, remediation hint in 503 response, no fail-fast guard for backward image. | `/readyz` migration probe: `backend/routers/health.py:120-178`; gauge fed: `backend/routers/health.py:431`; gauge defined: `backend/metrics.py:704-708`; container CMD bypasses alembic: `Dockerfile.backend:146`; no `deploy/prometheus/` rules found |
| ⑦ | Auth `/health` returns 401 | `auth_baseline.py:85` allowlist contains `/api/v1/health` but NOT `/health`; 4 other whitelists at `main.py:600/606/1033/1082` exempt `/health` for OTHER middlewares (graceful_shutdown / bootstrap / rate_limit) so the whitelist intent never reaches `auth_baseline`. Root cause: **no single-source-of-truth allowlist**; 4-out-of-5 whitelists are dead code for `/health` | `backend/auth_baseline.py:85`; `backend/main.py:600,606,1033,1082` |
| ⑧ | WSL2 graceful shutdown / docker 30 s timeout | `omnisight-compose-prod.service:TimeoutStopSec=60` for a 7-container stop (Docker default 10 s SIGTERM → SIGKILL × 7 ≥ 70 s minimum). No `stop_grace_period` on any prod service. `scripts/shutdown.sh` EXISTS with proper graduated timeouts (cloudflared → frontend 15 s → backend 40 s → workers 60 s) but is **NOT wired to systemd ExecStop**. WSL2 has a 10 s hard limit before `RB_POWER_OFF`. | `/etc/systemd/system/omnisight-compose-prod.service:46`; `docker-compose.prod.yml` (no `stop_grace_period` at lines 58 / 212 / 297 / 341 / 421 / 530 / 653 / 726 / 825); `scripts/shutdown.sh` exists but not invoked |
| ⑨ | Sister-project `ai_gateway` orphan | `~/work/sora/omnisight-ai-core/docker-compose.yml:33` has `restart: unless-stopped` on a gateway whose upstream `ai_engine` has been Exited (0) for 10 days → infinite crashloop. Productizer is **independent** (ollama is last in fallback chain after Anthropic/OpenAI/Google/Groq/DeepSeek/OpenRouter), so no service-uptime impact, but cross-stack daemon-resource hygiene is uncontracted. | `~/work/sora/omnisight-ai-core/docker-compose.yml:30-43`; Productizer fallback: `backend/config.py:104` |

**Key insight from audit**: today's incident exposed two qualitatively different problem classes which the operator correctly identified as one umbrella: every gap is a missing or partial *defense contract* — the feature exists, but its error / exception / exit / recovery / rescue behaviour was never explicitly designed and enforced.

This spec proposes formalizing those five dimensions into a Runtime Defense Contract, extending the G.A-v0/v1 schema, and instantiating concrete tickets that close ⑤⑥⑦⑧⑨ through that contract.

## §0.1. Review corpus + SD-1 dog-food evidence (added v1.3 per codex P0-1 + P0-2)

### Review corpus mapping (codex P0-1 fix)

The `related:` frontmatter lists cross-references but doesn't say where each lives. Codex P0-1 flagged that reviewers can't verify the spec without knowing which docs are accessible from which branch. Pinned mapping:

| Referenced doc | Live location | Status on this branch (`feature/sprint-s12-bedrock-specs-planning`) |
|---|---|---|
| `docs/adr/ADR-0023-foundation-rebuild.md` | `refs/remotes/gerrit/develop` | ❌ NOT in this branch's HEAD (this branch forked before merge); read via `git show refs/remotes/gerrit/develop:docs/adr/ADR-0023-foundation-rebuild.md` |
| `docs/adr/ADR-0033-governance-engine-and-operator-authority.md` | develop | ❌ Same as above |
| `docs/adr/ADR-0034-override-review-and-separation-of-duties.md` | develop | ❌ Same as above |
| `docs/adr/ADR-0035-runner-fsm-and-error-handling.md` | develop + 4 worktrees (claude/codex × 2) | ❌ NOT in this branch's HEAD; read from `~/work/sora/OmniSight-claude-worktree/docs/adr/ADR-0035-runner-fsm-and-error-handling.md` (211 LOC, fully shipped) |
| `docs/sprint-s12/sprint-s12g-governance-engine-spec.md` | This branch HEAD `f0274e7c` | ✅ Available; use `git show HEAD:docs/sprint-s12/sprint-s12g-governance-engine-spec.md` (working tree shows as `D` — deleted in the in-progress S12 rebuild teardown; HEAD copy is canonical) |
| `docs/sop/sprint-s12-ticket-decomposition-rules.md` | This branch HEAD `f0274e7c` | ✅ Same as above; HEAD copy authoritative |
| `docs/retrospectives/2026-05-14-host-reboot-image-db-drift.md` | This session 2026-05-14 (not yet committed) | ✅ Working tree; committed to repo on operator merge |
| `docs/architecture/2026-05-14-runner-pivot-and-three-layer-architecture.md` | This session 2026-05-14 (not yet committed) | ✅ Working tree |
| `docs/audit/codex-reviews/g-a-v2-codex-review-2026-05-14.txt` | Commit `c3f6c5e3` (2026-05-14) | ✅ Committed |
| `docs/audit/codex-reviews/runner-self-audit-and-redesign-2026-05-14.txt` | Commit `c3f6c5e3` (2026-05-14) | ✅ Committed |

For codex review of v1.3: read the develop-only docs via `git show refs/remotes/gerrit/develop:<path>`; the missing-from-branch state is by design (this branch is the planning artifact branch; the ADRs landed on develop after this branch forked).

### SD-1 dog-food evidence_grep_artifact (codex P0-2 fix)

Per the SD-1 rule introduced in §2 (this same document), novelty phrases in any ticket / ADR / architecture memo description require an accompanying `evidence_grep_artifact` block. The v1.1 + v1.2 versions of this spec failed that test (codex P0-2). v1.3 adds the missing evidence here:

```evidence_grep_artifact
# Verified novelty claims in this spec — all commands runnable from feature/sprint-s12-bedrock-specs-planning
# (uses `git show HEAD:` and `git show refs/remotes/gerrit/develop:` because some target docs are deleted
# in the in-progress S12 rebuild working-tree but present in HEAD / develop)

- claim: "Defense Contract — 5 new fields per ticket (error_detection / exception_handling / shutdown_contract / recovery_path / rescue_path)" (§2)
  cmd: git show HEAD:docs/sprint-s12/sprint-s12g-governance-engine-spec.md | grep -cE "defense_contract|error_detection|exception_handling|shutdown_contract|recovery_path|rescue_path"
  result: 0 matches in HEAD's G.A-v0/v1 governance-engine spec (verified 2026-05-14 from this branch); the 5 field names are genuinely new

- claim: "6 new forbidden_combinations rules (D1..D5 + D5-required-when-recovery-not-idempotent)" (§2)
  cmd: git show HEAD:docs/sprint-s12/sprint-s12g-governance-engine-spec.md | grep -cE "^\s*- [a-z_-]+ \+ [a-z_-]+"
  result: 3 existing forbidden_combinations entries in G.A-v0/v1 spec; the 6 D1/D2/D3/D4/D5-keyed rules introduced here are additions on top, taking total to 9

- claim: "v2-A8-SelfAudit closed-loop self-validation NEW" (§4)
  cmd: git show HEAD:docs/sprint-s12/sprint-s12g-governance-engine-spec.md | grep -cE "SelfAudit|self.audit|self_audit"
  result: 0 matches; closed-loop self-validation mechanism is new in this sub-phase

- claim: "v2-AlertBridge-Framework NEW shared plumbing" (§3)
  cmd: git show HEAD:docs/sprint-s12/phase-31g-ticket-spec.md | grep -cE "AlertBridge|alert_bridge|channel_adapter"
  result: 0 matches in 31.G ticket spec; 31.G defines AM endpoints but no shared alert-plumbing primitive; AlertBridge is new

- claim: "Family ⑩ Runner Defense Contract — backend/agents/runner_coordination.py + runner_claims table NEW (v1.2)" (§3)
  cmd: git show refs/remotes/gerrit/develop:backend/agents/jira_dispatch.py | grep -cE "runner_coordination|runner_claims"
  cmd2: git ls-tree -r refs/remotes/gerrit/develop -- backend/agents/ 2>/dev/null | grep -c "runner_coordination"
  result: 0 matches in develop's jira_dispatch.py; 0 matches for any runner_coordination module in develop's backend/agents/; the module + table are new

- claim: "SD-1 spec-discipline validator (novelty-claim-requires-evidence) NEW (v1.1)" (§2)
  cmd: git show HEAD:docs/sprint-s12/sprint-s12g-governance-engine-spec.md | grep -cE "novelty.claim|evidence_grep_artifact|spec.discipline"
  cmd2: git ls-tree -r HEAD -- docs/sop/ 2>/dev/null | grep -c "spec-discipline\|novelty"
  result: 0 matches in G.A-v0/v1 spec; 0 matches in docs/sop/ tree; concept is new in this spec (v1.1 introduction)

# Verification run 2026-05-14 (this branch); rerun to refresh as develop advances
```

**Where evidence is intentionally absent**: the architecture memo (`docs/architecture/2026-05-14-runner-pivot-and-three-layer-architecture.md`) §4 already includes a similar correction documenting that multi-tenant is NOT new (existing per ADR-0011) — that correction itself is the evidence for its own scope.

**SD-1 v1.3 scope adjustment** (codex Q5 answer): SD-1 should only fire on **historical novelty claims** (`first surfaced`, `never before documented`, `previously undocumented`, `不曾`, `從未`), not on **internal change descriptors** (`new field`, `new section`, `NEW shared plumbing` when referring to additions WITHIN the spec itself). The regex in v2-A9-SDValidator should exclude these intra-spec change markers. This narrows SD-1 to the actual anti-pattern (claiming external novelty without verification) while keeping authors free to describe their own edits.

## §1. Scope + Non-goals

### In scope
1. **5-dimension Defense Contract** added to G.A schema as 5 new required fields (per §2).
2. **G.A-v0 / G.A-v1 plugin extensions** to validate Defense Contract fields at filing time (per-field validators + cross-field rules).
3. **5 gap families** (⑤⑥⑦⑧⑨) each delivering: spec + ADR (where needed) + lib + smoke + integration + audit-evidence file (per §3).
4. **Prometheus alert rule wiring** that promotes already-existing metrics into actionable alerts (depends on 31.G LibsGate but allowed to file in parallel with 31.G).
5. `/readyz` response body **remediation contract** (structured `remediation` field with actionable next-step).

### Out of scope (deferred)
- Per-phase plugin runtime defense validation (G.B owns; this spec provides the schema + validators G.B plugins consume).
- Full cross-project lifecycle contract for arbitrary co-tenant compose stacks (⑨ ticket scope limited to **the `ai_gateway` instance + a documented decision pattern**; broader policy is post-rc2).
- Windows-side / WSL2 host-side automation (this spec covers Productizer-internal contract; Windows-side hooks remain operator manual).
- Building a chaos engineering framework (only focused chaos tests per defense family; broader framework is a future sprint).
- Backporting Defense Contract fields to all existing S12 phase tickets (G.A-v2 only requires it from G.A-v2 filings onward; existing 31.A–31.K v2 specs may add fields opportunistically, not retroactively).

## §2. Defense Contract — the 5 dimensions

Each S12 ticket (G.A-v2 onward) declares a `defense_contract` schema block.
Schema is hierarchical so phases can declare only the fields they need; G.A-v2
plugin validates presence + shape per ticket class.

```yaml
defense_contract:
  # --- D1 Error detection ---
  error_detection:
    signal: prometheus_metric | structured_log | exit_code | endpoint_status | none
    channel: <metric_name> | <log_query> | <endpoint_path> | null
    detection_latency_p99: <duration>      # e.g., "30s"; null if signal=none
    observability_test: <test_path>        # test exercising the failure path
    alert_rule_id: <prometheus_rule_id> | null   # populated when AM rule exists

  # --- D2 Exception handling ---
  exception_handling:
    enumerated_states: [<state_name>, ...]
    remediation_hint_contract: <description> | none
    user_facing: bool                      # if true, must include remediation in response
    fail_loudly: bool                      # if true, must NOT silently swallow

  # --- D3 Shutdown contract ---
  shutdown_contract:
    drain_seconds_p99: <int>               # null if feature has no lifecycle
    cleanup_sequence: [<step>, ...]        # ordered
    forced_termination_safe: bool          # is SIGKILL acceptable after grace?
    state_persistence: <description> | none

  # --- D4 Recovery path ---
  recovery_path:
    trigger_condition: <description>       # what indicates we're in failed state
    steps: [<step>, ...]                   # ordered, idempotent
    idempotent: bool                       # all steps idempotent (replay-safe)
    rto_seconds_p99: <int>                 # recovery time objective
    evidence_file: <path>                  # test / runbook proving it

  # --- D5 Rescue path ---
  rescue_path:
    trigger_condition: <description>       # "recovery failed N times" / etc.
    operator_actions: [<action>, ...]
    authority_required: L1 | L2 | L3 | none   # per G.A-v0 + ADR-0033
    audit_trail: <description>             # where rescue is logged
```

### Validator rules (added to G.A-v0 plugin via this spec)

| Rule | Forbids | Reason |
|---|---|---|
| `D1-required-for-runtime` | `runtime_capability: network-production-when-apply` + `error_detection.signal: none` | A feature that touches prod MUST be observable. |
| `D2-loud-or-none` | `exception_handling.fail_loudly: false` + `external_side_effect: network-production` | Silent failure on prod-touching code path. |
| `D2-remediation-on-user-facing` | `exception_handling.user_facing: true` + `remediation_hint_contract: none` | If error is user-visible, must give next step. |
| `D3-required-for-lifecycle` | ticket touches container / process / daemon + `shutdown_contract: null` | Lifecycle without shutdown contract = ⑧ class of bug. |
| `D4-required-for-destructive` | `destructive_op_classes: [...]` non-empty + `recovery_path.evidence_file: null` | Destructive ops without proven recovery. |
| `D5-required-when-recovery-not-idempotent` | `recovery_path.idempotent: false` + `rescue_path.trigger_condition: none` | If auto-recovery may fail, rescue path is mandatory. |

These extend G.A-v0 `forbidden_combinations` (per `sprint-s12g-governance-engine-spec.md §0 P1` table) with 6 new combinations.

### Spec-discipline validators (v1.1 addition, 2026-05-14)

Beyond the 6 *runtime-field* rules above, G.A-v2 also enforces **spec-writing discipline** at filing time. These rules scan ticket descriptions / ADRs / architecture memos (not field values), to prevent meta-bugs in the spec-authoring process itself.

The category exists because the runtime contract is only as good as the specs that consume it — and specs are written by humans + AI together, where the failure modes are different from runtime failures.

| Rule | Forbids | Reason | Enforcement |
|---|---|---|---|
| **SD-1 `novelty-claim-requires-evidence`** | Ticket / ADR / architecture-memo description text containing novelty phrases (`first surfaced`, `new context`, `never before documented`, `previously undocumented`, `初めて`, `第一次`, `没有過`, `從未`, or equivalents) WITHOUT an accompanying `evidence_grep_artifact` block listing the exact `grep` / `git log` / `git show` / `git ls-tree` commands run to verify the novelty claim | 2026-05-14 same-day recurrence proof: the "territory-before-map" principle was discussed twice in one session (once re: planning S12 phases, once re: runner runtime audit). Yet the strategic doc written **the same afternoon** still claimed "multi-tenant first surfaced today" when ADR-0011 had `"OmniSight is a multi-tenant SaaS with paying customers"` documented since ~2026-05 and 10+ backend files already referenced `tenant_id`. **Discipline cannot be enforced by memory or intent alone — must be filing-time hook.** | Filing-time validator scans description text for novelty phrases (Chinese + English + Japanese; extensible regex list). If matched, requires a fenced `evidence_grep_artifact` block of the form: ` ```evidence_grep_artifact ` then one-or-more lines like `cmd: grep -rEn "<pattern>" docs/adr/` then `result: <0 matches \| N matches at files:lines>`. Hook rejects filing if the claim is present but the evidence block is missing or empty. |

`evidence_grep_artifact` block schema:

```yaml
evidence_grep_artifact:
  - cmd: <exact shell command run, copy-pasteable>
    result: <0 matches | N matches at: file:line, file:line, ...>
  - cmd: <next command>
    result: <...>
  # repeat for every novelty claim in the doc; one block at top of doc covers all claims downstream
```

Coverage scope:
- All v2-* family tickets (G.A-v2 dog-foods this rule)
- All future ADRs filed under G.A-v2 governance
- All retrospective + architecture memo documents in `docs/retrospectives/` and `docs/architecture/`
- Optional retrofit on earlier docs (best-effort; not enforced retroactively)

Out of scope (won't trigger SD-1):
- Quoting prior text that contains novelty phrases (validator detects `>` quote markers + indented code blocks)
- Negation forms ("not new", "已有先例") — different semantic
- Hypothetical / future-tense ("if a new failure surfaces...") — discussed-but-not-claimed

Pilot enforcement: 4-week soft-fail (warning only, no rejection) starting at v2-A3 ship; then hard-fail. Operator can override per ADR-0034 if a genuine no-evidence claim is justified (e.g., something that can't be grep'd because it's a verbal operator decision).

## §3. Per-gap defense families

### Shared infrastructure (precedes ⑤ and ⑥) — `v2-AlertBridge-Framework`

**Contract spec**: [`docs/sprint-s12/2026-05-16-v2-alertbridge-framework-contract.md`](./2026-05-16-v2-alertbridge-framework-contract.md) (OP-1144). The full contract surface (required schema, severity enum, dedupe formula, cardinality cap, channel-adapter ABI, canonical-envelope normalisation rules, AM-bridge migration contract) lives there; this section carries only the rationale and ticket breakdown.

**Why**: Q6 operator decision = file AlertRule tickets BEFORE 31.G ships, but design them so the future AM bridge connection is a zero-surprise swap. Both v2-⑤-AlertRule and v2-⑥-AlertRule (and any future v2-* alert) consume this framework, so it's a single shared piece, not duplicated per family.

**Design forward-compat checklist** (the 7 AM-integration pitfalls we plan around now):

| Pitfall | Mitigation baked into framework |
|---|---|
| Label/annotation semantic drift between rule and AM | Required-label schema fixed in v2-AlertBridge-Framework: `severity`, `area`, `family`, `defense_dimension`; required annotations: `summary`, `description`, `runbook_url`, `remediation_hint` |
| Severity-vocabulary mismatch | Fixed enum `severity ∈ {page, warn, info}` (P1/P2/P3); AM routing reads this directly |
| Dedupe key collision across rules | Contract: `dedupe_key = sha256(alertname + family + sorted_critical_labels)`; rules MUST declare which labels are critical |
| Resolved-notification surprise | Policy fixed: `severity=page` emits P3 recovery on resolution; `warn`/`info` silently resolve |
| Time-series cardinality explosion | Each label capped at ≤10 distinct values; framework validator rejects rules that can produce more |
| Receiver routing inconsistency | Routing decision in framework config (severity → channel map), not in individual rules |
| Group_by / group_wait fragmentation | Standard grouping: `group_by: [severity, area]`, `group_wait: 30s`, `group_interval: 5m`, `repeat_interval: 4h` — same as 31.G 9a will use |

**v0 delivery (stdout + email adapter)**:
- Channel adapter pattern: rule fires → in-process bus → adapter resolves severity → channel (stdout for `info`, email for `warn`+`page`)
- AM bridge migration is then a config change (`channel_adapter: stdout_email` → `channel_adapter: alertmanager_webhook`), no rule changes.

**Tickets** (4):

| # | ID | Title | Tier | Class | blockedBy |
|---|---|---|---|---|---|
| 1 | v2-AlertBridge-1a | AlertRule contract spec — required labels / annotations / severity enum / dedupe / cardinality rules; channel adapter interface; AM-bridge migration contract | S | claude | G.A-v1-19 |
| 2 | v2-AlertBridge-1bc | `backend/alerting/bridge.py` — stdout + email channel adapter + label-cardinality validator + dedupe key generator + tests | S | codex | v2-AlertBridge-1a |
| 3 | v2-AlertBridge-2bc | `scripts/promote-alert-rule.py` — lints a rule YAML against the contract before adding to `deploy/prometheus/rules/`; CI hook | S | codex | v2-AlertBridge-1bc |
| 4 | v2-AlertBridge-AMMigrationTest | Migration smoke test design: synthetic alert fires → stdout adapter delivers payload P; switch adapter to AM webhook → AM delivers payload P' where `canonical_envelope(P) == canonical_envelope(P')` after normalisation (strip `fingerprint` / `startsAt` / `endsAt` / `generatorURL` / AM-injected receiver labels; preserve `severity` / `area` / `family` / `defense_dimension` / `runbook_url` / `remediation_hint`). **Codex P1-7 amendment**: byte-equivalence was wrong invariant — AM injects fields stdout cannot match. Canonical envelope contract = the actual portability guarantee | M | claude | v2-AlertBridge-1bc |

After this framework ships, v2-⑤-AlertRule and v2-⑥-AlertRule each become **just a rule YAML + a 1-line registration** (no plumbing code).



### §3.0.5. Critical-ticket boundary declarations (v1.3 per codex P0-4)

Codex P0-4 flagged that the v1.1/v1.2 ticket tables are overview-only; G.A-v1 quality requires per-child `boundaries:` blocks. Full per-child specs at OP-1079 detail level (300+ LOC each) are deferred to v2 codex amendment cycle. v1.3 lands the **highest-blast-radius tickets' boundaries inline** so filing-time validation can run on them.

**Filing-time materialization rule (v1.4, codex round-2 P2-6)**: every `required_area_labels:` entry inside a boundary block MUST be materialized as a JIRA label at filing time — they are NOT only stored as YAML metadata. The filing hook (extension of OP-1042) reads `required_area_labels` from each ticket's boundary block and rejects filing if the JIRA payload's `labels` field doesn't contain every entry. This closes the SOP §2 path-to-area rule loop: the YAML expresses intent, the JIRA payload carries the runtime label, the filing hook enforces consistency. Same rule applies to `required_paths:` — those don't materialize as labels but must be added to the ticket's description AC section so codex/claude pickup can verify "did I only touch files in the allowed set".

```yaml
# v2-⑥-RescueCLI (DB downgrade + manual upgrade + backup + L2 fingerprint)
boundaries:
  mutex_with: [prod-deploy, alembic-upgrade-on-startup, backup-restore, v2-⑥-2bc]
  destructive_op_classes: [db-downgrade, manual-schema-upgrade, backup-create]
  destructive_op_scope: system
  scope_components: [backend, alembic, ops-cli, audit-log]
  external_side_effect: network-production
  external_payload_class: key-material  # backup encryption needs GPG
  runtime_capability: network-production-when-apply
  evidence_class: operator-rehearsal
  tag_type: operator-window
  required_paths:
    - backend/alembic/**
    - scripts/omnisight-rescue/**
    - backend/auth/l2_fingerprint.py
  required_area_labels: [area:backend, area:db, area:security, area:ops]
  authority_required: L2  # per ADR-0033; downgrade is L2-gated
tier: M  # codex P1-1: was tier:S in v1.2; not atomic
```

```yaml
# v2-⑧-2a (systemd unit refactor — TimeoutStopSec + ExecStop wiring)
boundaries:
  mutex_with: [prod-compose-restart, windows-shutdown-drill]
  destructive_op_classes: [service-stop, systemd-unit-change]
  destructive_op_scope: system  # touches /etc/systemd outside repo
  scope_components: [deploy/systemd, scripts]
  external_side_effect: local-host-service
  runtime_capability: production-lifecycle-when-apply
  evidence_class: operator-rehearsal  # systemd-analyze verify + dry-run
  tag_type: operator-prepare-only
  required_paths:
    - deploy/systemd/omnisight-compose-prod.service
    - scripts/shutdown.sh
  required_area_labels: [area:devops, area:deployment]
  authority_required: L2
```

```yaml
# v2-⑤-2bc-Timer (systemd timer + JSON evidence + Discord post)
boundaries:
  mutex_with: [deployment-audit-run, prod-compose-recreate]
  destructive_op_classes: []
  destructive_op_scope: project
  scope_components: [scripts, docs/audit, deploy/systemd, integration:discord]
  external_side_effect: network-production  # Discord webhook post
  external_payload_class: operational
  runtime_capability: scheduled-production-audit
  evidence_class: audit-log
  tag_type: operator-prepare-only
  required_paths:
    - scripts/deployment-audit.sh
    - deploy/systemd/omnisight-deployment-audit.{service,timer}
    - docs/audit/AUDIT-deployment/**
  required_area_labels: [area:devops, area:tooling, area:integration]
```

```yaml
# v2-AlertBridge-1bc (channel adapter + cardinality + dedupe lib)
boundaries:
  mutex_with: [v2-⑤-AlertRule, v2-⑥-AlertRule, v2-⑩-AlertRule]  # all consume bridge
  destructive_op_classes: []
  destructive_op_scope: project
  scope_components: [backend/alerting, backend/metrics, integration:email]
  external_side_effect: email-send  # warn/page severity routes to email
  external_payload_class: operational
  runtime_capability: alert-routing
  evidence_class: unit + adapter-fake-integration
  required_paths:
    - backend/alerting/**
    - backend/tests/test_alerting_*.py
  required_area_labels: [area:backend, area:integration]
  cardinality_cap: 10  # per AlertBridge contract; hard-fail rule lint above this
```

```yaml
# v2-⑦-2bc (refactor 5 middleware → consume PUBLIC_PATH_ALLOWLIST)
boundaries:
  mutex_with: [any-auth-middleware-edit, v2-⑦-FixHealth]
  destructive_op_classes: [auth-policy-change]
  destructive_op_scope: project
  scope_components: [backend/auth, backend/middleware]
  external_side_effect: none  # internal middleware only
  runtime_capability: production-auth-policy-when-apply
  evidence_class: auth-contract-tests
  required_paths:
    - backend/auth_baseline.py
    - backend/auth.py
    - backend/main.py
    - backend/middleware_allowlist.py
    - backend/tests/test_*auth*
  required_area_labels: [area:backend, area:auth, area:security]
  class_override_required: yes  # codex picking up auth/security needs explicit operator approval per coordination.md:31-57
```

```yaml
# v2-⑩-2d (claim acquisition in 3 runner scripts; finally-block on every terminal path)
boundaries:
  mutex_with: [any-runner-script-edit]
  destructive_op_classes: [runner-control-flow-change]
  destructive_op_scope: project
  scope_components: [auto-runner-codex, auto-runner-jira, auto-runner-multi, backend/agents/jira_dispatch]
  external_side_effect: jira-write  # claim mutation via release_claim
  runtime_capability: production-lifecycle-when-apply
  evidence_class: integration  # cross-runner concurrency test required
  required_paths:
    - auto-runner-codex.py
    - auto-runner-jira.py
    - auto-runner-multi.py
    - backend/agents/jira_dispatch.py
    - backend/agents/runner_coordination.py  # consumes lib from v2-⑩-1bc
  required_area_labels: [area:backend, area:tooling]
  authority_required: L2  # changes the production runner control path
```

```yaml
# v2-⑩-RescueCLI (operator override CLI — stuck-claim dump + force-release)
boundaries:
  mutex_with: [v2-⑩-2d]
  destructive_op_classes: [claim-force-release]
  destructive_op_scope: system  # cross-runner state mutation
  scope_components: [scripts, backend/agents/runner_coordination, audit-log]
  external_side_effect: jira-write
  runtime_capability: network-production-when-apply
  evidence_class: operator-rehearsal
  tag_type: operator-window
  required_paths:
    - scripts/omnisight-rescue/runner_rescue.py
    - backend/agents/runner_coordination.py
  required_area_labels: [area:tooling, area:ops]
  authority_required: L2
```

```yaml
# v2-⑤-AutoRedeploy (v1.3 addition; cron-based prod compose pull + recreate)
boundaries:
  mutex_with: [prod-compose-restart, v2-⑥-2bc, v2-⑥-RescueCLI]
  destructive_op_classes: [service-recreate, image-pull]
  destructive_op_scope: system  # prod compose recreate is system-scoped
  scope_components: [docker-compose, deploy/systemd, scripts]
  external_side_effect: network-production  # GHCR digest poll + image pull
  external_payload_class: operational
  runtime_capability: scheduled-production-redeploy
  evidence_class: operator-rehearsal  # first run is operator-witnessed
  tag_type: operator-prepare-only
  required_paths:
    - docker-compose.prod.yml
    - deploy/systemd/omnisight-auto-redeploy.{service,timer}
    - scripts/auto-redeploy.sh
  required_area_labels: [area:devops, area:deployment, area:ops]
  authority_required: L2  # changes prod recreate cadence
tier: M  # v1.4 (codex round-2 P1): was tier:S, bumped because prod compose recreate + GHCR pull is M-scoped not atomic
# Cross-family: must respect v2-⑥-2bc backward-fail-fast — stale image into ahead-DB = exit 78
```

```yaml
# v2-⑦-Reproduce-401 (v1.3 addition; failing CI test enumerating auth decision points)
boundaries:
  mutex_with: []  # discovery-only, no other auth work in flight needed
  destructive_op_classes: []
  destructive_op_scope: project
  scope_components: [backend/tests, backend/auth, backend/middleware, ci]
  external_side_effect: none  # test-only; no prod hit
  runtime_capability: unit-only
  evidence_class: behavioral-smoke  # failing test = regression gate
  required_paths:
    - backend/tests/test_health_endpoint_auth_baseline_enforce_mode.py  # new
    - backend/auth_baseline.py  # read-only audit
    - backend/main.py  # read-only audit
    - backend/auth.py  # read-only audit
    - .github/workflows/ci.yml  # add to CI
  required_area_labels: [area:tests, area:auth, area:security, area:ci]
  authority_required: L3  # test work is L3-pickup
  # codex round 1 Q-D #4 originally flagged area:auth/security needs claude not codex; for a failing-test
  # discovery ticket this is relaxed because no production code path changes
```

**Boundaries for the remaining 61 tickets** to be added in v2 amendment cycle (per codex P0-4 amendment plan). **Filing gate (v1.4 codex round-2 caveat)**: no child ticket may file to JIRA until its own boundary block exists. The 9 above (7 from v1.3 + 2 added v1.4) are the highest-blast-radius / cross-area / operator-window cases — they are the ones where ambiguous boundaries are most expensive at filing time.

### Family ⑥ — Image-vs-DB alembic head drift  ⚠ HIGHEST PRIORITY

**Why highest priority**: this is the gap that produced today's outage; affects every prod restart; biggest design blind spot in S12 (S12 implicitly assumes drift is impossible).

**Defense dimensions covered**: D1 (detection) ✓ partial → full; D2 (exception with remediation) → new; D3 (shutdown gate already via /readyz) ✓; D4 (recovery) → new; D5 (rescue) → new.

**Architectural decision LOCKED (operator 2026-05-14, Q1)**: **Option C — asymmetric upgrade**

- If `image_alembic_head > db_alembic_head` (image AHEAD): auto-run `alembic upgrade head` on container startup, idempotent.
- If `image_alembic_head == db_alembic_head` (aligned): no-op.
- If `image_alembic_head < db_alembic_head` (image BEHIND): **refuse to start**; container exit-code 78 (`EX_CONFIG`); structured log line + remediation hint pointing to newer image tag.
- Rationale: rolled-back image must not advance the DB further, but a forward image must not silently run on a behind DB either. Option C makes drift impossible to mask: forward drift self-heals; backward drift is loud and operator-actionable.

Rejected: Option A (auto-up always — rolled-back image advances DB, increases drift); Option B (always block — deploy script bug = hard outage).

**Tickets** (10):

| # | ID | Title | Tier | Class | Defense dim | blockedBy |
|---|---|---|---|---|---|---|
| 1 | v2-⑥-1a | Image-vs-DB drift spec + Option C ADR rationale | S | claude | D1+D2+D4 (spec) | G.A-v1-19 |
| 2 | v2-⑥-2a | ADR-0036 forward-only-deploy invariant (canary-runner canonical path; Option C asymmetric upgrade) | S | claude | D4 (recovery contract) | v2-⑥-1a |
| 3 | v2-⑥-1bc | `alembic_drift` Prometheus gauge promoted to always-on routine scrape (decouple from /readyz call) + tests | S | codex | D1 | v2-⑥-1a |
| 4 | v2-⑥-2bc | Backend startup `alembic upgrade head` hook (idempotent; fail-fast if behind DB) + tests | S | codex | D4 | v2-⑥-2a |
| 5 | v2-⑥-3bc | `/readyz` 503 response add `remediation` field (e.g., `"redeploy backend image; current image lacks 0201,0202"`) + tests | S | codex | D2 | v2-⑥-1a |
| 6 | v2-⑥-AlertRule | Prometheus rule `OmniSightAlembicDrift` (fires on `omnisight_readyz_migrations_pending == 1` for >5 min) — declared per v2-AlertBridge-1a contract: `severity=page`, `area=deployment`, `family=⑥`, `defense_dimension=D1`, `runbook_url`, `remediation_hint`, dedupe by `(alertname, family, instance)`; v0 delivery via stdout+email adapter | S | claude | D1 | v2-⑥-1bc, v2-AlertBridge-1bc |
| 7 | v2-⑥-RescueCLI | Operator rescue CLI (`omnisight rescue drift --confirm`): supports DB downgrade with backup + manual upgrade with override; requires L2 fingerprint per ADR-0033. **v1.3 (codex P1-1)**: must implement the 7 deterministic subcontracts in §3.0.6 below (migration lock acquire / image-head resolution / forward hook / backward fail-fast / rescue backup capture / downgrade eligibility matrix / audit event writer) — each subcontract is a contract test, not just prose | M | claude + operator-window | D5 | v2-⑥-2bc, G.A-v0-A7 (manual override CLI) |
| 8 | v2-⑥-DRDrillForward | Chaos test: synthetic forward-drift scenario (image at N-1, DB at N) → recover via redeploy + verify alert fires | S | claude | D1+D4 | v2-⑥-2bc, v2-⑥-AlertRule |
| 9 | v2-⑥-DRDrillBackward | Chaos test: synthetic backward-drift scenario (image at N, DB at N-1) → fail-fast verified; rescue CLI exercised | S | claude + operator-rehearsal | D5 | v2-⑥-RescueCLI |
| 10 | v2-⑥-Integration | End-to-end soak: 14 days of drift-injection ticks (random subset) + 0 false-positive alerts + correct remediation in 100% of synthetic drift cases | L | claude | D1+D2+D4+D5 | all above |

#### §3.0.6 Family ⑥ deterministic subcontracts (v1.3 per codex P1-1)

Codex P1-1 flagged that v1.2's D4/D5 was "hand-wavy" — real drift causes include manual prod-DB alembic, cross-branch head merge, rollback residue. v1.3 makes each subcontract testable:

1. **Migration lock acquire** — `alembic_lock` advisory-lock key (Postgres `pg_try_advisory_lock`); on contention, second backend replica waits (max 60 s) then fails-fast with exit 78. Contract test: 2 concurrent replicas; exactly one runs upgrade; other waits / exits cleanly.
2. **Image-head resolution** — baked at `Dockerfile.backend` build time into `/app/MANIFEST.json` (`alembic_head_in_image` field per v2-⑤-Dockerfile-Manifest). Startup reads MANIFEST not filesystem (filesystem can be tampered post-build). **Build-time invariant (v1.4 codex caveat)**: `alembic heads` MUST return exactly one head; if multiple heads (un-merged branch divergence), build FAILS with structured remediation pointing at the `alembic merge` command. No "pick first / pick lexicographically last" silent resolution — multi-head is a developer error caught at build time, not a runtime ambiguity. Contract test: image build script invoked on a branch with 2 alembic heads → build exits non-zero with `multi_head_remediation` line in stderr.
3. **Forward startup hook** — if `image_alembic_head > db_alembic_head`, run `alembic upgrade head` inside the advisory lock. Idempotent — already-applied migration is no-op. Contract test: stale DB + fresh image → upgrade runs once, second startup attempt no-ops.
4. **Backward fail-fast** — if `image_alembic_head < db_alembic_head`, refuse start; exit 78; structured log: `{"event": "alembic_drift_backward", "image_head": X, "db_head": Y, "remediation": "deploy image >= Y or run rescue CLI to downgrade DB"}`. Contract test: rolled-back image + advanced DB → exit 78 verified; remediation message in log.
5. **Rescue backup capture** — before any downgrade, `pg_dump` to GPG-encrypted file at `/var/omnisight/rescue-backups/<timestamp>-<from-rev>-<to-rev>.sql.gpg`. Contract test: rescue downgrade emits backup; backup is decryptable + restores to recoverable state.
6. **Downgrade eligibility matrix** — each alembic migration tagged in its file header: `downgrade_safe: lossless | lossy-data | irreversible`. Rescue CLI reads tags; refuses downgrade if any spanned migration is `irreversible`; warns + requires `--force` if `lossy-data`. Contract test: matrix table covers all 0001-0200+ migrations.
7. **Audit event writer** — every rescue action writes `runner_audit_events(event_type, actor_fingerprint, ticket_ref, before_state, after_state, evidence_path, timestamp)` row before exit; persists across rescue process restart. Contract test: rescue CLI killed mid-flight → audit event still recorded for forensics.

These 7 subcontracts are the implementation contract for **v2-⑥-2bc** (forward startup hook + advisory lock + image-head resolution) and **v2-⑥-RescueCLI** (rescue backup + downgrade eligibility + audit event writer). v2-⑥-2bc covers subcontracts 1-4; v2-⑥-RescueCLI covers 5-7.

**Routing tally**: codex 3, claude 7. Operator-window 2 (RescueCLI, DRDrillBackward).

### Family ⑤ — Shipped-but-not-deployed runtime detector

**Contract spec doc** (filed 2026-05-16, OP-1154): [`docs/sprint-s12/2026-05-16-v2-family5-image-surfacing-contract.md`](2026-05-16-v2-family5-image-surfacing-contract.md) — audit contract (4 truth-sources + join rule), `/version` endpoint contract, `MANIFEST.json` schema + build-time bake invariants, drift detection state machine, evidence-file contract for `docs/audit/AUDIT-deployment/YYYY-MM-DD.json`, `OmniSightStaleImage` alert routing per v2-AlertBridge-1a, cross-family handoff to ⑥/⑩/31.G/31.C, AutoRedeploy Option (a)/(b) decision. Downstream Family ⑤ tickets cite that doc's §-anchors in their AC.

**Why**: today's image was 7 days stale; no system flagged it; AUDIT-23 / OP-976 promised `scripts/deployment-audit.sh` + daily cron but neither exists.

**Defense dimensions covered**: D1 (detection — new) + D2 (remediation — new). D3/D4/D5 N/A (audit is observational only).

**Tickets** (8):

| # | ID | Title | Tier | Class | Defense dim | blockedBy |
|---|---|---|---|---|---|---|
| 1 | v2-⑤-1a | Image surfacing spec — what backend exposes about its own version | S | claude | D1 (spec) | G.A-v1-19 |
| 2 | v2-⑤-1bc | Backend `/version` endpoint: image_sha + build_time + git_ref + alembic_head_in_image (from `MANIFEST.json` baked at build) + tests | S | codex | D1 | v2-⑤-1a |
| 3 | v2-⑤-Dockerfile-Manifest | `Dockerfile.backend` adds `RUN scripts/bake-image-manifest.sh > /app/MANIFEST.json` at build time (captures build_time, git_ref, alembic_head) | S | codex | D1 | v2-⑤-1a |
| 4 | v2-⑤-2bc | `scripts/deployment-audit.sh` — compares running container `/version` vs latest GHCR digest + DB alembic_version + filesystem alembic head; outputs JSON evidence | S | codex | D1+D2 | v2-⑤-1bc, v2-⑤-Dockerfile-Manifest |
| 5 | v2-⑤-2bc-Timer | systemd timer `omnisight-deployment-audit.timer` — daily run + JSON evidence to `docs/audit/AUDIT-deployment/YYYY-MM-DD.json` + posts to Discord on drift | S | claude + operator-prepare-only | D1+D2 | v2-⑤-2bc, 31.C-Integration-external |
| 6 | v2-⑤-AlertRule | Prometheus rule `OmniSightStaleImage` (fires if running image >24 h older than latest `:latest` GHCR digest) — declared per v2-AlertBridge-1a contract: `severity=warn`, `area=deployment`, `family=⑤`, `defense_dimension=D1`, `runbook_url`, dedupe by `(alertname, family, image_repo)`; v0 delivery via stdout+email adapter | S | claude | D1 | v2-⑤-2bc, v2-AlertBridge-1bc |
| 7 | v2-⑤-Integration | E2E: synthetic stale-image scenario → daily audit catches it + alert fires + Discord receives + evidence file written; rerun next day with fresh image → alert clears | L | claude | D1+D2 | all above |
| 8 | **v2-⑤-AutoRedeploy** (v1.3, codex P1-2) | Consumer-side auto-redeploy ticket — closes the OP-1035-only-half pipeline gap from the retrospective. Either (a) flip `pull_policy: missing` → `always` on prod compose + add cron `docker compose pull && up -d --quiet-pull` daily at low-traffic hour, OR (b) Watchtower-style sidecar that watches GHCR digest changes + triggers prod recreate. Includes the v2-⑥-2bc backward-fail-fast invariant test: a stale-image auto-redeploy attempt where DB is already ahead must exit 78 cleanly. **Decision pending operator** between (a) cron and (b) sidecar; spec defaults to (a) as lower-blast. **v1.4 (codex round-2)**: tier bumped to M (was S; prod compose recreate is system-scoped, not atomic). Boundary block in §3.0.5 | M | claude + operator-prepare-only | D4 | v2-⑤-Integration, v2-⑥-2bc |

### Family ⑦ — Auth middleware allowlist single-source-of-truth

**Contract spec doc** (filed 2026-05-16, OP-1146): [`docs/sprint-s12/2026-05-16-v2-family7-allowlist-contract.md`](2026-05-16-v2-family7-allowlist-contract.md) — full bug-class definition, Path A/B/C decision rationale, `PUBLIC_PATH_ALLOWLIST` + `is_public()` contract, consumer list (5 middlewares), drift-CI-contract outline, migration plan. Downstream Family ⑦ tickets cite that doc's §-anchors in their AC.

**Why**: today's `/health` 401 traces to a 4-of-5 whitelist drift; the symptom is benign but the underlying anti-pattern is dangerous (any auth endpoint policy change risks the same drift).

**Defense dimensions covered**: D2 (exception with remediation — new). D1/D3/D4/D5 N/A.

**Operator decision LOCKED (operator 2026-05-14, Q2)**: **Path C — single source of truth allowlist**

- One constant `PUBLIC_PATH_ALLOWLIST` in `backend/middleware_allowlist.py` consumed by all 5 middleware (`auth_baseline`, `_graceful_shutdown_gate`, `_bootstrap_gate`, `_rate_limit_gate`, `auth.py` if applicable).
- Whether `/health` ends up in the allowlist or out is a downstream decision recorded in v2-⑦-FixHealth (likely: remove `/health` and standardize on `/healthz` + `/livez`, but the decision is made under the single-source contract).
- Rationale: today's bug class (4-of-5 whitelists agree but the 5th doesn't) becomes structurally impossible once all 5 reference the same list. The minimum-cost fix (Path A) leaves the anti-pattern in place; we've already paid for the discovery cost once.

Rejected: Path A (cheapest but doesn't address the drift class); Path B (normalizes dead code).

**Tickets** (7):

| # | ID | Title | Tier | Class | Defense dim | blockedBy |
|---|---|---|---|---|---|---|
| 1 | v2-⑦-1a | Allowlist single-source spec + path decision (A/B/C) | S | claude | D2 (spec) | G.A-v1-19 |
| 1.5 | **v2-⑦-Reproduce-401** (v1.3, codex P1-3) | Write a failing CI integration test that reproduces the live `/health → 401` symptom from 2026-05-14 incident — must hit `OMNISIGHT_AUTH_BASELINE_MODE=enforce`; assert current 401 status (test PASSES on current code = bug verified); discover and enumerate ALL auth decision points (mount-order audit) + reverse-proxy health path + `/api/v1/health` vs `/health` + WebSocket bypass + docs/OpenAPI exception paths; failing-test artifact becomes the regression-gate for v2-⑦-2bc refactor | S | codex | D2 | v2-⑦-1a |
| 2 | v2-⑦-1bc | `backend/middleware_allowlist.py` — `PUBLIC_PATH_ALLOWLIST` single constant + helper `is_public(path)` + tests | S | codex | D2 | v2-⑦-1a, **v2-⑦-Reproduce-401** |
| 3 | v2-⑦-2bc | Refactor 5 middlewares (`auth_baseline`, `_graceful_shutdown_gate`, `_bootstrap_gate`, `_rate_limit_gate`, `auth.py`) to consume `is_public()` + drift contract test | S | codex | D2 | v2-⑦-1bc |
| 4 | v2-⑦-FixHealth | Resolve `/health` per chosen path (A or B); decision recorded in v2-⑦-1a | S | codex | D2 | v2-⑦-2bc |
| 5 | v2-⑦-ContractTest | CI integration test: every middleware honors `PUBLIC_PATH_ALLOWLIST`; adding new middleware that does NOT consult `is_public()` fails CI | S | codex | D2 | v2-⑦-2bc |
| 6 | v2-⑦-Integration | E2E: probe each endpoint in `PUBLIC_PATH_ALLOWLIST` under `OMNISIGHT_AUTH_BASELINE_MODE=enforce`; all return 200; probe a non-allowlisted endpoint → 401; **also assert reverse-proxy health paths + `/api/v1/health` vs `/health` + docs/OpenAPI exception paths + WebSocket auth bypass test all behave per allowlist contract**; tier:M per SOP §3.2 (E2E across 5 middleware × multiple path variants) | M | claude | D2 | all above |

### Family ⑧ — WSL2 graceful shutdown contract

**Why**: today's host reboot exposed that `scripts/shutdown.sh` exists with proper graduated timeouts but is NOT wired; `TimeoutStopSec=60` is mathematically insufficient; no PG `stop_grace_period`.

**Defense dimensions covered**: D3 (shutdown — new) + D4 (recovery — new). **D1/D2/D5 explicitly N/A in v1.3 (codex P1-4 fix)**:

- **D1 N/A rationale**: a "last shutdown was forced" signal would require Windows-side hook integration to read the BugCheck / Update / Schedule data. That's explicitly out of scope per §1.2; documented in `v2-⑧-4a-Doc` as operator-manual.
- **D2 N/A rationale**: post-reboot warning to operator is delivered via `v2-⑤-AlertRule` (`OmniSightStaleImage` if image got rolled back during forced shutdown) + `v2-⑥-AlertRule` (`OmniSightAlembicDrift` if DB+image misalign). Family ⑧ doesn't re-emit; cross-family delegation.
- **D5 N/A rationale**: at the host-shutdown level there is no rescue path — once SIGKILL/RB_POWER_OFF fires, the runtime is gone. Rescue paths exist at the per-feature level (v2-⑥-RescueCLI for DB; future per-app for stuck containers). Operator's `Restart-Service` from Windows is the only host-shutdown rescue and that's not a Productizer artifact.

**Tickets** (8):

| # | ID | Title | Tier | Class | Defense dim | blockedBy |
|---|---|---|---|---|---|---|
| 1 | v2-⑧-1a | Graceful shutdown contract spec — required timeouts per service class | S | claude | D3 (spec) | G.A-v1-19 |
| 2 | v2-⑧-1bc | `scripts/shutdown.sh` extended: explicit per-service grace periods + verification loop + idempotent + tests (using `docker compose` test fixture) | S | codex | D3 | v2-⑧-1a |
| 3 | v2-⑧-2a | systemd unit refactor: `omnisight-compose-prod.service` `ExecStop` calls `scripts/shutdown.sh` (not raw `docker compose down`); `TimeoutStopSec` raised to 180; verification via `systemd-analyze verify` | S | claude + operator-prepare-only | D3 | v2-⑧-1bc |
| 4 | v2-⑧-2b | `docker-compose.prod.yml` adds `stop_grace_period` per service: backend 40 s; PG 30 s; caddy / frontend / cloudflared 15 s; others 10 s (default) | S | codex | D3 | v2-⑧-1a |
| 5 | v2-⑧-3a | PG WAL safety verification — `pg_isready` + checkpoint completion check before signaling docker stop; alembic_version table integrity probe after restart | S | codex | D4 | v2-⑧-1bc |
| 6 | v2-⑧-4a-Doc | `docs/sop/wsl2-shutdown-contract.md` — operator runbook: how Windows shutdown interacts with WSL2 + the 10 s RB_POWER_OFF hard limit; recommended Windows side hooks (manual). **v1.4 mandatory section (codex P1-4 caveat)**: explicit "Productizer cannot detect last forced shutdown" limitation paragraph — Windows-side BugCheck / Update / Schedule data is not readable from inside WSL2 systemd; operator-visible runbook must call this out so post-reboot the operator knows to manually check whether prior shutdown was forced (and accordingly decide whether to manually run pg integrity probe / image-vs-DB drift check) | S | claude | D3 (operator-side) | v2-⑧-2a |
| 7 | v2-⑧-DRDrill | Chaos test: simulate forced shutdown (kill -9 systemd) → verify post-reboot PG integrity (pg_resetwal NOT needed) + backend healthcheck green within 60 s | S | claude + operator-rehearsal | D3+D4 | v2-⑧-2a, v2-⑧-2b, v2-⑧-3a |
| 8 | v2-⑧-Integration | 14-day soak: weekly synthetic shutdown + 0 corruption + RTO < 60 s | L | claude | D3+D4 | v2-⑧-DRDrill |

### Family ⑨ — Optional auxiliary service contract (ai-core / Local LLM)

**Reframe LOCKED (operator 2026-05-14, Q3)**: ai-core is an **附屬專案 / optional auxiliary**, not a hard dependency. Productizer must treat it as:

- **Available → auto-provide** the Local LLM service (ollama included in fallback chain, exposed via capability inventory, user-facing UI shows "Local LLM enabled").
- **Unavailable → quietly skip** that service; the rest of the system continues without error. Other providers (Anthropic / OpenAI / Google / Groq / DeepSeek / OpenRouter) handle traffic. The fact that Local LLM is missing is **visible but non-fatal**.
- **The `ai_gateway` orphan today is a symptom of the missing contract** — there's no probe deciding "is ai-core healthy?", so a dead container can sit there forever, AND the fallback chain can't dynamically include / exclude ollama. Today both problems are static.

This reframes the family from "cross-project hygiene" to **"optional auxiliary service contract"** — a reusable pattern that other future auxiliary services can adopt.

**Defense dimensions covered**: D1 (detection — new); D2 (loud-but-non-fatal exception — new). D3 / D4 / D5 N/A (auxiliary services don't have rescue paths; if unavailable, you just live without them).

**Codex P1-5 punt note (v1.3, refined v1.4)**: codex correctly flagged that the v1.2 reframe from "cross-stack hygiene" → "optional auxiliary service contract" abandoned the original retro gap about daemon-resource hygiene (a crashlooping sister container can hammer shared docker daemon CPU / log volume on the co-tenant host). v1.3 makes the punt **explicit**: cross-stack daemon hygiene is **out of scope** for G.A-v2 Family ⑨. **v1.4 specifies the post-rc2 META placeholder name**: a future META ticket `META-POST-RC2-CROSS-STACK-HYGIENE` (to be filed during the post-rc2 sprint planning) will own broader cross-project lifecycle policy — daemon orphan detection across co-tenant compose stacks, log/CPU resource caps for sister projects, cross-project shutdown contract. **Family ⑨ in v1.3+ does NOT close this gap**; it only handles the optional auxiliary service contract for ai-core specifically. The 2026-05-14 `ai_gateway` specific instance (currently `docker stop`'d by operator) is covered by `v2-⑨-3a-Decision`; broader policy waits for the named META. **Traceability**: when filing META-POST-RC2-CROSS-STACK-HYGIENE, link back to this spec §3 Family ⑨ as the deferring source.

**Tickets** (5):

| # | ID | Title | Tier | Class | Defense dim | blockedBy |
|---|---|---|---|---|---|---|
| 1 | v2-⑨-1a | Optional auxiliary service contract spec — "available-then-use, unavailable-then-skip" pattern; declares D1+D2 minimum contract for any auxiliary service | S | claude | D1+D2 (spec) | G.A-v1-19 |
| 2 | v2-⑨-1bc | `ai_core_probe` — periodic probe (60 s cadence) hits ai-core health endpoint; sets Prometheus gauge `omnisight_aux_service_available{service="ai_core"}` + in-process feature flag `AI_CORE_AVAILABLE`; tests cover up/down/flapping | S | codex | D1 | v2-⑨-1a |
| 3 | v2-⑨-2bc | `llm_fallback_chain` builder consumes `AI_CORE_AVAILABLE` flag — dynamically excludes `ollama` from chain when ai-core down; tests cover both states + capability inventory reflects state | S | codex | D2 | v2-⑨-1bc |
| 4 | v2-⑨-3a-Decision | One-shot operator decision recorded: current `ai_gateway` crashlooping container action — (a) keep stopped (current post-2026-05-14 state); (b) delete entirely; (c) restore when omnisight-ai-core work resumes. Affects local docker hygiene only; doesn't affect contract. | S | claude + operator-window | none (decision-only) | v2-⑨-1a |
| 5 | v2-⑨-Integration | Synthetic ai-core up/down/up cycle (chaos test via stopping ai_engine container): chain dynamically excludes ollama within 1 probe cycle when down; chain re-includes within 1 probe cycle when up; 0 spurious user-visible errors throughout; alert fires at warn-level (not page) after 24 h continuous unavailability; tier:M per SOP §3.2 (chaos test crosses probe / fallback chain / capability inventory / alert routing) | M | claude | D1+D2 | v2-⑨-1bc, v2-⑨-2bc |

### Family ⑩ — Runner Defense Contract  ⚠ ADDED 2026-05-14 post codex Package 2 self-audit

**Why this family exists**: codex Package 2 (`docs/audit/codex-reviews/runner-self-audit-and-redesign-2026-05-14.txt`) self-audit confirmed from INSIDE the runner — first-person lived-experience evidence — that ~50% of recent pickup failures are runner-state-leak issues, not code-generation issues. Specific lived failure modes codex named:

- **Mode 1** "Runner-owned artifacts make my successful work look unsafe" (OP-1070: codex completed AC at 02:07:39, reverted at 02:07:51 because `.runner-cwd-sentinel` had disappeared; later completed at 02:46:29, reverted at 02:46:42 because `progress.txt` was first uncommitted path)
- **Mode 2** "Claim and status state are split across labels, assignee, comments, and worktree side effects" (OP-1070: assignee diverged from pickup, multiple codex worktrees attempted same ticket)
- **Mode 3** "Health gates that are meant to protect me can block every pickup because they depend on the thing they are checking" (OP-1067/1077: bridge heartbeat coupled to event-loop progress → quiet Gerrit periods staled heartbeat → fleet-wide pickup DoS)

This matches operator's earlier empirical numbers from this session (22.4 labels/ticket, 45 % `runner-blocked`, 40 % `claim:*` residue, 50 % of SP-B-X 21 tickets are FIX-not-feature). ADR-0035 + SP-B-X 21 tickets give a **static contract + 12 corrective patches**. What's missing — and what this family adds — is a **runtime defense contract for the runner itself**, the same 5-dim contract Layer 1 primitives consume.

**Defense dimensions covered**: D1 + D2 + D3 + D4 + D5 (all five — the most comprehensive family in G.A-v2).

**Architectural decision LOCKED (codex Package 2 §0 + §4; operator agreement deferred to v1.2 review)**: Move claim ownership + runner-owned artifacts + pickup progress + terminal cleanup out of JIRA labels/comments into a Postgres-backed coordination substrate. Keep JIRA as the human-facing ticket ledger. Strangler-pattern migration: shadow-write 1 wk → enforce → drop label-based writes (keep read-only compat 1 sprint).

**Codex's concrete design (verbatim §4 of self-audit)**:
- New module: `backend/agents/runner_coordination.py`
- New table: `runner_claims(ticket_key, resource_key, lease_id, owner_agent_class, owner_instance_id, fencing_token, state, phase, heartbeat_at, acquired_at, released_at, release_reason, external_refs)`
- Function API: `acquire_claim()`, `release_claim()`, `record_phase()`, `find_active_holders()`
- Replace in `jira_dispatch.py`: `find_mutex_holders()` + any `claim:*` label manipulation → table read/write
- Replace in 3 runner entry points (`auto-runner-jira.py` / `auto-runner-multi.py` / `auto-runner-codex.py`): claim acquisition before `transition_to_in_progress()`; release in `finally` around every terminal path (pre-sync, capability failure, no-commit, dirty worktree, push hard-fail, success)

**Validation contract** (1 week after enforce mode, on last 40 runner-handled tickets):
- `claim:*` labels on terminal tickets: **40 % → 0 %**
- `runner-blocked`: **45 % → < 10 %**
- 0 ticket has > 1 active claim row per mutex resource
- 0 duplicate pickup attempts on a single ticket
- **Regression test**: replay OP-1070-like scenario where progress/sentinel runtime files are present; runner finalization must NOT revert due to its own artifacts

**This family unblocks SP-B-X-META**: SP-B-X-META closure criteria require "at least 1 alert from {C9 bridge-health, C10 instance drift, C11 human-authority-yield, C12 state-authority precedence}". v2-⑩-AlertRule provides those alerts. SP-B-X-META can finally close on top of this family.

**Tickets** (17):

| # | ID | Title | Tier | Class | Defense dim | blockedBy |
|---|---|---|---|---|---|---|
| 1 | v2-⑩-1a | Runner coordination substrate + state-separation contract spec; explicit `defense_contract` block for the runner itself; categorizes labels A-class (durable ticket metadata) vs B-class (volatile runtime state) | S | claude | D1+D2+D3+D4+D5 (spec) | G.A-v1-19 |
| 2 | v2-⑩-ADR | ADR-0037 — Runner state substrate decoupling (JIRA labels = ledger only; Postgres = lock + state); explicit migration rules for which existing `runner-*` and `claim:*` labels move where | S | claude | D4 | v2-⑩-1a |
| 3 | v2-⑩-1bc | `backend/agents/runner_coordination.py` module + `runner_claims` table + alembic migration + unit/integration tests (initially shadow-only: writes table but no read consumer) | S | codex | D1+D4 | v2-⑩-1a |
| 4 | v2-⑩-2-Shadow | Dual-write integration: every claim/release writes BOTH to labels AND to coordination table for 1 week observation; no behavior change yet; daily comparison report between the two states | S | codex | D1 | v2-⑩-1bc |
| 5 | v2-⑩-2bc | Replace `find_mutex_holders()` JQL → table query; replace `pre_pickup_ok()` mutex check; tests covering codex/claude concurrent pickup + the OP-1070-style 4-runner collision | S | codex | D2 | v2-⑩-2-Shadow |
| 6 | v2-⑩-2d | Replace claim acquisition in `auto-runner-jira.py` + `auto-runner-multi.py` + `auto-runner-codex.py` with `acquire_claim()` before `transition_to_in_progress()`; release in `finally` block covering ALL 6 terminal paths (pre-sync / capability / no-commit / dirty / push-fail / success) | S | codex | D3+D4 | v2-⑩-2bc |
| 7 | v2-⑩-2-Cutover | Drop label-based claim writes; keep read-only label compatibility for 1 sprint for dashboard consumers; 40-ticket sample verification at end | M | claude | D2+D4 | v2-⑩-2d |
| 8 | v2-⑩-3 | Centralize `RUNNER_RUNTIME_ARTIFACTS` constant + apply to every dirty-check / stash / change-id call site (closes the structural debt that SP-B-X-016/017/018 patched reactively in 3 separate hotfixes) | S | codex | D2 | v2-⑩-1a |
| 9 | v2-⑩-4a | Bridge-health graded contract spec — capability-scoped, NOT global; stale bridge = block only Gerrit-finalization tickets; allow code-only pickup with explicit "review pending bridge" lease state | S | claude | D2 (spec) | v2-⑩-1a |
| 10 | v2-⑩-4bc | Refactor `pre_pickup_ok()` bridge gate from hard global → graded per-capability; tests include OP-1067/1077 regression (stale bridge must NOT block code-only pickup) | S | codex | D2 | v2-⑩-4a |
| 11 | v2-⑩-5a | Capability registry spec — typed policy data structure (provider × model × tools × max-tier × cost-mode × health × known-failure-classes) replacing flat `capability:enable=*` labels | S | claude | D1+D2 (spec) | v2-⑩-1a |
| 12 | v2-⑩-5bc | `capability_profile` table + alembic migration + adapter layer reading both new table AND old `capability:enable=*` labels during transition + tests | S | codex | D1 | v2-⑩-5a |
| 13 | v2-⑩-5d | Pre-pickup quota / circuit-breaker enforcement via `provider_orchestrator.py` — fix codex finding: quota currently checked POST partial execution; must be enforced PRE pickup so cost-bearing failure doesn't happen mid-flight | S | codex | D2 | v2-⑩-5bc |
| 14 | v2-⑩-5e | Model deconfliction policy — failover model MUST NOT pick up a ticket that another model just failed UNLESS the failure class is one that fallback is expected to improve; failure-class catalog + decision matrix | S | claude | D2 | v2-⑩-5bc |
| 15 | v2-⑩-RescueCLI | Operator override CLI `omnisight runner-rescue {dump,release,reset}` — inspect coordination table + force-release stuck claims + audit trail to `audit_log` per ADR-0034; requires L2 fingerprint | S | claude + operator-window | D5 | v2-⑩-2-Cutover |
| 16 | v2-⑩-AlertRule | Prometheus rules via `v2-AlertBridge-Framework`: `runner_claim_stale` (heartbeat > 5 min), `runner_pickup_block_rate` (> 20 % blocked over 1 h), `runner_state_drift` (table vs JIRA label disagreement during shadow window) — **these alerts unblock SP-B-X-META 30-day stability closure criteria** | S | claude | D1 | v2-⑩-1bc, v2-AlertBridge-1bc |
| 17 | v2-⑩-Integration | 4-week soak; weekly tracking of 40-ticket sample; assert all validation contract targets above; specifically assert 0 OP-1070-pattern reverts; on-failure write structured incident to `docs/audit/AUDIT-G-A-v2-Family-10/` | M | claude | D1+D2+D3+D4+D5 | all above |

**Routing tally**: codex 9, claude 8. Operator-window 1 (RescueCLI).

**Critical path**: 1a → ADR → 1bc → 2-Shadow → 2bc → 2d → 2-Cutover → Integration. ~7 hops + parallel branches for 3 / 4a-bc / 5a-bc-d-e. Estimated 7-10 weeks; longer than codex's 6-8 weeks because we added explicit Shadow + ADR + RescueCLI + Integration tickets.

**Dependency note on G.A-v0 / G.A-v1**: Family ⑩ does NOT require G.A-v1 to be fully shipped — only G.A-v1-19 (the v1 schema kernel) is needed. Family ⑩ can run **in parallel with the rest of G.A-v1 if operator chooses to prioritize runner reliability over governance-engine completeness**.

## §4. Schema + plugin tickets (G.A-v2 framework itself)

These are the "build the contract, then everyone uses it" tickets. They block all 5 gap families above.

| # | ID | Title | Tier | Class | blockedBy |
|---|---|---|---|---|---|
| 1 | v2-A1 | `defense_contract` schema (Pydantic) — 5 dimensions × all sub-fields + enums | S | codex | G.A-v1-19 (G.A-v1 ship) |
| 2 | v2-A2 | 6 new forbidden_combinations added to G.A-v0 validator | S | codex | v2-A1 |
| 3 | v2-A3 | G.A-v0 plugin extended: defense_contract field-level validators — 5 dim × subfields × runtime cross-field rules. **v1.3 (codex P1-6)**: tier:M with explicit subscoping in description: A3.a schema-shape validators (per-dim required fields); A3.b runtime cross-field forbidden-combinations (the 6 D1-D5 rules from §2); A3.c bootstrap/audit-mode compatibility (so v2-A8-SelfAudit can run validator in audit mode against framework's own tickets without circular fail). 12+ fixture classes covering valid / shape-fail / cross-field-fail / bootstrap-allow / audit-mode-warn cases | M | codex | v2-A1, v2-A2 |
| 4 | v2-A4 | Golden fixture: 31.A-1a apt-base-tools payload UPDATED with defense_contract block | S | codex | v2-A3 |
| 5 | v2-A5 | jira_dispatch.py refusal extended: reject pickup if defense_contract violation | S | codex | v2-A3 |
| 6 | v2-A6 | Unit tests: schema + validators + golden + refusal | S | codex | v2-A5 |
| 7 | v2-A7-Integration | Synthetic ticket exercises all 5 dimensions; validates end-to-end via filing-time validator → runner pickup → defense-contract enforcement → metric emission → alert delivery; tier:M per SOP §3.2 (E2E spans schema + plugin + validator + runner + alerting) | M | claude | v2-A6 |
| 8 | **v2-A8-SelfAudit** | **Closed-loop self-validation (per Q4)** — once v2-A3 ships, run validator in audit mode against v2-A1..A7's own defense_contract blocks (each framework ticket describes itself per §2 schema). Produces `docs/audit/AUDIT-G-A-v2-self/{ticket-id}.json` evidence per ticket. If all 7 pass → ADR-0034 weekly batch review marks the bootstrap override resolved. If any fail → fix EITHER validator OR ticket (defect can be either side), re-run, repeat until closed. | S | claude | v2-A3, v2-A7-Integration |
| 9 | **v2-A9-SDValidator** | **Spec-discipline SD-1 `novelty-claim-requires-evidence` validator** (per §2 v1.1 addition) — regex matcher for novelty phrases (EN + ZH + JA, extensible); evidence_grep_artifact YAML block parser + schema validation; quote-marker + negation-form + hypothetical-form exclusions; 4-week soft-fail pilot mode flag; ADR-0034 override hook. Tests: 12 fixture docs covering matched / unmatched / negation / hypothetical / quoted / multi-lang / soft-fail / override / valid-with-evidence / invalid-with-empty-evidence cases. | S | codex | v2-A3 |

## §5. Children — overview summary

| Family | Tickets | Tier-L count | Tier-M count | Operator-window count |
|---|---|---|---|---|
| Schema + plugin (v2-A1..A9) | **9** | 0 | 2 (A3 + A7-Integration v1.3) | 0 |
| Shared alert plumbing (v2-AlertBridge-*) | **4** | 0 | 1 (AMMigrationTest v1.3) | 0 |
| ⑥ Image-vs-DB drift (v2-⑥-*) | 10 | 1 (Integration) | 1 (RescueCLI v1.3) | 2 (RescueCLI, DRDrillBackward) |
| ⑤ Shipped-not-deployed (v2-⑤-*) | **8** (v1.3 +AutoRedeploy) | 1 (Integration) | 0 | 2 (timer, AutoRedeploy) |
| ⑦ Allowlist single-source (v2-⑦-*) | **7** (v1.3 +Reproduce-401) | 0 | 1 (Integration v1.3) | 0 |
| ⑧ Graceful shutdown (v2-⑧-*) | 8 | 1 (Integration) | 0 | 2 (systemd refactor, DRDrill) |
| ⑨ Optional auxiliary service (v2-⑨-*) | 5 | 0 | 1 (Integration v1.3) | 1 (Decision) |
| **⑩ Runner Defense Contract (v2-⑩-*)** | **17** | 0 | 2 (Cutover, Integration) | 1 (RescueCLI) |
| **G.A-v2 META + StabilityCheckpoint** | 2 | 0 | 0 | 1 |
| **TOTAL v1.3** | **70** | **3** | **8** | **9** |

**Routing tally**: codex ~16, claude ~28. Heavier on claude because spec / ADR / chaos / runbook work dominates.

## §6. Filing order + cross-phase dependencies

```
G.A-v0 (✓ done — OP-1049..1056)
    ↓
G.A-v1 (in progress — OP-1078..1097; OP-1095 interruption point)
    ↓
G.A-v2 schema kernel (v2-A1..A9)
    ↓
v2-AlertBridge-Framework (shared plumbing, gates ⑤/⑥/⑩ AlertRule tickets)
    ↓
    ├──→ Family ⑩ (UPSTREAM of everything else: runner reliability gates *every* other v2 ticket's actual execution; Phase 1-2 can run parallel with G.A-v1 completion if operator prioritizes)
    ├──→ Family ⑥ (HIGHEST priority for prod outage class; gates routine prod deploys)
    ├──→ Family ⑤ (parallelizable with ⑥; share AlertRule infra; v1.3 also coupled to v2-⑤-AutoRedeploy)
    ├──→ Family ⑦ (independent after v2-A1; v1.3 starts with v2-⑦-Reproduce-401 failing-test gate)
    ├──→ Family ⑧ (independent; can start anytime after v2-A1)
    └──→ Family ⑨ (lowest priority; depends on operator decision in v2-⑨-3a-Decision; daemon-hygiene punted post-rc2)
    ↓
G.A-v2-StabilityCheckpoint (7-day soak after all 5 families' Integration close)
    ↓
G.B (11 phase plugins consume defense_contract validators) — unblocked
```

### Cross-phase externals
- **31.G-LibsGate-external**: blocks v2-⑥-AlertRule and v2-⑤-AlertRule (need Prometheus + AM stack)
- **31.C-Integration-external**: blocks v2-⑤-2bc-Timer (need Discord routing)
- **31.H-StabilityCheckpoint-external**: NOT blocking — but the v2-⑧ family results SHOULD inform 31.H BackupDRDrill (shared shutdown contract)
- **31.I-2bc**: consumes v2-⑥ healthcheck-validator contract (forward dependency; 31.I waits on us)

### Estimated effort
- Schema + plugin (v2-A1..A7): 1 week (≈ G.A-v0 sized kernel)
- Family ⑥: 2-3 weeks (10 tickets, heaviest D5 rescue work)
- Family ⑤: 1-2 weeks (8 tickets — v1.3 added AutoRedeploy tier:M; mostly script + manifest + consumer-side recreate)
- Family ⑦: 1 week (7 tickets — v1.3 added Reproduce-401 failing-test pre-refactor gate; main work is refactor + contract test)
- Family ⑧: 2 weeks (8 tickets, careful operator coordination)
- Family ⑨: 1 week (5 tickets, probe + dynamic chain + one operator decision; lower complexity since no rescue path)
- **Family ⑩ Runner Defense: 7-10 weeks** (17 tickets across 5 codex-named phases — substrate + shadow / enforce + cutover / artifacts / bridge graded / capability registry; largest family + highest impact; can run partial-parallel with G.A-v1 completion)

- v2-A8-SelfAudit: 0.5 week (audit cycle + override-resolution paperwork)
- v2-AlertBridge-* (shared plumbing): 1 week (4 tickets, mostly contract + adapter; saves duplicate work in ⑤ + ⑥ + ⑩)
- v2-A9-SDValidator: 0.5 week (regex + YAML parser + 12 fixtures; mostly codex-mechanical)

**Total G.A-v2 estimated** (v1.3): 12-15 weeks (v1.2 was 11-14 wk; v1.3 adds ~0.5-1 wk for v2-⑤-AutoRedeploy + v2-⑦-Reproduce-401 + Family ⑥ 7-subcontract detail + boundary blocks for the remaining 60 tickets per codex P0-4 amendment plan). If Family ⑩ Phase 1-2 fast-tracks **in parallel with G.A-v1** rather than serial, can compress to 10-13 weeks end-to-end. Consistent with codex's "smaller kernels, more of them" advice.

## §7. Decisions + open questions

### Locked decisions (operator review 2026-05-14)

| # | Question | Decision |
|---|---|---|
| **Q1** | Family ⑥ drift-handling option | **Option C** — auto-upgrade if image AHEAD; fail-fast (exit 78) if BEHIND. |
| **Q2** | Family ⑦ allowlist refactor scope | **Path C** — single source of truth `PUBLIC_PATH_ALLOWLIST` consumed by all 5 middleware. |
| **Q3** | Family ⑨ scope | **Optional auxiliary service pattern** — reframed from "orphan hygiene" to "available-then-use, unavailable-then-skip" contract. ai-core treated as 附屬 / optional; not a hard dependency. Broader cross-project policy is post-rc2. |
| **Q5** | G.A-v2 position | **Before G.B** — G.B's 11 phase plugins consume defense_contract validators from G.A-v2. |

### All 7 questions locked (operator review 2026-05-14)

Q1-Q3, Q5 above. Q4/Q6/Q7 answered as follows:

| # | Question | Decision |
|---|---|---|
| **Q4** | Can v2-A1..A7 (framework itself) self-validate in closed loop? | **Yes, via override + retroactive audit.** Mechanism: v2-A1..A7 file under ADR-0034 operator-override path (only path open since validator doesn't exist at filing time); each ticket declares its own defense_contract block describing itself. Once v2-A3 ships, **v2-A8-SelfAudit (NEW)** runs the validator in audit mode against v2-A1..A7 → produces evidence JSON per ticket → if all 7 pass, ADR-0034 weekly batch review marks the bootstrap override resolved → closed loop. If any fail, fix EITHER side (validator or the ticket — defect can be either), re-run, repeat. From v2-A8 onward, all new tickets validate normally without override. The framework genuinely validates itself; the override is bootstrap, not workaround. |
| **Q6** | AlertRule before 31.G — minimize integration surprises later? | **File early + add v2-AlertBridge-Framework (NEW shared plumbing, 4 tickets).** Pre-bakes the 7 known AM-integration pitfalls (label / severity / dedupe / resolved-notification / cardinality / routing / grouping) into a contract that both v2-⑤-AlertRule and v2-⑥-AlertRule consume. v0 delivery via stdout+email adapter; 31.G AM bridge migration is a config swap with canonical-envelope equivalence gate (`v2-AlertBridge-AMMigrationTest`; v1.3 codex P1-7 reframe: byte-for-byte was wrong invariant, canonical normalized envelope is the actual portability guarantee). |
| **Q7** | `defense_contract` field required-vs-optional rollout | **Required immediately on v2-\* family** (eat our own dog food per Q4 self-audit). **Optional on 31.A–31.K v2 tickets** for 4 weeks after G.A-v2-StabilityCheckpoint closes, then required (giving phase plugin authors time to backfill). |

### v1 draft now considered complete

No outstanding open questions. Spec ready for codex independent review + Gerrit +2 from non-AI reviewer before any v2-* JIRA ticket files.

## §8. Not in this spec (deferred / out-of-scope reminders)

- Auth strict-mode full rollout audit (⑦ only fixes the symptom; auth_baseline.py enforce mode itself may need a S12.G-Plus track)
- Windows-side WSL2 host hooks (operator-manual; documented in v2-⑧-4a-Doc but not automated)
- Broader cross-project lifecycle policy beyond `ai_gateway` (post-rc2 sprint)
- Per-phase backfill of defense_contract for 31.A–31.K tickets (G.B / phase plugin authors' responsibility under their own v2 amendment cycles)
- Chaos engineering framework (this spec only adds focused DR drills per family)

## §9. Approval checklist (pre-filing)

- [ ] Operator confirms Option C / Path C / scope-limited ⑨ (per §7 Q1-Q3)
- [ ] Codex independent review (mirrors G.A-v1-style v1→v2 amendment cycle)
- [ ] Gerrit +2 from non-AI reviewer before any v2-* JIRA ticket files
- [ ] Per-family ADR drafts (ADR-0036 forward-only-deploy invariant + others as needed) reviewed
- [ ] G.A-v2 META JIRA filed first (rollup); children file in dependency order

---

**End of v1 draft. Awaiting operator review + codex independent review per S12.G v2 codex-driven amendment process.**
