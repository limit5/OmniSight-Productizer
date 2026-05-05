"""AB.3 — Anthropic Messages Batch API integration.

Submits, tracks, and streams results from Anthropic batch jobs (50% off
input + output, up to 100K requests / 256 MB / 24h window). Persists
batch metadata + per-result rows so AB.4 dispatcher can route results
back to OmniSight task_ids even across worker restarts.

Two consumers:

  1. AB.4 batch dispatcher worker — submit batches grouped by model +
     tool subset, poll for completion, fan out results to per-task
     callbacks.
  2. Operator one-shot scripts — bulk-process a corpus (parse 1000
     schematics, run 5000 adversarial prompts) at 50% cost.

Scope of THIS module:

  - `BatchClient` wraps the SDK `messages.batches` namespace
  - In-memory `BatchPersistence` impl for dev / tests
  - Limit enforcement (AB.3.3): request_count / total_size / batch_age
  - `submit_batch()` returns BatchRun with status; never blocks on completion
  - `poll_batch()` re-fetches status from Anthropic, mirrors counts to
    persistence
  - `stream_results()` — async iterator over completed results, with
    optional per-result callback (AB.3.4); handles partial failure
    (AB.3.6) by surfacing each result's status independently
  - Cancel + delete

Out of scope (deferred):

  - Postgres column-level `tenant_id` scoping — app-layer request/result
    identity is tenant-aware for R80; SQL columns land with the production
    persistence migration.

ADR: docs/operations/anthropic-api-migration-and-batch-mode.md §4
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Protocol

logger = logging.getLogger(__name__)


# ─── Limits (AB.3.3) ─────────────────────────────────────────────

MAX_REQUESTS_PER_BATCH = 100_000
MAX_BATCH_SIZE_BYTES = 256 * 1024 * 1024  # 256 MB
MAX_PROCESSING_HOURS = 24


# ─── Domain models ───────────────────────────────────────────────


BatchRunStatus = Literal[
    "pending",      # constructed, not yet submitted to Anthropic
    "submitted",    # Anthropic accepted, processing
    "ended",        # Anthropic finished (success/partial/error mix)
    "canceled",     # operator canceled
    "expired",      # 24h window elapsed
    "failed",       # submit failed (network / 4xx)
]


BatchResultStatus = Literal[
    "pending",
    "succeeded",
    "errored",
    "canceled",
    "expired",
]


@dataclass(frozen=True)
class BatchRequest:
    """One request in a batch.

    `custom_id` is what Anthropic returns on each result and is the join
    key against `task_id`. Anthropic requires custom_id length 1-64 and
    uniqueness within the batch.

    `params` is the raw `messages.create()` shape — typically built via
    `AnthropicClient.simple_params()`.
    """

    custom_id: str
    params: dict[str, Any]
    task_id: str | None = None
    tenant_id: str | None = None


@dataclass
class BatchRun:
    """One batch as tracked by OmniSight (mirrors Anthropic batch state)."""

    batch_run_id: str
    status: BatchRunStatus
    request_count: int
    total_size_bytes: int = 0
    anthropic_batch_id: str | None = None
    submitted_at: datetime | None = None
    ended_at: datetime | None = None
    expires_at: datetime | None = None
    success_count: int = 0
    error_count: int = 0
    canceled_count: int = 0
    expired_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    created_by: str | None = None
    tenant_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class BatchResult:
    """One result entry, one per request in the batch."""

    batch_run_id: str
    custom_id: str
    status: BatchResultStatus
    task_id: str | None = None
    tenant_id: str | None = None
    response: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    final_text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    completed_at: datetime | None = None


# ─── Persistence Protocol + in-memory impl ───────────────────────


class BatchPersistence(Protocol):
    """Storage abstraction. SQL impl lands with AB.4 dispatcher."""

    async def save_batch_run(self, run: BatchRun) -> None: ...
    async def get_batch_run(self, batch_run_id: str) -> BatchRun | None: ...
    async def list_batch_runs(
        self, status: BatchRunStatus | None = None
    ) -> list[BatchRun]: ...
    async def save_batch_result(self, result: BatchResult) -> None: ...
    async def list_batch_results(self, batch_run_id: str) -> list[BatchResult]: ...
    async def find_result_by_task_id(
        self, task_id: str, tenant_id: str | None = None
    ) -> BatchResult | None: ...


class InMemoryBatchPersistence:
    """In-memory persistence — dev / test only.

    Production callers wire a Postgres-backed implementation via
    AB.4 dispatcher. Schema is alembic 0181.
    """

    def __init__(self) -> None:
        self._runs: dict[str, BatchRun] = {}
        self._results: dict[tuple[str, str], BatchResult] = {}

    async def save_batch_run(self, run: BatchRun) -> None:
        self._runs[run.batch_run_id] = run

    async def get_batch_run(self, batch_run_id: str) -> BatchRun | None:
        return self._runs.get(batch_run_id)

    async def list_batch_runs(
        self, status: BatchRunStatus | None = None
    ) -> list[BatchRun]:
        items = list(self._runs.values())
        if status:
            items = [r for r in items if r.status == status]
        return sorted(items, key=lambda r: r.created_at, reverse=True)

    async def save_batch_result(self, result: BatchResult) -> None:
        self._results[(result.batch_run_id, result.custom_id)] = result

    async def list_batch_results(self, batch_run_id: str) -> list[BatchResult]:
        return [r for (b, _), r in self._results.items() if b == batch_run_id]

    async def find_result_by_task_id(
        self, task_id: str, tenant_id: str | None = None
    ) -> BatchResult | None:
        for r in self._results.values():
            if r.task_id == task_id and (tenant_id is None or r.tenant_id == tenant_id):
                return r
        return None


# ─── Limit enforcement ───────────────────────────────────────────


class BatchLimitError(ValueError):
    """Raised when batch limits would be exceeded — AB.3.3."""


def estimate_request_size(params: dict[str, Any]) -> int:
    """Rough byte size: JSON-serialized length. Anthropic counts JSON bytes."""
    return len(json.dumps(params, ensure_ascii=False).encode("utf-8"))


def validate_batch_limits(requests: list[BatchRequest]) -> int:
    """Validate request_count + total_size + custom_id + tenant constraints.

    Returns total estimated size in bytes.
    """
    if not requests:
        raise BatchLimitError("Batch must contain at least one request.")

    if len(requests) > MAX_REQUESTS_PER_BATCH:
        raise BatchLimitError(
            f"Batch has {len(requests)} requests; max is {MAX_REQUESTS_PER_BATCH}. "
            f"Split into multiple batches."
        )

    seen_ids: set[str] = set()
    total_size = 0
    for r in requests:
        if not r.custom_id or not (1 <= len(r.custom_id) <= 64):
            raise BatchLimitError(
                f"custom_id {r.custom_id!r} length must be 1-64 chars "
                "(Anthropic constraint)."
            )
        if r.custom_id in seen_ids:
            raise BatchLimitError(
                f"Duplicate custom_id {r.custom_id!r} in batch "
                "(Anthropic requires uniqueness)."
            )
        seen_ids.add(r.custom_id)
        total_size += estimate_request_size(r.params)

    _single_tenant_id(requests, require_single=True)

    if total_size > MAX_BATCH_SIZE_BYTES:
        raise BatchLimitError(
            f"Batch payload {total_size:,} bytes exceeds {MAX_BATCH_SIZE_BYTES:,} "
            f"(256 MB). Split into smaller batches."
        )

    return total_size


def _single_tenant_id(
    requests: list[BatchRequest], *, require_single: bool = False
) -> str | None:
    """Return tenant_id when a batch is single-tenant, else None.

    R80 mitigation is enforced by AB.4 grouping before submission; this
    helper mirrors the invariant on BatchRun for audit/debug metadata.
    """
    tenant_ids = {r.tenant_id for r in requests}
    if require_single and len(tenant_ids) > 1:
        raise BatchLimitError("Batch cannot mix tenant_id values; split by tenant.")
    tenant_ids.discard(None)
    if len(tenant_ids) == 1:
        return next(iter(tenant_ids))
    return None


# ─── Status mapping helpers ──────────────────────────────────────


def _map_anthropic_processing_status(status: str) -> BatchRunStatus:
    """Anthropic's `processing_status` → our `BatchRunStatus`."""
    mapping: dict[str, BatchRunStatus] = {
        "in_progress": "submitted",
        "canceling": "submitted",
        "ended": "ended",
    }
    return mapping.get(status, "submitted")


