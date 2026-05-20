---
id: ADR-0021
title: Release Pipeline Coordinator (domain-specific supervisor agent for the autonomous release pipeline)
status: Accepted
date: 2026-05-13
---

# ADR-0021 — Release Pipeline Coordinator

- **Status**: Accepted (2026-05-13, operator `+2`). AUDIT-29f children to be filed against this ADR.
- **Deciders**: operator (`nanakusa sora`) + claude main-session
- **Tickets**: AUDIT-29 META (OP-979's successor; to be filed once this ADR is +2'd) · AUDIT-29f (this ADR's implementation parent)
- **Relates**: ADR-0015 (cross-task awareness 3D-memory architecture) · ADR-0019 (release:force-promote override) · ADR-0020 (release-cut as single merge change) · L-OP-870 (JIRA blockedBy direction) · L-OP-922 (release-as-state-machine) · L-OP-985 (release-cut vs code-review)
- **Blocks**: every AUDIT-29 phase Phase 6 — see §13

---

## §1. Context

### 1.1 The autonomous pipeline today

The operator has settled on an 8-step ideal flow for autonomous work:

```
  1. Coordinator standby (knows division of labor)            ❌ DOESN'T EXIST
  2. Runners auto-standby + JIRA pickup                       ⚠️ exists, not auto-start
  3. Runner state update + branch + push                      ✅
  4. Runner can't-execute → escalate to coordinator           ⚠️ partial (escalates to operator, no coordinator)
  5. Pre-review conflict self-fix loop                        ⚠️ partial
  6. Merger handles post-push conflicts; coordinator on impossible  ⚠️ merger exists, no escalation path
  7. Gerrit +2 → JIRA Under Review → Approved/Published       ✅ (bridge)
  8. Published → Archived auto-transition                     ❌ DOESN'T EXIST
```

Phase 0 audit (`docs/audit/2026-05-12-audit-29-phase-0-state-audit.md`) confirmed
the gap. The coordinator (step 1) is the **load-bearing missing piece**: without
it, steps 4 and 6 escalation paths terminate at the human operator regardless of
whether the issue is human-judgment-worthy.

### 1.2 What "coordinator" is — and is **not**

> **The release-pipeline coordinator is a *domain-specific supervisor agent*.
> Its domain is the release pipeline's work graph (JIRA META + R1-R13 + AUDIT
> chains + Sprint structure + runner classes + lesson library).**

It is **not** the main OmniSight product orchestrator (the one that routes
end-user prompts across multi-agent providers). Those are different agents
serving different domains. Operator distinguished these explicitly during the
2026-05-13 design conversation.

The dichotomy:

| Aspect | Main `product` orchestrator | Release-pipeline coordinator (this ADR) |
|---|---|---|
| Domain | end-user task routing, persona/provider selection | release pipeline work graph: META + children + blockedBy DAG |
| Service surface | OmniSight product runtime | OmniSight internal autonomous dev system |
| Knowledge base | LLM provider matrix, user persona contexts | RELEASE META structure, Sprint topology, runner class strengths, AUDIT-* / L-OP-* / ADR-* registries |
| Triggers | end-user prompt | runner failure events, JIRA state anomalies, periodic sweep |
| Escalation target | (none — terminal) | human operator |

### 1.3 Why a domain-specific agent (not a giant general-purpose one)

The pipeline coordinator needs deep knowledge that doesn't compose well with
general-purpose orchestration:

- **AUDIT-23 anti-pattern recognition** — shipped-but-not-deployed, locale-string-as-identifier, runtime-vs-script-context, etc. (12+ patterns in `architecture-anti-patterns.md`, growing). The coordinator must spot these in flight.
- **Runner class capability matrix** — claude (subscription) good for design / docs / ADR / architecture; codex (subscription) good for backend module rewrites + heavy test suites; API-tier runners (pay-as-you-go) for long-running tasks that exceed subscription TPM; local-llm (qwen 4080, paused) for cheap-slow batch work. Each has different cost, latency, capability — routing decisions need this matrix.
- **Sprint topology** — META → 13-children pattern, blockedBy chain semantics, the R3/R8 gate positions, force-promote escape hatches (ADR-0019).
- **Lesson library awareness** — 68 lessons in `docs/sop/lessons/`, with semantic-similarity recall via Cognee (Sprint F). Coordinator queries this on every novel decision.

A general orchestrator forced to also handle these would either:
(a) carry all this knowledge in every prompt — expensive + slow + dilutes performance on its primary task
(b) delegate to a sub-agent — which is *this ADR* by another name

We choose (b) and acknowledge it as a first-class architectural component.

---

## §2. Decision

