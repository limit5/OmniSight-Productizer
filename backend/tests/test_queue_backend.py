"""O2 (#265) — Message Queue abstraction tests.

Targets the in-memory backend (Redis backend shares observable
semantics; a separate `pytest -m redis` suite can target a real Redis
container by setting ``OMNISIGHT_REDIS_URL``).
"""

from __future__ import annotations

import threading
import time

import pytest

from backend import queue_backend as qb
from backend.catc import TaskCard
from backend.queue_backend import (
    MAX_DELIVERIES,
    InMemoryQueueBackend,
    InvalidStateTransition,
    MessageNotFound,
    PriorityLevel,
    QueueMessage,
    TaskState,
    ack,
    depth,
    dlq_list,
    dlq_purge,
    dlq_redrive,
    format_exc,
    get,
    nack,
    pull,
    push,
    set_backend_for_tests,
    set_state,
    sweep_visibility,
)


# ──────────────────────────────────────────────────────────────
#  Fixtures
# ──────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _fresh_backend():
    set_backend_for_tests(InMemoryQueueBackend())
    yield
    set_backend_for_tests(None)


def _make_card(ticket: str = "PROJ-1",
               allowed: list[str] | None = None) -> TaskCard:
    return TaskCard.from_dict({
        "jira_ticket": ticket,
        "acceptance_criteria": "criteria",
        "navigation": {
            "entry_point": "src/main.c",
            "impact_scope": {
                "allowed": allowed or ["src/main.c"],
                "forbidden": [],
            },
        },
    })


# ──────────────────────────────────────────────────────────────
#  1. Enums + state-machine guard
# ──────────────────────────────────────────────────────────────


class TestEnumsAndStateMachine:
    def test_priority_rank_order(self):
        assert PriorityLevel.P0.rank < PriorityLevel.P1.rank
        assert PriorityLevel.P1.rank < PriorityLevel.P2.rank
        assert PriorityLevel.P2.rank < PriorityLevel.P3.rank

    def test_priority_ordered_returns_p0_first(self):
        assert PriorityLevel.ordered() == [
            PriorityLevel.P0, PriorityLevel.P1,
            PriorityLevel.P2, PriorityLevel.P3,
        ]

    def test_state_enum_includes_spec_values(self):
        names = {s.value for s in TaskState}
        # spec: Queued → Blocked_by_Mutex → Ready → Claimed → Running → Done/Failed
        assert {"Queued", "Blocked_by_Mutex", "Ready", "Claimed",
                "Running", "Done", "Failed"} <= names

    def test_state_machine_legal_edge(self):
        # Queued -> Ready is allowed
        qb._check_transition(TaskState.Queued, TaskState.Ready)

    def test_state_machine_illegal_edge_raises(self):
        # Done is terminal
        with pytest.raises(InvalidStateTransition):
            qb._check_transition(TaskState.Done, TaskState.Queued)

    def test_state_machine_self_edge_noop(self):
        qb._check_transition(TaskState.Ready, TaskState.Ready)


# ──────────────────────────────────────────────────────────────
#  2. Push / pull / ack
# ──────────────────────────────────────────────────────────────


