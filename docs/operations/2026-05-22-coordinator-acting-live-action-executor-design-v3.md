# Coordinator §6 live action executor + acting-flip — DESIGN v3 (codex round-2 revised)

**Status**: DRAFT v3 for codex round-3 audit (2026-05-22). Supersedes
`...-design-v2.md`. Lineage retained:
- v1 + audit: `coordinator-acting-executor-design-codex-audit-2026-05-22.txt` (13 findings)
- v2 + audit: `coordinator-acting-executor-design-v2-codex-audit-2026-05-22.txt` (verdict **needs-v3**)

v2 resolved B1/B2/B5/S7/S8/S9/N11 at the design level. v3 closes the items the
round-2 audit left PARTIAL/NOT-RESOLVED **and** the 3 new BLOCKERs the
implementation-risk audit surfaced (Q7 dry_run no-op, Q8 idem grain, Q11 crash
window), plus repo-reality corrections.

## Problem / scope (unchanged)
`build_default_coordinator(config, acting=True, action_executor=...)` raises
without an executor (pipeline_coordinator.py:1883). `main` (:1936) only builds
`acting=False`; `from_env` (:149) reads no acting flag. This builds the §6 live
executor + the env-gated flip.

---

## v2 → v3 changelog (what round-2 forced)

| Round-2 finding | round-2 status | v3 resolution |
|---|---|---|
| **B3 idem keys** | PARTIAL — `coord:{decision_id}:...` never dedupes across ticks (decision_id fresh per tick, rules.py:266; idempotency.py:90 caches exact-key only) | **key grain corrected** → `coord:{kind}:{target}:{content_hash}` (stable across ticks) with per-REST-call suffixes `:add:<label>` / `:remove:<label>` / `:clear-assignee` / `:transition` / `:comment` (jira_dispatch `_request_idempotent` caches by key, :331 — one key per PUT/POST). See §Idempotency. |
| **B4 blast cap** | PARTIAL — cap location right, starvation/bulk-sweep undefined | default `MAX_ACTIONS_PER_TICK=1` kept, **+ documented ramp + fairness**: a per-target round-robin cursor so repeated single-action ticks drain a backlog (e.g. stale-claim-cleanup, one relabel/ticket rules.py:707) instead of starving. Ramp gated on canary evidence, not a bare env knob. See §Blast-radius. |
| **B6 cold-start cap** | PARTIAL — single global int ambiguous across systemctl/JIRA/sweep | **per-phase mutation accounting**: separate counters for Startup-1 `start_unit` (:653), Startup-2 transitions/labels (:735), Startup-3 sweep loop (:776), each with its own cap; "mutation" defined per phase. See §Cold-start budget. |
| **S10 crash recovery** | PARTIAL — L6 can't know which subcall of a composite action completed (log stores actions separately from action_results, :1394) | **action_results persisted alongside actions** in the tick record so L6 can read per-subcall outcomes; recovery keyed on the same ADR vocabulary + idem keys (re-issue is safe under stable keys). See §Crash recovery. |
| **N12 / Q7 dry_run** | NOT-RESOLVED / BLOCKING — nothing emits `dry_run=False` (factories default True rules.py:126; Tier-1 :904 + Tier-2 llm:1073/1132 pass through) → acting mode mutates nothing, forever | **OPEN DECISION — two options below (§dry_run), for codex round-3 to pick.** Either way v3 makes the flip explicit so acting actually mutates. |
| **N13 coord-skip recheck** | PARTIAL — didn't name the label-read helper or fail-mode | **fail-CLOSED**: executor reads live labels via `jira_dispatch.get_issue_labels` (or equivalent) immediately before mutating; a read failure → `executed:False,reason:"label_read_failed"` (do NOT mutate on unknown state). See §coord-skip. |
| **NEW (Sec2): destructive transition** | BLOCKER — `transition_back_to_todo` clears assignee (jira_dispatch:1991); rule context may be stale at execution time (:696) | **fresh recheck before destructive ops**: for `transition → To Do`, re-verify no-live-runner + status still 進行中 at execution time; abstain (`executed:False,reason:"context_stale"`) if changed. |
| **NEW (Sec2): env wiring reality** | BLOCKER — unit uses inline `Environment=` (pipeline-coordinator.service:29-32), **no EnvironmentFile**; runbook:53 says so + use a drop-in | **activation rewritten** to a systemd drop-in (`systemctl --user edit`), NOT an `env.conf` file. See §Activation. |
| **NEW (Sec3 Q11): crash window** | BLOCKER — `run_once` executes actions (:1703) BEFORE appending the tick record (:1708); death between = invisible to L6 | **intent-log before execute**: write a `tick_intent` record (planned actions + idem keys) BEFORE executing, then the outcome record after. L6 replays intent records whose outcome is missing, idempotently. See §Crash recovery. |
| **NEW (Sec3): capability scope** | clarification | documented: the coordinator daemon is **not** capability-matrix gated (that vocabulary, capability_matrix.yaml:27, governs runner pickup). The flip is a **daemon env authorization**, not a runner capability. |
| **NEW (Sec2/3): test updates** | — | explicit test-update list (§Tests): `test_l6_resolves_interrupted_actions_by_kind` (coldstart:473), `test_default_cold_start_gateway_shadow_vs_acting` (coldstart:954), `test_pipeline_coordinator_systemd.py:68`. |
| **NEW (Sec3 NICE): file_ticket accounting** | clarification | `file_ticket` records BOTH `executed:False (file_ticket, requires_operator)` AND the `mention_operator` side effect as a distinct result line, so decision-log readers don't misread `executed:False` as "nothing happened". |