def _map_anthropic_result_type(rtype: str) -> BatchResultStatus:
    """Anthropic's `result.type` → our `BatchResultStatus`."""
    return {
        "succeeded": "succeeded",
        "errored": "errored",
        "canceled": "canceled",
        "expired": "expired",
    }.get(rtype, "errored")


def _extract_text_and_usage(message: Any) -> tuple[str, dict[str, int]]:
    """Pull final text + token usage from an Anthropic Message object/dict."""
    content = (
        message.get("content")
        if isinstance(message, dict)
        else getattr(message, "content", None)
    )
    text_parts: list[str] = []
    for block in content or []:
        block_type = (
            block.get("type")
            if isinstance(block, dict)
            else getattr(block, "type", None)
        )
        if block_type == "text":
            text = (
                block.get("text")
                if isinstance(block, dict)
                else getattr(block, "text", "")
            )
            if text:
                text_parts.append(text)

    raw_usage = (
        message.get("usage")
        if isinstance(message, dict)
        else getattr(message, "usage", None)
    )

    def _u(name: str) -> int:
        if raw_usage is None:
            return 0
        if isinstance(raw_usage, dict):
            return int(raw_usage.get(name, 0) or 0)
        return int(getattr(raw_usage, name, 0) or 0)

    return "".join(text_parts), {
        "input_tokens": _u("input_tokens"),
        "output_tokens": _u("output_tokens"),
        "cache_read_tokens": _u("cache_read_input_tokens"),
        "cache_creation_tokens": _u("cache_creation_input_tokens"),
    }


