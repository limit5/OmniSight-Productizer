# llm

**Purpose**: Multi-provider LLM factory that resolves credentials, instantiates LangChain chat models via the adapter firewall, and wires per-turn observability (token usage, cache counters, rate-limit snapshots, turn events) into every call.

**Key types / public surface**:
- `get_llm(provider, model, bind_tools, *, allow_failover=True)` — primary factory with caching, failover chain, and per-tenant circuit breaker.
- `get_cheapest_model(bind_tools)` — routes utility calls (e.g. auto-titles) through a cheapest-first preference list, no failover cascade to flagship.
- `TokenTrackingCallback` — LangChain callback emitting `track_tokens`, `turn_metrics`, `turn.complete`, and mirroring rate-limit headers into SharedKV.
- `list_providers()` / `validate_model_spec(spec)` — metadata + spec validation for the Settings UI.
- `_normalize_ratelimit_headers(provider, headers)` — unifies Anthropic-native vs OpenAI-compatible header schemas.

**Key invariants**:
- NULL-vs-genuine-zero contract: missing fields stay `None` (UI renders "—"), never coerced to 0; `_normalize_ratelimit_headers` returns `{}` for unmapped providers (Ollama, Gemini today) so SharedKV writes are skipped rather than overwriting prior good data.
- Header extraction walks 5 LangChain locations because no stable path exists across `langchain-<provider>` versions; degrades to `{}` rather than raising.
- `get_cheapest_model` deliberately uses `allow_failover=False` — a missing DeepSeek key must NOT cascade to Opus, and "no key configured" is treated as operational state (not a circuit-breaker signal).
- Module-globals (`_PROVIDER_RATELIMIT_HEADERS`, `_CHEAPEST_MODEL_PREFERENCE`) are const literals; mutable cross-worker state lives in Redis-backed `SharedKV("provider_ratelimit")` with 60s TTL, lazy-init to survive `reset_for_tests`.

**Cross-module touchpoints**:
- Imports: `backend.llm_adapter` (firewall), `backend.llm_credential_resolver`, `backend.config.settings`, `backend.circuit_breaker`, `backend.shared_state.SharedKV`, `backend.events` (emit_turn_metrics/complete), `backend.routers.system` (track_tokens, freeze check), `backend.context_limits`.
- Called by agent/router code needing chat models; auto-title and similar utility paths use `get_cheapest_model`.