Build a **hybrid Python supervisor + LLM consultation** agent named
`pipeline_coordinator` with the following load-bearing properties:

1. **Hybrid decision engine**: deterministic Python rules handle ≥90% of decisions; LLM consultation (claude CLI) for novel / multi-plausible / cross-cutting cases.
2. **Dual-scope routing**: ticket-level routing (Tier 1, immediate) + sprint-level capacity-aware re-planning (Tier 2, hourly). The sprint-level re-plan is **required** because subscription-tier vs API-tier runner capability + budget vary enormously.
3. **Multi-mode personality**: behavioral mode is selected per-situation from `(urgency × risk × novelty × reversibility)` profile. Not a single personality.
4. **Cold-start = full recovery orchestrator**: on boot, coordinator verifies infra, reconciles JIRA state, recovers interrupted tasks. **The coordinator is the canonical startup orchestrator** — if it is healthy, everything else is.
5. **Learning loop**: every decision write-back to 3D memory (Cognee, post-Sprint F deploy). Periodic self-retrospective. Recurring decisions graduate from LLM-consult to Tier 1 rules.
6. **Bounded permission model**: coordinator can file/label/transition tickets and @-mention operator. It **cannot** cast Gerrit `+2`, force-revert other agents' Under-Review work, or override operator decisions.
7. **Operator authority is absolute**: any operator action wins. Coordinator has labels/env knobs to allow operator force-mode, force-skip, force-escalate.

---

## §3. Architecture

### 3.1 Module + deployment artifacts

```
backend/agents/pipeline_coordinator.py           ← core module (new)
backend/agents/pipeline_coordinator_modes.py     ← personality mode rules
backend/agents/pipeline_coordinator_rules.py     ← Tier 1 deterministic rules
backend/agents/pipeline_coordinator_capacity.py  ← runner capacity tracking
deploy/systemd/pipeline-coordinator.service      ← user systemd unit
deploy/systemd/pipeline-coordinator-watchdog.service  ← separate watchdog (L2)
~/.config/omnisight/coordinator/decision-log/YYYY-MM-DD.jsonl  ← append-only audit
~/.config/omnisight/coordinator/heartbeat        ← liveness file (L1, 60s)
~/.config/omnisight/coordinator/state/           ← optional in-flight LLM consultation persistence
```

> **Operator runbook (AUDIT-29f-13):** day-to-day monitoring, override,
> pause, decision-log inspection, and troubleshooting for these
> artifacts live in
> [`docs/operations/coordinator-runbook.md`](../operations/coordinator-runbook.md).

### 3.2 Process model

Single long-running Python daemon. Subscribes to events from multiple sources;
maintains in-memory work-graph cache (refreshed every tick); writes decisions to
append-only log. Stateless across restarts: every tick re-builds the world from
JIRA + bridge logs + decision log replay.

```
                    ┌──────────────────────┐
                    │  pipeline_coordinator │
                    │       (daemon)        │
                    │                       │
                    │  ┌─────────────────┐  │
   ┌────────────────┼──→ Event Sources  │  │
   │                │  │ (§4)            │  │
   │  JIRA poll  ──────→                 │  │
   │  Bridge tap ──────→                 │  │
   │  Hourly cron──────→                 │  │
   │  Startup    ──────→                 │  │
   │                │  └─────────┬───────┘  │
   │                │            ▼          │
   │                │  ┌─────────────────┐  │
   │                │  │ Decision Engine  │  │
   │                │  │ Tier 1 rules    │  │
   │                │  │ Tier 2 LLM      │  │
   │                │  │ (§5)            │  │
   │                │  └─────────┬───────┘  │
   │                │            ▼          │
   │                │  ┌─────────────────┐  │
   │  JIRA mutate ←──────│ Action Layer    │  │
   │  ←@operator  ←──────│ (§6)            │  │
   │  log decision←──────│                 │  │
   │                │  └─────────────────┘  │
   │                │                       │
   │                │  ┌─────────────────┐  │
   │  heartbeat   ←──────│ Resilience      │  │
   │                │  │ (§9 L1-L7)      │  │
   │                │  └─────────────────┘  │
   └────────────────┴───────────────────────┘
```

### 3.3 Cold-start = 4-phase recovery (Q-Coord-7)

**This is the coordinator's most important behavior**: it is *the* canonical startup orchestrator.

