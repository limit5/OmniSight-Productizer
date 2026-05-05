# AB R76-R80 Mitigation Evidence

> Status: evidence index
> Scope: Anthropic API + Batch mode R-series risks R76-R80.
> Source ADR:
> [`anthropic-api-migration-and-batch-mode.md`](../operations/anthropic-api-migration-and-batch-mode.md)

This index consolidates the repository evidence for the AB R76-R80
mitigations. It does not replace the ADR, runbook, or contract tests.
It points reviewers to the artifacts that prove each mitigation has a
runtime, test, or operator guard.

## 1. Scope boundary

This row covers only the R76-R80 mitigation landing evidence:

- R76 API key leak / bill-burn mitigation
- R77 batch 24h completion SLA / lane separation mitigation
- R78 rate-limit retry / DLQ mitigation
- R79 tool schema drift mitigation
- R80 tenant-aware batch task/result routing mitigation

It does not claim the one-week API-mode dogfood, first 100-task batch,
30-day subscription fallback disable, velocity measurement, ADR
completion, or operator runbook completion rows. Those are separate AB
Definition-of-Done rows.

Current status is `dev-only`. The runtime contracts and tests are in
the repository; production activation still requires operator rebuild,
API-mode smoke, and real Anthropic workspace caps.

## 2. R76-R80 mitigation matrix

| Risk | Required mitigation | Landing evidence | Drift guard |
|---|---|---|---|
| R76 API key leak -> bill burn | API key never appears in repr/log output; key is persisted through Token Vault wiring; operator sets Anthropic console Usage Limits as an external hard cap. | `backend/agents/rate_limiter.py::WorkspaceConfig.__repr__` redacts API keys; `backend/agents/anthropic_mode_manager.py` accepts a vault writer and stores only the fingerprint in state; `docs/ops/anthropic-api-migration-runbook.md` requires Anthropic Billing -> Usage Limits before switching mode. | `backend/tests/test_rate_limiter.py::test_workspace_config_redacts_api_key_in_repr`, `backend/tests/test_anthropic_mode_manager.py`, `backend/tests/test_ab_r76_r80_mitigation_evidence.py` |
| R77 batch result returns hours later | Callers explicitly choose realtime vs batch lane; batch submit failure and result-stream failure synthesize per-task errored callbacks so callers are not stranded. | `backend/agents/batch_dispatcher.py::submit_in_lane` keeps lane choice explicit; `_drain_and_submit_once()` notifies callbacks on submit failure; `_process_completed()` notifies callbacks if result streaming fails; runbook states batch may take up to 24h. | `backend/tests/test_batch_dispatcher.py::test_submit_in_lane_realtime_invokes_runner`, `test_submit_in_lane_batch_enqueues_and_returns_none`, `test_dispatcher_submit_failure_notifies_callbacks`, `test_dispatcher_stream_results_failure_notifies_callbacks` |
| R78 rate limit / overload drops tasks | 429/529 classify as rate limited; retry uses exponential backoff with capped Retry-After; max retry exhaustion and non-retryable errors land in DLQ. | `backend/agents/rate_limiter.py::classify_error`, `RetryPolicy`, `RetryableExecutor`, and `InMemoryDeadLetterQueue` implement the retry and DLQ contract. | `backend/tests/test_rate_limiter.py::test_classify_429_530_rate_limited`, `test_executor_rate_limited_honours_retry_after`, `test_executor_max_retry_dlq`, `test_executor_non_retryable_immediate_dlq` |
| R79 tool schema drift | ToolSearch lazy-load schemas carry a pinned schema version; Anthropic tools payload omits ToolSearch-only metadata; registry/doc sync and JSON schema validation are CI-guarded. | `backend/agents/tool_schemas.py::TOOL_SCHEMA_VERSION`, `to_toolsearch_schemas()`, `generate_markdown_reference()`, and `_validate_schemas()` provide versioned lazy-load output and schema validation. | `backend/tests/test_tool_schemas.py::test_to_toolsearch_schemas_returns_versioned_deferred_payload`, `test_toolsearch_input_schema_pins_accepted_schema_version`, `test_generated_doc_matches_committed_doc`, `test_validate_schemas_clean_on_default_registry` |
| R80 batch task cross-tenant isolation | Batch tasks and results carry `tenant_id`; dispatcher groups by `(tenant_id, model, tools_signature)` and callback routing keys on `(tenant_id, task_id)`, preventing same `task_id` collisions across tenants. | `backend/agents/batch_client.py::BatchRequest` / `BatchResult` carry tenant identity; `backend/agents/batch_dispatcher.py::chunk_by_model_tools` includes tenant in grouping; `_ActiveBatch.callbacks` uses `(tenant_id, task_id)` keys; Guild lane stamps `tenant_id` from metadata into `BatchableTask`. | `backend/tests/test_batch_client.py::test_submit_batch_preserves_tenant_id_mapping`, `backend/tests/test_batch_dispatcher.py::test_chunk_groups_by_model_and_tools`, `test_dispatcher_routes_duplicate_task_ids_by_tenant`, `test_submit_guild_task_batch_uses_submit_in_lane_and_stamps_metadata` |

## 3. Production status

This index and the R80 app-layer routing patch do not deploy production
infrastructure by themselves.

**Production status:** dev-only
**Next gate:** deployed-inactive - operator rebuilds the backend image,
runs the AB API-mode smoke, confirms Anthropic workspace Usage Limits
are set, and verifies batch/realtime routing plus DLQ metrics in the
provider observability dashboard.
