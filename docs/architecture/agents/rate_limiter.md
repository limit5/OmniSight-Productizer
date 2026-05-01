# rate_limiter

**Purpose**: Wraps Anthropic API calls with production-grade retry, exponential-backoff-with-jitter, sliding-window rate tracking, and a dead-letter queue, so transient 429/529/5xx/network failures don't surface as user-visible errors. Also encodes Anthropic Workspace partitioning (dev/batch/production) so quota and spend stay isolated per workspace.

**Key types / public surface**:
- `RetryableExecutor.execute(...)` — main entry point; runs an async call factory with retries + DLQ deposit.
- `RateLimitTracker` — sliding-window RPM/input-TPM/output-TPM tracker keyed by `(workspace, model)`; exposes `record()` and predictive `would_exceed()`.
- `RetryPolicy`, `classify_error()`, `parse_retry_after()`, `compute_backoff()` — tunable retry primitives.
- `DeadLetterQueue` Protocol + `InMemoryDeadLetterQueue` — pluggable DLQ; in-memory ships, PG-backed deferred.
- `TIER_4_LIMITS` / `get_rate_limit()` and `WorkspaceConfig` — Tier 4 quota table and per-workspace API-key holder (redacts key in `__repr__`).

**Key invariants**:
- Unknown models degrade `RateLimitTracker` to no-op rather than raising — caller must ensure model is registered for caps to apply.
- `RetryAfter` from server is capped to `policy.max_delay_seconds` to defend against malicious/buggy `Retry-After: 86400` values.
- Constructor uses `is None` checks (not `or`) for `dlq`/`tracker` because `InMemoryDeadLetterQueue` is falsy via `__len__` — replacing them would silently drop the caller's reference.
- On ambiguity, `classify_error` defaults to `retryable`: prefers retrying a transient blip over silently DLQ'ing.
- Tracker is **not** multi-tenant — all calls under a configured workspace share quota until KS.1 envelope arrives.

**Cross-module touchpoints**:
- Pure-stdlib module; no internal imports. Designed to be called by the AB.4 dispatcher (batch submit/poll/retrieve) and to feed AB.6 cost guard + Z spend-anomaly dashboards (per docstring).
- API keys in `WorkspaceConfig` are expected to come from the AS Token Vault / KS.1 envelope.
