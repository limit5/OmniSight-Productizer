# Coordinator §6 LiveActionExecutor + acting-flip — TICKET DRAFT (runner-verified, minimal-unit)

**Revision r4 (2026-05-23)** — decomposed to minimal execution units + tier
minimized per ADR-0005. Changes vs r3:
- **Split** the merged run_once ticket into **3 single-concern tickets**
  (T3 dry_run Option B / T4 blast-cap+kill-switch+fairness / T5 crash intent-log+L6).
  Safe because the **linear chain** serializes them (no concurrent edit of run_once →
  no file-mutex / merge conflict). Renumbered: env-gate → T6, activation → T7.
- **Tier corrected to M** for all impl tickets (the merged ticket was wrongly L).
  Per ADR-0005, tier is **path/change-type based, not effort-based**: a single-file
  non-security change to `backend/agents/pipeline_coordinator.py` = **M**; the L
  triggers (new module / new schema / auth-security-crypto / public-API change) do
  NOT apply.

## Tier rationale (ADR-0005 — verified, not chosen)
Tier is computed from touched paths, then change-type. **Tier S is unattainable for
any impl ticket**: the Tier S whitelist is `backend/tests/**` + `*.md` +
`messages/*.json` + `openapi/*.generated.json` only; every impl ticket edits
production code in `backend/agents/pipeline_coordinator.py` (not whitelisted) →
deny-by-default → **M**. To STAY at M (not be forced to L), the new
`LiveActionExecutor` is added **inside the existing `pipeline_coordinator.py`
module** — a brand-new module file would trip the "New module → L" change-type rule.
T7 (live activation, reversibility-impacting, operator-gated) = **X**, cannot be lower.

| ticket | touched paths | tier | why minimal |
|---|---|---|---|
| T1–T6 | `backend/agents/pipeline_coordinator.py` (+ `backend/tests/**`, `tests/…`) | **M** | production-code edit → can't be S; no L trigger → not L |
| T7 | systemd drop-in (live) + `docs/operations/coordinator-runbook.md` | **X** | live prod activation / reversibility-impacting / human-only |

## Runner-executability (pickup JQL + capability matrix — verified)
Pickup JQL (`jira_dispatch.py:357`) requires ALL: `issuetype = Story` AND
`status = "To Do"` AND `assignee EMPTY` AND `labels = "class:subscription-<runner>"`
AND `labels not in ("tier:X")`. Capability = **union across areas**
(`resolve_for_areas`). All impl tickets tier:M backend → matrix grants
`code_edit, run_tests, run_lint, gerrit_push, jira_update, mcp_search, memory_recall`
(capability_matrix.yaml:62) — **no `capability:enable=gerrit_push` needed** (that's a
tier:S workaround; M auto-grants push). T7 tier:X → JQL excludes → human-only.

## Class routing (operator knob)
Recommend **`class:subscription-codex`** for impl tickets (codex envelope proven for
extend-existing backend design+impl+tests; claude-review-after). Flip per-ticket by
swapping the one label. Linear chain ⇒ one pickable at a time ⇒ class choice doesn't
affect throughput.

## Filing protocol (SOP §3 Trap B — avoids stoploss)
1. File META + T1–T7 with **blockedBy links already wired** (never add a blockedBy
   after a runner grabs a child → submit-time `has_unresolved_blockedby` abort + stoploss).
2. Give **only T1** its `class:` label initially (pickable). T2–T7 filed WITHOUT the
   `class:` label (parked: blockers wired, not pickable).
3. As each ticket reaches **公開済み / Published** (NOT Archived — only 公開済み/Published
   resolves `has_unresolved_blockedby`, file_coordinator.py:21), add the `class:` label
   to the next ticket. T7 (GATE) stays human-only throughout.

Conventions: 4-AC; area:* spans all AC areas (9-value whitelist); tag
`[META|OP|GATE][coord-acting]`; default code posture `acting=False` (shadow) until T7.

---