class TestPushPullAck:
    def test_push_returns_message_id(self):
        mid = push(_make_card(), PriorityLevel.P2)
        assert mid.startswith("msg-")
        assert depth() == 1
        assert depth(priority=PriorityLevel.P2, state=TaskState.Queued) == 1

    def test_pull_claims_message_and_advances_state(self):
        mid = push(_make_card())
        msgs = pull("worker-1", count=1, visibility_timeout_s=10)
        assert len(msgs) == 1
        assert msgs[0].message_id == mid
        assert msgs[0].state == TaskState.Claimed
        assert msgs[0].claim_owner == "worker-1"
        assert msgs[0].delivery_count == 1
        assert msgs[0].claim_deadline > time.time()

    def test_ack_removes_message(self):
        mid = push(_make_card())
        pull("w", count=1, visibility_timeout_s=10)
        assert ack(mid) is True
        assert depth() == 0
        assert get(mid) is None

    def test_ack_unknown_returns_false(self):
        assert ack("msg-deadbeef") is False

    def test_pull_count_zero_returns_empty(self):
        push(_make_card())
        assert pull("w", count=0, visibility_timeout_s=10) == []

    def test_pull_with_no_messages_returns_empty(self):
        assert pull("w", count=5, visibility_timeout_s=10) == []

    def test_push_rejects_non_taskcard(self):
        with pytest.raises(TypeError):
            push({"jira_ticket": "PROJ-1"}, PriorityLevel.P2)  # type: ignore[arg-type]

    def test_push_rejects_non_priority(self):
        with pytest.raises(TypeError):
            push(_make_card(), "P0")  # type: ignore[arg-type]

    def test_pull_requires_consumer(self):
        with pytest.raises(ValueError):
            pull("", count=1)

    def test_round_trip_payload_preserved(self):
        card = _make_card(allowed=["src/a.c", "src/b.c"])
        mid = push(card, PriorityLevel.P1)
        m = get(mid)
        assert m is not None
        # Payload round-trips via TaskCard
        rebuilt = m.task_card()
        assert rebuilt.jira_ticket == "PROJ-1"
        assert rebuilt.navigation.impact_scope.allowed == ["src/a.c", "src/b.c"]


# ──────────────────────────────────────────────────────────────
#  3. Priority ordering
# ──────────────────────────────────────────────────────────────


class TestPriorityOrdering:
    def test_p0_drained_before_p3(self):
        m_p3 = push(_make_card(ticket="PROJ-3"), PriorityLevel.P3)
        m_p0 = push(_make_card(ticket="PROJ-1"), PriorityLevel.P0)
        m_p2 = push(_make_card(ticket="PROJ-2"), PriorityLevel.P2)
        msgs = pull("w", count=3, visibility_timeout_s=10)
        assert [m.message_id for m in msgs] == [m_p0, m_p2, m_p3]
        assert [m.priority for m in msgs] == [
            PriorityLevel.P0, PriorityLevel.P2, PriorityLevel.P3,
        ]

    def test_fifo_within_same_priority(self):
        first = push(_make_card(ticket="PROJ-1"), PriorityLevel.P2)
        second = push(_make_card(ticket="PROJ-2"), PriorityLevel.P2)
        third = push(_make_card(ticket="PROJ-3"), PriorityLevel.P2)
        msgs = pull("w", count=3, visibility_timeout_s=10)
        assert [m.message_id for m in msgs] == [first, second, third]

    def test_p3_does_not_starve_p0_added_later(self):
        # P3 enqueued first.
        push(_make_card(ticket="PROJ-1"), PriorityLevel.P3)
        push(_make_card(ticket="PROJ-2"), PriorityLevel.P3)
        # P0 added later — must come out first.
        m_urgent = push(_make_card(ticket="PROJ-9"), PriorityLevel.P0)
        msgs = pull("w", count=1, visibility_timeout_s=10)
        assert msgs[0].message_id == m_urgent

    def test_pull_count_caps_returned_messages(self):
        for i in range(5):
            push(_make_card(ticket=f"PROJ-{i}"), PriorityLevel.P2)
        msgs = pull("w", count=2, visibility_timeout_s=10)
        assert len(msgs) == 2
        assert depth(state=TaskState.Queued) == 3


# ──────────────────────────────────────────────────────────────
#  4. Visibility timeout
# ──────────────────────────────────────────────────────────────


class TestVisibilityTimeout:
    def test_claim_then_no_ack_requeues_after_sweep(self):
        mid = push(_make_card())
        msgs = pull("w-dead", count=1, visibility_timeout_s=0.05)
        assert msgs[0].state == TaskState.Claimed
        time.sleep(0.1)
        res = sweep_visibility()
        assert mid in res.requeued_message_ids
        # Now another worker can pick it up.
        msgs2 = pull("w-fresh", count=1, visibility_timeout_s=10)
        assert msgs2[0].message_id == mid
        # delivery_count reflects re-delivery
        assert msgs2[0].delivery_count == 2

    def test_sweep_does_not_touch_unexpired_claims(self):
        push(_make_card())
        pull("w", count=1, visibility_timeout_s=60)
        res = sweep_visibility()
        assert res.requeued_message_ids == []
        assert depth(state=TaskState.Claimed) == 1

    def test_sweep_visibility_returns_zero_on_empty(self):
        res = sweep_visibility()
        assert res.requeued_message_ids == []
        assert res.dlq_message_ids == []

    def test_visibility_timeout_into_dlq_on_max_deliveries(self):
        mid = push(_make_card())
        # Burn MAX_DELIVERIES-1 retries normally so the next visibility
        # sweep on a Claimed but unacked message hits the DLQ branch.
        for _ in range(MAX_DELIVERIES - 1):
            [m] = pull("w", count=1, visibility_timeout_s=10)
            nack(m.message_id, reason="transient")
        # Final claim with very short visibility — never ack.
        [m] = pull("w", count=1, visibility_timeout_s=0.01)
        time.sleep(0.05)
        res = sweep_visibility()
        assert mid in res.dlq_message_ids
        assert get(mid) is None
        assert any(e.message_id == mid for e in dlq_list())


