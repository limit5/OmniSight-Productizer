# Runner Supervisor Architecture — layered fail-safe + non-destructive autonomy

**Date:** 2026-06-29 · **Status:** design (agreed in operator design session) · **Owner:** runner evolution

This is the blueprint for evolving the runner from *"a mature worker watched by a
human in tmux"* into a self-sustaining, horizontally-scalable, multi-tenant-ready
fleet — **without changing the runner's immutable essence**.

---

## 1. Runner CHARTER (immutable — must NOT change across sub-projects / external projects)

The runner is an autonomous worker loop:

1. **Find** its own work on JIRA.
2. **Fetch** the correct codebase repo for that ticket (multi-project: "which repo" is data-driven — OP-2192~2200 routing).
3. **Execute** the task per the implementation SOP.
4. **If stuck** — ask-for-help / explain-why-blocked **on JIRA**, then revert the ticket to OP/coordinator.
5. **On success** — push to Gerrit code-review + report on JIRA + flip the ticket status.
6. **Loop** — pick the next task, forever.

Started as one runner; now 2×claude + 2×codex; **horizontally scalable (proven)**.
Each runner gets its own ephemeral work-path (`run-ephemeral.sh` clones develop
fresh each cycle into `/tmp/runner-workspaces/<inst>/run-XXXX`, parallel to the
project) so runners never collide and each can serve a *different* project.

## 2. The core gap

The runner-**worker** is mature. The un-built layer is the **SUPERVISOR** — today
that supervisor *is "a Claude sitting in tmux"*: watching logs, detecting
stuck/loop/regression, salvaging commits, unsticking tickets, feeding vetted work,
deciding when to escalate. This is not scalable (N runners ≠ 1 human) and it keeps
the human from focusing on the project. **Externalizing this manual layer into the
system is the runner's next evolution.**

A second, related gap (a *regression*): the bwrap sandbox (OP-1777, correct
prompt-injection defense) scrubs all `OMNISIGHT_*` secrets — incl the JIRA token —
from the jailed agent CLI. So the agent's **"voice"** to JIRA is severed: it can do
code, but can't post its own detailed AC verification (success) or its explanation
of *why it's stuck* (charter §4). The wrapper channel (pickup/pushed/bridge
comments + status transitions, all outside the jail) still works, so completions
*are* reported — but the agent's rich voice is lost.

## 3. The layered supervisor — split by intelligence cost

The two naive supervisor shapes both fail: a per-runner intelligent supervisor
(decentralized) → **token explosion**; a single intelligent supervisor
(centralized) → **overwhelmed in the worst case**. Both fail because they treat the
supervisor as *one always-thinking thing*. The fix: **split by how much intelligence
each job actually needs.** Most supervision is not intelligent.

```
Layer 3 — HUMAN (you / Claude)          ← only the genuinely-novel
   ▲ rich, cheap-to-act-on escalation
Layer 2 — LLM exception handler         ← RARE (only novel patterns); tokens spent here only
   │  diagnoses → emits a new Layer-1 rule OR escalates to human
   ▲ event-driven (woken by Layer 1)
Layer 1 — RULE ENGINE supervisor        ← cheap, deterministic, ~zero tokens, volume-proof
   │  "pattern → fixed non-destructive action" (the 80% I do by hand)
   ▲ observes fleet state (JIRA/git/heartbeats)
Layer 0 — DUMB FAIL-SAFES (in each runner)  ← pure code, zero tokens, cannot be overwhelmed
      circuit_breaker (N fails→stop), timeout, error→non-destructive revert,
      ephemeral isolation. THE SAFETY FLOOR.
```

- **Layer 0** is per-run, deterministic, already mostly present (circuit_breaker,
  timeout, revert paths). Scaling runners = more copies; zero cross-coupling.
- **Layer 1** is a centralized rule engine that *observes* the fleet's JIRA/git
  footprint and acts via JIRA/git — it never commands runners. A rule engine does
  not get overwhelmed by N simultaneous failures (loop + apply rules, cheap each).
  Seed rules from the patterns already hand-rescued: push-setup-fail
  ([[OP-2484]]), already-shipped-loop, stale-claim, requeue-needs-assignee-empty,
  zombie-in-progress.
- **Layer 2** (LLM / sub-agent) is invoked *only* when Layer 1 meets an unknown
  pattern. It produces a rich diagnosis → either a new Layer-1 rule or a human
  escalation. **Tokens are spent only on novelty**, so no explosion regardless of
  centralized/decentralized.
- **Layer 3** (human) handles only the truly-novel; over time each novel case
  accretes into a Layer-1 rule.

## 4. The two invariants (protect these; everything else can change)

### Invariant A — layers couple ONLY through observable shared state + non-destructive actions
The decoupling medium is **JIRA (state machine) + git/Gerrit + heartbeat files** —
never direct calls. Layer 1 observes runners' state footprint and acts via JIRA/git;
it does not call/kill runner processes. Consequence: **any layer can be
scaled / changed / rewritten independently** as long as it respects the
shared-state contract. *The only thing that re-introduces cascade failure is
violating this* (e.g. the supervisor directly killing a runner process). Keep the
state-contract stable + versioned.

