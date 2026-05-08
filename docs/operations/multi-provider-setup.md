# Multi-Provider Setup

Operator-facing setup guide for the Multi-Provider Subscription Orchestrator
([ADR-0007](/docs/adr/ADR-0007-multi-provider-subscription-orchestrator/)).

This file is being built out across Priority MP, Week 4. Sections below
either link to where the content currently lives or are reserved as
in-progress stubs for the wave that owns them. **MP.W13.3 owns the
Gemini section**, **MP.W14.3 owns the xAI section**, **MP.W16.1 owns
the operator setup section**, and **MP.W16.3 owns the Cap-hit recovery
runbook**; the remaining MP.W16 sub-waves own expiry monitoring and
cost calibration.

---

## Provider status snapshot

| Provider | Tier(s)                   | Status in this milestone | Owning wave |
| -------- | ------------------------- | ------------------------ | ----------- |
| Anthropic Claude Code | Pro / Max 5x / Max 20x | **First-class (v0.4.0 MVP)**         | MP.W3 / MP.W16.1 |
| OpenAI Codex          | Plus / Pro / Business  | **First-class (v0.4.0 MVP)**         | MP.W3 / MP.W16.1 |
| Google Gemini         | Advanced / Code Assist | **Structural slot** — adapter shell present, dispatch raises `NotImplementedError`. UI sphere grayed-out with "Coming v0.5.0" tooltip. | MP.W13 (this section) |
| xAI Grok              | SuperGrok              | **Structural slot** — adapter shell present, dispatch returns `{kind: 'not_ready'}`. UI sphere grayed-out with "Coming v0.6.0" tooltip. | MP.W14 (this section) |

The capability matrix that drives routing decisions lives in
[ADR-0007 § Vendor capability matrix](/docs/adr/ADR-0007-multi-provider-subscription-orchestrator/#vendor-capability-matrix-for-routing-policy).
That ADR is the single source of truth — this doc only covers
operational steps.

---

## Operator setup (Anthropic + OpenAI MVP)

This is the **MP.W16.1 deliverable**: the end-to-end checklist an
operator runs once per host before flipping `OMNISIGHT_MP_ENABLED=1`,
to bring up the two first-class providers shipped in v0.4.0:

- **Anthropic Claude** on a Pro / Max 5x / Max 20x subscription, dispatched
  via the `claude` CLI by
  [`anthropic_subscription.py`](../../backend/agents/provider_adapters/anthropic_subscription.py).
- **OpenAI Codex** on a Plus / Pro / Business subscription, dispatched
  via the `codex` CLI by
  [`openai_subscription.py`](../../backend/agents/provider_adapters/openai_subscription.py).

The two adapters are deliberately **CLI-mediated, not API-key-mediated**
— ADR-0007 §Vendor capability matrix records why (subscription
allowance is what the orchestrator is harvesting; it is not addressable
by `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`). The setup below is therefore
about getting two CLIs installed and signed-in on the host that runs
the runner, not about provisioning API keys.

### Pre-conditions

- A host that can reach `claude.ai` and `chatgpt.com` over HTTPS for
  the interactive sign-in flows.
- One Anthropic subscription (Pro, Max 5x, or Max 20x) on an account
  the operator controls. The plan tier is read back from
  `claude auth status` JSON (`subscriptionType`); the adapter accepts
  any non-empty, non-`"none"`, non-`"unknown"` value
  ([`anthropic_subscription.py:196-208`](../../backend/agents/provider_adapters/anthropic_subscription.py)),
  so all three tiers light up the same code path — what differs is the
  vendor-side cap, which feeds into MP.W16.3.
- One OpenAI Codex subscription (Plus, Pro, or Business). The adapter
  accepts the literal string `"logged in using chatgpt"` from
  `codex login status`
  ([`openai_subscription.py:196-198`](../../backend/agents/provider_adapters/openai_subscription.py)),
  so any of the three plan tiers works.
- Node.js available if the operator wants the `npm` install path for
  `codex`. The `claude` CLI is distributed by Anthropic separately —
  install instructions are on Anthropic's `claude` CLI docs page (do
  not memorise a binary URL here; the install method changes faster
  than this doc).

