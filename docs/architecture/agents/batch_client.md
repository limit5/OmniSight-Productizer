# batch_client

**Purpose**: Wraps Anthropic's Messages Batch API (50% cost discount, async 24h window) with OmniSight-side persistence so submitted batches, per-request `custom_id`s, and results can be tracked across worker restarts. Implements workitem AB.3; consumed by the AB.4 dispatcher and operator bulk-processing scripts.

**Key types / public surface**:
- `BatchClient` — submit / poll / stream / cancel batches; takes the SDK `messages` namespace + a `BatchPersistence` by injection.
- `BatchRequest` / `BatchRun` / `BatchResult` — domain dataclasses; `BatchRun` mirrors Anthropic batch state, `BatchResult` carries per-request status + tokens + final text. `BatchRequest` and `BatchResult` also carry `tenant_id` for R80 app-layer routing.
- `BatchPersistence` (Protocol) and `InMemoryBatchPersistence` — storage abstraction; in-memory impl for dev/tests only.
- `validate_batch_limits()` + `BatchLimitError` — enforces AB.3.3 limits (100k requests, 256 MB, custom_id 1–64 chars + unique).
- Constants `MAX_REQUESTS_PER_BATCH`, `MAX_BATCH_SIZE_BYTES`, `MAX_PROCESSING_HOURS`.

**Key invariants**:
- `custom_id` is the join key between Anthropic responses and OmniSight `task_id`; it must be 1–64 chars and unique per batch (Anthropic constraint, validated locally before submit).
- Pending `BatchResult` rows are written *before* SDK submit so the AB.4 dispatcher can resolve `task_id` mappings even if the worker dies mid-flight.
- `stream_results()` yields all results regardless of per-result status (partial failure per AB.3.6); caller filters. It assumes the batch has already ended — calling early relies on SDK-side errors.
- `_async_iter` deliberately accepts both sync and async iterables, since SDK choice (`Anthropic` vs `AsyncAnthropic`) determines `results()`'s shape.
- App-layer `tenant_id` identity is carried through pending and completed results. Postgres column-level filtering remains a production persistence migration follow-up; in-memory dev/test persistence already supports tenant-filtered task lookup.

**Cross-module touchpoints**:
- Imports only stdlib; the Anthropic SDK is injected (`sdk_messages_namespace`), keeping this module test-stubbable and decoupled from `AnthropicClient`.
- Docstring references `AnthropicClient.simple_params()` as the expected builder for `BatchRequest.params`.
- Designed to be called by the (not-yet-landed) AB.4 batch dispatcher worker and by operator one-shot scripts.
