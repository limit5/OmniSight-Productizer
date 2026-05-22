# Coordinator §6 live action executor + acting-flip — DESIGN v4 (codex round-3 pinned)

**Status**: DRAFT v4 for codex round-4 confirmation (2026-05-22). Supersedes
`...-design-v3.md`. Lineage:
- v1 audit (13 findings) → v2 audit (needs-v3) → v3 audit (needs-v4):
  `coordinator-acting-executor-design{,-v2,-v3}-codex-audit-2026-05-22.txt`

v3 reached convergence: round-3 surfaced **no new architecture problems** and
codex supplied a concrete answer for every PARTIAL. v4 *pins* those answers —
this is a mechanical close-out, not a redesign. The §LiveActionExecutor dispatch
table, blast-cap location, env-gate wiring, and activation are carried from v3
unchanged unless listed below.

---

## v3 → v4 changelog (round-3 answers, now pinned)

| Round-3 item | round-3 status | v4 pin |
|---|---|---|
| **Q7 dry_run fork** | codex **ruled Option B** | **Option B chosen.** A wrapper between `engine.evaluate` and the executor (run_once:1699-1703) does `dataclasses.replace(action, dry_run=False)` for every non-noop action **when `acting=True`**. Rationale (codex): `run_once` logs `actions` + top-level `dry_run` from the action objects (:1358); Option A would mutate while the log still reads `dry_run:true` → false audit + false crash-recovery input. The frozen-dataclass cost (rules.py:80) is paid via `replace`, accepted for truthful logs. |
| **B3 / Q12 idem hash** | RESOLVED w/ condition | **content_hash = stable digest of the action's *canonical semantic params*, EXCLUDING volatile fields** (`decision_id`, timestamps, transient observations). For operator-visible text (comments/reasons), the hash includes the **exact emitted string** (or a deterministically derived one) so a reworded comment is a new action, not a silent dedupe. Key = `coord:{kind}:{target}:{content_hash}` + per-REST suffix (`:add:<l>`/`:remove:<l>`/`:clear-assignee`/`:transition`/`:comment`/`:label`). |
| **N13 label recheck** | behaviorally right, **wrong helper name** | **use `fetch_ticket_labels(client, key)`** (jira_dispatch.py:1826) — `get_issue_labels` does not exist. On exception → `executed:False,reason:"label_read_failed"` (fail-CLOSED, never mutate on unknown state). |
| **Destructive transition recheck** | PARTIAL — name the live helpers | before `transition → To Do`, re-verify at execution time with **`get_issue_status(client, key)`** (jira_dispatch.py:1586) still ∈ 進行中 AND **`_ticket_has_live_runner(key)`** (pipeline_coordinator.py:986) is False; else `executed:False,reason:"context_stale"`. |
| **B6 / Q14 cold-start caps** | PARTIAL — defaults? | **first-flip defaults `INFRA=1`, `RECONCILE=1`, `SWEEP=1`** (`OMNISIGHT_COORDINATOR_COLD_START_MAX_{INFRA,RECONCILE,SWEEP}`); raise only after canary evidence. Per-phase counters: Startup-1 `start_unit` (:653), Startup-2 transitions/labels (:735), Startup-3 sweep (:776). |
| **B4 / Q13 fairness cursor** | PARTIAL — persistence? | **cursor state is decision-log-derived, NOT a new file.** ADR-0021 §3.2 (ADR-0021:105) blesses only decision-log/state artifacts for stateless-across-restarts; the round-robin position is rebuilt from the recent decision-log tail (which targets were acted on last), same pattern as the budget guard rebuilding spend from the log. |
| **S10 / Q11 crash correlation** | PARTIAL — correlation undefined | **see §Crash recovery below** — explicit `tick_intent`↔outcome correlation key + why replay is idempotent. |

---

## §Crash recovery — correlation + idempotent replay (S10 / Q11 pinned)

The round-3 ask: define how a `tick_intent` matches its outcome, and how
completed subcalls avoid replay. v4:

