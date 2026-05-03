# LLM Observability — Provider Support Matrix

> Z.5 (#294) checkbox 4 — canonical reference for **which LLM provider
> exposes which observability signal** via an API-key-authenticated path
> OmniSight can call from the backend without session cookies, dashboard
> scraping, or out-of-band credentials.

This page is the operator-facing answer to the question:

> "I configured an API key for provider X — what will the dashboard show
> me?"

The matrix below is the authoritative snapshot. Sections 1-5 cover
**which signal each provider exposes**; sections 6-9 cover **how an
operator reads, reloads, and reacts** to those signals day-to-day
(reading the UI, manually reloading the price table, per-provider
budget alarms, and the provider-dashboard link list).

---

## 1. Support matrix

| Provider   | Balance API              | Rate-Limit Headers | Tool Calling | Notes |
|------------|--------------------------|--------------------|--------------|----- |
| Anthropic  | ❌                       | ✅                 | ✅           | 官方無 balance API |
| OpenAI     | ❌（需 session cookie）  | ✅                 | ✅           | `/v1/usage` 不支援 API key auth |
| Google     | ❌（需 GCP）             | ⚠️                 | ✅           | Gemini API 部分 model 無 header |
| xAI        | ❌                       | ✅                 | ✅           | |
| Groq       | ❌                       | ✅                 | ✅           | OpenAI-compat tools；llama3.x / mixtral / gemma-v2 覆蓋主流 use case |
| DeepSeek   | ✅                       | ✅                 | ✅           | `/user/balance` |
| Together   | ❌                       | ⚠️                 | ⚠️           | tool calling model-dependent；僅 llama3.x / mistral 系有支援 |
| OpenRouter | ✅                       | ✅                 | ✅           | `/auth/key` 含 usage + credit_balance；tool schema passthrough |
| Ollama     | N/A                      | ❌                 | ⚠️           | 本地部署無餘額概念；tool calling model-dependent，見 `config/ollama_tool_calling.yaml` |

### Legend

- **✅** — Fully supported. OmniSight reads this signal on every
  applicable request and surfaces it in the dashboard **Providers** panel.
- **⚠️** — Partial / best-effort. The signal appears for *some* models,
  SDK versions, or request shapes but cannot be relied on across the
  whole provider surface — dashboard may render "no data" without it
  being an error.
- **❌** — Not supported. The provider exposes the data but only behind
  an auth path OmniSight does not (or will not — see Non-goals in the
  Z.5 TODO row) implement (session cookies, GCP OAuth, dashboard HTML
  scraping).
- **N/A** — Signal is definitionally meaningless for the provider (e.g.
  self-hosted Ollama has no remote balance to query).

---

## 2. What the two columns actually mean

### 2.1 Balance API

"API key auth to a public HTTP endpoint that returns the current
dollar / credit balance of the key's owning account." This is the
criterion `backend/llm_balance.py::SUPPORTED_BALANCE_PROVIDERS` gates
on. Providers absent from that registry get `{"status": "unsupported",
"reason": "provider does not expose a public balance API with API-key
authentication"}` from the
`GET /api/v1/runtime/providers/{provider}/balance` endpoint — the UI
renders that envelope as a greyed-out "—".

Two providers currently pass the bar:

- **DeepSeek** — `GET https://api.deepseek.com/user/balance`, header
  `Authorization: Bearer <DEEPSEEK_API_KEY>`; response includes
  `balance_infos[].total_balance` (USD-denominated top-up wallet).
- **OpenRouter** — `GET https://openrouter.ai/api/v1/auth/key`, same
  bearer header; response includes `data.usage` (lifetime USD consumed)
  and `data.limit - data.usage` as remaining credit.

Anthropic, OpenAI, Google, xAI, Groq, Together have no comparable
endpoint accepting the same API key the app already uses for
completions. OpenAI has `/v1/usage` but **it requires a session cookie
— the API key 403s**; that is not worth the fragility of cookie-minting
automation. Google balances live in GCP billing (separate OAuth surface).

### 2.2 Rate-Limit Headers

"Response headers the provider sets on `200` / `429` that OmniSight's
`on_llm_end` callback parses into the unified
`{remaining_requests, remaining_tokens, reset_at_ts, retry_after_s}`
shape, mirrored into `SharedKV("provider_ratelimit")[provider]` with
a 60 s TTL." See `backend/agents/llm.py::_PROVIDER_RATELIMIT_HEADERS`
for the mapping and `backend/tests/test_ratelimit_capture.py` for the
parse contract.

Seven providers register header names: anthropic, openai, xai, groq,
deepseek, together, openrouter. Together is marked ⚠️ because its
backend pool routes requests across heterogeneous model hosts and the
`x-ratelimit-*` headers are observed to be absent on a non-trivial
fraction of responses in production traffic. Google is marked ⚠️
because the `langchain-google-genai` adapter does not currently surface
the underlying `x-goog-quota-*` headers through any of the 5 paths
OmniSight's `_extract_response_headers` walks; when / if an adapter
version lands that mirrors them, the entry flips to ✅ without a schema
change. Ollama is ❌ (and will stay ❌) because local inference has no
remote rate to bound.

### 2.3 Tool Calling

"The provider's API — as accessed through OmniSight's adapter
(`backend/llm_adapter.py::tool_call()`) — supports structured tool /
function calls where the model returns a `tool_calls` block rather
than (or in addition to) a natural-language reply."

The criterion is whether `build_chat_model(...).bind_tools(tools)` and
a subsequent `.invoke()` reliably produces a parseable `tool_calls`
response field.  This is the mechanism agent dispatch uses: the
specialist node calls `tool_call(messages, tools, provider=…)` and
inspects `AdapterToolResponse.tool_calls`.

Column values for Tool Calling:

- **✅** — `bind_tools()` is fully wired and the provider's API
  returns structured tool calls for all production-grade models
  offered by that provider.  OmniSight passes tool schemas through
  without special handling.
- **⚠️** — Tool calling works for a *subset* of models or with
  known limitations.  OmniSight's adapter handles the happy path but
  callers should guard: Together only routes to models that themselves
  support function calling; Ollama support is model-dependent (see
  `config/ollama_tool_calling.yaml` for the per-model matrix) and
  Z.6.5's graceful fallback degrades to pure-chat + dashboard warning
  when the daemon or model cannot honour the `tool_calls` field.
- **❌** — Provider does not support structured tool calls via the
  API path OmniSight uses (not applicable to any provider in the
  current matrix — all nine have at least partial support).
- **N/A** — Not applicable for this provider (also not applicable in
  the current matrix — even Ollama has partial support).

**Ollama special case (Z.6)**: tool calling was not connected to the
Ollama provider prior to Z.6.2.  As of Z.6.2, `ChatOllama.bind_tools()`
is invoked on the same path as the other eight providers.  Z.6.5 adds
the graceful-fallback safety net: if the daemon raises (model
unsupported, connection failure) or the response cannot be parsed, the
adapter degrades to `AdapterToolResponse(tool_calls=[])`, increments
`SharedKV("ollama_tool_failures")`, and surfaces a dashboard alert — it
does not raise.  `config/ollama_tool_calling.yaml` lists the nine
confirmed-compatible models with `full` / `partial` support levels and
minimum Ollama daemon versions.

---

## 3. How the matrix maps to the dashboard

- **Providers panel → Balance column** — populated only for ✅ rows in
  the Balance API column. Everything else renders "—" (unsupported) or
  "stale since HH:MM" (supported provider but the last fetch 5xx'd —
  the boundary contract from Z.2 checkbox 5 writes the stale marker
  without overwriting the last good value).
- **Providers panel → Rate-limit badge** — populated for ✅ rows on
  every LLM turn; ⚠️ rows populate intermittently; ❌ / N/A rows render
  a grey dash. TTL is 60 s — the badge fades to "no recent data" if no
  turn touches that provider within the minute.
- **Providers panel → Tool Calling badge** — rendered on the provider
  card for Ollama (Z.6.4 catalog badge) showing per-model support level
  sourced from `config/ollama_tool_calling.yaml`.  For remote providers
  the badge is static (✅ / ⚠️ from the matrix) and does not require a
  live API call.  When `SharedKV("ollama_tool_failures").total` exceeds
  the alert threshold (Z.6.5), the badge flips to an amber warning icon
  with "fallback active — N failures" tooltip.
- **Roll-up tile** (Z.4 checkbox 5) — counts only providers where at
  least one of the two *observability* columns (Balance, Rate-limit) is
  non-grey; Ollama is excluded from the denominator so local-only
  deployments don't see a permanent "1/9 providers healthy" red number.
  The Tool Calling column does not feed the health denominator — it is a
  capability indicator, not a liveness signal.

---

## 4. Adding a new provider

When a new provider is added to OmniSight (`llm_credentials.py` +
adapter registration):

1. **Price row** — add a `providers[<name>]` block to
   `config/llm_pricing.yaml` (covered by Z.3 checkbox 1's YAML schema).
2. **Rate-limit headers** — if the provider sets `x-ratelimit-*` in
   responses, add a row to
   `backend/agents/llm.py::_PROVIDER_RATELIMIT_HEADERS` mapping the
   four contract keys. The `test_ratelimit_capture.py` four-provider
   parametrise serves as the copy-paste template.
3. **Balance fetcher** — only if the provider publishes an API-key-auth
   balance endpoint. Add a `fetch_balance_<name>` coroutine in
   `backend/llm_balance.py` returning a `BalanceInfo`, then register it
   in `SUPPORTED_BALANCE_PROVIDERS`. The endpoint + refresher pick it
   up automatically; no router change needed.
4. **Tool calling** — verify that `build_chat_model(provider, model).bind_tools(tools)`
   works end-to-end.  If the provider is a local runtime with per-model
   variance (like Ollama), add a `config/<provider>_tool_calling.yaml`
   matrix (use `config/ollama_tool_calling.yaml` as the template) and
   wire a graceful fallback in `backend/llm_adapter.py` (see the
   `_ollama_tool_call_fallback` pattern introduced in Z.6.5).  Remote
   providers that have uniform tool-calling support across their model
   catalogue do not need a per-model YAML.
5. **Update this matrix** — add a row with the correct ✅ / ⚠️ / ❌ /
   N/A cells across **all three signal columns** (Balance API,
   Rate-Limit Headers, Tool Calling) and any provider-specific caveat
   in the Notes column.  Keep the row order alphabetical within "remote
   providers", with Ollama (and any future local runtime) at the bottom.

---

## 5. Related files

- `backend/agents/llm.py` — rate-limit header parse + SharedKV mirror;
  `_PROVIDER_RATELIMIT_HEADERS` is the canonical header-name registry.
- `backend/llm_adapter.py` — `build_chat_model()` + `tool_call()`;
  all nine providers route through the common `bind_tools` step here;
  `_ollama_tool_call_fallback()` implements the Z.6.5 graceful-fallback
  path (daemon error / unsupported model / parse failure).
- `backend/llm_balance.py` — `SUPPORTED_BALANCE_PROVIDERS` registry +
  per-provider fetcher coroutines.
- `backend/llm_balance_refresher.py` — lifespan-scoped 10-min refresh
  loop that populates `SharedKV("provider_balance")`.
- `backend/routers/llm_balance.py` — `/runtime/providers/*/balance`
  endpoints (single + batch).
- `config/llm_pricing.yaml` — authoritative per-model USD/1M-token
  pricing consumed by `backend/pricing.py::get_pricing`.
- `config/ollama_tool_calling.yaml` — per-model tool-calling
  compatibility matrix for the Ollama provider: `full` / `partial` /
  `none` support levels + minimum Ollama daemon version (Z.6.4).
- `backend/tests/test_ratelimit_capture.py` — the four-provider
  end-to-end rate-limit header contract.
- `backend/tests/test_llm_balance.py` — DeepSeek + OpenRouter balance
  fetch contract + unsupported-provider envelope.
- `backend/tests/test_llm_adapter.py` — Ollama tool_call mock tests;
  validates that the adapter normalises `ChatOllama.invoke` tool_calls
  output into `AdapterToolResponse` on the same contract as remote
  providers (Z.6.6).
- `backend/tests/test_ollama_tool_fallback.py` — graceful-fallback
  test suite: unsupported model degrade, daemon-unreachable, parse
  failure; confirms no exception surfaces to callers (Z.6.7).

---

## 6. Operator UI — reading the Providers panel

The Providers panel lives on the main dashboard page (the same route
`TokenUsageStats` renders under). Each configured provider shows as a
collapsed summary row; clicking the chevron expands the row into the
three-line detail block rendered by
`components/omnisight/provider-card-expansion.tsx`:

```
Balance     $12.34 / $50.00
Rate-limit  982 req remaining / 198,402 tokens remaining (reset in 42s)
Last synced 0:05 ago
```

### 6.1 Reading the three rows

- **Balance row** — `$remaining / $granted_total` for ✅ providers.
  `$remaining` only (no "/ total") for providers that expose a spendable
  remainder but not a granted top-up total (OpenRouter pattern). Renders
  `—` for ⚠️ / ❌ / N/A rows. Currency prefix is `$` (USD) or `¥` (CNY,
  DeepSeek domestic plan).
- **Rate-limit row** — reads the SharedKV entry populated by the last
  LLM turn that hit this provider. Shows `N req remaining` and/or
  `N tokens remaining`, whichever subset the provider set headers for,
  followed by `(reset in NNs)` or `(retry after ~NNs)` when a 429 landed.
  The 60 s TTL means the row fades to `—` if no turn touches that
  provider within the minute — that is expected after idle periods, not
  an error.
- **Last synced row** — elapsed wall-clock since the balance refresher
  last wrote a non-error envelope. Format ladder: `Ns ago` (< 1 min),
  `M:SS ago` (< 1 h), `H:MM:SS ago` (< 1 d), `Xd ago`. `never` means the
  refresher has not yet run (fresh boot or provider key just configured
  — wait up to 10 min, the lifespan-scoped refresher cycle).

### 6.2 Unsupported providers

When the matrix row is ❌ for Balance API, the expansion short-circuits
to a single advisory line plus an external-link button routed to the
vendor's own dashboard (see section 9 for the full list):

```
This provider does not expose a public balance API. Open the
provider dashboard to view usage.                        [Open ↗]
```

Ollama renders the advisory without a dashboard link (local deployment
has no remote dashboard).

### 6.3 Error envelopes

When the backend's 10-min balance refresher catches a 401 / 403 /
network / 5xx from an otherwise-✅ provider, the row stays mounted but
the Balance value frozen at the last-good number, with a red error
message below the three rows (the
`provider-card-expansion-error-message` slot). Typical messages:

- `Balance refresh failed: 401 unauthorized` — rotate the key via
  `POST /api/v1/runtime/providers/{provider}/key` and wait for the
  next 10 min refresher tick (or restart the backend for an immediate
  retry).
- `Balance refresh failed: upstream 502` — vendor-side incident;
  check section 9's dashboard link and `status.<provider>.com`; the
  refresher will retry on its own schedule.

### 6.4 Roll-up tile

The header-level roll-up tile counts `healthy / total` across all
✅ + ⚠️ rows. Ollama is excluded from the denominator so local-only
deployments do not sit at a permanent "1/9 healthy" red number. A
provider counts as "healthy" when **either** of its two columns is
non-grey — i.e. the provider is doing its job even if only one signal
lands.

---

## 7. Manually reloading the price table

The backend loads `config/llm_pricing.yaml` at boot and keeps a
module-level `_PRICING_CACHE` per uvicorn worker. Without the reload
endpoint the only way to pick up a YAML edit is a rolling restart
through Caddy — which is both slow and needlessly disruptive for the
common case of "Anthropic bumped Sonnet from 3/15 to 3.5/16".

### 7.1 Endpoint

- **Method / path** — `POST /api/v1/runtime/pricing/reload`
- **Auth** — admin role. Router dependency:
  `backend/routers/system.py:2207` stacks
  `_REQUIRE_ADMIN = [Depends(_auth.require_role("admin"))]`; a
  regular-user session gets 403.
- **Cross-worker fan-out** — this worker reloads synchronously, then
  publishes `PRICING_RELOAD_EVENT = "pricing_reload"` via Redis
  pub/sub (`backend/shared_state.publish_cross_worker`). Every peer
  worker's `_on_pricing_reload_event` listener clears its
  `_PRICING_CACHE` so the next `get_pricing()` call re-reads the YAML.
  When Redis is unavailable the response body flags
  `"broadcast": "local_only"` and the operator must do a rolling
  restart for peer workers.

### 7.2 Operator runbook

```bash
# 1. Edit the YAML on the backend host (or in the repo + re-deploy).
$EDITOR /app/config/llm_pricing.yaml

# 2. Validate the edit as a dry read (optional but cheap).
curl -sS -H "Authorization: Bearer $ADMIN_TOKEN" \
     https://omnisight.example.com/api/v1/runtime/pricing \
   | jq '.loaded_from_yaml, .metadata.updated_at'

# 3. Trigger the hot-reload.
curl -sS -X POST \
     -H "Authorization: Bearer $ADMIN_TOKEN" \
     https://omnisight.example.com/api/v1/runtime/pricing/reload \
   | jq '.'
```

Happy-path response:

```json
{
  "status": "reloaded",
  "loaded_from_yaml": true,
  "providers": ["anthropic", "openai", "google", "xai", "groq",
                "deepseek", "together", "openrouter", "ollama"],
  "metadata": {"updated_at": "2026-04-25", "source": "..."},
  "broadcast": "redis_pubsub"
}
```

### 7.3 Degraded modes

- **`"loaded_from_yaml": false`** — YAML is missing / unreadable /
  corrupt; the backend kept the hard-coded fallback rates alive for
  boot-resilience (see `backend/pricing.py::_HARD_CODED_FALLBACK`).
  Fix the YAML and re-POST; you will not lose billing continuity
  because historical-cost rows are frozen at write time.
- **`"broadcast": "local_only"`** — Redis pub/sub failed. Only the
  worker that received the POST has the new cache. Roll-restart the
  backend service (`systemctl restart omnisight-backend` or the
  compose-equivalent `docker compose restart backend`) to force every
  worker to pick up the YAML.

### 7.4 What reload does *not* do

- It does **not** retroactively re-price past LLM calls. Historical
  `cost` values in `token_usage_log` / workflow-run records are
  frozen at the rate in effect when the call landed — intentional,
  see `TestHistoricalCostPreservation` in `backend/tests/test_pricing.py`.
- It does **not** reload per-provider API keys or model routing — only
  the USD/1M-token pricing table. Credential rotation has its own
  endpoint family under `/api/v1/runtime/providers/*/key`.

---

## 8. Per-provider budget alarms (Y9 deferred)

**Status today (2026-04-25)**: not implemented. The Z milestone scope
is explicitly "make the signal *visible*"; turning visibility into an
active alarm loop ("notify me when Anthropic credit drops below $100")
is deferred to the **Y9 Audit / Observability / Billing** milestone,
which layers a `(tenant_id, project_id)` billing-event bus on top of
the Z rate-limit + balance signals. See `TODO.md` section "Y9. Audit /
Observability / Billing 整合 (#285)" for the full scope, and the Z.5
row intro note "**和 Priority Y / T 的關係**" for the handshake
contract between Z (signal source) and T/Y9 (alarm consumer).

### 8.1 What does *not* exist yet

- No alarm-rule table (no `budget_alarm_rules` schema).
- No notification dispatch path (no Slack webhook, no email, no SSE
  event for "Anthropic credit crossed threshold").
- No per-tenant threshold storage (deferred to Y1's
  `projects.plan_override` + Y9's billing-event emitter).
- No forecasting ("Anthropic will run out in 4 days") — the Z.5 TODO
  Non-goals explicitly excludes budget consumption prediction ("需要
  time-series model，目前只做現值顯示").

### 8.2 Interim manual workflow (until Y9 ships)

Operators who need a budget alarm *today* have three options, ranked
by cost:

1. **Manual check** — open the dashboard Providers panel once per
   shift; any ✅ provider's Balance row is the current number.
2. **External cron against the REST endpoint** — scrape
   `GET /api/v1/runtime/providers/balance` from an external scheduler
   (Nagios / Prometheus blackbox / a 30-line Python script) and
   threshold on `providers[].balance_remaining`. Envelope shape is
   stable and is the exact same payload the UI reads, so a cron
   script will not drift relative to the panel.
3. **Vendor-side alerts** — most providers (Anthropic, OpenAI,
   OpenRouter, DeepSeek) let you set a low-balance email alert on
   their own dashboard (see section 9). These do not know about
   your tenant-split, but they do fire even if OmniSight is down,
   so they make a reasonable belt-and-braces safety net under any
   alarm strategy.

### 8.3 What Y9 will add

- `budget_alarm_rules` schema keyed on `(tenant_id, project_id,
  provider, threshold, currency, cooldown_s, delivery_channel)`.
- Delivery channels: Slack webhook, email (via the existing
  notification centre), SSE event for live dashboard toast.
- Hooks into the same `SharedKV("provider_balance")` surface the UI
  already reads — Z.2's balance refresher already writes the signal,
  so Y9 only needs to subscribe + threshold-check, not re-collect.

No schema migration or runtime code change in Z is blocking Y9 —
the signal surface is already stable.

---

## 9. Provider dashboard link list

When a provider row is ❌ / ⚠️ in the matrix, the operator's escape
hatch is the vendor's own console. These are the canonical links the
Providers panel surfaces as the "Open ↗" button
(`components/omnisight/provider-card-expansion.tsx::DEFAULT_PROVIDER_DASHBOARD_URLS`).

Kept here in the doc so operators running an air-gapped tenant (where
the "Open ↗" button is hidden by CSP) still have the list to hand:

| Provider   | Console URL                                              | Shows                        |
|------------|----------------------------------------------------------|------------------------------|
| Anthropic  | https://console.anthropic.com/settings/billing           | Credit balance, invoices     |
| OpenAI     | https://platform.openai.com/usage                        | Usage + rate-limit tiers     |
| Google     | https://aistudio.google.com/app/apikey                   | API keys; billing via GCP    |
| xAI        | https://console.x.ai/                                    | Credit balance, usage        |
| Groq       | https://console.groq.com/settings/billing                | Credits + rate-limit plan    |
| DeepSeek   | https://platform.deepseek.com/usage                      | `/user/balance` UI view      |
| Together   | https://api.together.ai/settings/billing                 | Credits, invoices            |
| OpenRouter | https://openrouter.ai/credits                            | Credit balance, key usage    |
| Ollama     | (local deployment — no remote dashboard)                 | N/A                          |

### 9.1 Keeping this list aligned with the UI

The frontend map in `provider-card-expansion.tsx` and the table above
must not drift. If you change one, change both in the same PR. The
table is alphabetised the same way the TypeScript map is declared
(alphabetical remote providers, then Ollama) so a diff review catches
renames easily.

Tenant-specific overrides (e.g. a CN-region deployment pointing to
DeepSeek's domestic console) are already plumbed through the
`dashboardUrl` prop on the expansion component — do **not** fork this
table per tenant; keep the default pristine and override at the prop
level.

---

## 10. Live integration test — coverage matrix + pass/fail SOP

Z.7 (2026-04-29 audit) wired a nightly live-test suite that hits the
three primary providers with real API keys to catch silent breaks caused
by LangChain upgrades, provider-side schema changes, and multi-turn
tool-use regressions.  This section documents **what is tested, what is
not, and how to triage failures**.

### 10.1 Coverage matrix

The test file is `backend/tests/test_llm_adapter_live.py`.  All tests
carry `@pytest.mark.live` and are skipped by default unless the
corresponding CI key environment variable is set.

| Scenario | Anthropic | OpenAI | Google Gemini | Notes |
|---|---|---|---|---|
| **basic\_invoke** — plain chat round-trip | ✅ | ✅ | ✅ | Sanity: LangChain adapter returns non-empty text |
| **tool\_call** — single-turn `get_weather` | ✅ | ✅ | ✅ | Validates `tool_calls[0].name`, `.args`, `.call_id` across all three |
| **multi\_turn\_tool\_loop** — tool\_use → fake `tool_result` → final text | ✅ | ✅ | ✅ | **Core gap pre-Z.7**: verifies the loop closes and the LLM reads the result |
| **streaming\_tool\_call** — stream mode delivers `tool_calls` | ✅ | ✅ | ⚠️ | Gemini skips with `pytest.skip` when the model/SDK version does not support streaming tool calls |
| **nested\_schema** — `book_flight` with `list[Passenger]` + enum | ✅ | ✅ | ⚠️ | Checks for silent field truncation; Gemini skips if `InvalidArgument` is raised |

**Models used in CI** (cheapest tier per provider to minimise spend):

| Provider | Model | Env var |
|---|---|---|
| Anthropic | `claude-haiku-4-5-20251001` | `ANTHROPIC_API_KEY_CI` |
| OpenAI | `gpt-3.5-turbo` | `OPENAI_API_KEY_CI` |
| Google Gemini | `gemini-1.5-flash` | `GOOGLE_API_KEY_CI` |

**Out-of-scope for Z.7** (intentional — see Non-goals in the Z.7 TODO row):

- The five OpenAI-compatible providers (xAI, Groq, DeepSeek, Together,
  OpenRouter) share the OpenAI code path and are covered transitively
  by the OpenAI live tests; they do not have dedicated live-test classes.
- Ollama (self-hosted) is not included — the Z.6 test suite
  (`test_ollama_tool_fallback.py`) covers it with mocks; a live
  Ollama test would require a daemon in CI, which is out of scope.
- Multi-tenant key isolation is not tested here (deferred to Y4 / Phase 5b).

### 10.2 Budget guard

Two enforcement layers keep a single nightly run well under USD $0.50:

1. **Per-call token ceiling** — `OMNISIGHT_CI_MAX_TOKENS_PER_CALL=2000`
   is read by `_ci_max_tokens()` and passed as `max_tokens` (Anthropic /
   OpenAI) or `max_output_tokens` (Google) to every `build_chat_model()`
   call.  Locally (key absent) the default fallback is 256 tokens.
2. **Max iterations cap** — `_MAX_LIVE_TEST_ITERATIONS=3` (env
   `OMNISIGHT_CI_MAX_ITER`) limits how many LLM round-trips any single
   multi-turn test may make.  Each multi-turn test asserts
   `_turns_used ≤ _MAX_LIVE_TEST_ITERATIONS` and fails immediately if
   the model exceeds the cap rather than silently burning budget.

`scripts/ci_budget_guard.py` runs in the `budget-guard` CI job after the
tests complete.  It computes a worst-case cost estimate from the caps
above; if the estimate exceeds `OMNISIGHT_CI_BUDGET_USD` (default $0.50),
the job fails with a `::error::` annotation and the `gate` job blocks
the workflow.

### 10.3 Running the live tests locally

```bash
# Run all live tests against Anthropic only:
ANTHROPIC_API_KEY_CI=sk-ant-... \
  pytest backend/tests/test_llm_adapter_live.py -m live -k anthropic -v

# Run all three providers:
ANTHROPIC_API_KEY_CI=sk-ant-... \
OPENAI_API_KEY_CI=sk-... \
GOOGLE_API_KEY_CI=AI... \
  pytest backend/tests/test_llm_adapter_live.py -m live -v

# Full suite with explicit budget caps (mirrors CI):
OMNISIGHT_CI_MAX_TOKENS_PER_CALL=2000 \
OMNISIGHT_CI_MAX_ITER=3 \
  pytest backend/tests/test_llm_adapter_live.py -m live -v
```

Without any of the three key environment variables set, all live tests
are skipped automatically by the `conftest.py` collection hook — running
`pytest` normally will not incur any API costs.

### 10.4 Nightly CI schedule and result surface

The workflow `.github/workflows/llm-live-tests.yml` triggers at
**06:00 UTC (14:00 Asia/Taipei)** daily.  Four jobs run in sequence:

| Job | Purpose |
|---|---|
| `live-tests` | Run `pytest -m live`; capture exit code + JSON report |
| `budget-guard` | Verify worst-case spend ≤ $0.50 |
| `report` | POST result to `SharedKV("llm_live_test_status")` + write GitHub step summary |
| `gate` | Propagate failure to workflow result so GitHub UI marks the run red |
| `escalate` | Fires only on failure: checks whether the *previous* completed run also failed; if so, invokes `scripts/llm_adapter_debug_bot.py` |

Results are visible in three places:

1. **GitHub Actions** — the workflow run page under
   `.github/workflows/llm-live-tests.yml` shows per-job status and the
   step summary table (provider pass/fail counts, budget headroom).
2. **Dashboard chip** — `components/omnisight/live-test-status-chip.tsx`
   polls `GET /api/v1/runtime/live-test-status` every 5 minutes and
   displays "Last live-test pass: Xh ago" (green) or "Live-test FAILING"
   (red) in the `Z provider observability` chip area of the
   `TokenUsageStats` panel.
3. **SharedKV** — `SharedKV("llm_live_test_status")` is readable
   programmatically for integration with external monitoring systems.

### 10.5 Pass/fail SOP — single failure

When a nightly run fails for the first time:

1. **Check which provider failed** — open the GitHub Actions run →
   `live-tests` job → "Run pytest" step output.  The JSON report and
   step summary table list per-provider pass/fail/skip counts.
2. **Check provider status pages** — a provider-side outage is the most
   common cause of a one-off failure.

   | Provider | Status page |
   |---|---|
   | Anthropic | https://status.anthropic.com |
   | OpenAI | https://status.openai.com |
   | Google | https://status.cloud.google.com |

3. **Check for a LangChain / SDK version bump** — look at `git log
   backend/requirements.txt` or recent Dependabot PRs.  If `langchain`,
   `langchain-anthropic`, `langchain-openai`, or `langchain-google-genai`
   was bumped, the new version may have reshaped tool-call response
   objects.  Re-run the failing test class locally against the same SDK
   version.
4. **Check CI key expiry** — sandbox keys may have zero remaining credit
   or been rotated.  A 401 / 403 in the test output is the signal.
   Rotate the key in GitHub Actions repo secrets
   (Settings → Secrets and variables → Actions) and re-trigger the
   workflow with `workflow_dispatch`.
5. **One-off transient** — if status pages are green, SDK version is
   unchanged, and keys are valid, the failure is likely a transient API
   hiccup.  The next nightly run will confirm.  No action required.

### 10.6 Pass/fail SOP — consecutive failures (escalation)

When **two consecutive** nightly runs both fail, the `escalate` job in
the workflow automatically:

1. Invokes `scripts/llm_adapter_debug_bot.py` with the current and
   previous run IDs.
2. The bot downloads both runs' pytest JSON reports, classifies the
   failure pattern (isolated single provider / all-three / new-failure /
   no-data), and generates a structured RCA checklist.
3. The bot opens a GitHub issue labelled `llm-live-test-failure` with
   the checklist.  It rate-limits itself: if an open issue with that
   label already exists, it skips the `gh issue create` call (no spam).

The `consecutive_failures` counter is also incremented in
`SharedKV("llm_live_test_status")` and surfaced in the dashboard chip
tooltip and the `GET /api/v1/runtime/live-test-status` response body.

**SOP for the on-call engineer receiving the escalation issue:**

1. Read the RCA checklist in the issue body — it calls out which provider
   failed and lists the most common root causes in priority order.
2. Work through the checklist items (provider status, SDK version,
   key validity, schema regression) and close the issue once the root
   cause is identified and resolved.
3. Trigger a manual `workflow_dispatch` run to confirm the fix before
   the next nightly window.
4. If the fix requires a code change, the PR description should reference
   the auto-filed issue number so the escalation trail is complete.

To **re-arm** the escalation after the issue is closed: simply close the
existing `llm-live-test-failure` issue.  The next consecutive-failure
streak will open a fresh one.

### 10.7 Pass/fail SOP — budget guard trip

If the `budget-guard` job fails:

1. The `gate` job propagates the failure — the overall workflow run is
   marked as failed in GitHub UI even if all tests passed.
2. Open the `budget-guard` job step output.  `ci_budget_guard.py` prints
   the estimated cost breakdown: tokens-per-call × iterations × providers
   × tests × USD-per-1M-tokens.
3. Common causes:
   - `OMNISIGHT_CI_MAX_TOKENS_PER_CALL` was raised above the safe
     threshold in the workflow env block.
   - `OMNISIGHT_CI_MAX_ITER` was raised above 3.
   - A new test class or test method was added without updating the
     estimator's test-count constant in `ci_budget_guard.py`.
   - Model pricing in `config/llm_pricing.yaml` changed and the
     estimator's hard-coded price constants are stale.
4. Fix whichever cause applies and re-trigger.  The estimator does **not**
   use actual token counts from the run — it is a static worst-case
   formula to catch configuration drift before real overspend occurs.

---

*Last verified 2026-05-03 against `_PROVIDER_RATELIMIT_HEADERS`,
`SUPPORTED_BALANCE_PROVIDERS`, `DEFAULT_PROVIDER_DASHBOARD_URLS`, and
`config/ollama_tool_calling.yaml` at commit-HEAD (Z.6.8). Re-verify
whenever a provider is added, renamed, or has its fetcher / dashboard
URL / tool-calling support changed — the matrix and link list are
human-maintained snapshots; the four source-of-truth artefacts (two
Python, one TypeScript, one YAML) are the runtime truth.*