# ──────────────────────────────────────────────────────────────
#  5. nack + DLQ
# ──────────────────────────────────────────────────────────────


class TestNackAndDlq:
    def test_nack_requeues_when_under_limit(self):
        push(_make_card())
        [m] = pull("w", count=1, visibility_timeout_s=10)
        result = nack(m.message_id, reason="bad day")
        assert result.state == TaskState.Queued
        assert depth(state=TaskState.Queued) == 1
        assert depth(state=TaskState.Claimed) == 0

    def test_third_failure_moves_to_dlq(self):
        mid = push(_make_card())
        for i in range(MAX_DELIVERIES):
            [m] = pull("w", count=1, visibility_timeout_s=10)
            nack(m.message_id, reason=f"fail-{i}", stack=f"stack-{i}")
        # Original message gone from live queue.
        assert depth() == 0
        assert get(mid) is None
        # And present in DLQ with the last-known reason + stack.
        entries = dlq_list()
        assert len(entries) == 1
        e = entries[0]
        assert e.message_id == mid
        assert e.failure_count == MAX_DELIVERIES
        assert e.root_cause == f"fail-{MAX_DELIVERIES - 1}"
        assert e.stack == f"stack-{MAX_DELIVERIES - 1}"

    def test_dlq_preserves_original_catc(self):
        card = _make_card(ticket="PROJ-99",
                          allowed=["src/x.c", "src/y.c"])
        push(card, PriorityLevel.P0)
        for _ in range(MAX_DELIVERIES):
            [m] = pull("w", count=1, visibility_timeout_s=10)
            nack(m.message_id, reason="boom")
        entry = dlq_list()[0]
        assert entry.priority == PriorityLevel.P0
        rebuilt = TaskCard.from_dict(entry.payload)
        assert rebuilt.jira_ticket == "PROJ-99"
        assert rebuilt.navigation.impact_scope.allowed == ["src/x.c", "src/y.c"]

    def test_nack_unknown_message_raises(self):
        with pytest.raises(MessageNotFound):
            nack("msg-deadbeef", reason="x")

    def test_dlq_purge_removes_entry(self):
        mid = push(_make_card())
        for _ in range(MAX_DELIVERIES):
            [m] = pull("w", count=1, visibility_timeout_s=10)
            nack(m.message_id, reason="boom")
        assert dlq_purge(mid) is True
        assert dlq_list() == []
        assert dlq_purge(mid) is False  # idempotent

    def test_dlq_redrive_creates_new_message(self):
        mid = push(_make_card(), PriorityLevel.P3)
        for _ in range(MAX_DELIVERIES):
            [m] = pull("w", count=1, visibility_timeout_s=10)
            nack(m.message_id, reason="boom")
        assert dlq_list()[0].priority == PriorityLevel.P3
        new_id = dlq_redrive(mid, new_priority=PriorityLevel.P0)
        assert new_id != mid
        assert dlq_list() == []
        assert depth(priority=PriorityLevel.P0, state=TaskState.Queued) == 1

    def test_dlq_redrive_unknown_raises(self):
        with pytest.raises(MessageNotFound):
            dlq_redrive("msg-deadbeef")

    def test_format_exc_renders_traceback(self):
        try:
            raise RuntimeError("synthetic")
        except RuntimeError as exc:
            text = format_exc(exc)
        assert "RuntimeError" in text and "synthetic" in text