---

## §dry_run — the one OPEN DECISION for codex round-3 (Q7)

Confirmed: no engine path emits `dry_run=False` today (rules.py:126 default,
Tier-1 pass-through :904, Tier-2 pass-through llm:1073/1132). So a literal
"only mutate when `dry_run=False`" executor would observe-only forever. v3 must
choose where the flip happens. **Two options, trade-offs stated; codex round-3
picks one** (this is the architecture fork):

### Option A — executor ignores `dry_run` in acting mode
`LiveActionExecutor` is constructed only on the `acting=True` path
(build_default_coordinator:1883), so its mere existence means "mutate". It
ignores the per-action `dry_run` flag entirely; the flag stays meaningful only
for `ShadowActionExecutor`.
- **+** smallest diff (no engine/factory change); single source of truth = the
  env-gate + which executor is wired.
- **+** no risk of a half-flipped state (some actions real, some not).
- **−** the per-action `dry_run` field becomes dead in acting mode; a future
  per-action dry-run (e.g. "shadow just this risky kind") needs new plumbing.
- **−** decision-log `dry_run:true` lines (logged from the action, :1358) would
  be misleading in acting mode unless the executor overwrites the logged flag.

### Option B — engine seam rewrites actions to `dry_run=False` when acting
A thin wrapper between `engine.evaluate` and the executor (run_once:1699-1703)
rebuilds each `Action` with `dry_run=False` when `acting=True`.
- **+** `dry_run` stays a real per-action property end-to-end; decision-log is
  accurate; enables future per-kind shadow-in-acting.
- **+** executor logic is uniform (always "mutate iff `dry_run=False`").
- **−** larger diff; `Action` is a frozen dataclass (rules.py:80) → rebuild via
  `dataclasses.replace`, touching every action each tick.
- **−** a second place (the wrapper) must stay in sync with the env-gate.

**v3 recommendation: Option A** (smallest blast radius for a first live flip;
the per-action dry-run nicety is YAGNI today). But this is the fork we want
codex to rule on with fresh eyes.

---

## Idempotency (B3 / Q8) — corrected grain