### Step 1 — Install the two CLIs

```bash
# Anthropic — install per the Anthropic docs page above. Verify with:
claude --version

# OpenAI Codex — npm or Homebrew (cross-link with codex-collaboration.md):
npm install -g @openai/codex     # Linux / macOS / Windows
# brew install --cask codex      # macOS only
codex --version
```

Both binaries must be on the runner's `PATH`. The adapters invoke them
by bare name (`["claude", ...]`,
`["codex", "exec", "--cd", os.getcwd(), "--yolo", "--json", "-"]` — see
[`openai_subscription.py:82`](../../backend/agents/provider_adapters/openai_subscription.py)
and
[`anthropic_subscription.py`](../../backend/agents/provider_adapters/anthropic_subscription.py))
through `subprocess`; an absolute path or shim is not currently
supported.

### Step 2 — Sign in

```bash
# Anthropic — interactive browser flow against claude.ai:
claude            # → "Sign in" → browser → return to terminal
claude auth status
#  expect JSON containing  "loggedIn": true,
#                           "authMethod": "claude.ai",
#                           "subscriptionType": "<pro|max-5x|max-20x>"

# OpenAI Codex — interactive ChatGPT sign-in:
codex             # → "Sign in with ChatGPT" → browser → return
codex login status
#  expect line containing  "Logged in using ChatGPT"
```

Auth state lives **on disk under the operator's home directory**
(`~/.config/claude/` / `~/.claude/` for Anthropic;
`~/.codex/` for Codex). It is **not** read from the OmniSight backend
or the `git_accounts` table — those store git-side credentials only,
not LLM subscription state. If the runner runs as a different OS user
than the one that signed in, repeat Step 2 as that user; otherwise the
adapter's health check will report `subscription_active=False` and
`RoutingPolicy.choose_provider()` will skip the provider.

### Step 3 — Wire the orchestrator's host-level knobs

The orchestrator pulls a small set of env vars on every dispatch. The
ones the operator controls at setup time:

| Variable | Default | Effect |
| -------- | ------- | ------ |
| `OMNISIGHT_MP_ENABLED` | unset (off) | Master switch. `is_enabled()` in [`routing_policy.py:628`](../../backend/agents/routing_policy.py) gates `choose_provider()` to `[]` when off. Resolved through `feature_flags.resolve_env_backed_feature_flag` so a registry row in `feature_flags` overrides the env if both are present. |
| `OMNISIGHT_PROVIDER_CAP_ANTHROPIC_SUBSCRIPTION_5H` | `200_000` tokens (`DEFAULT_5H_CAP_TOKENS` in [`provider_quota_tracker.py:34`](../../backend/agents/provider_quota_tracker.py)) | Per-provider 5h rolling cap that trips `provider_quota_cap_hit`. Set to the operator's chosen safety margin **below** the vendor's published Pro/Max cap. |
| `OMNISIGHT_PROVIDER_CAP_ANTHROPIC_SUBSCRIPTION_WEEKLY` | `2_000_000` tokens (`DEFAULT_WEEKLY_CAP_TOKENS`) | Same, weekly window. |
| `OMNISIGHT_PROVIDER_CAP_OPENAI_SUBSCRIPTION_5H` | `200_000` | OpenAI 5h cap. |
| `OMNISIGHT_PROVIDER_CAP_OPENAI_SUBSCRIPTION_WEEKLY` | `2_000_000` | OpenAI weekly cap. |
| `OMNISIGHT_ANTHROPIC_DISPATCH_TIMEOUT_S` | `1800` (30 min) | Per-task subprocess timeout for `claude`. Anything ≤0 falls back to the default ([`anthropic_subscription.py:159-167`](../../backend/agents/provider_adapters/anthropic_subscription.py)). |
| `OMNISIGHT_OPENAI_DISPATCH_TIMEOUT_S` | `1800` | Same shape, for `codex`. |

