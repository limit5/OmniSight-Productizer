"""AB.3 — Batch client tests with stubbed Anthropic SDK.

Locks:
  - validate_batch_limits: empty / >100K / size cap / dup custom_id /
    invalid custom_id length
  - submit_batch: persists pending → submitted, captures anthropic_batch_id,
    pre-creates per-result rows for AB.3.2 task_id mapping, fails-closed
    on submit error
  - poll_batch: maps Anthropic processing_status, mirrors request_counts,
    sets ended_at on completion
  - stream_results: yields all results regardless of per-item status
    (AB.3.6 partial-failure), populates response/error/usage,
    invokes on_result callback, persists each row
  - cancel_batch: handles both pre-submit and post-submit paths
  - find_result_for_task: AB.3.2 task_id reverse lookup
  - _async_iter: handles both sync and async iterables uniformly

ADR: docs/operations/anthropic-api-migration-and-batch-mode.md §4
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from backend.agents.batch_client import (
    BatchClient,
    BatchLimitError,
    BatchRequest,
    BatchResult,
    InMemoryBatchPersistence,
    MAX_BATCH_SIZE_BYTES,
    MAX_REQUESTS_PER_BATCH,
    estimate_request_size,
    validate_batch_limits,
)


# ─── Stub SDK shape ──────────────────────────────────────────────


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
        self.created: list[list[dict[str, Any]]] = []
        self.next_create_response: _StubBatch | Exception = _StubBatch(id="batch_stub_1")
        self.next_retrieve_response: _StubBatch | None = None
        self.next_results: list[Any] = []
        self.results_async: bool = False
        self.canceled_ids: list[str] = []

    def create(self, *, requests):  # noqa: ANN001
        self.created.append(list(requests))
        if isinstance(self.next_create_response, Exception):
            raise self.next_create_response
        return self.next_create_response

    def retrieve(self, batch_id):  # noqa: ARG002, ANN001
        return self.next_retrieve_response

    def results(self, batch_id):  # noqa: ARG002, ANN001
        if self.results_async:

            async def _aiter() -> AsyncIterator[Any]:
                for item in self.next_results:
                    yield item

            return _aiter()
        return iter(self.next_results)

    def cancel(self, batch_id):  # noqa: ANN001
        self.canceled_ids.append(batch_id)


class _StubMessagesNamespace:
    def __init__(self) -> None:
        self.batches = _StubBatchesNamespace()


# ─── validate_batch_limits ───────────────────────────────────────


def _req(custom_id: str, prompt: str = "x") -> BatchRequest:
    return BatchRequest(
        custom_id=custom_id,
        params={"messages": [{"role": "user", "content": prompt}]},
    )


def test_validate_rejects_empty():
    with pytest.raises(BatchLimitError, match="at least one"):
        validate_batch_limits([])


def test_validate_rejects_over_request_count():
    # Don't construct 100k+ real request objects; substitute a fake list.
    fake = [_req(f"id{i}") for i in range(3)]
    fake_long = fake * 50_000  # 150_000 entries
    assert len(fake_long) > MAX_REQUESTS_PER_BATCH
    with pytest.raises(BatchLimitError, match="max is"):
        validate_batch_limits(fake_long)


def test_validate_rejects_dup_custom_id():
    with pytest.raises(BatchLimitError, match="Duplicate custom_id"):
        validate_batch_limits([_req("a"), _req("b"), _req("a")])


def test_validate_rejects_bad_custom_id_length():
    with pytest.raises(BatchLimitError, match="length must be"):
        validate_batch_limits([BatchRequest(custom_id="", params={})])
    with pytest.raises(BatchLimitError, match="length must be"):
        validate_batch_limits([BatchRequest(custom_id="x" * 65, params={})])


def test_validate_rejects_mixed_tenant_ids():
    with pytest.raises(BatchLimitError, match="cannot mix tenant_id"):
        validate_batch_limits([
            BatchRequest(custom_id="a", params={}, tenant_id="tenant-a"),
            BatchRequest(custom_id="b", params={}, tenant_id="tenant-b"),
        ])


def test_validate_rejects_oversize_payload():
    big_str = "a" * (MAX_BATCH_SIZE_BYTES // 2)
    requests = [
        BatchRequest(custom_id="big1", params={"prompt": big_str}),
        BatchRequest(custom_id="big2", params={"prompt": big_str}),
        BatchRequest(custom_id="big3", params={"prompt": big_str}),
    ]
    with pytest.raises(BatchLimitError, match="exceeds"):
        validate_batch_limits(requests)


def test_validate_returns_total_size():
    requests = [_req("a"), _req("b")]
    size = validate_batch_limits(requests)
    assert size == sum(estimate_request_size(r.params) for r in requests)


# ─── BatchClient.submit_batch ────────────────────────────────────


@pytest.mark.asyncio
async def test_submit_batch_persists_and_calls_sdk():
    sdk_msgs = _StubMessagesNamespace()
    sdk_msgs.batches.next_create_response = _StubBatch(
        id="batch_anthropic_xyz", processing_status="in_progress"
    )
    persistence = InMemoryBatchPersistence()
    client = BatchClient(sdk_msgs, persistence=persistence)

    requests = [
        BatchRequest(custom_id="c1", params={"messages": [{"role": "user", "content": "a"}]}, task_id="t1"),
        BatchRequest(custom_id="c2", params={"messages": [{"role": "user", "content": "b"}]}, task_id="t2"),
    ]

    run = await client.submit_batch(requests, metadata={"phase": "HD.1"}, created_by="agent-bot")

    assert run.status == "submitted"
    assert run.anthropic_batch_id == "batch_anthropic_xyz"
    assert run.request_count == 2
    assert run.metadata == {"phase": "HD.1"}
    assert run.created_by == "agent-bot"
    assert run.submitted_at is not None

    # SDK called with the right shape
    assert len(sdk_msgs.batches.created) == 1
    payload = sdk_msgs.batches.created[0]
    assert {p["custom_id"] for p in payload} == {"c1", "c2"}

    # All results pre-persisted in pending status with task_id mapping
    pending = await persistence.list_batch_results(run.batch_run_id)
    assert {r.custom_id: (r.status, r.task_id) for r in pending} == {
        "c1": ("pending", "t1"),
        "c2": ("pending", "t2"),
    }


@pytest.mark.asyncio
async def test_submit_batch_preserves_tenant_id_mapping():
    """R80 — per-result task mapping carries tenant identity."""
    sdk_msgs = _StubMessagesNamespace()
    persistence = InMemoryBatchPersistence()
    client = BatchClient(sdk_msgs, persistence=persistence)

    run = await client.submit_batch([
        BatchRequest(
            custom_id="c1",
            params={"messages": [{"role": "user", "content": "a"}]},
            task_id="same-task",
            tenant_id="tenant-a",
        ),
        BatchRequest(
            custom_id="c2",
            params={"messages": [{"role": "user", "content": "b"}]},
            task_id="same-task",
            tenant_id="tenant-a",
        ),
    ])

    assert run.tenant_id == "tenant-a"
    pending = await persistence.list_batch_results(run.batch_run_id)
    assert {r.custom_id: r.tenant_id for r in pending} == {
        "c1": "tenant-a",
        "c2": "tenant-a",
    }
    found = await client.find_result_for_task("same-task", tenant_id="tenant-a")
    assert found is not None
    assert found.tenant_id == "tenant-a"
    assert await client.find_result_for_task("same-task", tenant_id="tenant-b") is None


@pytest.mark.asyncio
async def test_submit_batch_marks_failed_on_sdk_error():
    sdk_msgs = _StubMessagesNamespace()
    sdk_msgs.batches.next_create_response = RuntimeError("anthropic 503")
    persistence = InMemoryBatchPersistence()
    client = BatchClient(sdk_msgs, persistence=persistence)

    with pytest.raises(RuntimeError, match="anthropic 503"):
        await client.submit_batch([_req("c1")])

    runs = await persistence.list_batch_runs()
    assert len(runs) == 1
    assert runs[0].status == "failed"
    assert "anthropic 503" in runs[0].metadata["submit_error"]


@pytest.mark.asyncio
async def test_submit_batch_validates_limits_before_call():
    """Limit error raises BEFORE any SDK call (no batches.created appended)."""
    sdk_msgs = _StubMessagesNamespace()
    persistence = InMemoryBatchPersistence()
    client = BatchClient(sdk_msgs, persistence=persistence)

    with pytest.raises(BatchLimitError):
        await client.submit_batch([])

    assert sdk_msgs.batches.created == []
    assert await persistence.list_batch_runs() == []


# ─── BatchClient.poll_batch ──────────────────────────────────────


@pytest.mark.asyncio
async def test_poll_batch_maps_status_and_counts():
    sdk_msgs = _StubMessagesNamespace()
    sdk_msgs.batches.next_create_response = _StubBatch(id="b_a")
    persistence = InMemoryBatchPersistence()
    client = BatchClient(sdk_msgs, persistence=persistence)
    run = await client.submit_batch([_req("c1"), _req("c2"), _req("c3")])

    sdk_msgs.batches.next_retrieve_response = _StubBatch(
        id="b_a",
        processing_status="ended",
        request_counts={"succeeded": 2, "errored": 1, "canceled": 0, "expired": 0},
    )
    polled = await client.poll_batch(run.batch_run_id)
    assert polled.status == "ended"
    assert polled.success_count == 2
    assert polled.error_count == 1
    assert polled.ended_at is not None


@pytest.mark.asyncio
async def test_poll_batch_unknown_id_raises():
    client = BatchClient(_StubMessagesNamespace())
    with pytest.raises(KeyError):
        await client.poll_batch("br_nope")


# ─── BatchClient.stream_results ──────────────────────────────────


@pytest.mark.asyncio
async def test_stream_results_yields_succeeded_with_text_and_usage():
    sdk_msgs = _StubMessagesNamespace()
    sdk_msgs.batches.next_create_response = _StubBatch(id="b_a")
    persistence = InMemoryBatchPersistence()
    client = BatchClient(sdk_msgs, persistence=persistence)
    run = await client.submit_batch([_req("c1", "hello"), _req("c2", "world")])

    sdk_msgs.batches.next_results = [
        {
            "custom_id": "c1",
            "result": {
                "type": "succeeded",
                "message": {
                    "content": [{"type": "text", "text": "answer 1"}],
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "cache_read_input_tokens": 8,
                        "cache_creation_input_tokens": 0,
                    },
                },
            },
        },
        {
            "custom_id": "c2",
            "result": {
                "type": "succeeded",
                "message": {
                    "content": [{"type": "text", "text": "answer 2"}],
                    "usage": {"input_tokens": 12, "output_tokens": 7},
                },
            },
        },
    ]

    yielded: list[BatchResult] = []
    async for r in client.stream_results(run.batch_run_id):
        yielded.append(r)

    assert {r.custom_id for r in yielded} == {"c1", "c2"}
    by_id = {r.custom_id: r for r in yielded}
    assert by_id["c1"].status == "succeeded"
    assert by_id["c1"].final_text == "answer 1"
    assert by_id["c1"].input_tokens == 10
    assert by_id["c1"].cache_read_tokens == 8
    assert by_id["c2"].final_text == "answer 2"

    # Persistence reflects final state
    persisted = await persistence.list_batch_results(run.batch_run_id)
    statuses = {r.custom_id: r.status for r in persisted}
    assert statuses == {"c1": "succeeded", "c2": "succeeded"}


@pytest.mark.asyncio
async def test_stream_results_partial_failure_yields_all(monkeypatch):
    """AB.3.6 — succeeded + errored + expired all surface independently."""
    sdk_msgs = _StubMessagesNamespace()
    sdk_msgs.batches.next_create_response = _StubBatch(id="b_a")
    persistence = InMemoryBatchPersistence()
    client = BatchClient(sdk_msgs, persistence=persistence)
    run = await client.submit_batch(
        [_req("ok"), _req("bad"), _req("late")]
    )

    sdk_msgs.batches.next_results = [
        {
            "custom_id": "ok",
            "result": {
                "type": "succeeded",
                "message": {
                    "content": [{"type": "text", "text": "all good"}],
                    "usage": {"input_tokens": 5, "output_tokens": 2},
                },
            },
        },
        {
            "custom_id": "bad",
            "result": {
                "type": "errored",
                "error": {"type": "invalid_request_error", "message": "borked"},
            },
        },
        {
            "custom_id": "late",
            "result": {"type": "expired"},
        },
    ]

    by_id = {}
    async for r in client.stream_results(run.batch_run_id):
        by_id[r.custom_id] = r

    assert by_id["ok"].status == "succeeded"
    assert by_id["ok"].final_text == "all good"
    assert by_id["bad"].status == "errored"
    assert by_id["bad"].error == {"type": "invalid_request_error", "message": "borked"}
    assert by_id["late"].status == "expired"
    # Expired / errored entries have no usage data
    assert by_id["late"].input_tokens == 0


@pytest.mark.asyncio
async def test_stream_results_invokes_callback_per_result():
    sdk_msgs = _StubMessagesNamespace()
    sdk_msgs.batches.next_create_response = _StubBatch(id="b_a")
    client = BatchClient(sdk_msgs)
    run = await client.submit_batch([_req("c1"), _req("c2")])

    sdk_msgs.batches.next_results = [
        {"custom_id": "c1", "result": {"type": "succeeded", "message": {"content": []}}},
        {"custom_id": "c2", "result": {"type": "succeeded", "message": {"content": []}}},
    ]

    seen: list[str] = []

    async def cb(result: BatchResult) -> None:
        seen.append(result.custom_id)

    async for _ in client.stream_results(run.batch_run_id, on_result=cb):
        pass

    assert seen == ["c1", "c2"]


@pytest.mark.asyncio
async def test_stream_results_handles_async_iterable():
    """AsyncAnthropic SDK returns an async iterator; BatchClient must accept either."""
    sdk_msgs = _StubMessagesNamespace()
    sdk_msgs.batches.next_create_response = _StubBatch(id="b_a")
    sdk_msgs.batches.results_async = True  # toggle async iter
    sdk_msgs.batches.next_results = [
        {"custom_id": "c1", "result": {"type": "succeeded", "message": {"content": []}}}
    ]
    client = BatchClient(sdk_msgs)
    run = await client.submit_batch([_req("c1")])

    yielded = [r async for r in client.stream_results(run.batch_run_id)]
    assert len(yielded) == 1
    assert yielded[0].custom_id == "c1"


# ─── cancel_batch + find_result_for_task ──────────────────────────


@pytest.mark.asyncio
async def test_cancel_batch_post_submit():
    sdk_msgs = _StubMessagesNamespace()
    sdk_msgs.batches.next_create_response = _StubBatch(id="b_a")
    client = BatchClient(sdk_msgs)
    run = await client.submit_batch([_req("c1")])
    canceled = await client.cancel_batch(run.batch_run_id)
    assert canceled.status == "canceled"
    assert sdk_msgs.batches.canceled_ids == ["b_a"]


@pytest.mark.asyncio
async def test_cancel_batch_pre_submit_no_sdk_call():
    """Canceling a pending (never-submitted) batch must not call SDK.cancel."""
    sdk_msgs = _StubMessagesNamespace()
    persistence = InMemoryBatchPersistence()
    # Put a fake pending row in directly.
    from backend.agents.batch_client import BatchRun

    pending = BatchRun(
        batch_run_id="br_local", status="pending", request_count=1
    )
    await persistence.save_batch_run(pending)
    client = BatchClient(sdk_msgs, persistence=persistence)
    canceled = await client.cancel_batch("br_local")
    assert canceled.status == "canceled"
    assert sdk_msgs.batches.canceled_ids == []


@pytest.mark.asyncio
async def test_find_result_for_task_via_task_id_mapping():
    """AB.3.2 — task_id reverse lookup across batch_run_id + custom_id."""
    sdk_msgs = _StubMessagesNamespace()
    sdk_msgs.batches.next_create_response = _StubBatch(id="b_a")
    persistence = InMemoryBatchPersistence()
    client = BatchClient(sdk_msgs, persistence=persistence)

    requests = [
        BatchRequest(
            custom_id="c1",
            task_id="omnisight_task_42",
            params={"messages": [{"role": "user", "content": "x"}]},
        )
    ]
    run = await client.submit_batch(requests)

    # Before completion: pending row still findable
    pending = await client.find_result_for_task("omnisight_task_42")
    assert pending is not None
    assert pending.batch_run_id == run.batch_run_id
    assert pending.custom_id == "c1"
    assert pending.status == "pending"

    # After completion: status updated, lookup still works
    sdk_msgs.batches.next_results = [
        {
            "custom_id": "c1",
            "result": {
                "type": "succeeded",
                "message": {
                    "content": [{"type": "text", "text": "done"}],
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            },
        }
    ]
    async for _ in client.stream_results(run.batch_run_id):
        pass
    completed = await client.find_result_for_task("omnisight_task_42")
    assert completed is not None
    assert completed.status == "succeeded"
    assert completed.final_text == "done"


@pytest.mark.asyncio
async def test_find_result_for_unknown_task_returns_none():
    client = BatchClient(_StubMessagesNamespace())
    assert await client.find_result_for_task("never_existed") is None