## META — [META][coord-acting] Coordinator §6 live action executor + acting-flip (ADR-0021)
- **type**: meta · **labels**: `scope:coord-acting`, `area:backend`
- **Goal**: build the unbuilt ADR-0021 §6 autonomous action layer so the coordinator
  can, at `acting=True`, mutate live JIRA + steer the fleet — default-safe until the
  operator flip. Closes `build_default_coordinator(acting=True)` ValueError
  (pipeline_coordinator.py:1883).
- **Children**: T1→T7 (linear). **Relates**: OP-1555.
- **DoD**: all children 公開済み; codex round-4 conditions evidenced; phased
  first-flip shows `executed:True` matching intent.

## T1 — [OP][coord-acting] LiveActionExecutor core (dispatch + idempotency + outcomes)
- **issuetype**: Story · **tier**: M · **area**: `area:backend`, `area:tests`
- **labels**: `scope:coord-acting`, `agent:auto`, `class:subscription-codex` (pickable)
- **## Files/Paths**: `backend/agents/pipeline_coordinator.py` (add `LiveActionExecutor`
  in this EXISTING module near `ShadowActionExecutor` :266 / seam :295 — NOT a new
  file, which would force tier L), `backend/tests/` (new test module). **Out of scope**:
  run_once changes (T3–T5), env-gate (T6), pre-mutation rechecks (T2 — leave a `# T2` seam).
- **v4 coverage**: §LiveActionExecutor dispatch, §Idempotency, B1/B2/B5/N11, file_ticket accounting, S9.
- **Code AC**:
  - New `LiveActionExecutor` (drop-in for the `ActionExecutor` seam :295) dispatching
    on `action.kind` (rules.py:67), mirroring `SprintReplanActionLayer.execute`
    (sprint_replan.py:141). Client from `config.jira_agent_class` (S9).
  - Calls **low-level `jira_dispatch` helpers** (NOT the fail-open cold-start gateway).
  - Per-kind semantics (each an AC): `transition` → transition_back_to_todo /
    transition_to_under_review_if_needed per `to_status`; `relabel` → add/remove +
    ride-along clear_assignee on claim-label removal; `mention_operator` → comment +
    `needs-operator-action` label iff `urgency=="high"` (:807); `mark_for_followup` →
    `coord-resume-after:{when}` label + comment; `escalate` → label **+** comment always
    (B2); `noop` → executed:False; unknown → `unsupported`.
  - **Idempotency**: `coord:{kind}:{target}:{content_hash}` (hash = canonical semantic
    params excluding volatile decision_id/timestamps/transient; comment text included)
    + per-REST suffixes `:add:<l>`/`:remove:<l>`/`:clear-assignee`/`:transition`/
    `:comment`/`:label` (one key/PUT-POST; `_request_idempotent` caches by key :331).
  - `file_ticket` NOT auto-executed → `mention_operator` + **two** result lines.
  - Result envelope matches `ShadowActionExecutor`.
- **Deploy AC**: ships in coordinator package; default acting=False (shadow) → no prod
  change until T7. `deployed:` = present in next build.
- **Integration AC**: each kind → correct low-level call w/ correct idem key (mock);
  unknown → `unsupported`.
- **Exercised AC**: per-kind dispatch; escalate label+comment; idem stable across two
  ticks (diff decision_id) → one mutation; reworded comment → second; composite relabel
  (2 labels) → 2 un-deduped PUTs.
- **Go-Live**: shadow-safe; CI.

## T2 — [OP][coord-acting] Pre-mutation safety rechecks (coord-skip + destructive fail-closed)
- **issuetype**: Story · **tier**: M · **area**: `area:backend`, `area:tests` · **blockedBy**: T1
- **labels**: `scope:coord-acting`, `agent:auto` (NO `class:` until T1 公開済み)
- **## Files/Paths**: `backend/agents/pipeline_coordinator.py` (the `# T2` seam in
  `LiveActionExecutor`), `backend/tests/`. **Out of scope**: run_once, env-gate.
