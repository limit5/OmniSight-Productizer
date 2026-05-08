# Multi-Provider Setup

Operator-facing setup guide for the Multi-Provider Subscription Orchestrator
([ADR-0007](../adr/0007-multi-provider-subscription-orchestrator.md)).

This file is being built out across Priority MP, Week 4. Sections below
either link to where the content currently lives or are reserved as
in-progress stubs for the wave that owns them. **MP.W13.3 owns the
Gemini section**; the rest will be filled in by MP.W14.3 (xAI) and
MP.W16 (operator setup, expiry monitoring, cap-hit recovery, cost
calibration).

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

> Reserved for **MP.W16.3**. Will document operator triage when a 5h /
> weekly cap is hit and `routing_policy.py` is out of acceptable
> providers.

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