```
Phase Startup-1 — Infrastructure verification (~30s)
  Run equivalent of scripts/deployment-audit.sh
  For each expected service NOT live:
    systemctl --user start <unit>
  Verify after-start:
    - gerrit-jira-bridge running
    - 4 runners running (claude-1/2, codex-1/2)
    - 8 timers active (release-milestone-checker, staging-gate-*, etc.)
    - merger proactive trigger reachable via bridge
  Failure → @-mention operator, halt startup (don't enter normal loop with degraded infra)

Phase Startup-2 — JIRA state reconciliation (~1min)
  Query: project = OP AND status in ("In Progress", "進行中")
  For each ticket T:
    Is there a live runner process for T?
      → matches /proc/<pid>/cmdline or worktree sentinel
    YES → leave alone (runner is handling)
    NO  → T is interrupted. Decision tree:
        (a) feature/OP-T-runner-fresh branch has commits → resumable
            → label `runner:resume-from-feature-branch`, leave In Progress
            → runner re-pickup will continue from branch
        (b) commits but Gerrit change exists + mergeable=true → finished, missed transition
            → transition to Under Review (idempotent if already there)
        (c) no commits, no recent activity (>2× CLI timeout)
            → restart cleanly: revert To Do, clear assignee + claim labels
        (d) ambiguous → @-mention operator, leave alone

Phase Startup-3 — Stale-state sweep
  claim:default:* labels older than 2× CLI timeout → remove
  assignee=<bot> on To Do tickets with no claim:* label → clear
  runner-blocked:waiting-X labels where X is 公開済み → remove

Phase Startup-4 — Enter normal loop
  Start event subscribers
  Schedule hourly proactive sweep
  Log "coordinator entered steady state at <ts>"
```

---

## §4. Event sources + triggers

| Source | Mechanism | Latency | Cost |
|---|---|---|---|
| **JIRA poll** | Every 60s, query: `needs-coordinator` label OR `status in (..., Won't Do recently)` OR `status=進行中 AND no active runner` | up to 60s | 1 JIRA API call / min |
| **Bridge tap** | Subscribe to `~/.config/omnisight/coordinator/bridge-events.jsonl` (bridge appends; coordinator tails) | <1s | nil |
| **Periodic sweep** | Hourly cron: full work-graph anomaly scan (cycles, stuck-chains, capacity warnings) | hourly | one Sprint-level LLM consult |
| **Startup boot** | systemd `ExecStartPost`-fires-after-startup-complete event | once | 4-phase recovery (§3.3) |

Operator-facing trigger labels:
- `needs-coordinator` — explicit "look at this"
- `coord-mode:triage` / `coord-mode:investigation` / `coord-mode:execution` / `coord-mode:architecture` — force a personality mode for one decision
- `coord-skip` — coordinator does NOT touch this ticket (operator handles)
- `coord-resume-after:2026-05-15` — defer attention until date

---

## §5. Decision engine — two tiers

### 5.1 Tier 1 — deterministic rules (handles ≥90%)

Rules live in `pipeline_coordinator_rules.py`. Each rule has:

```python
@rule(name="dependency-out-of-area", priority=10)
def handle_out_of_area_dependency(ctx: DecisionContext) -> Action | None:
    if not has_label(ctx.ticket, "needs-coordinator"):
        return None
    recent_comment = latest_comment(ctx.ticket)
    if "[runner-discovered-dependency]" not in recent_comment.text:
        return None
    out_of_area = parse_runner_dep_comment(recent_comment)
    if out_of_area in KNOWN_AREAS:
        return Action.FileDependencyTicket(
            blocking=ctx.ticket.key,
            target_area=out_of_area,
            description=build_dep_description(...)
        )
    return None  # Tier 2 takes it
```

Initial rule set (from observed patterns this session):

