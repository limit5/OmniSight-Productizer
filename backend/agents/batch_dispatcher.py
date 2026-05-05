"""AB.4 — Batch task queue + dispatcher worker.

Long-running async worker that drains a task queue, groups tasks by
(model, tool-set) tuple, chunks them respecting Anthropic batch limits
(AB.3.3), submits via `BatchClient`, polls until completion, and fans
results back to per-task callbacks.

Architecture::

  caller.enqueue(BatchableTask) ──► [BatchTaskQueue] ──┐
                                                       ▼
                                              [BatchDispatcher loop]
                                                       │
                                  ┌────────────────────┼─────────────────┐
                                  │                    │                 │
                          drain + group         poll active        process completed
                          + submit               batches            (stream results +
                                                                   per-task callback)

Lane separation (AB.4.4) — callers choose explicitly:

  * ``lane="realtime"`` — skip the dispatcher, hit Anthropic Messages
    API directly via ``AnthropicClient.run_with_tools()``. Use for
    interactive tasks, chat UI, anything where p95 latency matters.
  * ``lane="batch"`` — go through this dispatcher. ~50% cheaper but
    can take up to 24h to complete (typically minutes-to-hours).
    Use for routine processing, large corpora, scheduled audits.

The dispatcher itself ONLY handles the batch lane; realtime callers
bypass entirely.

Out of scope (deferred):

  * Postgres-backed BatchPersistence — InMemoryBatchPersistence is
    canonical for tests; PG impl ships when first batch dispatcher
    runs against real DB (commits a BatchPersistencePostgres class
    in the same module without breaking the contract).
  * BP.B Guild integration (AB.4.6) — Guild is downstream consumer;
    once Guild lands it constructs BatchableTask + calls enqueue()
  * UI display (AB.4.5) — frontend reads via existing batch_runs
    table queries (already covered by AB.3 schema)

ADR: docs/operations/anthropic-api-migration-and-batch-mode.md §4
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from backend.agents.batch_client import (
    BatchClient,
    BatchRequest,
    BatchResult,
    BatchRun,
    MAX_REQUESTS_PER_BATCH,
    MAX_BATCH_SIZE_BYTES,
    estimate_request_size,
)
from backend.sandbox_tier import Guild

logger = logging.getLogger(__name__)


LaneType = Literal["realtime", "batch"]
PriorityLevel = Literal["P0", "P1", "P2", "P3"]
"""Priority semantics mirror queue_backend.py:
   P0 = incident   P1 = hotfix   P2 = sprint   P3 = backlog