- Key = `coord:{kind}:{target}:{content_hash}` where `content_hash` is a stable
  digest of the action's semantic payload (`params`), **independent of
  `decision_id`** — so the same logical action on two ticks dedupes (the bug
  round-2 caught: decision_id is fresh per tick, rules.py:266).
- Composite actions issue multiple REST calls; `_request_idempotent` caches by
  key (jira_dispatch.py:331), so each call gets a distinct **suffix**:
  `:add:<label>`, `:remove:<label>`, `:clear-assignee`, `:transition`,
  `:comment`. (Otherwise a relabel adding 2 labels would dedupe the 2nd PUT.)
- The named low-level helpers already accept `idem_key`
  (`transition_to_under_review_if_needed` :1649, `transition_back_to_todo`
  :1934, comment/label/assignee :2014) — **no signature change needed**, only
  key discipline.

---

## §LiveActionExecutor dispatch (carried from v2, unchanged vocabulary)

Dispatch on `action.kind` (sprint_replan.py:141 pattern) → low-level
`jira_dispatch` via a client built from `config.jira_agent_class` (fixes S9):

| kind | live op | idem suffix |
|---|---|---|
| `transition` | `transition_back_to_todo` / `transition_to_under_review_if_needed` per `to_status` + **destructive-recheck for To Do** | `:transition` |
| `relabel` | `add_label`/`remove_label`; ride-along `clear_assignee` on claim-label removal | `:add:<l>`/`:remove:<l>`/`:clear-assignee` |
| `mention_operator` | `add_comment(@op)`; label iff `urgency=="high"` (:807) | `:comment`(+`:label`) |
| `escalate` | `add_label("needs-operator-action")` **+** `add_comment(reason)` — always both (B2) | `:label`+`:comment` |
| `mark_for_followup` | `add_label("coord-resume-after:{when}")` + comment | `:label`+`:comment` |
| `file_ticket` | NOT auto-executed → `mention_operator` + 2 result lines (accounting fix) | `:comment`+`:label` |
| `noop` / unknown | none → `executed:False` (`reason:"unsupported"` for unknown) | — |

Outcome: real `executed:True/False` because we call low-level helpers (which
raise on failure) inside a per-action `try/except`, NOT the fail-open gateway
that swallows exceptions (:746/:762/:810).

---

## §Blast-radius (B4) — cap + fairness

`run_once` (:1699-1707) gains:
1. `OMNISIGHT_COORDINATOR_MAX_ACTIONS_PER_TICK` (default **1**). Excess →
   `executed:False,reason:"tick_cap"`, logged.