1. **dependency-out-of-area** — `runner-discovered-dependency` mentions a known area outside ticket's area: file dep ticket + reroute
2. **stale-claim-cleanup** — `claim:default:*` label > 2× CLI timeout, no live runner: remove label + clear assignee
3. **runner-blocked-marker-stale** — `runner-blocked:waiting-X` where X is 公開済み: remove marker
4. **merger-repeat-fail** — merger fails > 3 attempts same ticket: escalate to operator with full conflict context
5. **revert-loop-quarantine** — ticket reverted > 5 times in 24h: add `coord-quarantine` label + @-mention operator
6. **capability-blocked-known-locale** — `[runner-capability-blocked]` for issuetype=`ストーリー`: auto-apply `capability:enable=*` workaround (until AUDIT-27 actually wired)
7. **stuck-in-progress** — status `進行中` > 2× CLI timeout, no live runner process: trigger Phase Startup-2-style reconciliation for that single ticket
8. **chain-deadlock** — A blocks B blocks A (or longer cycle): file diagnostic comment + @-mention operator (this should never happen but if it does, halt)
9. **wrong-class-routing** — ticket area matches `area:backend` strongly, currently `class:subscription-claude`, codex is idle: suggest reroute (action: comment only — don't relabel without LLM consult)
10. **operator-keep-out** — `coord-skip` label present: no action, ever

Rules graduate from "first observed → Tier 2 LLM-consult handles → operator confirms → write Tier 1 rule" as patterns crystallize.

### 5.2 Tier 2 — LLM consultation (handles novel / multi-plausible / cross-cutting)

Triggered when:
- No Tier 1 rule matches the situation
- Multiple Tier 1 rules conflict (mutually exclusive actions)
- Action would affect >3 tickets (cross-cutting)
- Sprint-level periodic sweep (always Tier 2)

LLM context bundle (assembled by coordinator):

```
# Coordinator LLM consultation context

## Situation
{situation_profile}    # urgency × risk × novelty × reversibility
{personality_mode}     # selected mode (Triage / Investigation / Execution / Architecture / Rescue)

## Work-graph slice
{focal_ticket and 5-hop blockedBy DAG}
{sprint_topology if Sprint-level}
{recent_decisions on these tickets (last 7 days)}

## Runner state
{capacity by class}   # claude-sub: X% quota; codex-sub: Y%; api-claude: pay; ...
{currently_running_tickets}
{recent_failures by class}

## Relevant lessons (Cognee top-10 by similarity)
{title + one-line summary for each, with file link}

## Operator preferences (from memory)
{user.role, user.style, recent operator decisions on similar situations}

## Available actions (the only things you may instruct coordinator to do)
- file_ticket(spec)
- relabel(ticket, add=[...], remove=[...])
- transition(ticket, to_status)
- mention_operator(message, urgency)
- mark_for_followup(ticket, when, why)
- escalate(reason)
- noop(reason)

# Output format (strict JSON)
{
  "decision_rationale": "...",
  "actions": [{action, ticket, params}, ...],
  "confidence": "high|medium|low",
  "escalate_if_wrong": true/false,
  "learning": "<one-line lesson to write back if action succeeds>"
}
```

The coordinator parses the JSON, validates actions against the allowed list (anything else → drop), and executes.

### 5.3 LLM consultation budget

Cost-aware:

- Default: claude (subscription) when available
- If subscription quota < 20% remaining: API-tier (pay-as-you-go) with explicit budget cap per-decision
- Sprint-level re-plan (hourly): always API-tier (heavy context, can't afford subscription burn)
- Hard daily budget cap: configurable in `~/.config/omnisight/coordinator/budget.env`; coordinator halts LLM consult + degrades to "Tier 1 only" mode if cap hit, then re-evaluates next day

---

## §6. Action layer + permission model

### 6.1 Allowed actions

| Action | Mechanism | Bounds |
|---|---|---|
| `file_ticket(spec)` | JIRA POST /issue via `_create_issue` helper, with `coord-filed` label so it's traceable | Spec must include description, AC, areas, tier; no operator-only labels |
| `relabel(ticket, add, remove)` | JIRA PUT labels | Cannot add/remove operator-only labels (e.g., `release:force-create`, `release:force-promote`, `class:operator`) |
| `transition(ticket, to)` | JIRA POST /transitions | Allowed: To Do ↔ 進行中, Under Review → To Do (cancel work), 公開済み → Archived; Forbidden: Under Review → 公開済み (Gerrit owns; bridge handles), * → 公開済み (Gerrit owns), any → 承認済み directly |
| `mention_operator(msg, urgency)` | JIRA add_comment with `@nanakusa-sora` mention + label `needs-operator-action` if urgency=high | unlimited |
| `mark_for_followup(ticket, when, why)` | Add label `coord-resume-after:<date>`, no immediate action | unlimited |
| `escalate(reason)` | Add label `needs-operator-action` + comment with reason | unlimited |
| `noop(reason)` | Log decision + reason, take no action | unlimited |
| `write_lesson(...)` | Append to `docs/sop/lessons/L-COORD-<n>-<slug>.md` + index update | Tier 2 LLM-consult-confirmed pattern only; never Tier 1 alone |

### 6.2 Forbidden actions

- **Cast Gerrit `Code-Review` vote** — merger / operator authority
- **Push to Gerrit on a ticket** — runner authority (coordinator can `file` a ticket asking a runner to do it, but cannot push itself)
- **Force-revert In-Progress / Under-Review work from another runner** — runners revert their own work; coordinator can re-route via `noop + comment + file replacement ticket`
- **Touch operator-authority labels** — `coord-skip`, `release:force-*`, `class:operator`, `milestone:force-accept`
- **Modify capability_matrix.yaml or any code file** — out of scope; if pattern surfaces, file a ticket
- **Self-modify** — coordinator cannot relabel `coord-skip` off its own to-do list (operator escape hatch must remain intact)

### 6.3 Operator override

At any point, operator can:
- Add `coord-skip` label → coordinator never touches that ticket
- Set env `OMNISIGHT_COORDINATOR_PAUSED=1` → coordinator daemon stays alive but takes zero actions until env unset
- Set env `OMNISIGHT_COORDINATOR_DRY_RUN=1` → coordinator logs decisions but doesn't execute
- `kill -SIGUSR1 <coord-pid>` → graceful drain + write final decision log + exit (systemd will restart unless `OMNISIGHT_COORDINATOR_DISABLE_RESTART=1` is also set)
- `git revert` any committed decision-log entry (it's append-only file in operator's home)
- Add a Tier 1 rule directly in code (operator authors patches like any other)

---

## §7. Personality modes (Q-Coord-Personality)

### 7.1 Situation profile

For each decision, coordinator computes a 4-axis profile:

| Axis | Values | Signals |
|---|---|---|
| **urgency** | low / medium / high | Sprint deadline proximity; downstream blockers count; META priority; operator @-mention frequency |
| **risk** | low / medium / high | Blast radius of action (1 ticket vs full chain); reversibility of state changes; whether it touches `prod-gate` labelled tickets |
| **novelty** | low / medium / high | Cognee semantic-similarity score to past decisions; novelty = `1 - max(similarity)` |
| **reversibility** | low / medium / high | Can the action be undone in <5min? (relabel = yes; file-ticket = effectively no; transition = depends) |

### 7.2 Mode selection

| Mode | Profile | Behavior | Initial scope |
|---|---|---|---|
| **ExecutionMode** | high urgency + low novelty + low risk | Tier 1 rules dominate. Skip Tier 2 unless rules conflict. Fast routing decisions. | Phase 6 initial |
| **InvestigationMode** | low urgency + high novelty | Always Tier 2. LLM context wider (10-hop graph, 20 lessons). Files research / spike tickets. | Phase 6 initial |
| **TriageMode** | high urgency + high risk | Tier 2 with `confidence=high` required to act; otherwise `escalate(operator)` immediately. Bias toward human judgment. | later add |
| **ArchitectureMode** | low urgency + low reversibility + high impact | Slow, ADR-driven. Files ADR proposal ticket. Multiple LLM consultations with different framings. | later add |
| **RescueMode** | runner stuck > 24h OR > 5 reverts on one ticket | Full root-cause analysis. File `runner-blocked-pattern` ticket + write lesson. Aim: prevent recurrence, not just unstuck. | Phase 6 initial |

**Phase 6 ships with 3 modes** (Execution + Investigation + Rescue). Triage + Architecture added later as patterns crystallize.

### 7.3 Operator override

`coord-mode:<mode>` label on a ticket forces that mode for any decision involving that ticket. Useful when operator knows the system is misprofiling (e.g., "this looks routine but I want investigation mode because we keep getting bitten").

---

## §8. Capacity-aware sprint-level routing (Q-Coord-6)

### 8.1 Why ticket-level alone fails

Operator's concern (2026-05-13 conversation, paraphrased): **"subscription vs API runners have vastly different capabilities; pure ticket-level routing leads to wrong runner for the task, exhausted budgets, scope drift, duplicated work."**

Example failure mode (hypothetical but realistic):
- 10 simple low-priority tickets queue
- Each gets ticket-level-routed to `subscription-claude` because rule says "area:docs → claude"
- Claude subscription burns through weekly quota in 1 day
- The next 6 days, claude can't pick anything
- Meanwhile codex sits idle because its rules didn't match
- One critical `area:backend` ticket arrives; both lanes can in principle handle it
- Neither has capacity left

### 8.2 Sprint-level re-plan

Triggered hourly (and on demand via `needs-coord-replan` label). Process:

```
1. Enumerate next 10-20 pickable tickets across all sprints (priority DESC, age ASC)
2. Read current capacity:
   subscription-claude: X% weekly quota remaining (from claude usage telemetry)
   subscription-codex:  Y% weekly quota remaining
   api-claude (if configured): pay-as-you-go, budget cap from env
   api-openai (if configured): pay-as-you-go
   local-llm (qwen-4080, paused): N/A
3. Estimate token cost per ticket (heuristic: LOC budget × 4 + test cost)
4. LLM-consult (Tier 2, Investigation Mode): given (tickets, capacities, ticket-token-estimates), produce assignment plan
5. Apply assignment: relabel tickets with class change (without forcing — operator can override)
6. Identify scope-drift / duplication / merge candidates: file `coord-scope-review` tickets
7. If projected capacity insufficient for next-7-days work: @-mention operator with capacity report
```

### 8.3 Cost telemetry

Each runner (claude-1/2, codex-1/2) emits per-tick `runner-quota-state.jsonl` line:

```jsonc
{
  "runner_class": "subscription-claude",
  "instance_id": "default",
  "ts": "2026-05-13T...",
  "tokens_in_current_week": 1234567,
  "weekly_cap": 5000000,
  "reset_at": "2026-05-19T08:00:00Z",
  "tickets_completed_this_week": 12,
}
```

Coordinator tails these files + maintains `runner_capacity.json` summary.

---

## §9. Failure modes + recovery (Q-Coord-4)

Layered defense:

### L1 — Heartbeat

Coordinator writes `~/.config/omnisight/coordinator/heartbeat` every 60s. Also
emits `coordinator-alive` event to bridge log. Any watcher sees absence.

### L2 — Watchdog

`pipeline-coordinator-watchdog.service` — a separate systemd unit, polls heartbeat
every 30s. If absent for > 90s:
1. systemctl restart pipeline-coordinator.service
2. emit watchdog-restart event
3. If restart fails or restarted-then-died-again within 5min, escalate operator

### L3 — Suicide check

Coordinator's own loop detection: same `(ticket, action_type)` decision triggered
3 times within 1h with no progress → **pause self** (set internal `paused=True`)
+ emit `coordinator-self-paused` operator alert. Operator must `SIGUSR2`
unpause OR fix the underlying issue.

### L4 — Decision log

Every decision (action + reasoning + outcome later) is one JSONL line in
`~/.config/omnisight/coordinator/decision-log/YYYY-MM-DD.jsonl`. Append-only.
Survives crashes. Operator-grep-able. Drives both recovery (L5) and learning (§10).

### L5 — Drain on shutdown

SIGTERM handler:
1. Finish in-flight LLM consultation if any (max wait 60s — kill at 60s)
2. Write `shutdown_began` + `shutdown_complete` entries to decision log
3. Exit cleanly

### L6 — Crash recovery on startup

On boot, after Phase Startup-1, read last 24h of decision log:
- For any decision with `shutdown_began` but no `shutdown_complete`: a crash happened mid-decision
- Identify any incomplete action (e.g., relabeled but didn't transition)
- Resume / complete / rollback per action type
- Log `crash_recovery_applied` entries

### L7 — Chaos testing

CI test in `backend/tests/test_pipeline_coordinator_chaos.py`:
- Spawn coordinator
- Inject decisions mid-flight
- kill -9 randomly
- Verify next startup recovers cleanly (no orphan labels, no double-actions, no lost decisions)

### L8 (optional, deferred) — Standby instance

Single-instance is the default. Future: support a `standby` instance that watches
heartbeat and takes over if primary fails 3x in 10min. NOT in Phase 6.

---

## §10. Learning loop

### 10.1 Write-back to 3D memory

Each Tier 2 LLM-consulted decision generates:
- `decision_rationale` (already in decision log)
- Optional `learning` field — a one-line lesson

After the decision's outcome is observable (24h delay typical), coordinator runs
a `decision_outcome_check`:
- Did the actions produce the expected result?
- Did operator override or correct?
- Did downstream metrics improve (chain throughput, fewer reverts, etc.)?

Outcome + decision → write to Cognee as a node with edges to:
- the tickets it touched
- the lessons it cited
- the mode it operated in

### 10.2 Pattern graduation

Periodic (weekly) coordinator self-retrospective:
- Group decisions by (situation_profile_class, action_type)
- Decisions in same class with consistent outcomes → graduate to Tier 1 rule
- Write a draft Python rule to `coord_rule_proposals/<slug>.py`
- @-mention operator: "I'd like to graduate this pattern to Tier 1; review the proposal"
- Operator reviews + merges (or rejects)

This keeps Tier 2 LLM cost falling over time as patterns crystallize.

### 10.3 Coordinator's own lesson registry

Lessons authored BY coordinator (vs by humans) live in `docs/sop/lessons/L-COORD-NNN-*.md`. The `L-COORD-` prefix distinguishes them from human-authored `L-OP-` lessons. Same lesson index includes both.

---

## §11. Alternatives considered

### Alt-1 — Pure LLM agent (Option A from design discussion)

Every coordinator decision invokes claude CLI with full work-graph context.

**Pros**: Maximally flexible; no rule engine to maintain.

**Cons**: Cost (every decision = full CLI invocation, ~$0.20-1.00 per decision at current pricing). Latency (10-60s per decision; unacceptable for high-frequency simple routing decisions). No deterministic guarantee for known patterns.

Rejected: cost / latency. The "90% of decisions are simple" property of release pipeline work makes deterministic rules far more efficient.

### Alt-2 — Pure Python daemon (Option B from design discussion)

Hardcoded rules cover all cases; no LLM consultation.

**Pros**: Cheap, fast, deterministic, easy to unit test.

**Cons**: Can't handle novel patterns (the entire reason we need this). Maintenance burden grows quadratically as edge cases accumulate.

Rejected: Sprint F shipped Cognee + memory tools precisely to enable adaptive learning; throwing that away here would be wasteful.

### Alt-3 — Reuse main product orchestrator

Extend the OmniSight product orchestrator with release-pipeline-domain plugins.

**Pros**: One agent to maintain.

**Cons**: Domain conflation — release-pipeline concerns leak into product runtime; product runtime concerns leak into release pipeline; both suffer.

Rejected by operator (2026-05-13 conversation): "這個部分跟主系統的 ORCHESTRATOR 並不同".

### Alt-4 — Multiple specialized sub-coordinators (per-sprint, per-class, etc.)

Spawn N coordinators, each owning a slice.

**Pros**: Decomposed responsibility; coordinators can be simpler.

**Cons**: Coordination-of-coordinators problem; conflicting decisions; cross-sub state sharing. Worse than the original.

Rejected: premature decomposition. If single coordinator becomes overloaded later, decompose then with concrete evidence.

---

## §12. Open questions / future work

1. **Sprint G overlap**. Sprint G is "Release Conductor cron-driven automation" — partially overlapping scope. Need to reconcile: is Sprint G subsumed by AUDIT-29f / this ADR, or does Sprint G become "the cron-driven half" while this coordinator is "the event-driven half"? Pending operator decision.

2. **Multi-project coordinator**. When OmniSight expands beyond this single repo (which operator indicated is the broader project plan), does each repo get its own coordinator, or one cross-repo coordinator? Likely per-repo for isolation; cross-repo for shared-resource accounting. Deferred.

3. **Coordinator-to-coordinator handoff**. When there's a main-product orchestrator AND this pipeline coordinator AND (future) a hardware-engineering coordinator etc., do they need a communication protocol? Probably yes (ADR-0022 territory). Out of scope here.

4. **Standby HA pair**. §9 L8 — defer until single-instance shows reliability gaps in production.

5. **LLM provider abstraction**. Currently the Tier 2 consultation uses claude. Should provider be configurable (claude-via-anthropic, claude-via-AWS-Bedrock, openai, local)? Yes long-term; for Phase 6 use claude only. Configurability is Phase 7+.

6. **Action set extensibility**. The §6.1 allowed-action list is closed for safety. As patterns surface, operator adds new actions via reviewable patches (not Tier-2-LLM proposal). Hard rule.

---

## §13. Implementation plan (drives AUDIT-29f ticket structure)

The AUDIT-29f tickets to be filed once this ADR is `+2`'d:

```
AUDIT-29f META — Release Pipeline Coordinator
  AUDIT-29f-1   ADR-0021 review + lock                        (0.25d — operator review pass)
  AUDIT-29f-2   pipeline_coordinator skeleton + daemon                       (0.5d)
                   - module structure
                   - systemd user unit + watchdog unit
                   - heartbeat + drain handling
                   - empty decision engine that just logs
  AUDIT-29f-3   Tier 1 deterministic rules (initial 10 rules from §5.1)      (1d)
  AUDIT-29f-4   Capacity tracking (§8)                                       (0.5d)
                   - runner emit quota state to JSONL
                   - coordinator tails + maintains capacity view
  AUDIT-29f-5   Personality mode system (§7 — Execution + Investigation + Rescue) (0.5d)
  AUDIT-29f-6   Tier 2 LLM consultation (§5.2)                              (1d)
                   - context bundle assembly
                   - claude CLI invocation
                   - JSON output parsing + action validation
                   - budget guards
  AUDIT-29f-7   Cold-start 4-phase recovery (§3.3, §9 L6)                    (0.75d)
  AUDIT-29f-8   Bridge tap + JIRA poll + event subscription wiring (§4)      (0.5d)
  AUDIT-29f-9   Sprint-level periodic re-plan (§8.2)                         (0.5d)
  AUDIT-29f-10  Chaos test (§9 L7) + decision-log validation                 (0.5d)
  AUDIT-29f-11  Learning loop: write-back + outcome-check + graduation (§10) (1d)
  AUDIT-29f-12  Integration test: full pipeline scenario (runner stuck → coordinator handles → ticket flows through) (0.5d)
  AUDIT-29f-13  Documentation: docs/operations/coordinator-runbook.md       (0.5d)
                   - how operator monitors coordinator
                   - how operator overrides / pauses
                   - how operator inspects decision log
                   - troubleshooting common issues
  AUDIT-29f-14  Deploy + canary: 1-week shadow mode (logs decisions, doesn't act)  (1d ops + 7d shadow watch)
                   then enable acting mode after operator review of shadow log
```

**Total Phase 6 implementation**: ~7-8 days, plus 7-day shadow observation. Critical path within AUDIT-29 META.

### 13.1 Dependencies

- AUDIT-29b (Sprint F infra deploy) provides Cognee — required for §5.2 lesson recall
- AUDIT-29a (deployment audit baseline) + scripts/deployment-audit.sh — required for §3.3 Phase Startup-1
- AUDIT-29c (runner systemd-ify) — required for coordinator to know runners are systemd-managed (so it can restart them on Phase Startup-1)
- ADR-0019 (force-promote override) — escape-hatch reference for §6 permission model
- Sprint F (3D memory) — required for §5.2 + §10

### 13.2 Risks

| Risk | Mitigation |
|---|---|
| Coordinator becomes new single point of failure | §9 L1-L7 defense + can degrade to "Tier 1 only" if LLM unavailable |
| LLM budget runaway | Per-decision + daily budget caps; capacity-aware §8.2 |
| Action conflicts with operator | Operator overrides always win; `coord-skip` escape hatch; dry-run env |
| Pattern graduation introduces bugs | All graduated rules go through operator review (§10.2); not auto-merged |
| Decision log fills disk | Logrotate-style rotation (keep 90d); decisions queryable via grep + jq |
| Coordinator's own lessons drift from operator preferences | §10.2 periodic operator review; L-COORD prefix isolates if rejection needed |

---

## §14. Decision

**Build pipeline_coordinator as specified above**. Begin with AUDIT-29f-1 (this ADR review). Once `+2`'d, AUDIT-29f-2 through AUDIT-29f-14 execute in the listed order with parallel work where dependencies allow.

The coordinator does not run in production until AUDIT-29f-14's 7-day shadow observation completes and operator reviews the shadow decision log.

---

## Appendix A — Glossary

- **Tier 1 / Tier 2**: deterministic Python rules / LLM-consulted decisions
- **Personality mode**: behavioral profile selected per situation (Execution / Investigation / Rescue / Triage / Architecture)
- **Situation profile**: 4-axis vector `(urgency, risk, novelty, reversibility)` used to select mode
- **L-COORD-**: lessons authored by coordinator (vs L-OP- for human-authored)
- **Shadow mode**: coordinator logs decisions but takes no actions; for 7-day canary observation
- **Tier 1 graduation**: pattern observed in Tier 2 multiple times → operator-reviewed → becomes Tier 1 rule

## Appendix B — Operator override quick reference

| Action | Mechanism |
|---|---|
| Coordinator stops touching one ticket | Add label `coord-skip` |
| Coordinator stops touching all tickets | `OMNISIGHT_COORDINATOR_PAUSED=1` env |
| Coordinator logs but doesn't act | `OMNISIGHT_COORDINATOR_DRY_RUN=1` env |
| Coordinator drains + exits | `kill -SIGUSR1 <pid>` |
| Coordinator runs but won't restart on crash | `OMNISIGHT_COORDINATOR_DISABLE_RESTART=1` + drain |
| Force coordinator mode on one decision | Label `coord-mode:<execution|investigation|rescue|triage|architecture>` |
| Wake coordinator from self-pause (§9 L3) | `kill -SIGUSR2 <pid>` |

## Appendix C — Decision log entry format

```jsonc
{
  "ts": "2026-05-13T...Z",
  "trigger": "needs-coordinator-label" | "bridge-event:runner-stuck" | "hourly-sweep" | ...,
  "tickets": ["OP-XXX", ...],
  "situation_profile": {"urgency": "high", "risk": "medium", "novelty": "low", "reversibility": "high"},
  "mode": "ExecutionMode",
  "tier": 1 | 2,
  "rule_name": "dependency-out-of-area",   // if Tier 1
  "llm_consultation": {                     // if Tier 2
    "context_size_tokens": 5432,
    "response_tokens": 234,
    "decision_rationale": "...",
    "confidence": "high"
  },
  "actions_taken": [
    {"action": "relabel", "ticket": "OP-XXX", "params": {...}, "result": "success"}
  ],
  "learning": "...",   // optional one-line lesson
  "outcome": null      // filled in by decision_outcome_check (24h later)
}
```

---

**End of ADR-0021 draft. Awaiting operator review.**