The cap env-var name is built by `_env_provider()` in
[`provider_quota_tracker.py:342-357`](../../backend/agents/provider_quota_tracker.py):
non-alphanumeric characters in the provider id become `_`, then
upper-cased — so `anthropic-subscription` → `ANTHROPIC_SUBSCRIPTION`.
Misspelling the env-var name is silently ignored (the default cap
applies); double-check by reading back `_cap_for("anthropic-subscription", "5h")`
from a Python REPL after exporting.

The expiry-monitor knobs (`OMNISIGHT_MP_SUBSCRIPTION_MONITOR_INTERVAL_S`,
`OMNISIGHT_MP_SUBSCRIPTION_EXPIRY_WARN_S`,
`OMNISIGHT_MP_SUBSCRIPTION_EXPIRY_CRITICAL_S`) are MP.W16.2 territory —
defaults (6 h / 14 d / 3 d) are sane and the operator does not normally
need to tune them at first-time setup.

API-key fallback variables (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`) are
**not** read by the subscription adapters — they belong to the separate
API-mode adapters. Setting them does not bypass auth issues with the
CLI sign-in.

### Step 4 — Verify the adapter health check

Before flipping `OMNISIGHT_MP_ENABLED=1`, confirm both adapters report
`reachable=True`. The cleanest one-shot check is to import the adapter
and call `health_check()` directly:

```bash
cd /home/user/work/sora/OmniSight-Productizer
python3 -c '
import json
from backend.agents.provider_adapters.anthropic_subscription import (
    AnthropicSubscriptionAdapter,
)
from backend.agents.provider_adapters.openai_subscription import (
    OpenAISubscriptionAdapter,
)
for adapter in (AnthropicSubscriptionAdapter(), OpenAISubscriptionAdapter()):
    h = adapter.health_check()
    print(json.dumps({
        "provider": h.provider_id,
        "reachable": h.reachable,
        "cli_installed": h.cli_installed,
        "subscription_active": h.subscription_active,
        "subscription_expires_at": (
            h.subscription_expires_at.isoformat()
            if h.subscription_expires_at else None
        ),
    }, indent=2))
'
```

Both rows must show `reachable: true`. If `cli_installed: false`,
re-run Step 1 as the runner OS user. If `cli_installed: true` but
`subscription_active: false`, re-run Step 2 (the most common cause is
that the operator signed in interactively as a different shell user
than the runner runs under). The default health-check timeout is 5 s
(`HEALTH_CHECK_TIMEOUT_S` in both adapter modules); if the host is
behind a slow auth-proxy, the health check returns `(127, "", "")`
without reaching the CLI — fix the network path rather than raising
the timeout, since the same path is exercised by `subscription_account_monitor`
every 6 h in production.

### Step 5 — Flip `OMNISIGHT_MP_ENABLED` and dispatch one smoke task

```bash
export OMNISIGHT_MP_ENABLED=1
```

Then dispatch a tiny task with the right `agent_class` to force each
adapter (the labels live in `ROUTING_POLICY_PROVIDER_AGENT_CLASS_LABELS`
in [`routing_policy.py`](../../backend/agents/routing_policy.py)):

- `agent_class: subscription-claude` → must route to
  `anthropic-subscription`.
- `agent_class: subscription-codex` → must route to
  `openai-subscription`.

`RoutingPolicy.choose_provider()` should return a list of length 1
containing the matching provider id. Confirm a row landed in
`provider_usage_event` (alembic 0200) and that
`provider_quota_state.circuit_state` is still `'closed'`. The Provider
Constellation UI should show two green spheres (Anthropic + OpenAI)
and two grayed-out "Coming v0.5.0/v0.6.0" spheres (Gemini, xAI).

### Step 6 — Hand off to MP.W16.2 / MP.W16.3

Once Step 5 is green, the Pro/Max account is in the orchestrator's
hands. From that point:

- `subscription_account_monitor.py` (MP.W16.2) polls the same
  `health_check()` every `OMNISIGHT_MP_SUBSCRIPTION_MONITOR_INTERVAL_S`
  (default 6 h) and emits an alert when `subscription_expires_at` falls
  into the warning (14 d) or critical (3 d) window.
- `provider_quota_tracker.record_usage()` opens the circuit on the first
  cap-hit. If both providers go dark at once, follow the
  [Cap-hit recovery runbook](#cap-hit-recovery-runbook) below.

If at any point the operator changes plans (e.g., upgrades Anthropic
Pro → Max 20x), there is **no** OmniSight-side reconfiguration to do —
re-run Step 2's `claude auth status` to confirm the new
`subscriptionType` flows through, and (optionally) bump the matching
`OMNISIGHT_PROVIDER_CAP_…_5H` / `…_WEEKLY` to the new vendor cap.

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
  (per [ADR-0001](/docs/adr/ADR-0001-five-branch-gitflow/)).
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
  (see [Backend credentials model](/docs/adr/ADR-0003-gerrit-code-review/)
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
- Update [ADR-0007 § Vendor capability matrix](/docs/adr/ADR-0007-multi-provider-subscription-orchestrator/#vendor-capability-matrix-for-routing-policy)
  to flip the Gemini "MVP?" cell.
- Append a `lessons-learned.md` entry per
  [docs/sop/jira-ticket-conventions.md §14](../sop/jira-ticket-conventions.md)
  if anything in the upgrade surprised the operator (cap-signal shape,
  CLI quirks, etc.).

---

## v0.6.0 enablement path — xAI Grok

This section is the **MP.W14.3 deliverable**: the runbook an operator
will follow when v0.6.0 lands and xAI Grok graduates from *structural
slot* to *first-class provider*. The shape mirrors the Gemini path
above, but with three Grok-specific deltas worth flagging up front:

- **Cap signal is undocumented in ADR-0007.** The Vendor capability
  matrix lists Grok's cap signal as "undocumented" — that gap is the
  primary blocker. The v0.6.0 ADR addendum must nail down the cap
  shape (HTTP code, body field, retry-after style) before Step 2 of
  this runbook can be executed safely.
- **Capability rating is the lowest of the four vendors.** ADR-0007
  rates Grok's agentic loop ★★☆☆☆ ("experimental"). Routing and
  cost-estimator seeding should reflect that — start with a small
  per-`agent_class` allowance and let R-MP.2 calibration widen it.
- **SuperGrok is the only documented subscription tier.** Anthropic
  and OpenAI ship multiple tiers; xAI's plan ladder is still
  consolidating, so the operator path assumes SuperGrok and adds a
  TBD note for any successor tier.

Until v0.6.0, the xAI adapter's `dispatch()` returns
`DispatchResult(success=False, error='{"kind":"not_ready",...}')` —
running through the list below is what flips it on. The
`xai-subscription` provider is already structurally registered with
`provider_orchestrator` at module import; that registration does **not**
need to be re-added.

### Pre-conditions for starting the upgrade

Do not begin until **all** of the following are true:

- v0.6.0 release branch has been cut from `develop`
  (per [ADR-0001](/docs/adr/ADR-0001-five-branch-gitflow/)).
- ADR-0007 has an addendum (or successor ADR) that promotes Grok's
  row in the Vendor capability matrix from "placeholder" to "✅",
  records the dispatch / cap-signal contract xAI ships at that point,
  and clarifies the subscription tier(s) supported beyond SuperGrok.
- A `grok-cli`-equivalent agentic CLI exists and has been validated
  end-to-end on at least one OmniSight epic in a sandbox tenant
  (the v0.4.0 ADR called xAI's CLI "experimental" — that is the
  blocker we are waiting on).
- Operator has a SuperGrok subscription on the account that owns the
  OmniSight `git_accounts` row for xAI (see
  [Backend credentials model](/docs/adr/ADR-0003-gerrit-code-review/)
  context — credentials live in PG, not `.env`).

### Step 1 — Promote `agent_class` schema

`config/agent_class_schema.yaml` is the canonical machine-readable list
that drives TODO labels, RPG `class` field (ADR-0008), routing, and the
cost-estimator. It does **not** currently contain `subscription-xai` /
`api-xai`, even though
[`backend/agents/routing_policy.py`](../../backend/agents/routing_policy.py)
already gates `xai-subscription` to those exact strings via
`ROUTING_POLICY_PROVIDER_AGENT_CLASS_LABELS["xai"]`.

When v0.6.0 lands:

1. Add to `allowed_values`:
   - `subscription-xai`
   - `api-xai`
2. Add matching `values:` entries with `provider_family: xai`,
   `access_mode: subscription` / `api`, and the runner binary that
   ships with v0.6.0.
3. Bump `metadata.updated_at` and reference the v0.6.0 ticket.
4. Update **both** ADR-0007 (capability matrix row) and ADR-0008 (RPG
   class table) prose in the same change — drift between schema and
   ADRs is what MP.W0 explicitly forbids.

### Step 2 — Replace the adapter `not_ready` shells

In [`backend/agents/provider_adapters/xai_subscription.py`](../../backend/agents/provider_adapters/xai_subscription.py):

- `dispatch(task)` — wire to the v0.6.0 Grok agentic CLI. Replace the
  hard-coded `{"kind": "not_ready", ...}` error payload with real
  vendor-specific success / error shapes that match the cap-signal
  contract recorded in the ADR-0007 addendum.
- `health_check()` — return the live `HealthStatus` xAI's API exposes
  (auth status, recent error rate). Today's stub hard-codes
  `reachable=False` and `subscription_active=False`.
- `get_quota_state()` — return a real `QuotaState`. Because Grok's
  cap signal is currently undocumented, the v0.6.0 ADR addendum needs
  to settle which of these the QuotaTracker schema
  (`provider_quota_state` table, alembic 0199) will mirror:
  - 5h-rolling + weekly window similar to Anthropic/OpenAI (preferred
    if xAI publishes one), **or**
  - per-query rate fallback similar to the Gemini path (graceful "no
    rolling window" returning `remaining_5h` / `remaining_weekly`
    ratios of `1.0`), **or**
  - a new alembic migration that adds an xAI-shaped quota
    representation. Pick one in the v0.6.0 ADR addendum and document
    the choice here.

The `register_adapter(...)` call at module bottom does **not** change —
the adapter is already structurally registered.

### Step 3 — Un-gate the frontend

`MP.W14.2` shipped a grayed-out sphere with a "Coming v0.6.0" tooltip
in
[`components/omnisight/multi-provider-orchestrator/`](../../components/omnisight/multi-provider-orchestrator/).
To flip it on:

1. Remove the disabled / "Coming v0.6.0" branch in the Provider
   Constellation render path
   (`components/omnisight/multi-provider-orchestrator/ProviderConstellation.tsx`
   and friends; the same `disabled` prop on `ProviderEnergySphere`
   gates Gemini today).
2. The provider already exists as `xai` in
   [`lib/providers.ts`](../../lib/providers.ts) — no add is needed
   there for this step.
3. Confirm the sphere now picks up live SSE quota frames once
   `provider_quota_tracker.py` starts publishing xAI state.

### Step 4 — Cost-estimator seeding

`backend/agents/cost_estimator.py` carries baseline rates for the MVP
providers. Add an xAI entry:

- Subscription rate: `$0` within the operator's SuperGrok plan
  allowance.
- API spillover rate: xAI's then-current `$/M tokens` (read off xAI's
  pricing page at v0.6.0 cut, do not memorise the number here — it
  will go stale).
- Per-`agent_class` wall-time prediction: seed from the sandbox
  validation epics required by the pre-conditions above. Because of
  the ★★☆☆☆ capability rating, expect the seed to under-predict
  agentic-loop wall time for Tier M and above; over-pad on the first
  run.

The estimator's per-tenant calibration (R-MP.2) takes over after the
first ~5 dispatches; the seed only has to be *plausible*, not
*correct*.

### Step 5 — Drift guards

The drift tests landed by MP.W15 enforce these invariants — re-run them
after the changes above and expect them to **fail** until each lands:

- `MP.W15.1` — `lib/providers.ts` 4-vendor list ⊆
  `provider_orchestrator` registry. xAI already passes today
  because the placeholder adapter is registered at import time.
- `MP.W15.2` — ADR-0007 capability matrix labels ⊆ `routing_policy.py`
  consumed labels. Will trip if Step 1 changes `agent_class` strings
  but the ADR is not updated in the same commit.
- `MP.W15.3` — cap-hit integration test (Anthropic → OpenAI fallback).
  Add an analogous case where xAI takes over from a capped
  Anthropic+OpenAI(+Gemini) set.

### Step 6 — Smoke test before tagging v0.6.0

1. Dispatch one task with `agent_class: subscription-xai` and
   confirm `dispatch()` actually executes (no `kind: not_ready`
   payload in the `DispatchResult.error` field).
2. Force a cap-hit signal from each of Anthropic, OpenAI, and (if
   v0.5.0 has shipped) Gemini, and confirm the task migrates to xAI
   at the **task boundary** (mid-task switching is intentionally not
   supported — see ADR-0007 § Negative consequences).
3. Confirm the Provider Constellation sphere renders in `healthy`
   state and the tooltip no longer reads "Coming v0.6.0".
4. Confirm `provider_quota_state` PG table has a row for
   `provider = 'xai-subscription'` with non-stub values.

### Step 7 — Documentation

After the above lands:

- Move the xAI row in the §Provider status snapshot table from
  "Structural slot" → "First-class (v0.6.0)".
- Update [ADR-0007 § Vendor capability matrix](/docs/adr/ADR-0007-multi-provider-subscription-orchestrator/#vendor-capability-matrix-for-routing-policy)
  to flip the xAI "MVP?" cell and replace the "undocumented" cap-signal
  cell with the contract shape settled in Step 2.
- Append a `lessons-learned.md` entry per
  [docs/sop/jira-ticket-conventions.md §14](../sop/jira-ticket-conventions.md)
  if anything in the upgrade surprised the operator (cap-signal shape,
  CLI quirks, capability rating reality vs ADR estimate, etc.).

---

## Related

- [ADR-0007 — Multi-Provider Subscription Orchestrator](/docs/adr/ADR-0007-multi-provider-subscription-orchestrator/)
- [ADR-0008 — Agent RPG class & skill leveling](/docs/adr/ADR-0008-agent-rpg-class-skill-leveling/)
  (shares the `agent_class` schema)
- [`config/agent_class_schema.yaml`](../../config/agent_class_schema.yaml)
  — single source of truth for agent_class values
- [`backend/agents/provider_adapters/`](../../backend/agents/provider_adapters/)
  — adapter shells (Gemini / xAI return `DispatchResult(success=False,
  error='{"kind":"not_ready",...}')` until the enablement paths above
  run)
- [`backend/agents/routing_policy.py`](../../backend/agents/routing_policy.py)
  — `ROUTING_POLICY_PROVIDER_AGENT_CLASS_LABELS` already gates both
  `gemini`-family (`subscription-gemini` / `api-gemini`) and `xai`-family
  (`subscription-xai` / `api-xai`) routing