2. **Fairness cursor**: a persisted round-robin over targets so consecutive
   capped ticks drain a backlog rather than re-picking the same focal ticket
   (addresses round-2's starvation concern for one-action-per-ticket sweeps).
3. Per-action `try/except` so one failure logs + the tick still records (S7).
4. Kill-switch `OMNISIGHT_COORDINATOR_ACTING_KILL=1` → observe-only without
   restart.

---

## §Cold-start budget (B6) — per-phase accounting

Distinct caps, each defining "mutation" for its phase:
- Startup-1: `start_unit` systemctl starts (:653) — cap N₁.
- Startup-2: JIRA transitions/labels/comments (:735) — cap N₂.
- Startup-3: stale-sweep relabel/clear loop (:776) — cap N₃.
Env: `OMNISIGHT_COORDINATOR_COLD_START_MAX_{INFRA,RECONCILE,SWEEP}`. First flip
in an env with a known-small interrupted set; `cold_start_complete=[]` is NOT a
safety property (round-1 B6).

---

## §Crash recovery (S10 / Q11) — intent-log + persisted outcomes

1. **Intent-before-execute**: `run_once` writes a `tick_intent` record (planned
   actions + their idem keys) BEFORE executing (closes the :1703-before-:1708
   window). After execution it writes the outcome record.
2. **Persist `action_results`** alongside `actions` in the tick record (:1394)
   so L6 reads per-subcall outcomes.
3. L6 (`_resolve_interrupted_actions` :1514) replays any `tick_intent` whose
   outcome record is missing, re-issuing under the SAME stable idem key →
   idempotent, no double-apply. Recovery stays keyed on the ADR `ACTION_*`
   vocabulary (no gateway-method names introduced).

---

## §coord-skip / quarantine recheck (N13) — fail-closed
Before any mutation the executor re-reads live labels of `action.target`
(`jira_dispatch.get_issue_labels` or equivalent) and:
- `coord-skip`/`coord-quarantine` present → `executed:False,reason:"coord_skip"`.
- label read FAILS → `executed:False,reason:"label_read_failed"` (fail-CLOSED;
  never mutate on unknown state). Closes the Tier-2 gap (rules.py:611 guards
  only Tier-1).

---

## §Activation (rewritten — repo reality)
The deployed unit sets env **inline** via `Environment=`
(pipeline-coordinator.service:29-32); there is **no EnvironmentFile**
(coordinator-runbook.md:53). So activation is a **systemd drop-in**, not an
`env.conf`:
1. Phased: first flip in an env with a small interrupted set; verify per-phase
   cold-start caps not exceeded.
2. `systemctl --user edit pipeline-coordinator` → add
   `Environment=OMNISIGHT_COORDINATOR_ACTING=1` and remove the
   `OMNISIGHT_COORDINATOR_DAILY_BUDGET_USD=0` override (both together — D5).
   Confirm `MAX_ACTIONS_PER_TICK=1`.
3. `systemctl --user daemon-reload && systemctl --user restart pipeline-coordinator`.
4. Watch decision-log for `executed:True`; verify intent matches. Rollback =
   `OMNISIGHT_COORDINATOR_ACTING_KILL=1` (no restart) OR revert the drop-in +
   restart.

**Authorization scope**: this is a daemon env authorization. The coordinator
daemon is NOT capability-matrix gated (capability_matrix.yaml:27 `jira_update`
governs runner pickup, a different actor).

---

## §Tests (v3)
Carried from v2 plus:
- Idempotency: same logical action on two ticks (different decision_id) → ONE
  effective mutation (the Q8 regression test).
- Composite suffix: relabel adding 2 labels → 2 distinct PUTs, neither deduped.
- Destructive recheck: `transition→To Do` with a now-live runner → abstains.
- Crash window: intent written before execute; missing-outcome intent replays
  idempotently.
- coord-skip fail-closed: label read raises → no mutation.
- **Update existing**: `test_l6_resolves_interrupted_actions_by_kind`
  (coldstart:473), `test_default_cold_start_gateway_shadow_vs_acting`
  (coldstart:954, assert agent_class), `test_pipeline_coordinator_systemd.py:68`
  (new env assertions).

---

## Open questions for codex (round-3)
- **Q7 (the fork)**: pick Option A (executor ignores dry_run in acting) vs
  Option B (engine seam rewrites to dry_run=False). v3 recommends A.
- Q12: is `content_hash` over `params` enough for the idem grain, or must it
  include `kind`+`target` only (params may carry volatile fields)?
- Q13: fairness cursor persistence — reuse the decision-log, or a separate
  small state file? (Daemon is stateless-across-restarts by design, ADR §3.2.)
- Q14: per-phase cold-start caps — safe default integers (N₁/N₂/N₃)?
- Q15: anything v3 STILL misses.

## Audit asks for codex (round-3)
1. Re-confirm B3/B4/B6/S10/N12/N13 + the 4 new BLOCKERs (destructive transition,
   env reality, crash window Q11, dispatch) are RESOLVED, with file:line.
2. Rule on the Q7 fork (A vs B) with reasoning.
3. Final verdict: file-ready / file-ready-with-conditions / needs-v4.