# ──────────────────────────────────────────────────────────────
#  6. State transitions via set_state
# ──────────────────────────────────────────────────────────────


class TestSetState:
    def test_queued_to_blocked_to_ready(self):
        mid = push(_make_card())
        m1 = set_state(mid, TaskState.Blocked_by_Mutex)
        assert m1.state == TaskState.Blocked_by_Mutex
        m2 = set_state(mid, TaskState.Ready)
        assert m2.state == TaskState.Ready

    def test_ready_to_claimed_to_running_to_done_chain(self):
        # Skip the pull() flow and walk the state machine end-to-end.
        mid = push(_make_card())
        set_state(mid, TaskState.Ready)
        set_state(mid, TaskState.Claimed)
        set_state(mid, TaskState.Running)
        m = set_state(mid, TaskState.Done)
        # Done is recorded; subsequent transitions should fail.
        assert m.state == TaskState.Done
        with pytest.raises(InvalidStateTransition):
            set_state(mid, TaskState.Queued)

    def test_set_state_unknown_message_raises(self):
        with pytest.raises(MessageNotFound):
            set_state("msg-deadbeef", TaskState.Ready)

    def test_set_state_records_history(self):
        mid = push(_make_card())
        set_state(mid, TaskState.Blocked_by_Mutex)
        set_state(mid, TaskState.Ready)
        m = get(mid)
        assert m is not None
        states = [h[1] for h in m.history]
        # Initial Queued + the two transitions we drove.
        assert states == ["Queued", "Blocked_by_Mutex", "Ready"]


# ──────────────────────────────────────────────────────────────
#  7. depth() filtering
# ──────────────────────────────────────────────────────────────


class TestDepth:
    def test_depth_total_and_by_priority(self):
        push(_make_card(), PriorityLevel.P0)
        push(_make_card(), PriorityLevel.P0)
        push(_make_card(), PriorityLevel.P3)
        assert depth() == 3
        assert depth(priority=PriorityLevel.P0) == 2
        assert depth(priority=PriorityLevel.P3) == 1
        assert depth(priority=PriorityLevel.P1) == 0

    def test_depth_by_state(self):
        mid = push(_make_card())
        assert depth(state=TaskState.Queued) == 1
        pull("w", count=1, visibility_timeout_s=10)
        assert depth(state=TaskState.Queued) == 0
        assert depth(state=TaskState.Claimed) == 1
        ack(mid)
        assert depth() == 0


# ──────────────────────────────────────────────────────────────
#  8. Concurrency
# ──────────────────────────────────────────────────────────────