- **v4 coverage**: §coord-skip/destructive recheck, N13, condition #2.
- **Code AC**:
  - Before any mutation: `fetch_ticket_labels(client, target)` (jira_dispatch.py:1826);
    exception → `executed:False,reason:"label_read_failed"` (**fail-CLOSED**).
    `coord-skip`/`coord-quarantine` → `coord_skip`.
  - For `transition → To Do` only (destructive — clears assignee jira_dispatch.py:1991):
    strict recheck `get_issue_status` (jira_dispatch.py:1586) ∈ IN_PROGRESS AND
    `not _ticket_has_live_runner(target)` (:986). **`_ticket_has_live_runner` treats
    probe errors as "not live" (:987-990); the wrapper MUST flip that to fail-closed** →
    unknown/failed probe → `executed:False,reason:"context_stale"` (condition #2).
- **Deploy AC**: same package; default acting=False unchanged.
- **Integration AC**: coord-skip → no mutation; status changed / runner now live before
  To Do transition → abstains.
- **Exercised AC**: coord-skip blocks; label read raises → no mutation; destructive
  recheck w/ now-live runner OR probe error → `context_stale`.
- **Go-Live**: shadow-safe.

## T3 — [OP][coord-acting] run_once acting dry_run flip (Option B)
- **issuetype**: Story · **tier**: M · **area**: `area:backend`, `area:tests` · **blockedBy**: T2
- **labels**: `scope:coord-acting`, `agent:auto` (NO `class:` until T2 公開済み)
- **## Files/Paths**: `backend/agents/pipeline_coordinator.py` (`run_once` :1699-1703,
  `_build_tick_record` :1394), `backend/tests/`. **Out of scope**: blast cap (T4), crash
  log (T5), executor internals (T1/T2).
- **v4 coverage**: §dry_run Option B (Q7/condition #1).
- **Code AC**: between `engine.evaluate` and the executor, when `self._acting`, rewrite
  each non-noop action `dataclasses.replace(a, dry_run=False)` (NoopAction skipped;
  Action frozen rules.py:80) AND **rebuild** `result = dataclasses.replace(result,
  actions=actions)` so `_build_tick_record` (:1394) + learning-loop see `dry_run=false`;
  **preserve `decision_id`** (condition #1). Executor mutates iff `dry_run=False`.
- **Deploy AC**: default acting=False → inert in shadow.
- **Integration AC**: acting=True → actions reach executor with dry_run=False AND logged
  tick reads dry_run:false; acting=False → dry_run:true unchanged.
- **Exercised AC**: acting flip rewrites + DecisionResult rebuilt + decision_id preserved
  + logged dry_run:false.
- **Go-Live**: shadow-safe.

## T4 — [OP][coord-acting] run_once blast-radius cap + per-action try/except + kill-switch + fairness
- **issuetype**: Story · **tier**: M · **area**: `area:backend`, `area:tests` · **blockedBy**: T3
- **labels**: `scope:coord-acting`, `agent:auto` (NO `class:` until T3 公開済み)
- **## Files/Paths**: `backend/agents/pipeline_coordinator.py` (`run_once` action loop
  :1703-1709), `backend/tests/`. **Out of scope**: dry_run flip (T3), crash log (T5).
- **v4 coverage**: §Blast-radius (B4/S7), fairness cursor (Q13/Q17).
- **Code AC**:
  - Cap at `OMNISIGHT_COORDINATOR_MAX_ACTIONS_PER_TICK` (default **1**); excess →
    `executed:False,reason:"tick_cap"`. **Per-action try/except** so one failure logs
    `error` and the tick still appends (:1709). Kill-switch
    `OMNISIGHT_COORDINATOR_ACTING_KILL=1` → observe-only without restart.
  - **Fairness cursor** rebuilt from a bounded decision-log tail across all date files
    in the 24h window (NOT a new file — ADR §3.2 / ADR-0021:105), record-count cap (Q17).
- **Deploy AC**: default acting=False; cap default 1.
- **Integration AC**: > cap → capped + logged; raising executor → tick still records;
  kill-switch → observe-only; fairness cursor advances across capped ticks.
- **Exercised AC**: cap; try/except keeps log; kill-switch; fairness cursor.
- **Go-Live**: shadow-safe.

## T5 — [OP][coord-acting] Crash-safe intent log + L6 replay
- **issuetype**: Story · **tier**: M · **area**: `area:backend`, `area:tests` · **blockedBy**: T4
- **labels**: `scope:coord-acting`, `agent:auto` (NO `class:` until T4 公開済み)
- **## Files/Paths**: `backend/agents/pipeline_coordinator.py` (`run_once` intent write,
  `_build_tick_record` :1394, `_resolve_interrupted_actions`/L6 :1514, log scan :1493),
  `backend/tests/test_pipeline_coordinator_coldstart.py:473`. **Out of scope**: dry_run
  flip (T3), blast cap (T4) — depend on both being merged (chain).
