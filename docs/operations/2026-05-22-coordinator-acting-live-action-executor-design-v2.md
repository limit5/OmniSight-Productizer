# Coordinator §6 live action executor + acting-flip — DESIGN v2 (codex-revised)

**Status**: DRAFT v2 for codex re-audit (2026-05-22). Supersedes
`2026-05-22-coordinator-acting-live-action-executor-design.md` (v1). v1 + its
audit (`docs/audit/codex-reviews/coordinator-acting-executor-design-codex-audit-2026-05-22.txt`)
are retained as the historical record. This revision integrates all 13 codex
findings (6 BLOCKER / 4 SHOULD-FIX / 3 NICE).

## Problem / scope (unchanged from v1)
`build_default_coordinator(config, acting=True, action_executor=...)` raises
`ValueError` without an `action_executor` (pipeline_coordinator.py:1883–1884).
The CLI (`main`, :1936) only ever builds `acting=False` (shadow); `from_env`
(:149–174) reads no acting flag. The real executor "lands in a later phase"
(`ShadowActionExecutor` docstring, :269). This design builds it: the autonomous
§6 layer that, at `acting=True`, mutates live JIRA + steers the runner fleet.

---

## v1 → v2 changelog (what the audit forced)

| # | v1 said | codex finding | v2 does |
|---|---------|--------------|---------|
| B1 | dispatch keyed on gateway method names (`clear_assignee`, `reset_to_todo`, …) | **BLOCKER** — real vocabulary is `ACTION_*` kinds (rules.py:59–77); rules emit `Action.transition/relabel/escalate/mention_operator` | **dispatch keyed on `action.kind`** (the ADR §6.1 closed set), translating each kind → live op. Mirrors the proven `SprintReplanActionLayer.execute` pattern (sprint_replan.py:141–153). |
| B2 | `escalate` → `mention_operator(urgency="critical")` | **BLOCKER** — gateway adds `needs-operator-action` only when `urgency=="high"` (:808); `critical` silently drops the label, violating `Action.escalate` semantics (rules.py:206) | **`escalate` is a distinct path**: add `needs-operator-action` label + reason comment, never relying on urgency-string side effects. |
| B3 | "idem key derive — gateway ops *need a check*" (open Q) | **BLOCKER** — gateway ops pass no idem key; jira_dispatch helpers default to random UUID (jira_dispatch.py:1664/1964/2014/2023/2032) → restart double-applies | **stable idem keys** `coord:{decision_id}:{kind}:{target}` plumbed through the low-level `jira_dispatch` calls (not the bare gateway). |
| B4 | per-tick ceiling "e.g. ≤N; reuse RetryableExecutor?" (open Q) | **BLOCKER** — no current cap (run_once:1703 unbounded); RetryableExecutor is an API-retry/DLQ wrapper, wrong abstraction | **hard cap = 1 executed action / tick** for first flip, enforced in `run_once`; env-tunable ceiling raised only after canary evidence. |
| B5 | reuse `JiraDispatchColdStartGateway` directly as live backend | **BLOCKER** — gateway swallows exceptions + returns `None` (can't report executed True/False) and `reset_to_todo` hardcodes a cold-start comment (:759) wrong for steady-state | **call the low-level `jira_dispatch` helpers directly** from `LiveActionExecutor` (steady-state reasons, real outcomes, idem keys). Cold-start keeps its own gateway. |
| B6 | "acting=True flips everything in one restart — confirm safe" (open Q) | **BLOCKER** — `build_default_coordinator(acting=True)` flips action executor + sprint replan + cold-start gateway together (:1896,:1902); Startup-2/3 can mass-mutate | **separate first-boot cold-start budget** + phased rollout (see §Activation): a global boot cap distinct from the steady-state 1/tick cap. |
| S7 | D1 fail-OPEN per-action | **SHOULD-FIX** — daemon doesn't catch executor exceptions; a raise aborts the tick *before* logging (run_once:1703–1709) | executor catches per-action AND `run_once` wraps each executor call so one failure logs `executed:False,error` and the tick still records. |
| S8 | D4 env-gate reads `OMNISIGHT_COORDINATOR_ACTING` | **SHOULD-FIX** — not implemented; `main` always `acting=False`, `from_env` reads no such var | **actually wire it**: `from_env` parses the flag; `main` builds the executor + `acting=True` when set. |
| S9 | (not addressed) | **SHOULD-FIX** — cold-start gateway ignores `config.jira_agent_class` (:1149 vs default "claude" :621 vs config "subscription-claude" :128) | `_default_cold_start_gateway` + `LiveActionExecutor` both take `config.jira_agent_class`. |
| S10 | (not addressed) | **SHOULD-FIX** — L6 crash recovery only replays RELABEL/TRANSITION (:1523), reissues transition only for "Under Review" (:1525) | recovery semantics aligned with the executor kind table (§Crash recovery). |
| N11 | (table omitted file_ticket/relabel/transition/mark_for_followup) | **NICE** — all are in `ALLOWED_ACTION_KINDS` | every allowed kind has an explicit branch; unknown kind → `executed:False,reason:"unsupported"` (sprint_replan.py:148 pattern). |
| N12 | (not addressed) | **NICE** — `dry_run` defaults True (rules.py:100); daemon passes all non-noop regardless | executor honors `dry_run`: a `dry_run=True` action records observe-only even when `acting=True` (acting flips dry_run off at construction, see §dry_run). |
| N13 | (not addressed) | **NICE** — Tier-2 actions can target a `coord-skip` ticket; only Tier-1 rules short-circuit (rules.py:611) | executor re-checks `coord-skip` / `coord-quarantine` on the target immediately before mutating. |

---

## Design — `LiveActionExecutor` (v2)

A callable matching the existing seam `ActionExecutor = Callable[[Action, DecisionContext], dict]`
(pipeline_coordinator.py:295), drop-in for `ShadowActionExecutor` (:266). It
dispatches on **`action.kind`** (the ADR §6.1 vocabulary), exactly like
`SprintReplanActionLayer.execute` (sprint_replan.py:141):

| `action.kind` | live op (low-level `jira_dispatch`) | idem key | notes |
|---|---|---|---|
| `transition` | `transition_back_to_todo` / `transition_to_under_review_if_needed` per `params["to_status"]` | `coord:{decision_id}:transition:{target}` | steady-state reason string (NOT cold-start Startup-2(c)) |
| `relabel` | `add_label` / `remove_label` per `params["add"]`/`["remove"]`; ride-along `clear_assignee` when removing claim labels (stale-claim-cleanup, rules.py:724) | `coord:{decision_id}:relabel:{target}` | never adds operator-only labels (rule already guarantees) |
| `mention_operator` | `add_comment(@operator, message)`; `add_label("needs-operator-action")` iff `urgency=="high"` | `coord:{decision_id}:mention:{target}` | matches gateway:807–809 contract |
| `escalate` | `add_label("needs-operator-action")` **+** `add_comment(reason)` — always both | `coord:{decision_id}:escalate:{target}` | distinct path (B2); does NOT route through urgency strings |
| `mark_for_followup` | `add_label("coord-resume-after:{when}")` + reason comment | `coord:{decision_id}:followup:{target}` | per rules.py:197 |
| `file_ticket` | **NOT auto-executed in v2** → `executed:False,reason:"file_ticket_requires_operator"` + `mention_operator` | — | not safely repeatable (mirrors L6 :1530–1538); first flip stays conservative |
| `noop` | none | — | `executed:False` |
| _unknown_ | none | — | `executed:False,reason:"unsupported"` (sprint_replan.py:148) |

Result envelope (same shape as `ShadowActionExecutor`, so a decision-log reader
can diff shadow vs live): `{"kind","target","executed":bool,"outcome":...}` on
success, `{"executed":False,"error":...}` on a caught failure.

### Outcome propagation (B5)
Because `LiveActionExecutor` calls the low-level `jira_dispatch` helpers
directly (not the fail-open gateway that swallows exceptions, :747/:762/:810), a
raised exception is *caught by the executor* and turned into
`executed:False,error:...` — so `executed:True` means the REST call returned,
and `executed:False` carries the reason. The cold-start gateway keeps its own
fail-open behavior for boot; only the steady-state executor needs true outcomes.

### dry_run handling (N12)
`Action.dry_run` defaults `True` (rules.py:100). At `acting=True`, the executor
treats `dry_run=True` as **observe-only** (records `executed:False,dry_run:True`)
and only mutates when the action is `dry_run=False`. The acting flip is
responsible for emitting `dry_run=False` actions (engine wiring TBD — open Q7);
this guarantees a stray `dry_run=True` action can never mutate even in acting
mode. (Default-deny.)

### coord-skip / quarantine recheck (N13)
Before any mutation, the executor re-reads the live labels of `action.target`
and returns `executed:False,reason:"coord_skip"` if `coord-skip` or
`coord-quarantine` is present — closing the Tier-2 gap where an LLM action
targets a ticket the operator just paused (rules.py:611 only guards Tier-1).

---

## Blast-radius enforcement (B4) — in `run_once`, not the executor

`run_once` (pipeline_coordinator.py:1699–1707) currently executes *every*
non-noop action with an unbounded list comprehension. v2 changes the loop to:

1. cap at `OMNISIGHT_COORDINATOR_MAX_ACTIONS_PER_TICK` (**default 1** for first
   flip); excess actions record `executed:False,reason:"tick_cap"` and are
   logged for the operator.
2. wrap each executor call in `try/except` so an executor that raises records
   `executed:False,error` and the tick still logs (fixes S7 — today a raise
   aborts before `self._decision_log.append`, :1709).
3. a kill-switch env `OMNISIGHT_COORDINATOR_ACTING_KILL=1` forces the executor
   to observe-only without a restart (faster than the env-flip rollback).

---

## Cold-start first-boot budget (B6)

`acting=True` flips the action executor, sprint-replan handler, AND cold-start
gateway in one restart (build_default_coordinator:1896,:1902). The 4-phase
recovery (Startup-2/3) can mutate many tickets before the steady-state loop even
begins. v2 adds:
- `OMNISIGHT_COORDINATOR_COLD_START_MAX_MUTATIONS` (default small, e.g. 5) — a
  global boot cap distinct from the 1/tick steady-state cap.
- **phased rollout**: first flip in an environment with a known-small interrupted
  set; `cold_start_complete=[]` is NOT treated as a safety property (codex B6).

---

## Crash-recovery alignment (S10)

L6 `_resolve_interrupted_actions` (pipeline_coordinator.py:1514–1538) replays
`ACTION_RELABEL`/`ACTION_TRANSITION` (idempotent → re-issue) and escalates
`ACTION_FILE_TICKET` to the operator. v2 keeps recovery keyed on the SAME ADR
`ACTION_*` vocabulary as the executor (no `reset_to_todo`/`mark_resumable`
gateway names to reconcile, since v2 never introduced them). New kinds the
executor now mutates (`mention_operator`, `escalate`, `mark_for_followup`) are
all label/comment adds → idempotent under the stable idem key, so recovery can
mark them `completed` safely. This is documented so recovery and executor stay
in lockstep.

---

## D4 env-gate, default-safe (S8) — concrete wiring

1. `CoordinatorConfig.from_env` (:149) parses `OMNISIGHT_COORDINATOR_ACTING`
   (truthy → `acting=True`) into a new `acting: bool = False` config field.
2. `main` (:1936) reads it and, when set, constructs `LiveActionExecutor(...)`
   and calls `build_default_coordinator(config, acting=True, action_executor=...)`.
   Default/unset → today's `acting=False` shadow path (unchanged, still safe).
3. `_default_cold_start_gateway` (:1149) and `LiveActionExecutor` both receive
   `config.jira_agent_class` (fixes S9 identity mismatch — gateway default
   "claude" :621 vs config "subscription-claude" :128).

The flip is then a reversible env change + `systemctl --user restart
pipeline-coordinator` — NOT a redeploy.

---

## D5 budget coupling (unchanged from v1)
Removing `OMNISIGHT_COORDINATOR_DAILY_BUDGET_USD=0` happens AT the flip (shadow
pays for un-executed LLM calls otherwise). The activation doc states both env
changes go together.

---

## Open questions for codex (v2)
- Q7: **dry_run source of truth** — at `acting=True`, who emits `dry_run=False`?
  Should the acting flip rewrite actions to `dry_run=False` at the engine seam,
  or should the executor treat acting-mode as "ignore dry_run"? v2 chose
  default-deny (only `dry_run=False` mutates); confirm the engine actually emits
  `dry_run=False` somewhere when acting, else nothing ever mutates.
- Q8: **idem-key durability** — `idempotency.py:90` dedupes only when the SAME
  key is reused; `decision_id` is fresh per tick (DecisionResult docstring,
  rules.py:266). So two ticks deciding the same action get DIFFERENT keys and
  both apply. Is per-tick the right idempotency grain, or should the key be
  `coord:{kind}:{target}:{content-hash}` (stable across ticks)?
- Q9: **transition reason strings** — what steady-state reason should `transition
  → To Do` carry (the cold-start one at :759 is wrong)? Operator-readable text?
- Q10: **1/tick cap vs multi-ticket sweeps** — stale-claim-cleanup can legitimately
  want to clear N tickets in one sweep tick. Does a 1/tick cap starve legitimate
  bulk reconcile, and if so what's the safe ramp?
- Q11: anything the v2 design STILL misses for an autonomous live-JIRA mutator.

## Audit asks for codex (this round)
1. **Re-audit**: are B1–B6 / S7–S10 / N11–N13 actually resolved by v2, or only
   partially? Cite file:line.
2. **Planned-diff audit**: review the *intended code changes* implied by v2
   (new `LiveActionExecutor` class; `run_once` cap+try/except; `from_env`+`main`
   env-gate; `_default_cold_start_gateway` agent_class plumb; idem-key threading
   into `jira_dispatch` calls). What breaks? What existing test asserts the old
   behavior?
3. **Implementation-risk audit**: what will bite during implementation —
   e.g. `jira_dispatch` helper signatures that don't accept idem keys today,
   the `decision_id`-per-tick idempotency hole (Q8), the engine never emitting
   `dry_run=False` (Q7), capability-matrix / systemd EnvironmentFile gaps for
   the new env vars.

---

## Tests (planned, v2)
- Dispatch: each `action.kind` → correct low-level `jira_dispatch` call w/ idem
  key (mock dispatch); unknown kind → `unsupported`.
- escalate: adds `needs-operator-action` label AND reason comment (B2).
- Fail-open: a `jira_dispatch` call raising → `executed:False,error` + tick still
  logs (S7 — assert `decision_log.append` ran).
- dry_run: `dry_run=True` action at acting=True → observe-only (N12).
- coord-skip: target carries `coord-skip` → `executed:False,reason:"coord_skip"` (N13).
- Blast cap: > cap actions in a tick → capped + excess logged (B4).
- env-gate: `OMNISIGHT_COORDINATOR_ACTING` unset → shadow; set → acting + Live
  executor; `acting=True` w/o executor still raises (:1884).
- agent_class: `_default_cold_start_gateway` + executor receive
  `config.jira_agent_class` (S9).

## Activation sequence (after merge + human +2)
1. **Phased**: first flip in an env with a small interrupted-ticket set; verify
   the cold-start boot cap (B6) is not exceeded.
2. Set `OMNISIGHT_COORDINATOR_ACTING=1` + remove
   `OMNISIGHT_COORDINATOR_DAILY_BUDGET_USD=0` in the coordinator `env.conf`
   drop-in (both together, D5). Confirm `MAX_ACTIONS_PER_TICK=1` for first flip.
3. `systemctl --user restart pipeline-coordinator`.
4. Watch the decision-log for `executed:True` actions; verify each matches
   intent. Rollback = `OMNISIGHT_COORDINATOR_ACTING_KILL=1` (no restart) OR unset
   the env + restart (reversible).