def _to_dict(obj: Any) -> Any:
    """Best-effort serialize SDK objects to plain dict/list shape."""
    if obj is None:
        return None
    if isinstance(obj, (str, int, float, bool, list, dict)):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "__dict__"):
        return {k: _to_dict(v) for k, v in vars(obj).items() if not k.startswith("_")}
    return str(obj)


# ─── BatchClient ─────────────────────────────────────────────────


ResultCallback = Callable[[BatchResult], Awaitable[None]]


class BatchClient:
    """Submit / poll / stream Anthropic batches with persistence."""

    def __init__(
        self,
        sdk_messages_namespace: Any,
        persistence: BatchPersistence | None = None,
    ) -> None:
        # `sdk_messages_namespace` is `client.messages` from the Anthropic SDK
        # (i.e. AnthropicClient(...).messages). Injected explicitly so this
        # class can be stubbed without importing the SDK.
        self._messages = sdk_messages_namespace
        self.persistence: BatchPersistence = persistence or InMemoryBatchPersistence()

    async def submit_batch(
        self,
        requests: list[BatchRequest],
        *,
        metadata: dict[str, Any] | None = None,
        created_by: str | None = None,
    ) -> BatchRun:
        """Validate, persist, and submit a batch to Anthropic.

        Raises `BatchLimitError` BEFORE any API call if limits violated.
        Persists a row with status="pending" before submit, then updates
        to "submitted" on success or "failed" on API error.
        """
        total_size = validate_batch_limits(requests)

        run = BatchRun(
            batch_run_id=f"br_{uuid.uuid4().hex[:16]}",
            status="pending",
            request_count=len(requests),
            total_size_bytes=total_size,
            metadata=dict(metadata or {}),
            created_by=created_by,
            tenant_id=_single_tenant_id(requests),
        )
        await self.persistence.save_batch_run(run)

        # Pre-persist all results in pending state so AB.4 dispatcher can
        # find task_ids before the batch finishes.
        for req in requests:
            await self.persistence.save_batch_result(
                BatchResult(
                    batch_run_id=run.batch_run_id,
                    custom_id=req.custom_id,
                    task_id=req.task_id,
                    tenant_id=req.tenant_id,
                    status="pending",
                )
            )

        anthropic_payload = [
            {"custom_id": r.custom_id, "params": r.params} for r in requests
        ]

        try:
            sdk_batch = self._messages.batches.create(requests=anthropic_payload)
        except Exception as e:  # noqa: BLE001 - external boundary
            run.status = "failed"
            run.metadata["submit_error"] = str(e)[:500]
            await self.persistence.save_batch_run(run)
            raise

        run.anthropic_batch_id = getattr(sdk_batch, "id", None) or (
            sdk_batch.get("id") if isinstance(sdk_batch, dict) else None
        )
        run.status = _map_anthropic_processing_status(
            getattr(sdk_batch, "processing_status", None)
            or (sdk_batch.get("processing_status") if isinstance(sdk_batch, dict) else "")
            or "in_progress"
        )
        run.submitted_at = datetime.now(timezone.utc)
        await self.persistence.save_batch_run(run)
        return run

    async def poll_batch(self, batch_run_id: str) -> BatchRun:
        """Re-fetch Anthropic status, update persistence, return latest."""
        run = await self.persistence.get_batch_run(batch_run_id)
        if run is None:
            raise KeyError(f"Unknown batch_run_id {batch_run_id!r}")
        if not run.anthropic_batch_id:
            return run

        sdk_batch = self._messages.batches.retrieve(run.anthropic_batch_id)
        sdk_status = (
            getattr(sdk_batch, "processing_status", None)
            or (sdk_batch.get("processing_status") if isinstance(sdk_batch, dict) else None)
            or "in_progress"
        )
        run.status = _map_anthropic_processing_status(sdk_status)

        rc = (
            getattr(sdk_batch, "request_counts", None)
            or (sdk_batch.get("request_counts") if isinstance(sdk_batch, dict) else None)
        )
        if rc is not None:
            def _rc(name: str) -> int:
                if isinstance(rc, dict):
                    return int(rc.get(name, 0) or 0)
                return int(getattr(rc, name, 0) or 0)

            run.success_count = _rc("succeeded")
            run.error_count = _rc("errored")
            run.canceled_count = _rc("canceled")
            run.expired_count = _rc("expired")

        if run.status == "ended":
            run.ended_at = datetime.now(timezone.utc)

        await self.persistence.save_batch_run(run)
        return run

    async def stream_results(
        self,
        batch_run_id: str,
        *,
        on_result: ResultCallback | None = None,
    ) -> AsyncIterator[BatchResult]:
        """Iterate completed results. Persists each, optionally invokes callback.

        Caller's responsibility to ensure batch has ended (poll first); calling
        before completion typically raises from the SDK side. AB.4 dispatcher
        polls until status="ended" before invoking this.

        Partial-failure handling (AB.3.6): each result carries an independent
        status (succeeded / errored / canceled / expired). The iterator yields
        ALL results regardless of status; downstream filters as needed.
        """
        run = await self.persistence.get_batch_run(batch_run_id)
        if run is None:
            raise KeyError(f"Unknown batch_run_id {batch_run_id!r}")
        if not run.anthropic_batch_id:
            raise ValueError(
                f"Batch {batch_run_id!r} was never submitted to Anthropic."
            )

        # Look up custom_id → task_id mapping from our persisted pending rows.
        existing = {
            r.custom_id: r
            for r in await self.persistence.list_batch_results(batch_run_id)
        }

        sdk_iter = self._messages.batches.results(run.anthropic_batch_id)

        async for entry in _async_iter(sdk_iter):
            custom_id = (
                entry.get("custom_id") if isinstance(entry, dict)
                else getattr(entry, "custom_id", None)
            )
            sdk_result = (
                entry.get("result") if isinstance(entry, dict)
                else getattr(entry, "result", None)
            )
            rtype = (
                sdk_result.get("type") if isinstance(sdk_result, dict)
                else getattr(sdk_result, "type", "errored")
            )
            status = _map_anthropic_result_type(rtype or "errored")

            message = (
                sdk_result.get("message") if isinstance(sdk_result, dict)
                else getattr(sdk_result, "message", None)
            )
            error = (
                sdk_result.get("error") if isinstance(sdk_result, dict)
                else getattr(sdk_result, "error", None)
            )

            final_text = ""
            usage = {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
            }
            if status == "succeeded" and message is not None:
                final_text, usage = _extract_text_and_usage(message)

            previous = existing.get(custom_id)
            result = BatchResult(
                batch_run_id=batch_run_id,
                custom_id=custom_id or "",
                task_id=previous.task_id if previous else None,
                tenant_id=previous.tenant_id if previous else None,
                status=status,
                response=_to_dict(message) if message is not None else None,
                error=_to_dict(error) if error is not None else None,
                final_text=final_text,
                completed_at=datetime.now(timezone.utc),
                **usage,
            )
            await self.persistence.save_batch_result(result)
            if on_result:
                await on_result(result)
            yield result

    async def cancel_batch(self, batch_run_id: str) -> BatchRun:
        """Request Anthropic to cancel; mirror state to persistence."""
        run = await self.persistence.get_batch_run(batch_run_id)
        if run is None:
            raise KeyError(f"Unknown batch_run_id {batch_run_id!r}")
        if not run.anthropic_batch_id:
            run.status = "canceled"
            await self.persistence.save_batch_run(run)
            return run

        try:
            self._messages.batches.cancel(run.anthropic_batch_id)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Cancel batch %s failed: %s; marking canceled locally anyway",
                run.anthropic_batch_id,
                e,
            )
        run.status = "canceled"
        run.ended_at = datetime.now(timezone.utc)
        await self.persistence.save_batch_run(run)
        return run

    async def find_result_for_task(
        self, task_id: str, tenant_id: str | None = None
    ) -> BatchResult | None:
        """Look up the result row for a given OmniSight task_id (AB.3.2 mapping)."""
        return await self.persistence.find_result_by_task_id(task_id, tenant_id)


async def _async_iter(maybe_async_iterable: Any) -> AsyncIterator[Any]:
    """Iterate either a sync or async iterable uniformly.

    Anthropic SDK results() may be either, depending on whether the caller
    used `anthropic` vs `anthropic.AsyncAnthropic`. We accept both.
    """
    if hasattr(maybe_async_iterable, "__aiter__"):
        async for item in maybe_async_iterable:
            yield item
    else:
        for item in maybe_async_iterable:
            yield item