- **v4 coverage**: §Crash recovery (S10/Q11/condition #3/Q16).
- **Code AC**:
  - Write a `tick_intent` record BEFORE executing (closes :1703-before-:1708), carrying
    the **final resolved stable idem keys** + `decision_id` (Q16); outcome `decision_tick`
    after, same `decision_id`. Persist `action_results` alongside `actions` (:1394).
  - L6 (:1514; scan :1493) also scans `tick_intent`: a `tick_intent` with no matching
    `decision_tick` → replay under the SAME idem keys (idempotent — store :90 dedupes
    applied subcalls); replay ONLY within the 24h idem TTL (idempotency.py:40); beyond
    it, skip.
- **Deploy AC**: default acting=False → intent records harmless in shadow.
- **Integration AC**: simulated crash (intent, no outcome) → L6 replays missing subcalls
  only, no double-apply; intent >24h → skipped.
- **Exercised AC**: intent-before-execute ordering; replay idempotent; 24h cutoff;
  **update `test_pipeline_coordinator_coldstart.py:473`** (L6 handles tick_intent).
- **Go-Live**: shadow-safe.

## T6 — [OP][coord-acting] env-gate wiring + cold-start agent_class + per-phase caps
- **issuetype**: Story · **tier**: M · **area**: `area:backend`, `area:devops`, `area:tests` · **blockedBy**: T5
- **labels**: `scope:coord-acting`, `agent:auto` (NO `class:` until T5 公開済み)
- **## Files/Paths**: `backend/agents/pipeline_coordinator.py` (`from_env` :149, `main`
  :1936, `_default_cold_start_gateway` :1149), `tests/test_pipeline_coordinator_systemd.py`,
  `backend/tests/test_pipeline_coordinator_coldstart.py`. **Out of scope**: run_once, executor internals.
- **v4 coverage**: §env-gate (S8/D4), S9 agent_class, §Cold-start budget (B6/Q14), capability-scope note.
- **Code AC**:
  - `from_env` (:149) parses `OMNISIGHT_COORDINATOR_ACTING` → new `acting: bool=False`;
    `main` (:1936) builds `LiveActionExecutor` + `build_default_coordinator(acting=True,
    action_executor=...)` when set, else shadow. `self._acting` single source of truth.
  - `_default_cold_start_gateway` (:1149) + `LiveActionExecutor` receive
    `config.jira_agent_class` (S9: gateway default "claude" :621 vs config "subscription-claude" :128).
  - Per-phase cold-start caps (`..._COLD_START_MAX_{INFRA,RECONCILE,SWEEP}`, defaults
    **1/1/1**): Startup-1 :653, Startup-2 :735, Startup-3 :776.
- **Deploy AC**: env var documented; NO behavior change unless set. Note: coordinator
  daemon is NOT capability-matrix gated (daemon env authorization, not runner pickup —
  capability_matrix.yaml:27 is a different actor).
