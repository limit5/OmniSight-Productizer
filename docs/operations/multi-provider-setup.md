# Multi-Provider Setup

Operator-facing setup guide for the Multi-Provider Subscription Orchestrator
([ADR-0007](../adr/0007-multi-provider-subscription-orchestrator.md)).

This file is being built out across Priority MP, Week 4. Sections below
either link to where the content currently lives or are reserved as
in-progress stubs for the wave that owns them. **MP.W13.3 owns the
Gemini section** and **MP.W16.3 owns the Cap-hit recovery runbook**;
the rest will be filled in by MP.W14.3 (xAI) and the remaining MP.W16
sub-waves (operator setup, expiry monitoring, cost calibration).

---

## Provider status snapshot

| Provider | Tier(s)                   | Status in this milestone | Owning wave |
| -------- | ------------------------- | ------------------------ | ----------- |
| Anthropic Claude Code | Pro / Max 5x / Max 20x | **First-class (v0.4.0 MVP)**         | MP.W3 / MP.W16.1 |
| OpenAI Codex          | Plus / Pro / Business  | **First-class (v0.4.0 MVP)**         | MP.W3 / MP.W16.1 |
| Google Gemini         | Advanced / Code Assist | **Structural slot** — adapter shell present, dispatch raises `NotImplementedError`. UI sphere grayed-out with "Coming v0.5.0" tooltip. | MP.W13 (this section) |
| xAI Grok              | SuperGrok              | **Structural slot** — adapter shell, "Coming v0.6.0" tooltip. | MP.W14 |