### Invariant B — the supervisor is NON-DESTRUCTIVE-ONLY (it is an optimizer, not a safety system)
The auto-supervisor (Layers 1–2) may: unstick, requeue (→ To Do), mark
`runner-blocked:*`, salvage a commit, strip stale claims, escalate. It may NOT:
close, abandon, merge, +2, or change config — those escalate to a human. Therefore
it is **powerless to cause a disaster**: worst case it escalates unnecessarily
(cheap) or fails to rescue (falls back to human). "Ensure it can't cause problems"
becomes *provable by construction* (it has no destructive powers) instead of
*"enumerate every bad scenario"* (impossible).

**Why both worries dissolve:** safety lives in Layer 0 (each runner's own dumb
fail-safes), so the supervisor is **not safety-critical**. A failed/overwhelmed
supervisor → rescues pause → runners degrade to "stop cleanly and wait for human"
(*safe*), never "fleet runs amok" (*disaster*). This dissolves *"who supervises the
supervisor?"* — nobody must, because its failure is bounded by Layer 0. A trivial
`heartbeat watchdog` (a cron comparing a timestamp — too dumb to be a weak link)
pings the human if the supervisor goes silent.

## 5. Accretion model — the supervisor grows, it isn't built all at once
Each novel failure a human hand-rescues → document → encode as a Layer-1 rule →
the residual unknown set shrinks. The human's role shifts from *"watch everything"*
to *"handle only the genuinely-novel."* This session already did one full cycle
(push-bug rescued by hand → OP-2484 automated it).

## 6. Upstream lever — shrink the SOP↔execution gap at the source
The filing SOP already mandates *"cut task granularity to the smallest reasonable
size."* Smaller + more-constrained tasks → less room to deviate from the SOP →
higher completion → less rescue needed. Plus **capability-matched routing** (don't
give a hard task to a low-capability agent — the multi-agent design's tiers). Half
of *"the runner often fails"* is solved upstream in the **fuel/planner layer**
(triage / 4-AC / decomposition / matching), not in execution.

## 7. Multi-tenant — scoping, not re-architecture

The future is multi-tenant (multiple customers, isolated repos/JIRA-scope/secrets).
**This architecture holds, and is *more* valuable there** — Invariants A+B (which we
adopted to avoid disaster) are *exactly* the properties that bound cross-tenant
blast radius. Multi-tenant is a **scoping** change (parameterize state-access by
tenant), not a reshape of the layers.

**Reuse as-is (tenant-agnostic mechanics):**
- Layer 0 dumb fail-safes (circuit_breaker / timeout / non-destructive revert).
- The **ephemeral per-run isolation already built** — this IS the foundation of
  per-tenant isolation (each run already has its own workspace + jail + scrubbed
  env). Multi-tenant just tags a run with "which tenant."
- Layer 1 rule LOGIC (runner-mechanics patterns are tenant-agnostic).
- Invariants A+B, the accretion model, the heartbeat watchdog.

**Must add (the tenant-scoping ring) — the 1A runner-tenant-isolation work:**
- Every supervisor read/write bound to a tenant scope: `set_tenant_id` before
  acting, **per-tenant credentials** for JIRA/git, tenant-scoped queries
  (`runner_tenant`, `db_context`). (Capability audit: isolation exists in the API
  plane, ABSENT in the runner — this is the gap.)
- Layer 2's LLM context must **never** cross tenants (scope each diagnosis to one
  tenant's failure).
- Supervisor tenancy fork: Layer 1 may stay centralized but tenant-scoped (it is
  just rules, low leak risk); Layer 2 LLM is best per-tenant-context.

## 8. Implementation roadmap (B-foundation → A-supervision)

| Phase | What | Layer | Why first |
|---|---|---|---|
| **B-Voice** | Restore the agent→wrapper JIRA relay (agent writes AC/escalation to a worktree-or-`/tmp/runner-<ticket>` file inside the jail; wrapper reads + posts via its out-of-jail creds; never bind creds into the jail). | ③ voice | A supervisor can't read the runner's true state until the voice is restored; also fixes charter §4 (explain-why-stuck) now. |
| **0-Floor** | Harden every per-runner fail-safe: audit/confirm circuit_breaker, task timeout, and that EVERY error path is non-destructive + emits a rich `runner-blocked:*` escalation. | 0 | The floor that makes the supervisor non-safety-critical. |
| **1-Rules** | Rule-engine supervisor: encode the hand-rescued patterns as deterministic detectors + non-destructive actions; centralized; observes JIRA/git; honours Invariants A+B. | 1 | Removes the bulk of manual watching. |
| **1-Obs** | Wire observability (the `DATABASE_URL`/metrics gap) so the rule engine + human can SEE the fleet. | cross | The rule engine needs to read true state. |
| **2-Novel** | LLM/sub-agent exception handler for unknown patterns → propose rule or escalate; non-destructive-only. | 2 | Handles the long tail; accretes rules. |
| **MT-Scope** | Tenant-scope the supervisor (1A): per-tenant creds + `set_tenant_id` + LLM context isolation. | cross | When a 2nd tenant onboards. |

Heartbeat watchdog + per-action audit/rollback are cross-cutting throughout.

## 9. Non-goals / disciplines
- Do NOT bind tenant/JIRA secrets into the agent jail (re-opens the OP-1777 L3 leak).
- Do NOT let the supervisor command/kill runner processes (violates Invariant A).
- Do NOT give the supervisor destructive powers (violates Invariant B).
- Keep the runner's CHARTER (§1) fixed; only the surrounding layers evolve.