1. **Correlation key** = the tick's `decision_id` (rules.py:266 — fresh per tick,
   so unique per intent). `run_once` writes:
   - `tick_intent` record `{decision_id, planned: [{kind, target, idem_keys:[...]}], ts}` **before** executing (closes the execute-before-append window at :1703-before-:1708);
   - the existing outcome `decision_tick` record `{decision_id, action_results, ...}` **after**, carrying the SAME `decision_id`.
2. **L6 replay rule** (`_resolve_interrupted_actions` :1514; L6 scans
   `decision_tick` + shutdown records today, :1493 — extend it to also scan
   `tick_intent`): a `tick_intent` whose matching `decision_tick` (same
   `decision_id`) is **absent** is a crash window → replay its planned actions.
3. **Why replay is safe (no double-apply)**: replay re-issues under the SAME
   stable idem keys (`coord:{kind}:{target}:{content_hash}:<suffix>`). The
   idempotency store (idempotency.py:90) dedupes any subcall that already
   completed before the crash — so a partially-applied composite action
   completes exactly the missing subcalls. This is the property the v3 B3 fix
   buys us: stable keys make crash-replay correct *by construction*, no
   per-subcall completion bookkeeping needed.
4. `action_results` is persisted in the `decision_tick` record (:1394) for
   observability/diff, but L6 correctness relies on the idem store, not on
   parsing partial `action_results`.

---

## §dry_run flip — Option B wiring (Q7 pinned)

```
result = self._engine.evaluate(ctx)                      # :1699 (unchanged)
actions = result.actions
if self._acting:                                          # new: only in acting mode
    actions = tuple(
        a if isinstance(a, NoopAction) else replace(a, dry_run=False)
        for a in actions
    )
# tick-record / dry_run logging (:1358) now sees the real dry_run=False ─ truthful
action_results = [...]  # cap + per-action try/except (B4/S7), over `actions`
```
- `self._acting` is set from the env-gate (`OMNISIGHT_COORDINATOR_ACTING`, S8) at
  construction, the single source of truth.
- Executor logic stays uniform: "mutate iff `dry_run=False`". A stray
  `dry_run=True` action in acting mode still can't mutate (it was a NoopAction or
  the replace didn't touch it) — default-deny preserved.
- Decision-log + `tick_intent` now record `dry_run:false` truthfully, which is
  exactly what L6 reads for crash recovery (the reason codex rejected Option A).

---

## §coord-skip / destructive recheck — pinned helper names

Immediately before any mutation, in the executor:
1. `labels = fetch_ticket_labels(client, action.target)` (jira_dispatch.py:1826);
   on exception → `executed:False,reason:"label_read_failed"` (fail-closed).
2. `coord-skip`/`coord-quarantine` ∈ labels → `executed:False,reason:"coord_skip"`.
3. **For `transition → To Do` only** (destructive — clears assignee,
   jira_dispatch.py:1991): additionally require
   `get_issue_status(client, target)` (jira_dispatch.py:1586) ∈ IN_PROGRESS names
   AND `not _ticket_has_live_runner(target)` (pipeline_coordinator.py:986); else
   `executed:False,reason:"context_stale"`.

---

## Carried unchanged from v3 (still in force)
- Dispatch on `action.kind` → low-level `jira_dispatch` via a client built from
  `config.jira_agent_class` (B1/B5/S9); escalate = label + comment (B2).
- Blast cap in `run_once`: `MAX_ACTIONS_PER_TICK` default 1 + per-action
  try/except + kill-switch `OMNISIGHT_COORDINATOR_ACTING_KILL=1` (B4/S7).
- env-gate: `from_env` parses `OMNISIGHT_COORDINATOR_ACTING`; `main` builds the
  executor + `acting=True` when set (S8). Activation via **systemd drop-in**
  (`systemctl --user edit`), NOT `env.conf` — inline `Environment=`
  (pipeline-coordinator.service:29-32), no EnvironmentFile (runbook:53).
- `file_ticket` not auto-executed → `mention_operator` + two result lines.
- Coordinator daemon is NOT capability-matrix gated (capability_matrix.yaml:27
  is runner pickup); the flip is daemon env authorization.

