# batch_dispatcher

**Purpose**: Implements AB.4 — a long-running async worker that drains a priority queue of `BatchableTask`s, groups them by `(model, tools_signature)`, submits via `BatchClient`, polls for completion, and fans results back through per-task callbacks. Also provides the lane router that lets callers choose realtime vs. batch execution.

**Key types / public surface**:
- `BatchableTask` — frozen dataclass wrapping `task_id`, Anthropic `params`, async `callback`, and priority. Exposes `model`/`tools_signature`/`estimate_size`.
- `BatchTaskQueue` — in-memory async priority queue (P0→P3) with `enqueue`, `drain`, `wait_until_nonempty`. Designed as a swap-in point for Postgres/Redis.
- `BatchDispatcher` — the worker; `start()`, `stop(drain_in_flight=...)`, `enqueue()`, `stats()`.
- `chunk_by_model_tools()` — pure function that groups tasks into `BatchGroup`s respecting Anthropic count/byte limits.
- `submit_in_lane()` — caller-side router; `lane="realtime"` runs immediately, `lane="batch"` enqueues and returns `None`.
- `submit_guild_task_in_lane()` — thin BP.B Guild client adapter; validates the Guild slug, stamps `dispatch_source="guild"` / `guild_id` / `task_kind` metadata, then delegates to `submit_in_lane()`.

**Key invariants**:
- Grouping by `(model, tools_signature)` is for prompt-cache hit rate, not Anthropic correctness — batches don't need to be homogeneous, but breaking the grouping silently kills the ~90% cache discount.
- On submit failure the dispatcher synthesizes an `errored` `BatchResult` per task and fires callbacks (R77: callers must learn the batch never landed). Same fallback applies if `stream_results` blows up mid-stream.
- `stop(drain_in_flight=False)` orphans active batches on the Anthropic side; recovery requires persistence to survive restart and a future `start()` to re-adopt them.
- Groups beyond `max_concurrent_batches` capacity get re-enqueued (priority preserved), but order within priority is not strictly preserved across the round-trip.

**Cross-module touchpoints**:
- Imports from `backend.agents.batch_client` (`BatchClient`, `BatchRequest/Result/Run`, size limits, `estimate_request_size`).
- Priority semantics are documented as mirroring `queue_backend.py`, though no direct import.
- Intended downstream callers: BP.B Guild calls `submit_guild_task_in_lane()` after it builds params via `AnthropicClient.simple_params`; non-Guild callers can keep using `submit_in_lane()` directly.