class TestConcurrency:
    def test_concurrent_pull_no_double_delivery(self):
        # Push 20 messages, run 5 concurrent pullers, ensure each msg_id
        # is delivered to exactly one consumer.
        ids = [push(_make_card(ticket=f"PROJ-{i}"), PriorityLevel.P2)
               for i in range(20)]
        delivered: list[str] = []
        delivered_lock = threading.Lock()
        barrier = threading.Barrier(5)

        def consumer(name: str) -> None:
            barrier.wait()
            for _ in range(8):
                msgs = pull(name, count=2, visibility_timeout_s=60)
                if not msgs:
                    return
                with delivered_lock:
                    delivered.extend(m.message_id for m in msgs)

        threads = [threading.Thread(target=consumer, args=(f"w{i}",))
                   for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sorted(delivered) == sorted(ids)

    def test_concurrent_push_total_count_correct(self):
        N_THREADS = 4
        N_PER = 10
        barrier = threading.Barrier(N_THREADS)

        def producer(tag: int) -> None:
            barrier.wait()
            for i in range(N_PER):
                push(_make_card(ticket=f"PROJ-{tag * 100 + i}"), PriorityLevel.P2)

        threads = [threading.Thread(target=producer, args=(t,))
                   for t in range(N_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert depth() == N_THREADS * N_PER


# ──────────────────────────────────────────────────────────────
#  9. Integration scenarios
# ──────────────────────────────────────────────────────────────


class TestIntegration:
    def test_push_pull_ack_full_flow(self):
        ids = [
            push(_make_card(ticket=f"PROJ-{i}"), PriorityLevel.P2)
            for i in range(3)
        ]
        # Two workers pull and ack in interleaved order.
        worker_a = pull("worker-a", count=2, visibility_timeout_s=10)
        worker_b = pull("worker-b", count=1, visibility_timeout_s=10)
        for m in worker_a + worker_b:
            assert ack(m.message_id) is True
        assert depth() == 0
        assert sorted([m.message_id for m in worker_a + worker_b]) == sorted(ids)

    def test_visibility_timeout_full_recovery(self):
        # Worker claims, dies (no ack), sweep requeues, peer claims + acks.
        mid = push(_make_card(), PriorityLevel.P1)
        first = pull("dying-worker", count=1, visibility_timeout_s=0.05)
        assert first[0].claim_owner == "dying-worker"
        time.sleep(0.1)
        sweep_visibility()
        second = pull("rescue-worker", count=1, visibility_timeout_s=60)
        assert second[0].message_id == mid
        assert second[0].claim_owner == "rescue-worker"
        assert second[0].delivery_count == 2
        assert ack(mid) is True

    def test_priority_drain_ordering_under_mixed_load(self):
        # Enqueue mixed priorities; pull one at a time, expect strict
        # P0 → P1 → P2 → P3 ordering between buckets.
        push(_make_card(ticket="PROJ-3"), PriorityLevel.P3)
        push(_make_card(ticket="PROJ-2"), PriorityLevel.P2)
        push(_make_card(ticket="PROJ-1"), PriorityLevel.P1)
        push(_make_card(ticket="PROJ-0"), PriorityLevel.P0)
        order: list[PriorityLevel] = []
        for _ in range(4):
            [m] = pull("w", count=1, visibility_timeout_s=10)
            order.append(m.priority)
        assert order == [
            PriorityLevel.P0, PriorityLevel.P1,
            PriorityLevel.P2, PriorityLevel.P3,
        ]


# ──────────────────────────────────────────────────────────────
#  10. Reserved backend names (RabbitMQ / SQS)
# ──────────────────────────────────────────────────────────────


class TestReservedBackends:
    def test_rabbitmq_backend_selection_raises_not_implemented(self, monkeypatch):
        set_backend_for_tests(None)
        monkeypatch.setenv("OMNISIGHT_QUEUE_BACKEND", "rabbitmq")
        with pytest.raises(NotImplementedError) as exc:
            push(_make_card())
        assert "rabbitmq" in str(exc.value).lower()

    def test_sqs_backend_selection_raises_not_implemented(self, monkeypatch):
        set_backend_for_tests(None)
        monkeypatch.setenv("OMNISIGHT_QUEUE_BACKEND", "sqs")
        with pytest.raises(NotImplementedError) as exc:
            push(_make_card())
        assert "sqs" in str(exc.value).lower()

    def test_rabbit_alias_backend_selection_raises_not_implemented(self, monkeypatch):
        set_backend_for_tests(None)
        monkeypatch.setenv("OMNISIGHT_QUEUE_BACKEND", "rabbit")
        with pytest.raises(NotImplementedError) as exc:
            push(_make_card())
        assert "rabbitmq" in str(exc.value).lower()


# ──────────────────────────────────────────────────────────────
#  11. Metrics wired
# ──────────────────────────────────────────────────────────────


class TestMetricsWired:
    def test_metrics_objects_exist(self):
        from backend import metrics
        assert hasattr(metrics, "queue_depth")
        assert hasattr(metrics, "queue_claim_duration_seconds")

    def test_metrics_exercised_on_push_pull(self):
        # Just confirm the calls don't blow up — no-op safe under
        # missing prometheus_client; semantic counters tested elsewhere.
        push(_make_card())
        pull("w", count=1, visibility_timeout_s=10)


# ──────────────────────────────────────────────────────────────
#  12. QueueMessage round-trip
# ──────────────────────────────────────────────────────────────


class TestQueueMessageRoundTrip:
    def test_to_dict_from_dict_preserves_fields(self):
        mid = push(_make_card())
        m = get(mid)
        assert m is not None
        roundtrip = QueueMessage.from_dict(m.to_dict())
        assert roundtrip.message_id == m.message_id
        assert roundtrip.priority == m.priority
        assert roundtrip.state == m.state
        assert roundtrip.payload == m.payload
        assert roundtrip.history == m.history
