"""Tests for backend.agents.op_queue (META OP-757 / H10 of OP-747).

Each Acceptance Criterion from the ticket has at least one named test
below so the AC ↔ test mapping in the JIRA verification comment is
mechanical to read off.

Tests use ``tmp_path`` for the SQLite DB and a fake clock so the
exponential-backoff schedule is exercised deterministically without
real ``time.sleep``.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from backend.agents import op_queue
from backend.agents.op_queue import (
    BACKOFF_BASE_SECONDS,
    MAX_ATTEMPTS,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_IN_PROGRESS,
    STATUS_PENDING,
    Op,
    OpQueue,
    Worker,
    enqueue_gerrit_push,
    enqueue_jira_comment,
    enqueue_jira_label,
    enqueue_jira_transition,
    register_handler,
    reset_handlers_for_tests,
)


# ── Fakes ─────────────────────────────────────────────────────────────


class FakeClock:
    def __init__(self, t: float = 1_000.0) -> None:
        self.t = t

    def now(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _fresh_queue(tmp_path: Path, clock: FakeClock | None = None) -> OpQueue:
    clock = clock or FakeClock()
    return OpQueue(path=tmp_path / "op-queue.db", clock=clock.now, escalate_fn=lambda op: None)


@pytest.fixture(autouse=True)
def _isolate_handler_registry():
    """Each test starts with an empty registry, so cross-test handler
    leakage is impossible."""
    reset_handlers_for_tests()
    yield
    reset_handlers_for_tests()


# ── AC: SQLite DB with WAL mode + spec'd schema ──────────────────────


def test_sqlite_uses_wal_mode_and_creates_ops_table(tmp_path: Path) -> None:
    """AC: 'SQLite DB at ~/.config/omnisight/op-queue.db with WAL mode'."""
    db_path = tmp_path / "op-queue.db"
    OpQueue(path=db_path, escalate_fn=lambda op: None)
    assert db_path.exists()
    with sqlite3.connect(str(db_path)) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
        cols = {r[1] for r in conn.execute("PRAGMA table_info(ops)").fetchall()}
    expected = {
        "id", "action", "args_json", "idempotency_key", "status",
        "created_at", "completed_at", "last_error", "retry_count",
    }
    # Spec schema columns are all present (extra columns for ordering
    # are additive and documented).
    assert expected.issubset(cols)
    assert "ordering_key" in cols and "seq" in cols


def test_idempotent_insert_returns_same_id(tmp_path: Path) -> None:
    """AC: 'idempotency keys make retry safe' — same key → same row."""
    queue = _fresh_queue(tmp_path)
    a = queue.enqueue("jira.add_comment", {"key": "OP-1", "text": "hi"}, "comment-OP-1-fixed")
    b = queue.enqueue("jira.add_comment", {"key": "OP-1", "text": "again"}, "comment-OP-1-fixed")
    assert a == b
    op = queue.get(a)
    assert op is not None
    assert op.args["text"] == "hi"  # second insert was a no-op


# ── AC: Producer inserts ops + commits before doing the work ─────────


def test_enqueue_persists_before_handler_runs(tmp_path: Path) -> None:
    """AC: 'Producer inserts ops + commits before doing the actual work'.

    After enqueue, the row exists with status=pending in the DB even
    though the worker has not run yet.
    """
    queue = _fresh_queue(tmp_path)
    op_id = queue.enqueue("jira.add_comment", {"key": "OP-2", "text": "x"}, "comment-OP-2")
    op = queue.get(op_id)
    assert op is not None
    assert op.status == STATUS_PENDING
    assert op.args == {"key": "OP-2", "text": "x"}


# ── AC: Worker drains by claim-then-update with single-claim guarantee


def test_claim_next_marks_in_progress_atomically(tmp_path: Path) -> None:
    """AC: 'worker (single thread) drains by SELECT FOR UPDATE'.

    Two consecutive claim_next calls must NOT return the same op.
    """
    queue = _fresh_queue(tmp_path)
    queue.enqueue("jira.add_comment", {"key": "OP-3", "text": "x"}, "k-OP-3")
    first = queue.claim_next()
    assert first is not None and first.status == STATUS_IN_PROGRESS
    second = queue.claim_next()
    # Same ordering_key (default) — second claim is gated by the
    # in-progress sibling.
    assert second is None


def test_claim_next_skips_when_only_in_progress_exists(tmp_path: Path) -> None:
    """Recovery should be needed before claim_next picks anything up
    after a crash that leaves a row stuck at in_progress."""
    queue = _fresh_queue(tmp_path)
    op_id = queue.enqueue("jira.add_comment", {"key": "OP-4", "text": "x"}, "k-OP-4")
    claimed = queue.claim_next()
    assert claimed is not None
    # Simulate crash: row sits at in_progress, never completed.
    again = queue.claim_next()
    assert again is None
    # Recovery brings it back.
    assert queue.recover() == 1
    revived = queue.claim_next()
    assert revived is not None and revived.id == op_id


# ── AC: Recovery on startup resets in_progress → pending ─────────────


def test_recover_on_construction_resets_in_progress(tmp_path: Path) -> None:
    """AC: 'any in_progress ops are reset to pending on startup'."""
    db_path = tmp_path / "op-queue.db"
    queue1 = OpQueue(path=db_path, escalate_fn=lambda op: None)
    queue1.enqueue("jira.add_comment", {"key": "OP-5", "text": "x"}, "k-OP-5")
    queue1.claim_next()  # leave in_progress
    # New OpQueue instance simulates a process restart.
    queue2 = OpQueue(path=db_path, escalate_fn=lambda op: None)
    op = queue2.by_idempotency_key("k-OP-5")
    assert op is not None
    assert op.status == STATUS_PENDING


# ── AC: Ordering — transition → push → comment for same ticket ──────


def test_ordering_constraint_serialises_per_ordering_key(tmp_path: Path) -> None:
    """AC: 'Each op has ordering constraint (e.g. transition → push →
    comment must run in order for same ticket)'.

    Two ops with the same ordering_key cannot run concurrently — the
    second sits pending while the first is in_progress, and they drain
    in seq order.
    """
    clock = FakeClock()
    queue = _fresh_queue(tmp_path, clock)
    seen: list[str] = []

    def h_a(args, idem):
        seen.append(f"A:{args['n']}")

    def h_b(args, idem):
        seen.append(f"B:{args['n']}")

    register_handler("test.a", h_a)
    register_handler("test.b", h_b)

    # Two ops grouped under "OP-6": A first (seq 1), B second (seq 2).
    queue.enqueue("test.a", {"n": 1}, "k-A", ordering_key="OP-6")
    queue.enqueue("test.b", {"n": 2}, "k-B", ordering_key="OP-6")
    # And one ungrouped op — its own ordering group, runs in parallel.
    queue.enqueue("test.a", {"n": 99}, "k-other", ordering_key="OTHER")

    worker = Worker(queue)
    drained = worker.drain_until_empty()
    assert len(drained) == 3
    # The two OP-6 ops must execute in seq order.
    op6_seen = [s for s in seen if not s.endswith(":99")]
    assert op6_seen == ["A:1", "B:2"]


def test_ordering_does_not_block_other_keys(tmp_path: Path) -> None:
    """While OP-6's group is in_progress, OP-7's group can still run."""
    queue = _fresh_queue(tmp_path)
    queue.enqueue("test.a", {}, "OP-6-1", ordering_key="OP-6")
    queue.enqueue("test.a", {}, "OP-7-1", ordering_key="OP-7")

    first = queue.claim_next()
    assert first is not None
    second = queue.claim_next()
    assert second is not None
    assert first.ordering_key != second.ordering_key


# ── AC: Retry policy — exponential backoff up to 5 attempts ──────────


def test_retry_uses_exponential_backoff_up_to_max_attempts(tmp_path: Path) -> None:
    """AC: 'exponential backoff up to 5 attempts; then escalate'."""
    clock = FakeClock(t=1000.0)
    escalations: list[Op] = []
    queue = OpQueue(
        path=tmp_path / "q.db",
        clock=clock.now,
        escalate_fn=escalations.append,
    )

    def always_fails(args, idem):
        raise RuntimeError("nope")

    register_handler("test.fail", always_fails)
    queue.enqueue("test.fail", {}, "k-fail")
    worker = Worker(queue)

    # Each drain advances the clock past not_before.
    completed_attempts = 0
    for attempt in range(1, MAX_ATTEMPTS + 1):
        worker.drain_once()
        completed_attempts += 1
        op = queue.by_idempotency_key("k-fail")
        assert op is not None
        if attempt < MAX_ATTEMPTS:
            assert op.status == STATUS_PENDING
            assert op.retry_count == attempt
            assert op.not_before == pytest.approx(
                clock.now() + BACKOFF_BASE_SECONDS ** attempt
            )
            # Advance past the backoff so the next claim succeeds.
            clock.advance(BACKOFF_BASE_SECONDS ** attempt + 1)
        else:
            assert op.status == STATUS_FAILED
            assert op.retry_count == MAX_ATTEMPTS
            assert "nope" in (op.last_error or "")

    assert completed_attempts == MAX_ATTEMPTS
    # Operator was alerted exactly once after exhaustion.
    assert len(escalations) == 1
    assert escalations[0].action == "test.fail"


def test_handler_success_marks_completed_and_clears_error(tmp_path: Path) -> None:
    queue = _fresh_queue(tmp_path)
    register_handler("test.ok", lambda args, idem: None)
    queue.enqueue("test.ok", {}, "k-ok")
    worker = Worker(queue)
    worker.drain_once()
    op = queue.by_idempotency_key("k-ok")
    assert op is not None
    assert op.status == STATUS_COMPLETED
    assert op.last_error is None
    assert op.completed_at is not None


def test_transient_failure_then_success(tmp_path: Path) -> None:
    """A 1-attempt failure followed by recovery completes the op."""
    clock = FakeClock(t=1000.0)
    queue = OpQueue(
        path=tmp_path / "q.db",
        clock=clock.now,
        escalate_fn=lambda op: None,
    )

    state = {"calls": 0}

    def flaky(args, idem):
        state["calls"] += 1
        if state["calls"] == 1:
            raise RuntimeError("transient")

    register_handler("test.flaky", flaky)
    queue.enqueue("test.flaky", {}, "k-flaky")
    worker = Worker(queue)

    worker.drain_once()
    op = queue.by_idempotency_key("k-flaky")
    assert op is not None and op.status == STATUS_PENDING and op.retry_count == 1

    clock.advance(BACKOFF_BASE_SECONDS ** 1 + 1)
    worker.drain_once()
    op = queue.by_idempotency_key("k-flaky")
    assert op is not None and op.status == STATUS_COMPLETED


# ── AC: Phased rollout — Phase A handlers (JIRA transitions) ─────────


def test_phase_a_jira_transition_in_progress_invokes_dispatch(tmp_path: Path, monkeypatch) -> None:
    """AC Phase A: JIRA transitions go through the queue.

    The worker calls into ``jira_dispatch.transition_to_in_progress``
    with the correct args + idempotency key.
    """
    clock = FakeClock()
    queue = OpQueue(path=tmp_path / "q.db", clock=clock.now, escalate_fn=lambda op: None)

    captured: list[tuple[str, str, str]] = []

    class FakeClient:
        agent_class = "subscription-claude"

    def fake_make_client(agent_class):
        return FakeClient()

    def fake_transition(client, key, idem_key=None):
        captured.append(("transition", key, idem_key))

    monkeypatch.setattr(op_queue, "_make_jira_client", fake_make_client)
    from backend.agents import jira_dispatch
    monkeypatch.setattr(jira_dispatch, "transition_to_in_progress", fake_transition)

    op_queue.register_default_handlers()
    enqueue_jira_transition(
        queue, agent_class="subscription-claude", key="OP-757", target="in_progress",
        idempotency_key="my-idem",
    )
    Worker(queue).drain_once()

    assert captured == [("transition", "OP-757", "my-idem")]
    op = queue.by_idempotency_key("my-idem")
    assert op is not None and op.status == STATUS_COMPLETED


def test_phase_a_back_to_todo_passes_reason(tmp_path: Path, monkeypatch) -> None:
    queue = _fresh_queue(tmp_path)
    captured: list[tuple] = []

    monkeypatch.setattr(op_queue, "_make_jira_client", lambda ac: object())
    from backend.agents import jira_dispatch
    monkeypatch.setattr(
        jira_dispatch,
        "transition_back_to_todo",
        lambda client, key, reason, idem_key=None: captured.append((key, reason, idem_key)),
    )

    op_queue.register_default_handlers()
    enqueue_jira_transition(
        queue, agent_class="subscription-claude", key="OP-100",
        target="back_to_todo", reason="CLI exit 1", idempotency_key="back-1",
    )
    Worker(queue).drain_once()
    assert captured == [("OP-100", "CLI exit 1", "back-1")]


def test_phase_a_back_to_todo_requires_reason(tmp_path: Path) -> None:
    queue = _fresh_queue(tmp_path)
    with pytest.raises(ValueError, match="reason="):
        enqueue_jira_transition(
            queue, agent_class="subscription-claude", key="OP-100",
            target="back_to_todo",
        )


# ── AC: Phase B — Gerrit pushes + JIRA comments through the queue ───


def test_phase_b_jira_comment_invokes_dispatch(tmp_path: Path, monkeypatch) -> None:
    queue = _fresh_queue(tmp_path)
    captured: list[tuple] = []
    monkeypatch.setattr(op_queue, "_make_jira_client", lambda ac: object())
    from backend.agents import jira_dispatch
    monkeypatch.setattr(
        jira_dispatch,
        "add_comment",
        lambda client, key, text, idem_key=None: captured.append((key, text, idem_key)),
    )

    op_queue.register_default_handlers()
    enqueue_jira_comment(
        queue, agent_class="subscription-claude", key="OP-101", text="hello",
        idempotency_key="comment-1",
    )
    Worker(queue).drain_once()
    assert captured == [("OP-101", "hello", "comment-1")]


def test_phase_b_gerrit_push_invokes_dispatch(tmp_path: Path, monkeypatch) -> None:
    queue = _fresh_queue(tmp_path)
    from backend.agents import jira_dispatch

    class FakeResult:
        success = True
        change_number = 42
        change_url = "https://gerrit.example/c/p/+/42"
        detail = "ok"

    captured: list[tuple] = []

    def fake_push(worktree_path, agent_class, target="develop"):
        captured.append((str(worktree_path), agent_class, target))
        return FakeResult()

    monkeypatch.setattr(jira_dispatch, "push_to_gerrit_for_review", fake_push)
    op_queue.register_default_handlers()
    enqueue_gerrit_push(
        queue,
        agent_class="subscription-claude",
        worktree_path="/tmp/wt",
        ticket_key="OP-101",
        idempotency_key="push-1",
    )
    Worker(queue).drain_once()
    assert captured == [("/tmp/wt", "subscription-claude", "develop")]


def test_phase_b_gerrit_push_failure_retries(tmp_path: Path, monkeypatch) -> None:
    """A failed Gerrit push moves the op to pending+retry_count, not
    failed (until attempts exhaust)."""
    clock = FakeClock(t=1000.0)
    queue = OpQueue(path=tmp_path / "q.db", clock=clock.now, escalate_fn=lambda op: None)
    from backend.agents import jira_dispatch

    class FakeResult:
        success = False
        change_number = None
        change_url = None
        detail = "permission denied"

    monkeypatch.setattr(jira_dispatch, "push_to_gerrit_for_review", lambda *a, **kw: FakeResult())
    op_queue.register_default_handlers()
    enqueue_gerrit_push(
        queue, agent_class="subscription-claude", worktree_path="/tmp/wt",
        ticket_key="OP-200", idempotency_key="push-fail",
    )
    Worker(queue).drain_once()
    op = queue.by_idempotency_key("push-fail")
    assert op is not None
    assert op.status == STATUS_PENDING
    assert op.retry_count == 1
    assert "permission denied" in (op.last_error or "")


# ── AC: Stats + introspection (operator dashboards) ─────────────────


def test_stats_groups_by_status(tmp_path: Path) -> None:
    queue = _fresh_queue(tmp_path)
    register_handler("test.ok", lambda args, idem: None)
    register_handler("test.fail", lambda args, idem: (_ for _ in ()).throw(RuntimeError("x")))
    queue.enqueue("test.ok", {}, "ok-1", ordering_key="o1")
    queue.enqueue("test.fail", {}, "fail-1", ordering_key="o2")
    Worker(queue).drain_once()  # ok-1 → completed
    stats = queue.stats()
    assert stats.get(STATUS_COMPLETED, 0) == 1
    assert stats.get(STATUS_PENDING, 0) == 1


def test_label_helpers_use_correct_action(tmp_path: Path, monkeypatch) -> None:
    """``add=True`` → jira.add_label; ``add=False`` → jira.remove_label."""
    queue = _fresh_queue(tmp_path)
    add_id = enqueue_jira_label(
        queue, agent_class="subscription-claude", key="OP-300",
        label="runner-skipped:file-collision", add=True, idempotency_key="add-1",
    )
    rm_id = enqueue_jira_label(
        queue, agent_class="subscription-claude", key="OP-300",
        label="runner-skipped:file-collision", add=False, idempotency_key="rm-1",
    )
    assert queue.get(add_id).action == "jira.add_label"
    assert queue.get(rm_id).action == "jira.remove_label"


# ── AC: Recovery survives full ProcessRestart ─────────────────────────


def test_recovery_survives_restart_with_pending_op(tmp_path: Path, monkeypatch) -> None:
    """End-to-end: enqueue, simulate crash mid-run, restart, op completes."""
    db_path = tmp_path / "q.db"
    captured: list[str] = []

    def handler(args, idem):
        captured.append(f"{args['n']}:{idem}")

    # First "process": enqueue + start running but don't complete.
    queue1 = OpQueue(path=db_path, escalate_fn=lambda op: None)
    queue1.enqueue("test.h", {"n": 1}, "k-1")
    op = queue1.claim_next()
    assert op is not None
    # crash before mark_completed

    # Second "process": fresh OpQueue + handler.
    register_handler("test.h", handler)
    queue2 = OpQueue(path=db_path, escalate_fn=lambda op: None)
    Worker(queue2).drain_once()
    op = queue2.by_idempotency_key("k-1")
    assert op is not None and op.status == STATUS_COMPLETED
    assert captured == ["1:k-1"]
