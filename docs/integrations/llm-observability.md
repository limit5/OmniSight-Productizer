# LLM Observability — Provider Support Matrix

> Z.5 (#294) checkbox 4 — canonical reference for **which LLM provider
> exposes which observability signal** via an API-key-authenticated path
> OmniSight can call from the backend without session cookies, dashboard
> scraping, or out-of-band credentials.

This page is the operator-facing answer to the question:

> "I configured an API key for provider X — what will the dashboard show
> me?"

The matrix below is the authoritative snapshot. Downstream checkboxes in
Z.5 layer an operator how-to (UI walkthrough, manual price-table reload,
per-provider budget alarm) on top — see the Z.5 row in `TODO.md` for
sibling scope boundaries.

---

## 1. Support matrix

| Provider   | Balance API              | Rate-Limit Headers | Notes |
|------------|--------------------------|--------------------|-------|
| Anthropic  | ❌                       | ✅                 | 官方無 balance API |
| OpenAI     | ❌（需 session cookie）  | ✅                 | `/v1/usage` 不支援 API key auth |
| Google     | ❌（需 GCP）             | ⚠️                 | Gemini API 部分 model 無 header |
| xAI        | ❌                       | ✅                 | |
| Groq       | ❌                       | ✅                 | |
| DeepSeek   | ✅                       | ✅                 | `/user/balance` |
| Together   | ❌                       | ⚠️                 | |
| OpenRouter | ✅                       | ✅                 | `/auth/key` 含 usage + credit_balance |
| Ollama     | N/A                      | ❌                 | 本地部署無餘額概念 |

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
- **Roll-up tile** (Z.4 checkbox 5) — counts only providers where at
  least one of the two columns is non-grey; Ollama is excluded from
  the denominator so local-only deployments don't see a permanent
  "1/9 providers healthy" red number.

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
4. **Update this matrix** — add a row with the correct ✅ / ⚠️ / ❌ /
   N/A cells and any provider-specific caveat in the Notes column.
   Keep the row order alphabetical within "remote providers", with
   Ollama (and any future local runtime) at the bottom.

---

## 5. Related files

- `backend/agents/llm.py` — rate-limit header parse + SharedKV mirror.
- `backend/llm_balance.py` — `SUPPORTED_BALANCE_PROVIDERS` registry +
  per-provider fetcher coroutines.
- `backend/llm_balance_refresher.py` — lifespan-scoped 10-min refresh
  loop that populates `SharedKV("provider_balance")`.
- `backend/routers/llm_balance.py` — `/runtime/providers/*/balance`
  endpoints (single + batch).
- `config/llm_pricing.yaml` — authoritative per-model USD/1M-token
  pricing consumed by `backend/pricing.py::get_pricing`.
- `backend/tests/test_ratelimit_capture.py` — the four-provider
  end-to-end contract.
- `backend/tests/test_llm_balance.py` — DeepSeek + OpenRouter balance
  fetch contract + unsupported-provider envelope.

---

*Last verified 2026-04-25 against `_PROVIDER_RATELIMIT_HEADERS` and
`SUPPORTED_BALANCE_PROVIDERS` at commit-HEAD. Re-verify whenever a
provider is added, renamed, or has its fetcher removed — the matrix
is a human-maintained snapshot; the two Python registries are the
runtime truth.*
