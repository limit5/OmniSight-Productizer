# Coordinator §6 live action executor + acting-flip — DESIGN (for codex audit)

**Status**: DRAFT for codex audit (2026-05-22). This is the unbuilt §6 action layer that `acting=True` requires — NOT a CLI switch. Implements the autonomous layer that mutates live JIRA + steers the runner fleet.

## Problem / scope
`build_default_coordinator(config, acting=True, action_executor=...)` raises `ValueError` without an `action_executor` (pipeline_coordinator.py:1884). The CLI (`:1936`) only ever builds `acting=False` (shadow). The real executor "lands in a later phase" (ShadowActionExecutor docstring, :266) — it is unbuilt. This design builds it.

## Key reuse insight (keeps risk low)
`JiraDispatchColdStartGateway` (pipeline_coordinator.py:606) is the LIVE gateway already used by the **cold-start** live path (`_default_cold_start_gateway(acting=True)`), and it already implements EVERY mutation the steady-state actions need:
- `mark_resumable` (:735), `transition_under_review` (:738), `reset_to_todo` (:750), `clear_assignee` (:787), `mention_operator` (:799).

So the steady-state §6 executor is a **thin dispatcher over the SAME proven live gateway** — not new mutation code. (`Action`: `pipeline_coordinator_rules.py:81`, frozen dataclass `kind`/`target`/`params`; `kind` ∈ `ALLOWED_ACTION_KINDS`.)

## Design — `LiveActionExecutor`
A callable `execute(action, ctx) -> dict` (same seam as `ShadowActionExecutor`, `ActionExecutor = Callable[[Action, DecisionContext], dict]`, :295), wrapping a `JiraDispatchColdStartGateway`:

| Action.kind | live op | notes |
|---|---|---|
| `clear_assignee` | `gw.clear_assignee(target)` | also jira_dispatch.clear_assignee(:2023) |
| `reset_to_todo` | `gw.reset_to_todo(target)` | |
| `transition_under_review` | `gw.transition_under_review(target)` | |
| `mark_resumable` | `gw.mark_resumable(target)` | |
| `mention_operator` | `gw.mention_operator(target, params["message"], urgency=params.get("urgency","high"))` | |
| `escalate` | **OPEN Q1** — no gateway op; proposed = `mention_operator` with `urgency="critical"` + an `escalated` result flag (do NOT silently drop) | |
| `noop` | no mutation; `executed=False` | |

Returns `{"kind","target","executed":True,"outcome":...}` on success, or `{"executed":False,"error":...}` on failure (folded into the decision-log line, same shape as the shadow result so readers can diff shadow-vs-live).

## Design decisions (for audit)
- **D1 — error posture: fail-OPEN per-action.** A failed mutation logs + records `executed:False,error:...` and the tick CONTINUES (never crash the daemon / wedge the coordinator on one bad JIRA call). Rationale: the coordinator is the fleet's brain — one failed action must not take it down. (Mirrors the gateway's lazy/fail-open construction note, :608.)
- **D2 — idempotency.** Derive a per-action idem key (`coord:{decision_id}:{kind}:{target}`) so a retried tick / restart does not double-apply. clear_assignee already takes `idem_key`; the other gateway ops need a check (OPEN Q2 — do they dedupe?).
- **D3 — blast-radius cap.** A per-tick mutation ceiling (e.g., ≤ N executed mutations/tick, default small) + a kill-switch env so a misfiring rule can't mass-mutate the fleet. (OPEN Q3 — right ceiling? reuse rate_limiter.RetryableExecutor:431?)
- **D4 — env-gate, default-safe.** CLI `main()` reads `OMNISIGHT_COORDINATOR_ACTING` (truthy → `acting=True` + `LiveActionExecutor`; default/unset → `acting=False` shadow). Code default stays acting=False. The flip is then a reversible env change + a coordinator restart — NOT a redeploy.
- **D5 — budget coupling.** Removing `OMNISIGHT_COORDINATOR_DAILY_BUDGET_USD=0` happens AT the flip (restoring it while shadow pays for un-executed LLM calls). The env-gate doc must state both env changes go together.
- **D6 — decision-log audit.** Every executed action writes `executed:True` + outcome to the decision-log (the audit trail for "what did the autonomous coordinator actually do"). Already the daemon folds the executor result into the line.

## Open questions for codex
- Q1: `escalate` semantics — mention_operator(critical) vs a distinct path? Is `escalate` ever emitted today (grep the rules)?
- Q2: do `reset_to_todo`/`transition_under_review`/`mark_resumable`/`mention_operator` on `JiraDispatchColdStartGateway` already dedupe / take idem keys, or can a restart double-apply?
- Q3: blast-radius — is there an existing per-tick/rate cap, or must D3 add one? What ceiling is safe for the live fleet?
- Q4: is `JiraDispatchColdStartGateway` safe to call from the STEADY-STATE path, or does it assume cold-start context (e.g., only-once semantics, boot-time assumptions)?
- Q5: cold-start vs steady-state — when acting=True, BOTH the cold-start gateway AND this executor go live in the same restart. Is the combined first-boot behavior safe (the cold_start_complete=[] today says nothing to reset, but confirm)?
- Q6: anything the design MISSED for an autonomous layer that mutates live JIRA + steers the fleet.

## Tests (planned)
- Dispatch: each kind → the right gateway method called with the right args (mock gateway).
- Fail-open: a gateway op raising → result `executed:False,error` + tick continues.
- Idempotency: same action twice → one effective mutation.
- Blast-radius: > ceiling actions in a tick → capped + logged.
- env-gate: `OMNISIGHT_COORDINATOR_ACTING` unset → shadow (acting=False, executor=Shadow); set → acting=True + LiveActionExecutor; `acting=True` without executor still raises.

## Activation sequence (after merge + your +2)
1. Set `OMNISIGHT_COORDINATOR_ACTING=1` + remove `OMNISIGHT_COORDINATOR_DAILY_BUDGET_USD=0` in the coordinator `env.conf` drop-in.
2. `systemctl --user restart pipeline-coordinator`.
3. Watch the decision-log for `executed:True` actions + verify they match intent; rollback = unset the env + restart (reversible).
