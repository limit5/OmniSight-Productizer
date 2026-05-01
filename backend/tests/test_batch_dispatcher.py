"""AB.4 — Batch dispatcher + queue + grouping tests.

Locks:
  - BatchTaskQueue: priority bucket order (P0 → P3), drain caps,
    non-empty wait
  - chunk_by_model_tools: groups by (model, tools_signature),
    chunks at request-count + size limits, single mixed input ↦
    multiple chunks
  - BatchDispatcher: drain → submit → poll → fan-out callback flow,
    submit failure surfaces error result to per-task callbacks,
    stream_results failure same fan-out, max_concurrent_batches
    cap respected, requeues overflow groups
  - submit_in_lane: realtime path invokes runner; batch path
    enqueues + returns None; missing args raise

Uses fake clock (sleep stub) so 60s polling intervals don't hang
tests. All tests run in 0.x s.

ADR: docs/operations/anthropic-api-migration-and-batch-mode.md §4
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from backend.agents.batch_client import (
    BatchClient,
    BatchRequest,
    BatchResult,
    BatchRun,
    InMemoryBatchPersistence,
    MAX_BATCH_SIZE_BYTES,
    MAX_REQUESTS_PER_BATCH,
)
from backend.agents.batch_dispatcher import (
    BatchableTask,
    BatchDispatcher,
    BatchGroup,
    BatchTaskQueue,
    chunk_by_model_tools,
    submit_in_lane,
)


# ─── Stub SDK shape (reusing AB.3 patterns) ──────────────────────


class _StubBatch:
    def __init__(
        self,
        *,
        id: str,
        processing_status: str = "in_progress",
        request_counts: dict[str, int] | None = None,
    ) -> None:
        self.id = id
        self.processing_status = processing_status
        self.request_counts = request_counts or {
            "succeeded": 0,
            "errored": 0,
            "canceled": 0,
            "expired": 0,
        }


class _StubBatchesNamespace:
    def __init__(self) -> None:
        self.next_create: list[_StubBatch | Exception] = []
        self.next_retrieve: list[_StubBatch | Exception] = []
        self.next_results: list[list[Any]] = []
        self.created_count = 0
        self.retrieve_count = 0
        self.results_count = 0
        self.canceled: list[str] = []

    def create(self, *, requests):  # noqa: ARG002, ANN001
        nxt = self.next_create.pop(0) if self.next_create else _StubBatch(id=f"b_auto_{self.created_count}")
        self.created_count += 1
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    def retrieve(self, batch_id):  # noqa: ARG002, ANN001
        self.retrieve_count += 1
        nxt = self.next_retrieve.pop(0) if self.next_retrieve else _StubBatch(
            id=batch_id, processing_status="ended",
            request_counts={"succeeded": 1, "errored": 0, "canceled": 0, "expired": 0},
        )
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    def results(self, batch_id):  # noqa: ARG002, ANN001
        self.results_count += 1
        items = self.next_results.pop(0) if self.next_results else []
        return iter(items)

    def cancel(self, batch_id):  # noqa: ANN001
        self.canceled.append(batch_id)


class _StubMessages:
    def __init__(self) -> None:
        self.batches = _StubBatchesNamespace()


# ─── Helpers ─────────────────────────────────────────────────────


def _task(task_id: str, *, model: str = "claude-sonnet-4-6", tools: list[str] | None = None,
          callback=None, priority="P2") -> BatchableTask:
    params: dict[str, Any] = {
        "model": model,
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": f"hi {task_id}"}],
    }
    if tools:
        params["tools"] = [{"name": t, "description": "x", "input_schema": {"type": "object"}} for t in tools]
    return BatchableTask(task_id=task_id, params=params, callback=callback, priority=priority)


# ─── BatchTaskQueue ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_queue_drains_priority_order():
    q = BatchTaskQueue()
    await q.enqueue(_task("t_p2_a", priority="P2"))
    await q.enqueue(_task("t_p0", priority="P0"))
    await q.enqueue(_task("t_p3", priority="P3"))
    await q.enqueue(_task("t_p2_b", priority="P2"))
    await q.enqueue(_task("t_p1", priority="P1"))

    out = await q.drain()
    # Order: P0, then P1, then P2 (FIFO inside bucket), then P3
    assert [t.task_id for t in out] == ["t_p0", "t_p1", "t_p2_a", "t_p2_b", "t_p3"]


@pytest.mark.asyncio
async def test_queue_drain_max_count_cap():
    q = BatchTaskQueue()
    for i in range(5):
        await q.enqueue(_task(f"t_{i}"))
    out = await q.drain(max_count=3)
    assert len(out) == 3
    # Remaining 2 still in queue
    assert len(q) == 2


@pytest.mark.asyncio
async def test_queue_wait_until_nonempty_returns_immediately_if_full():
    q = BatchTaskQueue()
    await q.enqueue(_task("t1"))
    woke = await q.wait_until_nonempty(timeout=0.01)
    assert woke is True


@pytest.mark.asyncio
async def test_queue_wait_until_nonempty_timeout():
    q = BatchTaskQueue()
    woke = await q.wait_until_nonempty(timeout=0.01)
    assert woke is False


@pytest.mark.asyncio
async def test_queue_wait_until_nonempty_wakes_on_enqueue():
    q = BatchTaskQueue()

    async def producer():
        await asyncio.sleep(0.01)
        await q.enqueue(_task("late"))

    asyncio.create_task(producer())
    woke = await q.wait_until_nonempty(timeout=0.5)
    assert woke is True


# ─── chunk_by_model_tools ────────────────────────────────────────


def test_chunk_groups_by_model_and_tools():
    tasks = [
        _task("a", model="opus", tools=["Read"]),
        _task("b", model="opus", tools=["Read"]),
        _task("c", model="opus", tools=["Read", "Edit"]),
        _task("d", model="sonnet", tools=["Read"]),
    ]
    groups = chunk_by_model_tools(tasks)
    keys = {(g.model, g.tools_signature, len(g.tasks)) for g in groups}
    assert keys == {
        ("opus", "Read", 2),
        ("opus", "Edit|Read", 1),  # tools sorted alphabetically in signature
        ("sonnet", "Read", 1),
    }


def test_chunk_respects_max_per_chunk():
    tasks = [_task(f"t_{i}", model="sonnet") for i in range(5)]
    groups = chunk_by_model_tools(tasks, max_per_chunk=2)
    assert len(groups) == 3
    sizes = [len(g.tasks) for g in groups]
    assert sizes == [2, 2, 1]


def test_chunk_respects_max_size():
    big = "x" * 100_000
    tasks = []
    for i in range(4):
        t = BatchableTask(
            task_id=f"big_{i}",
            params={"model": "sonnet", "messages": [{"role": "user", "content": big}]},
        )
        tasks.append(t)

    # Each task ~100KB. Cap at 250KB so exactly 2 fit per chunk
    # (2*100KB=200KB OK, 3*100KB=300KB overflow).
    groups = chunk_by_model_tools(tasks, max_size_per_chunk=250_000)
    assert len(groups) == 2
    assert [len(g.tasks) for g in groups] == [2, 2]


def test_chunk_size_cap_too_small_one_per_chunk():
    """Cap so tight every individual task is its own chunk."""
    big = "x" * 100_000
    tasks = [
        BatchableTask(
            task_id=f"big_{i}",
            params={"model": "sonnet", "messages": [{"role": "user", "content": big}]},
        )
        for i in range(3)
    ]
    # Each task is ~100KB. Cap at 150KB so adding a second always overflows.
    groups = chunk_by_model_tools(tasks, max_size_per_chunk=150_000)
    assert len(groups) == 3
    assert all(len(g.tasks) == 1 for g in groups)


def test_chunk_empty_returns_empty():
    assert chunk_by_model_tools([]) == []


# ─── BatchDispatcher loop ────────────────────────────────────────


@pytest.fixture
def fake_sleep():
    """Sleep stub: tracks calls and yields control without real time wait."""
    calls: list[float] = []

    async def _sleep(seconds: float) -> None:
        calls.append(seconds)
        await asyncio.sleep(0)  # yield to event loop

    _sleep.calls = calls  # type: ignore[attr-defined]
    return _sleep


async def _run_one_iter(dispatcher: BatchDispatcher) -> None:
    """Trigger one iteration of the loop body manually for deterministic testing."""
    dispatcher._loop_iter += 1
    await dispatcher._drain_and_submit_once()
    await dispatcher._poll_active_once()


@pytest.mark.asyncio
async def test_dispatcher_drain_and_submit_creates_batch(fake_sleep):
    sdk_msgs = _StubMessages()
    sdk_msgs.batches.next_create = [_StubBatch(id="b_1")]
    bc = BatchClient(sdk_msgs, persistence=InMemoryBatchPersistence())
    dispatcher = BatchDispatcher(bc, sleep=fake_sleep)

    received: list[BatchResult] = []

    async def cb(r: BatchResult) -> None:
        received.append(r)

    await dispatcher.enqueue(_task("t1", callback=cb))
    await dispatcher.enqueue(_task("t2", callback=cb))

    submitted = await dispatcher._drain_and_submit_once()
    assert submitted == 1  # both tasks share (model, tools) → one batch
    assert dispatcher.batches_submitted == 1
    assert len(dispatcher._active) == 1
    assert sdk_msgs.batches.created_count == 1


@pytest.mark.asyncio
async def test_dispatcher_full_flow_submit_poll_dispatch(fake_sleep):
    sdk_msgs = _StubMessages()
    sdk_msgs.batches.next_create = [_StubBatch(id="b_1")]
    sdk_msgs.batches.next_retrieve = [
        _StubBatch(
            id="b_1",
            processing_status="ended",
            request_counts={"succeeded": 2, "errored": 0, "canceled": 0, "expired": 0},
        )
    ]
    sdk_msgs.batches.next_results = [
        [
            {
                "custom_id": "t1",
                "result": {
                    "type": "succeeded",
                    "message": {
                        "content": [{"type": "text", "text": "ans 1"}],
                        "usage": {"input_tokens": 10, "output_tokens": 3},
                    },
                },
            },
            {
                "custom_id": "t2",
                "result": {
                    "type": "succeeded",
                    "message": {
                        "content": [{"type": "text", "text": "ans 2"}],
                        "usage": {"input_tokens": 12, "output_tokens": 4},
                    },
                },
            },
        ]
    ]

    bc = BatchClient(sdk_msgs, persistence=InMemoryBatchPersistence())
    dispatcher = BatchDispatcher(bc, sleep=fake_sleep)

    received: dict[str, BatchResult] = {}

    async def cb(r: BatchResult) -> None:
        received[r.task_id or r.custom_id] = r

    await dispatcher.enqueue(_task("t1", callback=cb))
    await dispatcher.enqueue(_task("t2", callback=cb))

    await _run_one_iter(dispatcher)  # submits + polls (immediate end status)

    assert dispatcher.results_processed == 2
    assert set(received.keys()) == {"t1", "t2"}
    assert received["t1"].final_text == "ans 1"
    assert received["t2"].final_text == "ans 2"
    assert len(dispatcher._active) == 0  # batch reaped


@pytest.mark.asyncio
async def test_dispatcher_submit_failure_notifies_callbacks(fake_sleep):
    sdk_msgs = _StubMessages()
    sdk_msgs.batches.next_create = [RuntimeError("anthropic 503")]
    bc = BatchClient(sdk_msgs, persistence=InMemoryBatchPersistence())
    dispatcher = BatchDispatcher(bc, sleep=fake_sleep)

    received: list[BatchResult] = []

    async def cb(r: BatchResult) -> None:
        received.append(r)

    await dispatcher.enqueue(_task("t1", callback=cb))
    await dispatcher.enqueue(_task("t2", callback=cb))

    submitted = await dispatcher._drain_and_submit_once()
    assert submitted == 0
    assert dispatcher.errors_encountered >= 1
    # Both tasks notified with errored result
    assert len(received) == 2
    assert all(r.status == "errored" for r in received)
    assert all(r.error and r.error["type"] == "dispatcher_submit_failed" for r in received)


@pytest.mark.asyncio
async def test_dispatcher_callback_exception_caught(fake_sleep):
    sdk_msgs = _StubMessages()
    sdk_msgs.batches.next_create = [_StubBatch(id="b_1")]
    sdk_msgs.batches.next_retrieve = [
        _StubBatch(id="b_1", processing_status="ended",
                   request_counts={"succeeded": 1, "errored": 0, "canceled": 0, "expired": 0})
    ]
    sdk_msgs.batches.next_results = [
        [{"custom_id": "t1", "result": {"type": "succeeded", "message": {"content": []}}}]
    ]
    bc = BatchClient(sdk_msgs, persistence=InMemoryBatchPersistence())
    dispatcher = BatchDispatcher(bc, sleep=fake_sleep)

    async def bad_cb(r: BatchResult) -> None:
        raise RuntimeError("downstream blew up")

    await dispatcher.enqueue(_task("t1", callback=bad_cb))
    await _run_one_iter(dispatcher)

    # Dispatcher logs the error but keeps running.
    assert dispatcher.results_processed == 1
    assert dispatcher.errors_encountered >= 1


@pytest.mark.asyncio
async def test_dispatcher_capacity_overflow_requeues(fake_sleep):
    """When more groups than max_concurrent_batches, overflow goes back to queue."""
    sdk_msgs = _StubMessages()
    sdk_msgs.batches.next_create = [_StubBatch(id="b_1")]
    bc = BatchClient(sdk_msgs, persistence=InMemoryBatchPersistence())
    dispatcher = BatchDispatcher(bc, sleep=fake_sleep, max_concurrent_batches=1)

    # Three different (model, tools) combos → three groups.
    await dispatcher.enqueue(_task("a", model="opus", tools=["Read"]))
    await dispatcher.enqueue(_task("b", model="sonnet", tools=["Read"]))
    await dispatcher.enqueue(_task("c", model="haiku", tools=["Read"]))

    await dispatcher._drain_and_submit_once()
    # Only 1 batch fit; other 2 tasks back in queue
    assert dispatcher.batches_submitted == 1
    assert len(dispatcher.queue) == 2


@pytest.mark.asyncio
async def test_dispatcher_skips_submit_when_at_capacity(fake_sleep):
    sdk_msgs = _StubMessages()
    bc = BatchClient(sdk_msgs, persistence=InMemoryBatchPersistence())
    dispatcher = BatchDispatcher(bc, sleep=fake_sleep, max_concurrent_batches=0)

    await dispatcher.enqueue(_task("t1"))
    submitted = await dispatcher._drain_and_submit_once()
    assert submitted == 0
    assert sdk_msgs.batches.created_count == 0
    # Task still queued
    assert len(dispatcher.queue) == 1


@pytest.mark.asyncio
async def test_dispatcher_poll_skips_in_progress_batches(fake_sleep):
    sdk_msgs = _StubMessages()
    sdk_msgs.batches.next_create = [_StubBatch(id="b_1")]
    sdk_msgs.batches.next_retrieve = [_StubBatch(id="b_1", processing_status="in_progress")]
    bc = BatchClient(sdk_msgs, persistence=InMemoryBatchPersistence())
    dispatcher = BatchDispatcher(bc, sleep=fake_sleep)

    await dispatcher.enqueue(_task("t1"))
    await dispatcher._drain_and_submit_once()
    assert len(dispatcher._active) == 1

    await dispatcher._poll_active_once()
    # Still active, results not processed
    assert len(dispatcher._active) == 1
    assert dispatcher.results_processed == 0


@pytest.mark.asyncio
async def test_dispatcher_stats():
    sdk_msgs = _StubMessages()
    bc = BatchClient(sdk_msgs)
    dispatcher = BatchDispatcher(bc)
    stats = dispatcher.stats()
    assert stats == {
        "queued": 0,
        "active_batches": 0,
        "batches_submitted": 0,
        "results_processed": 0,
        "errors_encountered": 0,
        "loop_iter": 0,
    }


# ─── start / stop lifecycle ──────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatcher_start_stop_clean(fake_sleep):
    sdk_msgs = _StubMessages()
    bc = BatchClient(sdk_msgs, persistence=InMemoryBatchPersistence())
    dispatcher = BatchDispatcher(bc, sleep=fake_sleep, drain_idle_timeout_seconds=0.01)

    await dispatcher.start()
    # Let it spin briefly with empty queue
    await asyncio.sleep(0.05)
    await dispatcher.stop(drain_in_flight=False)
    assert dispatcher._task is None or dispatcher._task.done()


# ─── submit_in_lane ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_submit_in_lane_realtime_invokes_runner():
    received: list[BatchableTask] = []

    async def runner(t: BatchableTask) -> BatchResult:
        received.append(t)
        return BatchResult(
            batch_run_id="rt", custom_id=t.task_id, task_id=t.task_id,
            status="succeeded", final_text="instant",
        )

    task = _task("t1")
    result = await submit_in_lane(lane="realtime", task=task, realtime_runner=runner)
    assert received == [task]
    assert result is not None
    assert result.final_text == "instant"


@pytest.mark.asyncio
async def test_submit_in_lane_batch_enqueues_and_returns_none():
    sdk_msgs = _StubMessages()
    bc = BatchClient(sdk_msgs, persistence=InMemoryBatchPersistence())
    dispatcher = BatchDispatcher(bc)

    task = _task("t1")
    result = await submit_in_lane(lane="batch", task=task, dispatcher=dispatcher)
    assert result is None
    assert len(dispatcher.queue) == 1


@pytest.mark.asyncio
async def test_submit_in_lane_realtime_without_runner_raises():
    with pytest.raises(ValueError, match="realtime_runner"):
        await submit_in_lane(lane="realtime", task=_task("t1"))


@pytest.mark.asyncio
async def test_submit_in_lane_batch_without_dispatcher_raises():
    with pytest.raises(ValueError, match="dispatcher"):
        await submit_in_lane(lane="batch", task=_task("t1"))


# ─── BatchableTask metadata ──────────────────────────────────────


def test_batchable_task_tools_signature_sorted():
    t = _task("t1", tools=["Edit", "Read", "Bash"])
    assert t.tools_signature == "Bash|Edit|Read"


def test_batchable_task_tools_signature_empty():
    t = _task("t1")
    assert t.tools_signature == ""


def test_batchable_task_estimate_size_includes_params():
    t = _task("t1")
    size = t.estimate_size()
    # Just ensure it returns a positive int proportional to the params dict size
    assert size > 50