- **Integration AC**: env unset → shadow; set → acting + LiveActionExecutor;
  `acting=True` w/o executor still raises (:1884); per-phase caps enforced.
- **Exercised AC**: env-gate matrix; agent_class threaded (**update
  `test_default_cold_start_gateway_shadow_vs_acting` coldstart:954**); per-phase cap;
  **update `tests/test_pipeline_coordinator_systemd.py:68`**.
- **Go-Live**: shadow-safe (flip is T7).

## T7 — [GATE][coord-acting] Activate acting=True (phased first live flip) 🔒 HUMAN-ONLY
- **issuetype**: Story · **tier**: **X** · **area**: `area:devops`, `area:docs` · **agent**: none (operator)
- **labels**: `scope:coord-acting`, `requires:operator-approval`, `tier:X` (NO `class:`/`agent:auto`)
- **blockedBy**: T6, **OP-1555**
- **v4 coverage**: §Activation, D5 budget coupling, phased rollout.
- **Code/Config AC**: systemd **drop-in** via `systemctl --user edit pipeline-coordinator`
  adding `Environment=OMNISIGHT_COORDINATOR_ACTING=1` (NOT env.conf — inline `Environment=`,
  no EnvironmentFile; pipeline-coordinator.service:29-32, runbook:53); **simultaneously
  remove `OMNISIGHT_COORDINATOR_DAILY_BUDGET_USD=0`** (D5). Confirm `MAX_ACTIONS_PER_TICK=1`.
- **Deploy AC**: phased — first flip in an env with a known-small interrupted set;
  per-phase cold-start caps (1/1/1) not exceeded; daemon-reload + restart.
- **Integration AC**: decision-log shows `executed:True` matching intent; rollback
  verified = `OMNISIGHT_COORDINATOR_ACTING_KILL=1` (no restart) OR revert drop-in + restart.
- **Exercised AC**: a live action observed end-to-end + a deliberate kill-switch rollback;
  update `docs/operations/coordinator-runbook.md` activation+rollback.
- **Go-Live**: this IS the go-live (operator-gated).

---

## Coverage map — every v4 deliverable → ticket (1:1 after split)
| v4 section / item | ticket |
|---|---|
| §LiveActionExecutor dispatch (B1) + per-kind semantics | T1 |
| escalate distinct path (B2) | T1 |
| low-level helpers not gateway (B5) | T1 |
| all kinds + unsupported (N11) | T1 |
| file_ticket two-line accounting | T1 |
| §Idempotency content_hash + suffixes (B3/Q12) | T1 |
| coord-skip fail-closed fetch_ticket_labels (N13) | T2 |
| destructive transition strict recheck (condition #2) | T2 |
| §dry_run Option B + DecisionResult rebuild (Q7/condition #1) | T3 |
| §Blast-radius cap + try/except + kill-switch (B4/S7) | T4 |
| fairness cursor decision-log-derived (Q13/Q17) | T4 |
| §Crash recovery intent-log + replay (S10/Q11/condition #3/Q16) | T5 |
| persist action_results (:1394) | T5 |
| env-gate from_env+main (S8/D4) | T6 |
| agent_class plumb (S9) | T6 |
| per-phase cold-start caps (B6/Q14) | T6 |
| capability-scope clarification | T6 |
| §Activation systemd drop-in + D5 + phased (operator) | T7 |
| update test_l6_resolves_interrupted_actions_by_kind | T5 |
| update test_default_cold_start_gateway_shadow_vs_acting | T6 |
| update tests/test_pipeline_coordinator_systemd.py:68 | T6 |

## Status
Decomposed to minimal execution units (7 impl, each single-concern). Tiers verified
against ADR-0005 (all M — S unattainable for production-code edits; T7=X). Runner-
executability verified (pickup JQL + capability union + filing protocol). Linear chain
T1→…→T7 makes the fine-grained split conflict-free. Coverage 1:1 with v4 + 3 conditions.
Ready to file pending operator OK.
