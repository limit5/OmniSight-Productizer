# AUDIT-29 Phase 0 — Pre-rc2 stabilization state audit

**Date**: 2026-05-12 23:05 CST · **Author**: claude main-session (operator-led) · **Status**: complete

**Purpose**: Before scoping AUDIT-29 tickets, audit (a) what infrastructure is *actually* live vs. *coded*, (b) whether the meta-system that surfaces lessons to future agents already exists or needs to be built, (c) which of my 6 proposed pattern-fix Phases overlap with already-shipped work, (d) the real gap surface area before we commit to ticket structure.

**Verdict**: My earlier AUDIT-29 proposal was **incomplete in scope** (only tactical fixes, missed the meta-learning dimension as user noted) AND **wrong in priority** (didn't include Sprint F 3D-memory infra deployment, which is the underlying mechanism for "lessons being surfaced when relevant"). Revised proposal in §6.

---

## §1. 3D Memory state — Sprint F deeper than expected, but largely **not deployed**

### 1.1 Sprint F ticket completion

`Sprint F: 17 tickets`

| Status | Count | Notes |
|---|---|---|
| ✅ 公開済み | 16 | All children F1-F16 |
| ⏸ To Do (META OP-898) | 1 | Operator gate — META hasn't been closed |

So at the ticket level Sprint F is essentially done. But the *deployment* picture is very different.

### 1.2 Code shipped — what's on develop

| Module | Path | Status |
|---|---|---|
| Cognee integration | `backend/agents/cognee_integration.py` | ✅ |
| Memory Tool handler | `backend/agents/memory_tool_handler.py` | ✅ |
| Graphiti MCP client | `backend/agents/graphiti_mcp_client.py` | ✅ |
| project-state aggregator | `backend/agents/project_state_aggregator.py` | ✅ |
| project-state cache | `backend/agents/project_state_cache.py` | ✅ |
| release_notifications | `backend/agents/release_notifications.py` | ✅ |
| project-state router | registered in `backend/main.py:1472-1473` | ✅ |

### 1.3 Runtime infrastructure — NOT deployed

`docker ps | grep -iE 'cognee|graphiti|neo4j'` → **empty**

- ❌ Cognee container — NOT running
- ❌ Neo4j container — NOT running
- ❌ Graphiti MCP container — NOT running
- ❌ `/var/omnisight/memory/` — does NOT exist (F2 OP-900 provisioning never executed on this host)
- ❌ `backend/agents/agent_feature_flags.py` — empty file or doesn't exist (feature flag module not deployed)
- ❌ `OMNISIGHT_FAILURE_GRAPH_FIXTURE` env var — not set (failure_graph injection silently disabled)

### 1.4 Runner consumption — partial wiring

The runner code DOES call:
- `memory_writeback._run_memory_writeback()` (line 847 in auto-runner-jira.py) — at task completion
- `failure_graph.render_pickup_context()` (line 510) — but **gated on FAILURE_GRAPH_FIXTURE env, which is unset → silent no-op**
- `_build_project_state_block()` — but gated on `agent_feature_flags.is_project_state_inject_enabled_sync()` — and feature_flags module is empty, so this also no-ops

The wiring is *there*; the *infrastructure those wires connect to* doesn't exist.

### 1.5 Net assessment

Sprint F is **another case of the "shipped-but-not-deployed" anti-pattern AUDIT-23 named** (proposed pattern #13). All 16 children show 公開済み in JIRA. The code paths are wired. But the runtime infrastructure (Cognee, Neo4j, Graphiti, memory storage path) was never stood up, and the feature flag module that gates everything is empty.

⇒ **Implication for AUDIT-29**: deploying the 3D memory infrastructure is itself part of "building the prod-grade autonomous system" and is what makes lesson-surfacing actually work. Cannot be deferred to a later sprint.

---

## §2. Lesson-surface mechanism state

### 2.1 Lesson registry

```
docs/sop/lessons/ — 68 files (L-OP-<key>-<slug>.md)
docs/sop/architecture-anti-patterns.md — 380 LOC, 12 numbered patterns
```

### 2.2 What's auto-injected to runner prompts today (via `_build_prompt`)

| Block | Source | Gating | Always active? |
|---|---|---|---|
| `capabilities_block` | OP-855 capability matrix | none | ✅ yes |
| `fg_block` (failure graph) | OP-858 — neighbour incidents | `OMNISIGHT_FAILURE_GRAPH_FIXTURE` env (unset on prod) | ❌ silent no-op |
| `ps_block` (project state) | OP-905 F7 — `/api/v1/project-state` | feature flag `is_project_state_inject_enabled` + agent_feature_flags module | ❌ silent no-op (module empty) |
| `ops_only_block` | OP-956 ops-only sigil | `runner:no-commits-expected` label | ✅ per-ticket |

### 2.3 What's NOT injected

`grep -nE 'inject.*lesson|inject.*pattern|relevant_lessons|anti_pattern.*prompt' auto-runner-jira.py backend/agents/*.py` → **0 results**

- ❌ Lessons (L-OP-*.md files) are NOT searched + injected into runner prompts
- ❌ Anti-patterns from architecture-anti-patterns.md are NOT injected
- ❌ Cognee semantic recall is NOT invoked at pickup time (the integration code exists in `cognee_integration.py` but `_build_prompt` doesn't call it)
- ❌ Memory Tool query is NOT invoked at pickup

### 2.4 Net assessment

The lesson-surface mechanism that the user explicitly wants (*"lessons that actually surface when relevant"*) is **not built**. Two paths could supply it:

1. **F7 (OP-905) project-state injection** — already wired in runner code but blocked by missing infrastructure (Cognee + feature flags)
2. **A new explicit lesson-recall step** — not yet attempted

Path 1 is the architectural correct path. It requires deploying the Sprint F infra (Cognee/Neo4j) AND extending `project_state_aggregator` to include "relevant lessons for this ticket".

---

## §3. AUDIT-29 candidate patterns — overlap analysis

| Pattern (from my earlier proposal) | Existing partial mechanism | Real gap |
|---|---|---|
| **Daemon startup contract** (Phase 1 runner systemd-ify) | `deploy/systemd/*.service` template exists for many services (gerrit-jira-bridge, release-milestone-checker etc.) | 4 runner tmux loops are the only daemons NOT systemd-managed. Need 4 new unit files + an SOP. |
| **Port-based portability** (Phase 2 drop subdomain) | `infra/staging/.env.template` already uses env vars; Caddy config has path-based AND host-based mixed | Caddy still has `host: staging.sora.services` clause; smoke probe defaults to `https://staging.sora.services`. Both need to be port-based. |
| **Reverse-proxy completeness** (Phase 3 add /audit/* route) | Caddy json hand-written; OpenAPI surface known | Currently `/audit/*` is missing; would benefit from auto-derive from OpenAPI but not in scope of this AUDIT |
| **Locale string as identifier** (Phase 4) | AUDIT-27 / OP-986 shipped — capability_matrix now has `ストーリー` alias. L-OP-986 lesson exists. | No CI check to prevent recurrence. Only Story type aliased — Bug/Task open. |
| **Runtime vs script context** | OP-981 / AUDIT-26b lazy-init pattern shipped for backend.audit | Only backend.audit was fixed; other modules (db_pool consumers in other paths) not audited yet |
| **Audit sink mismatch** (release_audit table vs generic audit) | OP-964 wire-up done; memory entry exists; AUDIT-13 follow-up open | OP-877 `release_audit` table still not actually written to by D5 (writes go to generic `audit` table — `release_audit_sink_mismatch` per memory anchor) |

Plus the new patterns surfaced by Phase 0 investigation:

| New pattern | Source | Suggested handling |
|---|---|---|
| **Shipped-but-not-deployed** | AUDIT-23 proposed it; not yet in anti-patterns.md | Merge as pattern #13 in AUDIT-29e |
| **Infrastructure prerequisites not declared per ticket** | Sprint F shipped without operator deploy steps fired | New SOP: every ticket whose code spawns a daemon/container/migration MUST include explicit "operator activation" AC item |
| **Silent gating (feature flag with no producer)** | F7 project-state injection silently disabled because `agent_feature_flags.py` empty | Build-time check that every feature flag referenced has a definition |

---

## §4. Service inventory + cold-start state

### 4.1 Currently running

| Layer | Count | Auto-start? |
|---|---|---|
| Docker containers (prod stack) | 7 | ✅ via docker daemon + restart policies |
| Docker containers (staging stack) | 6 | ✅ via docker daemon + restart policies (but compose unit reports failed) |
| Docker containers (other: ai_tunnel/gateway/cache, llama) | 4 | ✅ |
| systemd user timers active | 8 | ✅ (linger=yes) |
| systemd user services running | 1 | ✅ (gerrit-jira-bridge) |
| **runner tmux loops** | 4 | ❌ **NOT auto-started** — die on cold boot |

### 4.2 `scripts/deployment-audit.sh` results (run 2026-05-12 23:05)

```
summary: 2 green · 8 red · 0 warn · 4 red-with-expected=yes (fatal)
RESULT: FAIL
```

**4 fatal red rows** (expected=yes, found red):
1. `auto-promote-develop.timer` — not installed (OP-877)
2. `auto-promote-main.service` — not installed (OP-766)
3. `OMNISIGHT_DATABASE_URL` env in `auto-promote-develop.service` — service not running (OP-964)
4. `sora-bridge-sync.timer` — not installed (OP-798)
5. (also) `alembic-head auto` check — alembic_version empty / DB unreachable from script context (OP-964 sub-symptom)

**4 gated red rows** (peer-dependent, not fatal):
- `staging@http://localhost:8010/healthz` — staging container probe fails (port mismatch — should be 18010 for staging-backend-a)
- `staging-gate-canary.timer` — service exits with error (OP-965 — same DNS issue)
- `staging-gate-smoke.timer` — service exits with error
- (a couple more)

### 4.3 Cold-start survival forecast

If host is rebooted right now:
- Docker daemon: auto-starts ✅
- All prod containers: auto-restart ✅ (restart=always policies)
- All staging containers: auto-restart ✅
- systemd user units (linger=yes): auto-start, BUT:
  - 6 staging-related units would re-fire and immediately fail (same as now)
  - gerrit-jira-bridge would resume
- runner tmux loops: **GONE** — no auto-start mechanism, operator must SSH in and re-launch
- `scripts/runner_wrappers/*.sh` would still be at `/tmp/runner_wrappers/` — actually `/tmp` is wiped on boot on most distros — these wrappers would also be gone!

So cold-start survival = ~70% functional, 30% requires operator intervention.

---

## §5. AUDIT-23 (OP-976) — deployment audit findings status

**Doc**: `docs/audit/2026-05-12-shipped-not-deployed-sprint-dEF.md`
**Harness**: `scripts/deployment-audit.sh` — 4 fatal reds remaining (§4.2 above)
**Pattern #13 proposed**: ⚠ NOT yet merged to `architecture-anti-patterns.md` — gap to close in AUDIT-29e

AUDIT-23 was a meta-audit of OP-767/OP-878/OP-965 etc. It correctly identified the pattern. Its remediation list (§5 of the doc) listed 8 findings, all mapped to existing tickets — but those mappings haven't been closed.

So AUDIT-23 status = audit done, but remediation incomplete. The remediation IS what AUDIT-29 should close.

---

## §6. Revised AUDIT-29 recommendation

### 6.1 Revised META scope

```
AUDIT-29 META — Pre-rc2 stabilization: deployment-grade + 3D-memory-as-active-surface

Phase 1 — Deployment audit baseline (1d)
  29a-i   Run scripts/deployment-audit.sh; close 4 fatal red rows  
  29a-ii  Merge pattern #13 (shipped-but-not-deployed) to architecture-anti-patterns.md
  29a-iii Add CI check: any ticket with area:devops MUST list operator activation step

Phase 2 — Sprint F infra deployment (3-5d, the meta-system)
  29b-i   Cognee container + Neo4j 5.24 systemd unit + on-host TLS  
  29b-ii  /var/omnisight/memory/ provisioning + fleet dirs
  29b-iii Graphiti MCP service + DNS/port + auth
  29b-iv  agent_feature_flags module wired (currently empty)
  29b-v   Wire runner _build_prompt to query Cognee for relevant lessons + anti-patterns per ticket  
          (this is the "lessons that actually surface when relevant" mechanism)

Phase 3 — Runner systemd-ification (0.5d)
  29c-i   4 runner units: runner-claude-bot@default.service etc.
  29c-ii  Replace /tmp/runner_wrappers/ with /home/user/sora-bridge/deploy/runner-wrappers/
  29c-iii Pattern doc: docs/sop/daemon-startup-contract.md
  29c-iv  Pre-commit hook: any new systemd service must have Restart= + StandardOutput= + ExecStart=

Phase 4 — Port-based staging (0.5d)
  29d-i   Caddy: remove host-based rule; add /audit/* path-rule
  29d-ii  Smoke probe: OMNISIGHT_STAGING_URL=http://localhost:18080  
  29d-iii Pattern: docs/sop/cross-host-portability-staging.md
  29d-iv  Linear-history-consumer audit (deferred from AUDIT-26a) — verify MERGE_ALWAYS impact

Phase 5 — Fix remaining failing units (1d, mostly Phase 1-4 follow-throughs)
  29e-i   staging-pg-snapshot: rewrite snapshot-restore.sh to use docker exec (no host psql)
  29e-ii  omnisight-staging-compose: fix HEALTHZ_URL port (was 19000, should be 18080)
  29e-iii staging-sync, staging-gate-canary/smoke: auto-fix via Phase 4
  29e-iv  gerrit-jira-bridge-watchdog: diagnose + close
  29e-v   API token for staging /audit/verify auth

Phase 6 — Release Pipeline Coordinator (~8d, drives ADR-0021)
  29f-1   ADR-0021 review + lock (operator review pass, 0.25d)
  29f-2   pipeline_coordinator skeleton + daemon + systemd + watchdog (0.5d)
  29f-3   Tier 1 deterministic rules (initial 10) (1d)
  29f-4   Runner capacity tracking (§8 of ADR) (0.5d)
  29f-5   Personality mode system (Execution + Investigation + Rescue) (0.5d)
  29f-6   Tier 2 LLM consultation + budget guards (1d) — depends 29b (Cognee)
  29f-7   Cold-start 4-phase recovery (0.75d) — depends 29a + 29c
  29f-8   Event sources: bridge tap + JIRA poll + subscription wiring (0.5d)
  29f-9   Sprint-level periodic re-plan (0.5d)
  29f-10  Chaos test + decision-log validation (0.5d)
  29f-11  Learning loop: write-back + outcome-check + Tier-1 graduation (1d)
  29f-12  Integration test: full pipeline scenario (0.5d)
  29f-13  Operator runbook: docs/operations/coordinator-runbook.md (0.5d)
  29f-14  Deploy + 7-day shadow mode → enable acting mode (1d ops + 7d watch)

Phase 7 — Pre-review conflict self-fix loop (~1.5d)
  29g-i   Runner: detect mergeable=false on own change pre-+1, attempt rebase + force-push
  29g-ii  Cap: 3 self-fix attempts per change; >3 → file `pre-review-self-fix-exhausted` + @coordinator
  29g-iii Pattern: docs/sop/lessons/L-COORD-NN-pre-review-self-fix.md
  29g-iv  Cross-codebase audit: ensure no other agent already does rebase-on-conflict (avoid duplicate)
  29g-v   Verify: integration test injects merge conflict → confirm self-fix → confirm 3-attempt cap

Phase 8 — Auto-archive 公開済み → Archived (~0.5d)
  29h-i   Bridge handler: on change_merged + ticket already 公開済み + age > N days → transition Archived
  29h-ii  Configurable retention window (env OMNISIGHT_ARCHIVE_AGE_DAYS, default 30)
  29h-iii Operator escape: label `coord-keep-open` blocks auto-archive
  29h-iv  Pattern: docs/sop/release-lifecycle-states.md (公開済み vs Archived semantics)
  29h-v   Verify: end-to-end test from To Do → 公開済み → Archived

Phase 9 — Cold-start dry-run (0.5d, was old Phase 6)
  29i-i   Stop all units cleanly; wait; start; verify all containers + timers + runners + coordinator up
  29i-ii  Document recovery time
  29i-iii Update docs/operations/deployment-inventory.md
  29i-iv  Run deployment-audit.sh; expect 0 fatal reds

Phase 10 — rc2 bootstrap (1h, gated on Phases 1-9 green, was old Phase 7)
  29j-i   Create v0.5.0-rc2 JIRA fixVersion
  29j-ii  Bootstrap rc2 META + R1-R13 children via release_conductor_cron
  29j-iii Verify chain runs end-to-end with REAL green gates (no force-promote needed)

Phase 11 — rc1 cleanup + AUDIT-26 META close + retros (was old Phase 8)
  29k-i   OP-922 → Won't Do; R1-R13 → Archived per AUDIT-26f Option I
  29k-ii  AUDIT-26 META OP-979 → 公開済み
  29k-iii Sprint F META OP-898 → 公開済み (now actually deployed)
  29k-iv  AUDIT-29 META → 公開済み
  29k-v   Operator retrospective + lesson capture (including coordinator shadow-mode learnings)
```

**Phase 6/7/8 are NEW** (inserted in this revision after the 11-phase reframe). Phase 9/10/11 = the original Phase 6/7/8 pushed back. Letter mapping: 29f/g/h are now coordinator/pre-review/auto-archive; 29i/j/k are cold-start/rc2/rc1. Sub-tickets 29f-1 .. 29f-14 detailed in ADR-0021 §13.

### 6.2 Per-Phase deliverable structure (per user agreement: fix + pattern + audit + verify)

Each Phase produces 4 outputs:

```
Phase X.fix     — concrete fixes for the gaps in scope
Phase X.pattern — abstract pattern → docs/sop/lessons/L-OP-XXX-*.md
                  + docs/sop/architecture-anti-patterns.md entry
                  + memory entry under ~/.claude/projects/.../memory/
Phase X.audit   — cross-codebase scan for other instances of the pattern
                  → file batch follow-up ticket OR fix inline
Phase X.verify  — CI check / lint / pre-commit / runbook discipline that prevents recurrence
```

### 6.3 Time estimate (revised)

| Phase | Tactical (fix only) | + pattern + audit + verify |
|---|---|---|
| 29a Deployment audit baseline | 0.5d | 1d |
| 29b Sprint F infra deploy | 3-5d | 4-6d (much pattern overlap with AUDIT-23) |
| 29c Runner systemd-ify | 0.5d | 0.75d |
| 29d Port-based staging | 0.5d | 1d |
| 29e Fix remaining units | 1d | 1.5d |
| 29f Coordinator (ADR-0021) | 7d impl | 8d impl + 7d shadow watch |
| 29g Pre-review self-fix loop | 1d | 1.5d |
| 29h Auto-archive | 0.25d | 0.5d |
| 29i Cold-start dry-run | 0.5d | 0.5d (the verify itself) |
| 29j rc2 bootstrap | 1h | 1h |
| 29k rc1 cleanup + retros | 1h | 0.5d (retro=meta-learning, needs care) |

**Total**: 18-23 days work; can compress to 13-17 with parallel runner work where 29f's 7-day shadow watch overlaps with 29i preparation.

### 6.4 Critical path (revised for 11 phases)

```
29a (1d) ────────┐
                  ├──→ 29c (0.75d) ──┐
29b (4-6d) ──────┤                    │
                  ├──→ 29d (1d)   ───┤
                  │                    │
                  └─→ 29e (1.5d) ────┤
                                      ├──→ 29f impl (8d) ── 29f shadow (7d) ──┐
                                      │   (29g + 29h can ship within 29f shadow)
                                      ├──→ 29g (1.5d) ─────────────────────────┤
                                      └──→ 29h (0.5d) ─────────────────────────┤
                                                                                ├──→ 29i (0.5d) ──→ 29j (1h) ──→ 29k (0.5d)
                                                                                │
                                                                                └─── all Phase 2-8 must complete first
```

29b (Sprint F infra) is the long pole BEFORE coordinator. 29f-shadow (7d) is the long pole BEFORE rc2. The lesson-surface meta-mechanism (29b-v) is built into 29b; coordinator (29f) actively uses it via Tier 2 LLM consultation (ADR-0021 §5.2).

### 6.5 What this proposal does that my earlier one did NOT

| Concern | Earlier proposal | This proposal |
|---|---|---|
| Sprint F infra deployment | ❌ ignored | ✅ Phase 2 |
| Lesson auto-surface to runners | ❌ ignored | ✅ Phase 2-v (wire Cognee into _build_prompt) |
| Anti-pattern #13 merge | ❌ ignored | ✅ Phase 1-ii |
| AUDIT-23 remediation closure | ❌ assumed done | ✅ Phase 1-i (run + close) |
| Pattern extraction per phase | ❌ no | ✅ every phase has `.pattern` output |
| Cross-codebase audit per pattern | ❌ no | ✅ every phase has `.audit` output |
| CI prevention | ❌ no | ✅ every phase has `.verify` output |

---

## §7. Open questions for operator decision

1. **Sprint F infra deploy effort estimate** — Cognee + Neo4j + Graphiti deployment is genuinely 3-5 days of work. Is this acceptable, or do we want to defer that to a separate META (and accept that AUDIT-29 ships without lesson-surface mechanism)?

2. **postgresql-client install** — Phase 5-i has same fork: install via apt (sudo) vs rewrite via docker exec. Default: docker exec. Confirm?

3. **`/audit/verify` auth** — same choice as Phase 0 question: (a) dedicated staging API key, (b) bootstrap admin password reuse, (c) staging-only unauthenticated. Default: (a). Confirm?

4. **rc1 vs rc2 disposition** — Option I (cancel rc1, cut rc2) per AUDIT-26f. Confirm before Phase 8?

5. **Pattern #13 anti-pattern entry text** — I'd draft it from AUDIT-23's §1.1; operator should review draft.

6. **Lesson-injection scope** — Phase 2-v: inject TOP-K relevant lessons via Cognee semantic recall at ticket pickup. K=? (3? 5?). Budget guards (token cost)? operator preference?

---

## §8. Memory + cross-reference

- `memory/project_audit_findings.md` — AUDIT-23 origin
- `memory/project_staging_gate_gap.md` — staging gate non-deployment
- `memory/project_release_audit_sink_mismatch.md` — D5 audit sink wrong table
- `memory/project_d5_main_promote_mechanism.md` — superseded by ADR-0020
- `memory/project_release_cut_mechanism.md` — AUDIT-26's new design (if it exists; if not, create in 29h)
- After Phase 2: `memory/project_3d_memory_deployment.md` (NEW) — concrete deploy + activation steps for Cognee/Neo4j/Graphiti
- After Phase 7: `memory/project_release_pipeline_first_real_run.md` (NEW) — rc2 retrospective

---

## Recommendation

Before filing tickets, confirm with operator:
- §7.1 (Sprint F infra in scope — yes/no?)
- §7.4 (rc1 disposition — Option I confirmed?)
- §7.6 (lesson-injection K value + token budget)

Then file AUDIT-29 META + 8 children per §6.1.