The capability matrix that drives routing decisions lives in
[ADR-0007 § Vendor capability matrix](../adr/0007-multi-provider-subscription-orchestrator.md#vendor-capability-matrix-for-routing-policy).
That ADR is the single source of truth — this doc only covers
operational steps.

---

## Operator setup (Anthropic + OpenAI MVP)

> Reserved for **MP.W16.1**. Will cover Claude Pro/Max account creation,
> `claude` CLI auth, OpenAI Codex Plus/Pro auth via `codex` CLI, and the
> per-host credential locations the orchestrator reads.

## Subscription expiry monitoring

> Reserved for **MP.W16.2**. Will document `subscription_account_monitor.py`
> + the operator alert path before a Pro/Max plan lapses (R-MP.3 in
> ADR-0007).

## Cap-hit recovery runbook

This is the **MP.W16.3 deliverable**: operator triage when a 5h-rolling
or weekly cap is hit and `routing_policy.py` returns no acceptable
providers.

`record_usage()` in `backend/agents/provider_quota_tracker.py` opens the
provider's circuit (`circuit_state = 'open'`) the first time rolling
usage crosses the configured cap, writes an `audit_log` row with
`action = 'provider_quota_cap_hit'`, and emits an SSE quota-update
frame with `reason = 'cap_hit'`. From that moment until either the
rolling window naturally drains or an operator runs the reset path
below, `RoutingPolicy.choose_provider()` will skip the provider and —
if every other vendor is also out — return an empty list to the
caller. There is no exception raised; an empty list is the only signal
that no provider is currently acceptable.

### When this runbook applies

Run this runbook when **any** of the following is true:

- A dispatch attempt logs `RoutingPolicy.choose_provider()` returning
  an empty list and the orchestrator's `subscription_account_monitor`
  alert (MP.W16.2) shows all subscription accounts as `active` (i.e.
  this is a quota issue, not an account-expiry issue).
- The Provider Constellation UI shows every sphere in red (`<30 %`
  remaining) or gray (no acceptable provider) at the same time.
- The `audit_log` table has a fresh `provider_quota_cap_hit` row that
  matches the provider you expected the next task to dispatch on, and
  no fallback provider absorbed the queue.

If only one provider is capped and another is still green/yellow, the
orchestrator is doing its job — the task-boundary switch will route
the next task to the healthy provider with no operator action needed.
This runbook is for the all-providers-exhausted case.

### Detection signals — what to look at first

| Signal | Where | What "cap hit" looks like |
| ------ | ----- | ------------------------- |
| Audit row | `audit_log` (PG, control plane) | `action = 'provider_quota_cap_hit'`, `entity_kind = 'provider_quota'`, `entity_id = '<provider>-subscription'`, `after_json.circuit_state = 'open'` |
| Quota row | `provider_quota_state` (PG, alembic 0199) | `circuit_state = 'open'`, non-NULL `last_cap_hit_at`, `rolling_5h_tokens` ≥ configured cap |
| In-memory suppression | `routing_policy._recently_capped` (per-process) | provider id present with `until_ts` in the future; default suppression `DEFAULT_CAP_SUPPRESSION_S = 5 h`, or vendor `retry_after_s` if the adapter returned one |
| Adapter return shape | `dispatch()` return value (Anthropic / OpenAI subscription adapters) | Anthropic: `{kind: 'cap_exceeded', retry_after_s: …}` (HTTP 429 + `usage_exceeded`); OpenAI: `{kind: 'rate_limit_exceeded', reset_at_ts: …}` (HTTP 429 + `rate_limit_exceeded`) |
| SSE frame | quota-update channel (frontend Provider Constellation) | `reason = 'cap_hit'`, `scopes` containing `'5h'` and/or `'weekly'` |

The audit row is the authoritative single record per cap event. The
SQL row in `provider_quota_state` is the live state the routing layer
reads on every dispatch.

### Step 1 — Identify provider, scope, and timestamp

Read the most recent cap-hit event from the audit log:

```sql
SELECT ts, entity_id, after_json
FROM audit_log
WHERE action = 'provider_quota_cap_hit'
ORDER BY ts DESC
LIMIT 5;
```

The `after_json` column carries `rolling_5h_tokens`, `weekly_tokens`,
`last_cap_hit_at`, and `circuit_state` at the moment the cap tripped.
Compare against the live row:

```sql
SELECT provider, rolling_5h_tokens, weekly_tokens,
       last_reset_at, last_cap_hit_at, circuit_state
FROM provider_quota_state
WHERE circuit_state = 'open';
```

You will use the (provider, scope) pair from this step in every
subsequent step. `scope` is `'5h'` if `rolling_5h_tokens` ≥ the
provider's 5h cap, `'weekly'` if `weekly_tokens` ≥ the weekly cap, or
both. Both caps default to the constants in
`backend/agents/provider_quota_tracker.py` (`DEFAULT_5H_CAP_TOKENS`,
`DEFAULT_WEEKLY_CAP_TOKENS`) unless `OMNISIGHT_PROVIDER_CAP_<NAME>_5H`
is set in the environment.

### Step 2 — Decide between **wait** and **operator override**

In the absence of operator action, the system self-heals:

- The 5h rolling window drains by SQL — once events older than 5 h
  age out of `provider_usage_event`, the next `record_usage()` call
  re-evaluates and `circuit_state` will return to `'closed'` on its
  own. Same shape for the weekly window over 7 days.
- The in-memory `_recently_capped` suppression decays after
  `DEFAULT_CAP_SUPPRESSION_S` (5 h) or earlier if the adapter
  surfaced a vendor `retry_after_s`.

**Default to wait.** Manual override should only be used when the
audit trail proves the cap was tripped by faulty accounting (e.g.
duplicate event ingestion, a runaway test run, or a per-tenant
calibration drift logged by `cost_estimator.py`) — not because the
operator wants to keep dispatching past the vendor's actual limit.
Resetting the window when the vendor still considers the account
capped will trip the cap again on the very next dispatch and, on
some plans, escalate to a longer hard block.

Reset only when **all** of these hold:

- The audit row's tokens-at-cap value is implausible compared to the
  task volume the operator actually ran in the window (clear
  accounting error, not real consumption).
- A direct check against the vendor's own dashboard / billing UI shows
  the subscription is **not** rate-limited from the vendor side.
- No human-assigned task is currently in-flight on the affected
  provider (Tier X dispatch — see `routing_policy.py`
  `_human_assignment_resolver`).

### Step 3 — Operator override (only after Step 2 says reset)

For the affected (provider, scope) pair, call:

```python
from backend.agents.provider_quota_tracker import reset_window
from backend.agents.routing_policy import on_cap_hit, _recently_capped

# Clear the SQL window and close the circuit.
reset_window("anthropic-subscription", "5h")     # or "weekly"

# Drop the in-memory suppression so RoutingPolicy stops skipping the
# provider before the natural 5 h timer elapses.
with __import__("backend.agents.routing_policy", fromlist=["_RECENTLY_CAPPED_LOCK"])._RECENTLY_CAPPED_LOCK:
    _recently_capped.pop("anthropic-subscription", None)
```

`reset_window()` (`backend/agents/provider_quota_tracker.py`):

- `DELETE`s `provider_usage_event` rows for the provider in the chosen
  rolling window.
- `UPDATE`s `provider_quota_state` to set `circuit_state = 'closed'`,
  `last_cap_hit_at = NULL`, refreshes `last_reset_at`.
- Emits a quota-update frame with `reason = 'window_reset'` so the
  frontend sphere flips back to green/yellow on the next SSE tick.

Note that **only one uvicorn worker's `_recently_capped` dict can be
reached from one Python session**, because the dict is process-local.
For a clustered deployment, prefer relying on the natural decay
(`DEFAULT_CAP_SUPPRESSION_S = 5 h`) rather than trying to clear every
worker's dict. The SQL `circuit_state` is shared across workers and
is the load-bearing piece; the in-memory dict is a per-process
optimisation that drops a provider from rotation faster than the next
SQL refresh — it does not gate the underlying state.

### Step 4 — Verify the provider is back in rotation

Re-read live state and confirm the routing layer accepts the provider
again:

```sql
SELECT provider, circuit_state, last_cap_hit_at, last_reset_at,
       rolling_5h_tokens, weekly_tokens
FROM provider_quota_state
WHERE provider = 'anthropic-subscription';
```

Expected: `circuit_state = 'closed'`, `last_cap_hit_at IS NULL`,
`rolling_5h_tokens` (and/or `weekly_tokens`) reduced by the events
just deleted.

Then run a synthetic dispatch — pick a small task with the
`agent_class` matching the provider family (`subscription-claude` for
Anthropic, `subscription-codex` for OpenAI; see
`ROUTING_POLICY_PROVIDER_AGENT_CLASS_LABELS` in `routing_policy.py`)
and confirm `RoutingPolicy.choose_provider()` returns a non-empty list
with the recovered provider in it. The Provider Constellation sphere
should flip back to its non-red colour on the next SSE tick.

### Step 5 — All providers exhausted at once

If Step 1 shows **every** provider with `circuit_state = 'open'`, the
orchestrator has nowhere to send work and `choose_provider()` returns
`[]` for every task. In that case:

1. Do **not** reset all providers at once just to clear the queue —
   that risks a same-second re-cap loop on whichever provider is
   actually closest to the vendor-side limit.
2. Pause the runner-driven dispatch (stop new task pickup) until at
   least one provider has a believable wait-out path. The simplest
   pause is to flip `OMNISIGHT_MP_ENABLED=0` — `is_enabled()` in
   `routing_policy.py` short-circuits `choose_provider()` to `[]`
   when the flag is off, and the runner-side dispatch loop treats
   that as "park the task".
3. Pick the provider whose `last_cap_hit_at` is oldest and whose 5h
   window is closest to draining naturally; let it self-heal first.
4. Re-enable `OMNISIGHT_MP_ENABLED` once at least one provider's row
   has flipped back to `circuit_state = 'closed'`.

### Step 6 — Record the incident

After recovery:

- If the cap-hit was anticipated (real consumption that crossed the
  vendor's published limit), no further action — the audit row plus
  the quota-update SSE frame are the durable record.
- If the cap-hit was **unexpected** (accounting drift, duplicate
  events, vendor-side outage masquerading as a cap, an unintended
  runaway dispatch), append a one-entry note to
  `docs/sop/lessons-learned.md` per
  [docs/sop/jira-ticket-conventions.md §14](../sop/jira-ticket-conventions.md)
  with Situation / Fix / Verification — vague "be more careful"
  entries are auto-rejected.
- If the cap was tripped by a `cost_estimator.py` prediction error
  (estimated tokens diverged from actual by more than 50 %), this is
  R-MP.2 territory — flag in the lesson and link to the per-tenant
  calibration story (MP.W16.4 stub).

### Reference — files and tests

- `backend/agents/provider_quota_tracker.py` — `record_usage`,
  `get_quota_state`, `is_at_cap`, `reset_window`, audit emit.
- `backend/agents/routing_policy.py` — `RoutingPolicy.choose_provider`,
  `on_cap_hit`, `_recently_capped` suppression dict,
  `DEFAULT_CAP_SUPPRESSION_S`, `is_enabled` / `MP_ENABLED_ENV`.
- `backend/agents/provider_adapters/anthropic_subscription.py`,
  `openai_subscription.py` — vendor-specific cap signal parsing
  (HTTP 429, `usage_exceeded` / `rate_limit_exceeded`,
  `retry_after_s` / `reset_at_ts`).
- `backend/alembic/versions/0199_provider_quota_state.py`,
  `0200_provider_usage_event.py` — schema for the two tables this
  runbook touches.
- `backend/tests/test_provider_quota_tracker.py` — exercises
  `record_usage` → cap-hit transition and the `audit_log` row
  shape.
- `backend/tests/test_provider_orchestrator.py` —
  `test_routing_on_cap_hit_suppresses_provider_until_retry_after`,
  `test_routing_on_cap_hit_uses_default_suppression_window`,
  `test_cap_hit_retry_boundary_switches_from_anthropic_to_openai`
  cover the suppression-window and task-boundary switch contracts
  this runbook relies on.

## Cost-estimator calibration

> Reserved for **MP.W16.4**. Will document per-tenant calibration of the
> token-prediction model (R-MP.2), including the >50 % divergence
> warning surfaced by `cost_estimator.py`.

---

## v0.5.0 enablement path — Gemini

This section is the **MP.W13.3 deliverable**: the runbook an operator
will follow when v0.5.0 lands and Google Gemini graduates from
*structural slot* to *first-class provider*. Until then, Gemini's
`provider_adapter` raises `NotImplementedError` from every callable
surface — running through this list is what flips it on.

### Pre-conditions for starting the upgrade

Do not begin until **all** of the following are true:

- v0.5.0 release branch has been cut from `develop`
  (per [ADR-0001](../adr/0001-five-branch-gitflow.md)).
- ADR-0007 has an addendum (or successor ADR) that promotes Gemini's
  row in the Vendor capability matrix from "placeholder" to "✅" and
  records the dispatch / cap-signal contract Google ships with their
  CLI at that point.
- A `google-cli`-equivalent agentic CLI exists and has been validated
  end-to-end on at least one OmniSight epic in a sandbox tenant
  (the v0.4.0 ADR called Gemini's CLI "more chat-mode" — that is the
  blocker we are waiting on).
- Operator has a Gemini Advanced *or* Code Assist subscription on the
  account that owns the OmniSight `git_accounts` row for Google
  (see [Backend credentials model](../adr/0003-gerrit-code-review.md)
  context — credentials live in PG, not `.env`).

### Step 1 — Promote `agent_class` schema

`config/agent_class_schema.yaml` is the canonical machine-readable list
that drives TODO labels, RPG `class` field (ADR-0008), routing, and the
cost-estimator. It does **not** currently contain `subscription-gemini` /
`api-gemini`, even though `routing_policy.py` line 310-311 already
gates `gemini-subscription` to those exact strings.

When v0.5.0 lands:

1. Add to `allowed_values`:
   - `subscription-gemini`
   - `api-gemini`
2. Add matching `values:` entries with `provider_family: google`,
   `access_mode: subscription` / `api`, and the runner binary that
   ships with v0.5.0.
3. Bump `metadata.updated_at` and reference the v0.5.0 ticket.
4. Update **both** ADR-0007 (capability matrix row) and ADR-0008 (RPG
   class table) prose in the same change — drift between schema and
   ADRs is what MP.W0 explicitly forbids.

### Step 2 — Replace the adapter `NotImplementedError` shells

In `backend/agents/provider_adapters/gemini_subscription.py`:

- `dispatch(task)` — wire to the v0.5.0 Gemini agentic CLI.
- `health_check()` — return the live `HealthStatus` Google's API
  exposes (auth status + recent error rate).
- `get_quota_state()` — return a real `QuotaState`. Note the matrix
  in ADR-0007 lists Gemini as **per-query rate, no rolling cap**, so
  the QuotaTracker schema (`provider_quota_state` table, alembic
  0198) needs either:
  - a graceful "no rolling window" path that yields `remaining_5h` /
    `remaining_weekly` ratios of `1.0` (Gemini never trips a cap),
    **or**
  - a new alembic migration that adds a Gemini-shaped quota
    representation. Pick one in the v0.5.0 ADR addendum and
    document the choice here.

The `register_adapter(...)` call at module bottom does **not** change —
the adapter is already structurally registered.

### Step 3 — Un-gate the frontend

`MP.W13.2` shipped a grayed-out sphere with a "Coming v0.5.0" tooltip.
To flip it on:

1. Remove the disabled / "Coming v0.5.0" branch in the Provider
   Constellation render path
   (`components/omnisight/multi-provider-orchestrator/ProviderConstellation.tsx`
   and friends).
2. The provider already exists as `google` in `lib/providers.ts` — no
   add is needed there for this step.
3. Confirm the sphere now picks up live SSE quota frames once
   `provider_quota_tracker.py` starts publishing Gemini state.

### Step 4 — Cost-estimator seeding

`backend/agents/cost_estimator.py` carries baseline rates for the MVP
providers. Add a Gemini entry:

- Subscription rate: `$0` within the operator's plan allowance.
- API spillover rate: Google's then-current `$/M tokens` (read off
  Google's pricing page at v0.5.0 cut, do not memorise the number
  here — it will go stale).
- Per-`agent_class` wall-time prediction: seed from the sandbox
  validation epics required by the pre-conditions above.

The estimator's per-tenant calibration (R-MP.2) takes over after the
first ~5 dispatches; the seed only has to be *plausible*, not
*correct*.

### Step 5 — Drift guards

The drift tests landed by MP.W15 enforce these invariants — re-run them
after the changes above and expect them to **fail** until each lands:

- `MP.W15.1` — `lib/providers.ts` 4-vendor list ⊆
  `provider_orchestrator` registry. Gemini already passes today
  because the placeholder adapter is registered at import time.
- `MP.W15.2` — ADR-0007 capability matrix labels ⊆ `routing_policy.py`
  consumed labels. Will trip if Step 1 changes `agent_class` strings
  but the ADR is not updated in the same commit.
- `MP.W15.3` — cap-hit integration test (Anthropic → OpenAI fallback).
  Add an analogous case where Gemini takes over from a capped
  Anthropic+OpenAI pair.

### Step 6 — Smoke test before tagging v0.5.0

1. Dispatch one task with `agent_class: subscription-gemini` and
   confirm `dispatch()` actually executes (no NotImplementedError in
   logs).
2. Force a 429 from each of Anthropic and OpenAI and confirm the task
   migrates to Gemini at the **task boundary** (mid-task switching is
   intentionally not supported — see ADR-0007 § Negative consequences).
3. Confirm the Provider Constellation sphere renders in `healthy`
   state and the tooltip no longer reads "Coming v0.5.0".
4. Confirm `provider_quota_state` PG table has a row for
   `provider = 'gemini-subscription'` with non-stub values.

### Step 7 — Documentation

After the above lands:

- Move the Gemini row in the §Provider status snapshot table from
  "Structural slot" → "First-class (v0.5.0)".
- Update [ADR-0007 § Vendor capability matrix](../adr/0007-multi-provider-subscription-orchestrator.md#vendor-capability-matrix-for-routing-policy)
  to flip the Gemini "MVP?" cell.
- Append a `lessons-learned.md` entry per
  [docs/sop/jira-ticket-conventions.md §14](../sop/jira-ticket-conventions.md)
  if anything in the upgrade surprised the operator (cap-signal shape,
  CLI quirks, etc.).

---

## v0.6.0 enablement path — xAI Grok

> Reserved for **MP.W14.3**. Will mirror the v0.5.0 Gemini section
> above, with the Grok-specific differences (cap signal shape is
> currently undocumented in ADR-0007, so the v0.6.0 ADR addendum will
> need to nail that down before this section is drafted).

---

## Related

- [ADR-0007 — Multi-Provider Subscription Orchestrator](../adr/0007-multi-provider-subscription-orchestrator.md)
- [ADR-0008 — Agent RPG class & skill leveling](../adr/0008-agent-rpg-class-skill-leveling.md)
  (shares the `agent_class` schema)
- [`config/agent_class_schema.yaml`](../../config/agent_class_schema.yaml)
  — single source of truth for agent_class values
- [`backend/agents/provider_adapters/`](../../backend/agents/provider_adapters/)
  — adapter shells (Gemini / xAI raise `NotImplementedError` until the
  enablement path above runs)
- [`backend/agents/routing_policy.py`](../../backend/agents/routing_policy.py)
  — `_agent_class_allows_provider` already gates Gemini routing
