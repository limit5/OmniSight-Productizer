# Bridge-health graded contract — capability-scoped, NOT global

**Status**: Spec draft 2026-05-14 (Sprint Atlas, OP-1112 / `v2-Ⅹ-4a`)
**Sprint**: Atlas (S12.G G.A-v2 Family ⑩ — Runner Defense Contract)
**Spec parent**: `docs/sprint-s12/sprint-s12g-A-v2-runtime-defense-contract-spec.md` §3 Family ⑩ §4 ticket row 9
**Empirical source**: OP-1067 (C9 bridge-health gate, original SP-B-X-009) → OP-1077 (lived DoS recurrence) + codex Package 2 self-audit §1 Mode 3
**Codifies**: the decision matrix `(bridge state) × (ticket capability set) → action` that replaces today's hard global gate at `auto-runner-jira.py:1759`
**Consumed by**: `v2-Ⅹ-4bc` (OP-1112's implementation companion — refactors `pre_pickup_ok()` / `_bridge_health_pickup_gate` per this matrix)
**Supersedes**: the hard short-circuit `if not _bridge_health_pickup_gate(client): return 0` at `auto-runner-jira.py:1759` (kept as read-only fallback during the strangler window; see §6)

## 1. Why this exists

The C9 gate (OP-1067, shipped as `_bridge_health_pickup_gate` +
`check_bridge_heartbeat` in 2026-04-XX) treats bridge staleness as a
**global** signal: one heartbeat older than
`OMNISIGHT_BRIDGE_STALE_AFTER_SEC` (default 300 s) halts **every**
runner instance from picking up **any** ticket on the next tick.

That fail-closed posture made sense at filing time (one alarm, fleet
fails safe), but two incidents proved it produces a denial-of-service
**larger than the failure it was protecting against**:

| Date | Incident | Symptom | Lived cost |
|---|---|---|---|
| 2026-04-XX | **OP-1067 itself** | A quiet Gerrit period (no `change-merged` events for ~10 min during a low-activity Sunday) staled the heartbeat coupled to event-loop progress. Every runner across the fleet refused pickup. | Cascading idle window until operator manually restarted the bridge; tickets that had **no Gerrit-finalization dependency at all** (pure-docs, pure-research, capability-spec drafting like this very ticket) were blocked anyway. |
| 2026-05-XX | **OP-1077** | Repeat of the OP-1067 pattern after a bridge daemon restart left the heartbeat file unwritable for ~12 minutes. Same fleet-wide blockage. | Codex Package 2 §1 Mode 3 named this as one of three "health gate becomes a DoS" failure modes the runner experiences from the inside. |

**Codex Package 2 verdict (§1 Mode 3, verbatim)**:

> "Health gates that are meant to protect me can block every pickup
> because they depend on the thing they are checking. OP-1067/1077:
> bridge heartbeat coupled to event-loop progress → quiet Gerrit
> periods staled heartbeat → fleet-wide pickup DoS."

The fix is **not** to remove the gate. The bridge is genuinely
load-bearing for the `change-merged → Published` transition (see
`docs/sop/runbooks/bridge-health-degraded.md` §recovery — every
ticket finalized through Gerrit while the bridge is down wedges at
**Approved** waiting for a transition the daemon will never make).
The fix is to make the gate **capability-scoped**: stale-bridge
should only block tickets whose **completion path** requires the
bridge — concretely, tickets that will eventually trigger a
`change-merged` event the bridge must translate.

## 2. The graded model — three bridge states, three capability buckets

### 2.1 Bridge state taxonomy

The current `check_bridge_heartbeat()` returns
`(is_fresh, age_seconds, resolved_path)` — a Boolean. This contract
**generalizes** that to a three-state enum without changing the
underlying mtime check; the third state is derived from the same
age value.

| State | Definition | Today's `is_fresh` value | Detection (no code change required for §3 matrix) |
|---|---|---|---|
| `fresh` | `age_seconds ≤ OMNISIGHT_BRIDGE_STALE_AFTER_SEC` (default ≤ 300 s) | `True` | Same as today. |
| `degraded` | `OMNISIGHT_BRIDGE_STALE_AFTER_SEC < age_seconds ≤ OMNISIGHT_BRIDGE_DEGRADED_AFTER_SEC` (proposed default 900 s = 3× stale threshold) | `False` (today's binary collapses this into `stale`) | Same heartbeat mtime; `v2-Ⅹ-4bc` introduces the second threshold env var. |
| `stale` | `age_seconds > OMNISIGHT_BRIDGE_DEGRADED_AFTER_SEC`, or heartbeat file missing entirely | `False` | Same as today's `False` branch. |

**Why three states, not two**: the matrix in §3 needs to distinguish
**"bridge briefly silent — Gerrit-finalization tickets should wait but
not be refused"** from **"bridge presumed down — Gerrit-finalization
tickets must be refused outright"**. Without the middle state, the
gate continues to be binary and either over-blocks (today's bug) or
under-protects (the naive fix of just lowering the threshold).

The thresholds are tunable per-host via env var; the default 300/900
split is the conservative starting point — `v2-Ⅹ-4bc`'s regression
tests pin both thresholds to fixed values per the §7 test plan, so
operator tuning has no effect on the regression contract.

### 2.2 Ticket capability buckets

Every ticket pickup has a **resolved capability set** by the time
`_bridge_health_pickup_gate` would fire today — the matrix lookup
runs earlier in `_check_pre_pickup_candidate` (`backend/agents/capability_matrix.py`)
and the result is available on the snapshot. This contract reads
that set and routes by it.

For matrix purposes, capabilities partition into **three buckets**:

| Bucket | Definition | Examples (OP-855 vocabulary) | Bridge dependency |
|---|---|---|---|
| **G — Gerrit-finalizing** | The ticket's terminal-success path **emits a `change-merged` event** the bridge must translate to `Published`. | `gerrit_push`, `run_migration`, `deploy_action` | Hard. A `change-merged` event landing while the bridge is stale wedges the ticket at Approved. |
| **R — review-yielding** | The ticket produces work that lands in Gerrit as a change but is not authorized to merge it (typically AI-reviewer-class tickets, or human-approval-pending classes). It still needs the bridge to recognize the eventual merge **at some later time**, but the **pickup-time** bridge state does not gate the work product. | `gerrit_push` paired with the operator-set `runner-batch-merge-candidate` / `runner-glance-required` / `class:subscription-codex-batch-merge` envelope | Soft. Pickup OK; **finalization** state is held until bridge resumes (see §3 row 2 — "hold with review-pending-bridge lease state"). |
| **C — code-only / docs-only / spec / META** | No `gerrit_push` capability at all — terminal-success is a commit + push to Gerrit refs/for/develop for human review (review path is the same as bucket R), **or** an entirely Gerrit-free completion (META roll-ups, operator-window tickets, pure-research with `mcp_search`/`memory_recall` only). | Anything **without** `gerrit_push` (e.g. `code_edit + run_lint + jira_update`); the `read_only_default` `{mcp_search, memory_recall}` set; META tickets with `type:meta` | None at pickup time. A bridge that is down for the entire ticket's lifetime is irrelevant to terminal success. |

> **Important — bucket R vs C boundary**: today the runner does **not**
> have a first-class "pickup_only_no_push" flag; it does have
> `runner:no-commits-expected` (ops-only ticket) and
> `runner-needs-refinement` (excluded from pickup entirely). The
> matrix treats **the absence of `gerrit_push` in the resolved
> capability set** as bucket C; the presence of `gerrit_push` plus a
> bucket-R operator label (`runner-batch-merge-candidate` /
> `runner-glance-required` / `class:subscription-codex-batch-merge`)
> is bucket R; everything else is bucket G.

### 2.3 The third axis we deliberately do NOT add

A natural fourth axis would be **"which capabilities the bridge
recovery itself needs"** — e.g. running the bridge's own AC requires
the bridge being able to **start**, not be **fresh**. We avoid this:
bridge-recovery tickets (OP-1067 lineage, future bridge bugs) are
operator-window class (`class:operator-window`) and the runner does
not pick them up at all. They are filed at `tier:X` per
[[feedback_human_only_tickets_tier_x]] and excluded from the pickup
JQL upstream of this gate. No matrix row is needed for them.

## 3. Decision matrix — bridge state × capability bucket → action

This is the contract `v2-Ⅹ-4bc` implements. Each cell is one of:

- **allow** — pickup proceeds; runner behaves as if the gate were not present.
- **allow + lease tag** — pickup proceeds but the runner records a soft state marker (substrate-side per [[project_runner_substrate_contract]] §5; label-side fallback `runner-bridge-degraded-at-pickup` during the strangler window) so post-push handling knows the finalization may be delayed.
- **allow + hold-finalization** — pickup proceeds, code-side AC completes, push to Gerrit succeeds, but the runner records a **review-pending-bridge** lease state instead of treating the push as the terminal success. The Gerrit change-merged → JIRA Published transition is held by the bridge whenever it returns; the runner does not re-pick the ticket.
- **block + page** — pickup refused this tick; `operator_notifier.notify(Severity.CRITICAL, "bridge_down", ...)` fires (today's behavior, scoped down).
- **block (silent)** — pickup refused this tick; **no** `bridge_down` notification (avoid pager-storm — only bucket-G blockage warrants paging).

| | `fresh` (≤ 300 s) | `degraded` (300 s < age ≤ 900 s) | `stale` (> 900 s or file missing) |
|---|---|---|---|
| **G — Gerrit-finalizing** (`gerrit_push` ∈ caps, no batch-merge envelope) | **allow** | **allow + lease tag** (`runner-bridge-degraded-at-pickup`; post-push handler logs `[runner-bridge-degraded]` comment but does NOT block; finalization may lag) | **block + page** (today's only behavior, narrowed to this cell only) |
| **R — review-yielding** (`gerrit_push` ∈ caps AND any of: `runner-batch-merge-candidate` / `runner-glance-required` / `class:subscription-codex-batch-merge`) | **allow** | **allow + lease tag** (same as G-degraded — code-side completes, post-push records degraded state) | **allow + hold-finalization** (runner records `state='review-pending-bridge'` lease; pickup, code work, and push to refs/for/develop all proceed; finalization waits for bridge return) |
| **C — code-only / docs / spec / META** (no `gerrit_push` in resolved caps) | **allow** | **allow** | **allow** (one warning log line per tick, no pager, no JIRA comment) |

### 3.1 Cell-by-cell rationale

Each cell maps to a concrete OP-1067/OP-1077-style scenario; cells
without an empirical anchor are explicit "no policy change vs today".

- **(C, *)** — *the row that fixes OP-1067/OP-1077*. A bridge-down
  day with five docs-only tickets in the queue (this very ticket
  qualifies — capability set is `{code_edit, gerrit_push, jira_update,
  mcp_search, memory_recall, run_lint}`, no `run_migration` /
  `deploy_action`; but more strictly, see §5 worked example for an
  even cleaner case) should not result in zero progress. Today's
  global gate produces exactly that.
- **(R, stale)** — *the row that introduces `review-pending-bridge`
  lease*. Codex Package 2 §1 Mode 3 close: "allows code-only pickup
  with 'review pending bridge' lease state". Even bucket-R tickets
  (need the bridge eventually) can finish all their non-bridge work
  during a stale window; finalization waits in the substrate, not
  in the pickup gate.
- **(G, fresh) / (R, fresh) / (G, stale)** — *cells that keep
  today's behavior*. The current gate stays correct in these cases;
  this contract is a strict **expansion** of the allow-set, not a
  loosening of the block-set.
- **(G, degraded) / (R, degraded)** — *new "degraded" cells*. Today
  collapses these into the stale row and blocks. New behavior allows
  pickup but tags the lease so the post-push observer (substrate
  per §5 of [[project_runner_substrate_contract]]) can render
  "finalization may lag" in operator dashboards.

### 3.2 What "block (silent)" never becomes

The matrix has no cell that becomes **block (silent)**. Either we
block and page, or we allow. This is deliberate: a silent block is
indistinguishable from a runner idle tick and would have prevented
the OP-1067 retrospective from identifying the bug in the first
place. Every refusal of pickup leaves an observable trace.

## 4. State-marker conventions (bridge-side, runner-side)

Both the **lease tag** (degraded row) and the **hold-finalization
lease state** (R-stale cell) need durable, queryable markers.

### 4.1 Substrate-side (preferred, post-`v2-Ⅹ-2-Cutover`)

After [[project_runner_substrate_contract]] §5 ships, the
`runner_claims` table has a `state` column with values
`active | blocked | paused | released | force-released | expired`.
This spec extends that enum:

| New value | Cell that writes it | Released by |
|---|---|---|
| `review-pending-bridge` | (R, stale) — allow + hold-finalization | The bridge daemon's post-stale recovery: when the next `change-merged` event matching this lease's `external_refs.gerrit_change_id` lands, the bridge updates the row to `state='released'` with `release_reason='success'`. **Idempotent**: a second `change-merged` is a no-op. |

`release_reason` gets one new canonical value: `bridge-recovered-finalized`
(the bridge's post-recovery sweep distinguishes its own auto-released
leases from operator force-releases for forensics).

The **lease tag** for degraded cells does NOT need a new `state` value
— the row stays `state='active'` through the whole pickup, and the
soft signal is carried on `external_refs.bridge_state_at_pickup =
"degraded"` written by `record_phase()` at the `pickup` phase.

### 4.2 Label-side (transitional, pre-Cutover)

Until the substrate cutover lands, the same signals are encoded as
B-class labels (will migrate per [[project_runner_substrate_contract]]
§4):

| Label | Cell that writes it | Cleared by |
|---|---|---|
| `runner-bridge-degraded-at-pickup` | (G/R, degraded) — allow + lease tag | The next runner tick on the same ticket once `bridge_state == 'fresh'`. |
| `runner-review-pending-bridge` | (R, stale) — allow + hold-finalization | The bridge daemon's post-recovery sweep (next `change-merged` event matching the ticket's open change) — same trigger as the substrate variant. |

Both labels are B-class (volatile runtime state), and both follow the
[[feedback_stale_claim_labels]] rule: any path that reverts the
ticket to TODO must strip them. `v2-Ⅹ-4bc` is responsible for that
wiring in the runner FSM.

## 5. Worked example — this very ticket (OP-1112)

To make the matrix concrete, here is the pickup decision for OP-1112
itself under the three bridge states.

**Ticket fingerprint** (from the pickup prompt):
- Capabilities: `{code_edit, gerrit_push, jira_update, mcp_search, memory_recall, run_lint}`
- No batch-merge envelope label
- Areas: `backend, docs`
- Tier: `S`

**Capability bucket determination**:
- `gerrit_push` ∈ caps → not bucket C
- No `runner-batch-merge-candidate` / `runner-glance-required` /
  `class:subscription-codex-batch-merge` → not bucket R
- → **Bucket G** (Gerrit-finalizing)

| Bridge state at pickup | Action (per §3 row 1) | Why |
|---|---|---|
| `fresh` | **allow** | Today's behavior. |
| `degraded` (e.g. 400 s old) | **allow + lease tag** (`runner-bridge-degraded-at-pickup`) | Pickup proceeds; the spec is committed and pushed; post-push handler notes finalization may lag a few minutes. |
| `stale` (e.g. 1200 s old, OP-1077 pattern) | **block + page** | The push at the end of this ticket emits a `change-merged` event the bridge must translate to `Published`. A stale bridge wedges that. Pager fires on this single bucket-G refusal — but every C-bucket ticket in the queue (any pure-research, META, operator-window ticket without `gerrit_push`) is allowed through, breaking the OP-1067/OP-1077 fleet-wide DoS. |

A **bucket-C worked example** that contrasts cleanly: a `type:meta`
roll-up ticket with capability set `{mcp_search, memory_recall}`
(the `read_only_default` of `config/capability_matrix.yaml`) is
allowed under all three bridge states — its terminal success is a
single JIRA comment summarizing children, no Gerrit change at all.
That class of ticket should never have been blocked by the OP-1067
gate in the first place; this matrix corrects that.

## 6. Strangler migration — how `v2-Ⅹ-4bc` lands this without flipping in one shot

Per [[project_runner_substrate_contract]] §7 strangler order, the
implementation companion ticket `v2-Ⅹ-4bc` ships behind a feature
flag for one observation week before the old gate is removed.

1. **Add three-state classifier** to `gerrit_jira_bridge.py`:
   `classify_bridge_state(age_seconds, env) -> Literal['fresh', 'degraded', 'stale']`
   reading both `OMNISIGHT_BRIDGE_STALE_AFTER_SEC` (existing) and
   `OMNISIGHT_BRIDGE_DEGRADED_AFTER_SEC` (new, default 900). The old
   `check_bridge_heartbeat()` becomes a thin wrapper that returns
   `is_fresh = (state == 'fresh')` so today's callers stay green.
2. **Add `bucket_for_ticket(snapshot, resolved_caps) -> Literal['G','R','C']`**
   to `jira_dispatch.py`. Pure function; covered by unit tests with
   12+ fixtures spanning all three buckets and the batch-merge
   envelope edge cases.
3. **Refactor `_bridge_health_pickup_gate` in `auto-runner-jira.py`**:
   - Old signature `(client) -> bool` becomes
     `(client, snapshot, resolved_caps) -> tuple[Literal['allow','allow+tag','allow+hold','block+page','block+silent'], reason]`.
   - The single short-circuit at `auto-runner-jira.py:1759` becomes
     a per-candidate check inside `_check_pre_pickup_candidate`
     (`auto-runner-jira.py:458`) so the matrix runs against the
     candidate's actual capabilities, not at fleet level.
   - Behind `OMNISIGHT_BRIDGE_GRADED_GATE=1` (off by default for
     the first deploy); when off, falls back to today's hard-gate
     behavior at the same call site.
4. **Observation week**: graded gate on **codex-class only** runners
   to bound blast radius. Daily comparison metric:
   - `runner_bridge_pickup_outcomes_total{bucket, bridge_state, action}` —
     count of every decision the matrix made, by (bucket × state × action).
   - Manual review at end of week confirms the action distribution
     matches the §3 matrix; any drift indicates a bug in the bucket
     classifier or the bridge-state classifier.
5. **Cutover**: flip `OMNISIGHT_BRIDGE_GRADED_GATE` default to `1` on
   all runner classes; remove the `if not _bridge_health_pickup_gate(client): return 0`
   short-circuit at `auto-runner-jira.py:1759`. The graded gate is
   the only path.
6. **Read-only fallback retained 1 sprint** — the env var stays
   wired so an operator can revert to hard-gate behavior if the
   matrix has a class of bug we did not anticipate.

## 7. Regression test contract for `v2-Ⅹ-4bc`

These tests **must exist** as part of `v2-Ⅹ-4bc`; this spec is the
contract they assert.

### 7.1 OP-1067 / OP-1077 regression — explicit

| Test name | Setup | Assertion |
|---|---|---|
| `test_bucket_c_pickup_allowed_under_stale_bridge` | Bridge heartbeat 1200 s old; ticket fixture is META with caps `{mcp_search, memory_recall}` | Matrix returns `('allow', _)`; no `operator_notifier.notify` call; no `bridge_down` log line |
| `test_bucket_c_pickup_allowed_under_degraded_bridge` | Bridge heartbeat 400 s old; same fixture | Matrix returns `('allow', _)`; no label write |
| `test_bucket_g_pickup_blocks_paged_under_stale_bridge` | Bridge heartbeat 1200 s old; ticket fixture has `gerrit_push` and no batch-merge envelope | Matrix returns `('block+page', _)`; `operator_notifier.notify(Severity.CRITICAL, 'bridge_down', ...)` called exactly once |
| `test_bucket_g_pickup_lease_tagged_under_degraded_bridge` | Bridge heartbeat 400 s old; same G fixture | Matrix returns `('allow+tag', _)`; `runner-bridge-degraded-at-pickup` label written (substrate variant: `external_refs.bridge_state_at_pickup='degraded'`) |
| `test_bucket_r_pickup_hold_finalization_under_stale_bridge` | Bridge heartbeat 1200 s old; ticket fixture has `gerrit_push` AND `class:subscription-codex-batch-merge` | Matrix returns `('allow+hold', _)`; lease state is `review-pending-bridge` (substrate) or `runner-review-pending-bridge` label (transitional) |

### 7.2 Bridge-state classifier — explicit

| Test name | Setup | Assertion |
|---|---|---|
| `test_classify_bridge_state_fresh_below_threshold` | age=200, stale=300, degraded=900 | `'fresh'` |
| `test_classify_bridge_state_degraded_between_thresholds` | age=400, stale=300, degraded=900 | `'degraded'` |
| `test_classify_bridge_state_stale_above_degraded_threshold` | age=1200, stale=300, degraded=900 | `'stale'` |
| `test_classify_bridge_state_missing_file_is_stale` | heartbeat file does not exist | `'stale'` (never `'degraded'` — missing file is never recoverable on its own) |
| `test_classify_bridge_state_degraded_collapses_when_only_stale_env_set` | `OMNISIGHT_BRIDGE_DEGRADED_AFTER_SEC` unset; age=400, stale=300 | `'stale'` (back-compat: env-var-unset behavior matches today's binary classifier) |

### 7.3 Bucket classifier — explicit

| Test name | Setup | Assertion |
|---|---|---|
| `test_bucket_g_default_capabilities` | caps include `gerrit_push`, no batch-merge label | `'G'` |
| `test_bucket_r_batch_merge_candidate_label` | caps include `gerrit_push`, label `runner-batch-merge-candidate` | `'R'` |
| `test_bucket_r_glance_required_label` | caps include `gerrit_push`, label `runner-glance-required` | `'R'` |
| `test_bucket_r_subscription_codex_batch_merge_class` | caps include `gerrit_push`, label `class:subscription-codex-batch-merge` | `'R'` |
| `test_bucket_c_no_gerrit_push` | caps `{code_edit, run_lint, jira_update}` | `'C'` |
| `test_bucket_c_readonly_default` | caps `{mcp_search, memory_recall}` (`read_only_default`) | `'C'` |
| `test_bucket_c_type_meta_no_gerrit_push` | `type:meta` label, caps lack `gerrit_push` | `'C'` |

### 7.4 Feature-flag fallback — explicit

| Test name | Setup | Assertion |
|---|---|---|
| `test_graded_gate_disabled_falls_back_to_hard_gate` | `OMNISIGHT_BRIDGE_GRADED_GATE=0`; bridge stale; bucket C ticket | Runner short-circuits at the same call site as today, returns 0; no matrix consulted |
| `test_graded_gate_enabled_uses_matrix` | `OMNISIGHT_BRIDGE_GRADED_GATE=1`; bridge stale; bucket C ticket | Matrix consulted; pickup proceeds |

## 8. Defense contract (D2) for the graded gate itself

Per G.A-v2 Q7 — every v2-* substrate ticket dog-foods the
`defense_contract` schema. The graded gate is a **D2 — exception
handling** contract; it does not own its own metrics surface (the
metrics live in the substrate per [[project_runner_substrate_contract]]
§8) but does declare exception/recovery posture.

```yaml
defense_contract:
  exception_handling:
    enumerated_decisions: [allow, allow_tag, allow_hold, block_page, block_silent_unused]
    remediation_hint_contract: "every block_page emits operator_notifier with the heartbeat path, age_sec, and the resolved bucket; operator can pass straight to `docs/sop/runbooks/bridge-health-degraded.md` triage"
    user_facing: false
    fail_loudly: true

  recovery_path:
    trigger_condition: "bridge state transitions stale → fresh"
    steps:
      - "next runner tick re-evaluates matrix; bucket-G blocked tickets become allowed at next pickup attempt"
      - "outstanding review-pending-bridge leases (bucket R, stale-window pickups) get released by the bridge daemon's post-recovery `change-merged` sweep — no runner action required"
      - "degraded-at-pickup lease tags are cleared by the next pickup of the same ticket once bridge is fresh"
    idempotent: true
    rto_seconds_p99: 60                # one runner tick interval
    evidence_file: tests/test_runner_bridge_health_gate.py + v2-Ⅹ-4bc's additions

  error_detection:
    signal: prometheus_metric
    channel: runner_bridge_pickup_outcomes_total{bucket, bridge_state, action}
    detection_latency_p99: 60s
    observability_test: v2-Ⅹ-4bc adds backend/tests/test_runner_bridge_health_gate_metrics.py
    alert_rule_id: BridgeDegradedFinalizationLag (90% of bucket-G/R picks with bridge_state='degraded' for 3 consecutive 5-min windows → warn-level only, not page; pager already fires on bucket-G/stale)
```

## 9. Out-of-scope (deferred or covered by sibling specs)

- **Bridge auto-restart** — explicitly NOT in scope. Per the
  existing runbook §"Non-recovery: do NOT auto-recover" the
  bridge is operator-only to recover. This contract preserves
  that posture.
- **Multi-bridge / standby bridge** — a redundancy story (warm
  standby on a second host) would moot most of this contract.
  Filed conceptually under Family ⑩'s long-tail but not part of
  G.A-v2 scope.
- **Runtime artifact externalization** — `.runner-cwd-sentinel`,
  `progress.txt`, and friends are covered by
  [[project_runner_substrate_contract]] §4.2 + `v2-Ⅹ-3`; this
  contract neither writes nor reads them.
- **Bridge-recovery ticket itself** — operator-window class,
  `tier:X`, never picked up by the runner, no matrix row needed
  per §2.3.

## 10. Open questions / operator sign-off needed

None block this spec. Items for `v2-Ⅹ-ADR` (ADR-0037) close:

1. **Degraded threshold default**: 900 s (3× stale) is a
   conservative starting point. After 1 month of
   `runner_bridge_pickup_outcomes_total` data, operator should
   review the (bucket × state) distribution and tune. Initial
   default lives in `gerrit_jira_bridge.py` constants.
2. **Bucket R envelope expansion**: today's R bucket triggers
   only on three operator labels
   (`runner-batch-merge-candidate` / `runner-glance-required` /
   `class:subscription-codex-batch-merge`). When `v2-Ⅹ-5a`
   capability registry lands, the bucket-R predicate moves to a
   typed `review_yielding: bool` field; until then the label list
   is authoritative. Sign-off needed before the registry refactor:
   confirm the three-label list is complete (no missing envelope).
3. **Pager dedup horizon**: `block+page` fires
   `operator_notifier.notify(Severity.CRITICAL, 'bridge_down', ...)`
   on **every** bucket-G refusal — with N hosts and M bucket-G
   tickets in the queue, that is N×M pages per bridge-down
   minute. `operator_notifier` already does dedup but the
   horizon is not pinned in this spec. Confirm dedup is already
   ≤ 1 page per `bridge_down` per host per 5 min before the
   graded gate's first soak.

## 11. References

- **Spec parent**: `docs/sprint-s12/sprint-s12g-A-v2-runtime-defense-contract-spec.md` §3 Family ⑩ §4 row 9 (this ticket's row); row 10 is the `v2-Ⅹ-4bc` implementation companion that consumes this contract.
- **Sibling spec**: `docs/sop/runner-substrate-contract.md` (`v2-Ⅹ-1a` / OP-1104) — the coordination substrate this contract reads via the `runner_claims.state` enum extension (`review-pending-bridge`).
- **Incident sources**: OP-1067 (the original C9 gate filing — SP-B-X-009), OP-1077 (the lived-DoS recurrence that motivated this graded reframe), codex Package 2 self-audit `docs/audit/codex-reviews/runner-self-audit-and-redesign-2026-05-14.txt` §1 Mode 3.
- **Today's hard gate** (being replaced): `auto-runner-jira.py:1759` (`_bridge_health_pickup_gate` call site) + `_bridge_health_pickup_gate` function at `auto-runner-jira.py:399`.
- **Today's heartbeat primitive** (unchanged): `backend/agents/gerrit_jira_bridge.py:165` `check_bridge_heartbeat()` + the `OMNISIGHT_BRIDGE_HEARTBEAT_PATH` / `OMNISIGHT_BRIDGE_STALE_AFTER_SEC` env vars.
- **Today's tests** (extended by `v2-Ⅹ-4bc`): `tests/test_runner_bridge_health_gate.py`.
- **Capability matrix** (resolved-caps source): `backend/agents/capability_matrix.py` + `config/capability_matrix.yaml` (with `read_only_default: {mcp_search, memory_recall}` as the bucket-C anchor).
- **Operator runbook** (unchanged, referenced from the `block+page` reason string): `docs/sop/runbooks/bridge-health-degraded.md`.
- **ADR antecedents**: ADR-0033 (L1/L2/L3 authority gates), ADR-0035 (runner FSM + error handling).