"""


@dataclass(frozen=True)
class BatchableTask:
    """One task waiting to be batched.

    Caller-supplied fields:
      - task_id: stable OmniSight identifier; round-trips to BatchResult.task_id
      - params: full Anthropic messages.create() params dict
                (typically built via AnthropicClient.simple_params)
      - callback: async fn invoked with the BatchResult once complete
      - tools_signature: hash of tool names included in params, used
        for grouping (tasks with same tools batch together)
    """

    task_id: str
    params: dict[str, Any]
    callback: Callable[[BatchResult], Awaitable[None]] | None = None
    priority: PriorityLevel = "P2"
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def model(self) -> str:
        return self.params.get("model", "unknown")

    @property
    def tools_signature(self) -> str:
        """Stable signature of the tool subset used; tasks sharing a
        signature can share a single Anthropic batch."""
        tools = self.params.get("tools") or []
        names = sorted(t.get("name", "") for t in tools if isinstance(t, dict))
        return "|".join(names)

    def estimate_size(self) -> int:
        return estimate_request_size(self.params)


# ─── Queue ───────────────────────────────────────────────────────


class BatchTaskQueue:
    """In-memory async queue with priority bucketing.

    Production swap-in point: this same surface backed by Postgres or
    Redis Streams. Tests use the in-memory version directly.
    """

    def __init__(self) -> None:
        # One deque-like list per priority. P0 drained first.
        self._buckets: dict[PriorityLevel, list[BatchableTask]] = {
            "P0": [],
            "P1": [],
            "P2": [],
            "P3": [],
        }
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()

    async def enqueue(self, task: BatchableTask) -> None:
        async with self._lock:
            self._buckets[task.priority].append(task)
            self._wake.set()

    async def drain(
        self, max_count: int = MAX_REQUESTS_PER_BATCH, max_size: int = MAX_BATCH_SIZE_BYTES
    ) -> list[BatchableTask]:
        """Pull up to `max_count` tasks (highest priority first) within
        the size budget. Returns empty list if queue empty.
        """
        out: list[BatchableTask] = []
        size_used = 0
        async with self._lock:
            for prio in ("P0", "P1", "P2", "P3"):
                bucket = self._buckets[prio]  # type: ignore[index]
                while bucket and len(out) < max_count:
                    next_size = bucket[0].estimate_size()
                    if size_used + next_size > max_size and out:
                        # would overflow; bail to current group
                        return out
                    task = bucket.pop(0)
                    out.append(task)
                    size_used += next_size
            if not any(self._buckets.values()):
                self._wake.clear()
        return out

    async def wait_until_nonempty(self, timeout: float | None = None) -> bool:
        """Block until queue has tasks. Returns True if woken, False on timeout."""
        async with self._lock:
            if any(self._buckets.values()):
                return True
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    def __len__(self) -> int:
        return sum(len(b) for b in self._buckets.values())


# ─── Grouping helper ─────────────────────────────────────────────


@dataclass(frozen=True)
class BatchGroup:
    """A homogeneous chunk submittable as a single Anthropic batch."""

    model: str
    tools_signature: str
    tasks: tuple[BatchableTask, ...]


def chunk_by_model_tools(
    tasks: list[BatchableTask],
    *,
    max_per_chunk: int = MAX_REQUESTS_PER_BATCH,
    max_size_per_chunk: int = MAX_BATCH_SIZE_BYTES,
) -> list[BatchGroup]:
    """Group tasks by (model, tools_signature), then chunk to limits.

    Anthropic doesn't require homogeneous batches, but grouping by
    (model, tools) keeps prompt-cache hit rate high (system + tools
    repeat exactly across same-group tasks → 90% off cached input).
    """
    by_key: dict[tuple[str, str], list[BatchableTask]] = {}
    for task in tasks:
        by_key.setdefault((task.model, task.tools_signature), []).append(task)

    groups: list[BatchGroup] = []
    for (model, sig), bucket in by_key.items():
        chunk: list[BatchableTask] = []
        chunk_size = 0
        for task in bucket:
            t_size = task.estimate_size()
            would_overflow_count = len(chunk) >= max_per_chunk
            would_overflow_size = chunk_size + t_size > max_size_per_chunk
            if chunk and (would_overflow_count or would_overflow_size):
                groups.append(BatchGroup(model, sig, tuple(chunk)))
                chunk = []
                chunk_size = 0
            chunk.append(task)
            chunk_size += t_size
        if chunk:
            groups.append(BatchGroup(model, sig, tuple(chunk)))

    return groups


# ─── Dispatcher worker ───────────────────────────────────────────


@dataclass
class _ActiveBatch:
    """Tracks one submitted batch + the callbacks owed to its tasks."""

    batch_run_id: str
    callbacks: dict[str, Callable[[BatchResult], Awaitable[None]] | None]
    submitted_at_loop_iter: int = 0


class BatchDispatcher:
    """Long-running worker: queue → group → submit → poll → fan-out results.

    Lifecycle::

        dispatcher = BatchDispatcher(batch_client, queue)
        await dispatcher.start()
        # ... callers enqueue tasks ...
        await dispatcher.stop()  # graceful: drains in-flight, no new submits

    Polling cadence default: 60s, configurable. Small enough that batches
    completing in minutes get processed quickly; large enough to not pound
    Anthropic's retrieve endpoint.
    """

    def __init__(
        self,
        batch_client: BatchClient,
        queue: BatchTaskQueue | None = None,
        *,
        poll_interval_seconds: float = 60.0,
        drain_idle_timeout_seconds: float = 5.0,
        max_concurrent_batches: int = 10,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.batch_client = batch_client
        self.queue = queue or BatchTaskQueue()
        self.poll_interval_seconds = poll_interval_seconds
        self.drain_idle_timeout_seconds = drain_idle_timeout_seconds
        self.max_concurrent_batches = max_concurrent_batches
        self._sleep = sleep
        self._active: dict[str, _ActiveBatch] = {}
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._loop_iter = 0
        self.results_processed = 0
        self.batches_submitted = 0
        self.errors_encountered = 0

    # ── Public API ──────────────────────────────────────────────

    async def enqueue(self, task: BatchableTask) -> None:
        await self.queue.enqueue(task)

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run_loop(), name="batch-dispatcher")

    async def stop(self, *, drain_in_flight: bool = True) -> None:
        """Stop accepting new submits. Optionally wait for in-flight batches.

        With drain_in_flight=True, waits for all currently-active batches
        to complete (up to ~24h Anthropic SLA). With drain_in_flight=False,
        bails immediately — in-flight batches keep processing on Anthropic
        side but won't be reaped by this dispatcher (next start() picks
        them up if persistence survives).
        """
        self._stopping.set()
        if self._task is None:
            return
        if not drain_in_flight:
            self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, KeyboardInterrupt):
            pass
        self._task = None

    def stats(self) -> dict[str, Any]:
        return {
            "queued": len(self.queue),
            "active_batches": len(self._active),
            "batches_submitted": self.batches_submitted,
            "results_processed": self.results_processed,
            "errors_encountered": self.errors_encountered,
            "loop_iter": self._loop_iter,
        }

    # ── Loop body ───────────────────────────────────────────────

    async def _run_loop(self) -> None:
        try:
            while not self._stopping.is_set():
                self._loop_iter += 1
                # Drain queue and submit new batches if capacity available.
                if len(self._active) < self.max_concurrent_batches:
                    await self._drain_and_submit_once()

                # Poll active batches.
                await self._poll_active_once()

                # If both queue and active set are empty, wait briefly.
                if not self._active and len(self.queue) == 0:
                    await self.queue.wait_until_nonempty(
                        timeout=self.drain_idle_timeout_seconds
                    )
                else:
                    await self._sleep(self.poll_interval_seconds)

            # Drain phase: finish active batches before exit.
            while self._active:
                await self._poll_active_once()
                if self._active:
                    await self._sleep(self.poll_interval_seconds)
        except asyncio.CancelledError:
            logger.info("BatchDispatcher cancelled — abandoning %d active batches",
                        len(self._active))
            raise
        except Exception:  # noqa: BLE001
            logger.exception("BatchDispatcher loop crashed")
            self.errors_encountered += 1
            raise

    async def _drain_and_submit_once(self) -> int:
        capacity = self.max_concurrent_batches - len(self._active)
        if capacity <= 0:
            return 0

        tasks = await self.queue.drain()
        if not tasks:
            return 0

        groups = chunk_by_model_tools(tasks)
        # Limit how many groups we submit this iteration to keep within
        # max_concurrent_batches.
        submitted = 0
        for group in groups[:capacity]:
            try:
                run = await self._submit_group(group)
                self._active[run.batch_run_id] = _ActiveBatch(
                    batch_run_id=run.batch_run_id,
                    callbacks={t.task_id: t.callback for t in group.tasks},
                    submitted_at_loop_iter=self._loop_iter,
                )
                submitted += 1
                self.batches_submitted += 1
            except Exception as e:  # noqa: BLE001
                logger.exception("submit_batch failed for group model=%s tools=%s: %s",
                                 group.model, group.tools_signature, e)
                self.errors_encountered += 1
                # Notify each task in failed group via its callback (R77 — caller
                # must know batch never landed).
                for task in group.tasks:
                    if task.callback:
                        await self._safe_callback(
                            task.callback,
                            BatchResult(
                                batch_run_id="",
                                custom_id=task.task_id,
                                task_id=task.task_id,
                                status="errored",
                                error={"type": "dispatcher_submit_failed",
                                       "message": str(e)[:500]},
                            ),
                        )

        # Any group beyond capacity goes back into queue with its original
        # priority preserved.
        for group in groups[capacity:]:
            for task in group.tasks:
                await self.queue.enqueue(task)

        return submitted

    async def _submit_group(self, group: BatchGroup) -> BatchRun:
        requests = [
            BatchRequest(
                custom_id=task.task_id,
                task_id=task.task_id,
                params=task.params,
            )
            for task in group.tasks
        ]
        metadata = {
            "model": group.model,
            "tools_signature": group.tools_signature,
            "task_count": len(group.tasks),
        }
        return await self.batch_client.submit_batch(
            requests, metadata=metadata, created_by="batch-dispatcher"
        )

    async def _poll_active_once(self) -> None:
        for batch_run_id in list(self._active):
            try:
                run = await self.batch_client.poll_batch(batch_run_id)
            except Exception:  # noqa: BLE001
                logger.exception("poll_batch failed for %s", batch_run_id)
                self.errors_encountered += 1
                continue

            if run.status not in ("ended", "canceled", "expired", "failed"):
                continue

            await self._process_completed(batch_run_id)

    async def _process_completed(self, batch_run_id: str) -> None:
        active = self._active.pop(batch_run_id, None)
        if active is None:
            return
        try:
            async for result in self.batch_client.stream_results(batch_run_id):
                self.results_processed += 1
                cb = active.callbacks.get(result.task_id or result.custom_id)
                if cb:
                    await self._safe_callback(cb, result)
        except Exception:  # noqa: BLE001
            logger.exception("stream_results failed for %s", batch_run_id)
            self.errors_encountered += 1
            # Notify any pending callbacks so callers aren't stranded.
            for task_id, cb in active.callbacks.items():
                if cb:
                    await self._safe_callback(
                        cb,
                        BatchResult(
                            batch_run_id=batch_run_id,
                            custom_id=task_id,
                            task_id=task_id,
                            status="errored",
                            error={"type": "stream_results_failed"},
                        ),
                    )

    async def _safe_callback(
        self,
        cb: Callable[[BatchResult], Awaitable[None]],
        result: BatchResult,
    ) -> None:
        try:
            await cb(result)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Per-task callback failed for task=%s status=%s",
                result.task_id, result.status,
            )
            self.errors_encountered += 1


# ─── Lane router ─────────────────────────────────────────────────


async def submit_in_lane(
    *,
    lane: LaneType,
    task: BatchableTask,
    realtime_runner: Callable[[BatchableTask], Awaitable[BatchResult]] | None = None,
    dispatcher: BatchDispatcher | None = None,
) -> BatchResult | None:
    """Caller-side helper to route a task into the right lane (AB.4.4).

    `lane="realtime"` — invokes `realtime_runner(task)` directly and returns
    the result. Use for interactive / latency-sensitive paths. Caller
    typically wires `realtime_runner` to a function that calls
    `AnthropicClient.run_with_tools()` and synthesises a BatchResult.

    `lane="batch"` — enqueues to the dispatcher and returns None; the
    task's `callback` will be invoked when the batch completes.
    """
    if lane == "realtime":
        if realtime_runner is None:
            raise ValueError("lane='realtime' requires realtime_runner")
        return await realtime_runner(task)

    if dispatcher is None:
        raise ValueError("lane='batch' requires dispatcher")
    await dispatcher.enqueue(task)
    return None


async def submit_guild_task_in_lane(
    *,
    guild_id: str | Guild,
    lane: LaneType,
    task_id: str,
    params: dict[str, Any],
    task_kind: str = "generic_dev",
    callback: Callable[[BatchResult], Awaitable[None]] | None = None,
    priority: PriorityLevel = "P2",
    metadata: dict[str, Any] | None = None,
    realtime_runner: Callable[[BatchableTask], Awaitable[BatchResult]] | None = None,
    dispatcher: BatchDispatcher | None = None,
) -> BatchResult | None:
    """Guild-side client adapter for AB.4.6.

    BP.B Guild dispatch remains the caller: it builds Anthropic params
    (typically via ``AnthropicClient.simple_params()``), chooses a lane,
    and hands the task here. This helper only stamps the Guild audit
    metadata and routes through ``submit_in_lane()`` so the batch
    dispatcher stays an independent worker mode.
    """
    guild = guild_id if isinstance(guild_id, Guild) else Guild(guild_id)
    task_metadata = dict(metadata or {})
    task_metadata.update({
        "dispatch_source": "guild",
        "guild_id": guild.value,
        "task_kind": task_kind,
    })
    task = BatchableTask(
        task_id=task_id,
        params=params,
        callback=callback,
        priority=priority,
        metadata=task_metadata,
    )
    return await submit_in_lane(
        lane=lane,
        task=task,
        realtime_runner=realtime_runner,
        dispatcher=dispatcher,
    )