---

## Open questions for codex (round-4)
- Q16: is `decision_id` the right correlation key, or should `tick_intent` carry
  an explicit `intent_id` (e.g. if a tick can be re-evaluated)? 
- Q17: rebuilding the fairness cursor from the decision-log tail — how deep a
  tail is safe/cheap, and does it interact with log rotation (YYYY-MM-DD.jsonl)?
- Q18: anything v4 STILL misses for first live flip.

## Audit asks for codex (round-4 — confirmation)
1. Confirm Q7 Option B is wired correctly (replace at the seam, dry_run logging
   now truthful) — file:line.
2. Confirm each round-3 PARTIAL is now RESOLVED: idem hash (volatile exclusion),
   N13 helper (`fetch_ticket_labels`), destructive recheck (`get_issue_status` +
   `_ticket_has_live_runner`), cold-start defaults, fairness cursor (decision-log
   derived), crash correlation (`decision_id` + idempotent replay).
3. Final verdict: **file-ready** / file-ready-with-conditions / needs-v5.

---

## ✅ Round-4 verdict: file-ready-with-conditions

codex round-4 (`...-v4-codex-audit-2026-05-22.txt`) confirmed RESOLVED: idem hash,
N13 helper (`fetch_ticket_labels`), cold-start defaults (1/1/1), fairness cursor
(decision-log derived), dispatch, systemd drop-in, capability scope. Three items
are **implementation conditions** (not design changes) — carry these verbatim as
ticket acceptance criteria:

1. **Option B must rebuild `DecisionResult`, not just a local tuple.** After the
   acting dry-run flip, do `result = dataclasses.replace(result, actions=actions)`
   so `_build_tick_record()` (pipeline_coordinator.py:1394) AND the learning-loop
   writeback see `dry_run=false`. Rewriting only a local `actions` tuple leaves
   the logged tick reading `dry_run:true` (the exact failure that sank Option A).
   The rebuilt `DecisionResult` MUST preserve the same `decision_id`.
2. **Destructive `To Do` recheck must fail CLOSED on probe uncertainty.**
   `_ticket_has_live_runner()` treats probe errors as "not live" by design
   (pipeline_coordinator.py:987-990). The live executor needs a STRICT wrapper:
   an unknown/failed probe → abstain `executed:False,reason:"context_stale"`
   (do NOT proceed to clear assignee on an uncertain runner state).
3. **`tick_intent` stores FINAL stable idem keys + 24h replay window.** The
   intent record carries the resolved `coord:{kind}:{target}:{hash}:<suffix>`
   keys (not just action payloads); L6 replays a missing-outcome intent ONLY
   within the idempotency TTL window (idempotency.py:40, 24h) — beyond it the
   idem store has expired and replay could double-apply.

Round-4 Q-answers folded in:
- **Q16**: `decision_id` is the correct correlation key; add `intent_seq` (not a
  new identity) only if one evaluated decision ever emits multiple intent records.
- **Q17**: rebuild the fairness cursor from a bounded decision-log tail spanning
  **all date files in the 24h replay window** (not just today's `YYYY-MM-DD.jsonl`),
  with a record-count cap as a perf guard.

---

## §Tests (v4 — delta over v3)
- Option B: in acting mode every non-noop action reaches the executor with
  `dry_run=False` AND the logged tick record reads `dry_run:false`.
- idem hash: two ticks, same semantic action, different `decision_id` → one
  mutation; a reworded comment → a second mutation (not deduped).
- destructive recheck: now-live runner / status changed → `context_stale`.
- label read fail-closed: `fetch_ticket_labels` raises → no mutation.
- crash replay: `tick_intent` without matching `decision_tick` → replay; already
  applied subcalls deduped by idem store → no double-apply.
- Update existing: `test_l6_resolves_interrupted_actions_by_kind` (coldstart:473,
  now must handle `tick_intent`), `test_default_cold_start_gateway_shadow_vs_acting`
  (coldstart:954, assert agent_class), `test_pipeline_coordinator_systemd.py:68`.
