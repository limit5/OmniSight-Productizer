"""O1 (#264) — Redis distributed file-path mutex lock tests.

Exercises the in-memory backend (the Redis backend shares the same
observable semantics; a separate `pytest -m redis` suite can point
``OMNISIGHT_REDIS_URL`` at a real server).
"""

from __future__ import annotations

import threading
import time

import pytest

from backend import dist_lock
from backend.dist_lock import (
    DEFAULT_TTL_S,
    InMemoryLockBackend,
    acquire_paths,
    all_entries,
    build_wait_graph,
    detect_deadlock_cycles,
    extend_lease,
    get_lock_holder,
    get_locked_paths,
    new_task_id,
    preempt_paths,
    release_paths,
    run_deadlock_sweep,
    set_backend_for_tests,
)


# ──────────────────────────────────────────────────────────────
#  Fixtures
# ──────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _fresh_backend():
    """Give every test a fresh in-memory backend."""
    set_backend_for_tests(InMemoryLockBackend())
    yield
    set_backend_for_tests(None)


# ──────────────────────────────────────────────────────────────
#  1. Path normalisation
# ──────────────────────────────────────────────────────────────


class TestPathNormalisation:
    @pytest.mark.parametrize(
        "given,expected",
        [
            ("src/main.c", "src/main.c"),
            ("/src/main.c", "src/main.c"),
            ("src/main.c/", "src/main.c"),
            ("src\\main.c", "src/main.c"),
            ("src//main.c", "src/main.c"),
            ("  src/main.c  ", "src/main.c"),
        ],
    )
    def test_normalisation_cases(self, given, expected):
        assert dist_lock._normalise(given) == expected

    def test_empty_path_rejected(self):
        with pytest.raises(ValueError):
            dist_lock._normalise("/")

    def test_wrong_type_rejected(self):
        with pytest.raises(TypeError):
            dist_lock._normalise(123)  # type: ignore[arg-type]

    def test_dedup_and_sort(self):
        norm = dist_lock._normalise_many(
            ["b/x", "a/y", "b/x", "/a/y/"]
        )
        assert norm == ["a/y", "b/x"]

    def test_dedup_across_slash_variants(self):
        norm = dist_lock._normalise_many(["src/main.c", "src\\main.c"])
        assert norm == ["src/main.c"]


# ──────────────────────────────────────────────────────────────
#  2. Basic acquire / release / extend
# ──────────────────────────────────────────────────────────────


class TestBasicAcquireRelease:
    def test_acquire_empty_list_succeeds(self):
        res = acquire_paths("t1", [])
        assert res.ok
        assert res.acquired == []

    def test_acquire_then_release(self):
        res = acquire_paths("t1", ["src/a.c", "src/b.c"], ttl_s=60)
        assert res.ok
        assert res.acquired == ["src/a.c", "src/b.c"]
        assert res.expires_at > time.time()
        assert res.wait_seconds >= 0.0

        assert get_lock_holder("src/a.c") == "t1"
        assert get_lock_holder("src/b.c") == "t1"
        assert get_locked_paths("t1") == ["src/a.c", "src/b.c"]

        released = release_paths("t1")
        assert released == 2
        assert get_lock_holder("src/a.c") is None

    def test_release_is_idempotent(self):
        acquire_paths("t1", ["x"])
        assert release_paths("t1") == 1
        assert release_paths("t1") == 0  # second call no-op

    def test_reacquire_own_paths(self):
        """A task re-acquiring its own paths must succeed (and refresh)."""
        r1 = acquire_paths("t1", ["src/x.c"], ttl_s=60)
        assert r1.ok
        r2 = acquire_paths("t1", ["src/x.c"], ttl_s=120)
        assert r2.ok

    def test_conflict_returns_blocker(self):
        acquire_paths("t1", ["src/a.c"])
        res = acquire_paths("t2", ["src/a.c", "src/b.c"])
        assert not res.ok
        assert res.conflicts == {"src/a.c": "t1"}
        # No partial acquisition — b.c must still be free.
        assert get_lock_holder("src/b.c") is None

    def test_all_or_nothing(self):
        """If any requested path conflicts, none of the others are taken."""
        acquire_paths("t1", ["src/a.c"])
        res = acquire_paths("t2", ["src/b.c", "src/a.c", "src/c.c"])
        assert not res.ok
        for p in ("src/b.c", "src/c.c"):
            assert get_lock_holder(p) is None

    def test_extend_lease_refreshes(self):
        acquire_paths("t1", ["src/a.c"], ttl_s=60)
        original_exp = next(e.expires_at for e in all_entries())
        time.sleep(0.05)
        assert extend_lease("t1", ttl_s=120) is True
        new_exp = next(e.expires_at for e in all_entries())
        assert new_exp > original_exp

    def test_extend_lease_on_no_holdings(self):
        assert extend_lease("ghost-task", ttl_s=60) is False

    def test_default_ttl_is_30min(self):
        res = acquire_paths("t1", ["x"])
        assert res.expires_at - time.time() == pytest.approx(DEFAULT_TTL_S, abs=2)

    def test_invalid_task_id(self):
        with pytest.raises(ValueError):
            acquire_paths("", ["x"])
        with pytest.raises(ValueError):
            release_paths("")
        with pytest.raises(ValueError):
            extend_lease("")


# ──────────────────────────────────────────────────────────────
#  3. TTL expiry / auto-revoke
# ──────────────────────────────────────────────────────────────


class TestTTLExpiry:
    def test_ttl_expires_and_lock_auto_releases(self):
        acquire_paths("t1", ["src/a.c"], ttl_s=0.1)
        time.sleep(0.15)
        # fresh acquire from a different task should now succeed
        res = acquire_paths("t2", ["src/a.c"], ttl_s=60)
        assert res.ok
        assert get_lock_holder("src/a.c") == "t2"

    def test_extend_keeps_lock_alive(self):
        """Heartbeat prevents expiry — basic liveness property."""
        acquire_paths("t1", ["src/a.c"], ttl_s=0.2)
        for _ in range(3):
            time.sleep(0.08)
            assert extend_lease("t1", ttl_s=0.2)
        assert get_lock_holder("src/a.c") == "t1"

    def test_missed_heartbeat_auto_revokes(self):
        """Missing the heartbeat → another task gets the lock."""
        acquire_paths("t1", ["src/a.c"], ttl_s=0.1)
        time.sleep(0.15)  # t1 "crashes" without extending
        res = acquire_paths("t2", ["src/a.c"])
        assert res.ok


# ──────────────────────────────────────────────────────────────
#  4. Sorted acquisition / AB-BA deadlock prevention
# ──────────────────────────────────────────────────────────────


class TestSortedAcquisition:
    def test_paths_sorted_deterministically(self):
        res = acquire_paths("t1", ["z", "a", "m", "b"])
        assert res.ok
        assert res.acquired == ["a", "b", "m", "z"]

    def test_two_tasks_same_paths_different_order(self):
        """Classic AB / BA scenario — sorted acquisition makes it safe.

        t1 holds both A and B.  t2 tries to take B+A in reverse order;
        because the backend sorts paths before acquiring, it sees the
        full conflict atomically instead of taking B and then blocking
        on A (which would be the AB-BA deadlock pattern).
        """
        assert acquire_paths("t1", ["A", "B"]).ok
        res = acquire_paths("t2", ["B", "A"])
        assert not res.ok
        # conflicts must report *both* paths, not just one — proving the
        # atomic all-or-nothing check ran across the full sorted list.
        assert set(res.conflicts) == {"A", "B"}
        # t1 still holds everything it had.
        assert get_lock_holder("A") == "t1"
        assert get_lock_holder("B") == "t1"


# ──────────────────────────────────────────────────────────────
#  5. Wait-for graph + deadlock detection
# ──────────────────────────────────────────────────────────────


class TestDeadlockDetection:
    def test_wait_graph_empty_when_no_contention(self):
        acquire_paths("t1", ["a"])
        acquire_paths("t2", ["b"])
        assert build_wait_graph() == {}

    def test_wait_graph_records_waiters(self):
        """When t2 is blocked on t1's path and records a wait, the graph
        must show t2 → t1."""
        acquire_paths("t1", ["a"])
        acquire_paths("t2", ["a"], wait_timeout_s=0.01)  # will record wait
        graph = build_wait_graph()
        assert graph == {"t2": {"t1"}}

    def test_two_cycle_deadlock_detected(self):
        """t1 holds A, t2 holds B, t1 wants B, t2 wants A → cycle."""
        acquire_paths("t1", ["A"], priority=10)
        acquire_paths("t2", ["B"], priority=20)
        acquire_paths("t1", ["B"], wait_timeout_s=0.01, priority=10)
        acquire_paths("t2", ["A"], wait_timeout_s=0.01, priority=20)

        cycles = detect_deadlock_cycles()
        assert len(cycles) == 1
        assert set(cycles[0]) == {"t1", "t2"}

    def test_sweep_kills_lowest_priority(self):
        acquire_paths("t1", ["A"], priority=10)   # lowest
        acquire_paths("t2", ["B"], priority=50)
        acquire_paths("t1", ["B"], wait_timeout_s=0.01, priority=10)
        acquire_paths("t2", ["A"], wait_timeout_s=0.01, priority=50)

        result = run_deadlock_sweep()
        assert "t1" in result.killed_task_ids
        assert get_lock_holder("A") is None  # t1 released
        # t2 is still holding B (it wasn't the victim)
        assert get_lock_holder("B") == "t2"

    def test_sweep_is_noop_without_cycles(self):
        acquire_paths("t1", ["A"])
        acquire_paths("t2", ["B"])
        assert run_deadlock_sweep().killed_task_ids == []

    def test_three_way_cycle_detected(self):
        """t1→t2→t3→t1 — bigger cycle; all three members are in SCC."""
        acquire_paths("t1", ["A"], priority=5)
        acquire_paths("t2", ["B"], priority=10)
        acquire_paths("t3", ["C"], priority=15)
        acquire_paths("t1", ["B"], wait_timeout_s=0.01, priority=5)
        acquire_paths("t2", ["C"], wait_timeout_s=0.01, priority=10)
        acquire_paths("t3", ["A"], wait_timeout_s=0.01, priority=15)

        cycles = detect_deadlock_cycles()
        assert len(cycles) == 1
        assert set(cycles[0]) == {"t1", "t2", "t3"}


# ──────────────────────────────────────────────────────────────
#  6. Preemption
# ──────────────────────────────────────────────────────────────


class TestPreemption:
    def test_cannot_preempt_fresh_lock(self):
        """A freshly-acquired lock is not preemptable — even by higher prio."""
        acquire_paths("t1", ["A"], ttl_s=60, priority=10)
        res = preempt_paths("t2", ["A"], ttl_s=60, priority=100)
        assert not res.ok
        assert get_lock_holder("A") == "t1"

    def test_preempt_stale_lower_priority(self):
        """TTL × 2 elapsed + higher priority → preemption allowed."""
        acquire_paths("t1", ["A"], ttl_s=0.05, priority=10)
        # 2x TTL + some slack
        time.sleep(0.2)
        # In-memory backend expires the lease first → normal acquire wins.
        # To test *actual preemption* we shorten but don't let it auto-expire:
        # re-establish with a fresh TTL and then manipulate time via the
        # preempt_after_s argument.
        set_backend_for_tests(InMemoryLockBackend())
        acquire_paths("t1", ["A"], ttl_s=3600, priority=10)
        # Simulate 2× TTL by calling preempt with preempt_after_s semantically
        # via a negative TTL (not exposed publicly; we fake via mutating
        # acquired_at in the backend):
        backend = dist_lock._get_backend()
        assert isinstance(backend, InMemoryLockBackend)
        entry = backend._holders["A"]
        entry.acquired_at = time.time() - 7200  # pretend 2h old
        res = preempt_paths("t2", ["A"], ttl_s=60, priority=100)
        assert res.ok
        assert get_lock_holder("A") == "t2"

    def test_preempt_denied_if_same_priority(self):
        """Must be STRICTLY higher priority than the stale holder."""
        acquire_paths("t1", ["A"], ttl_s=3600, priority=50)
        backend = dist_lock._get_backend()
        assert isinstance(backend, InMemoryLockBackend)
        backend._holders["A"].acquired_at = time.time() - 7200
        res = preempt_paths("t2", ["A"], ttl_s=60, priority=50)  # ties → no
        assert not res.ok

    def test_preempt_paths_empty_list(self):
        res = preempt_paths("t1", [])
        assert res.ok


# ──────────────────────────────────────────────────────────────
#  7. Integration: 3 tasks × 10 paths (spec requirement)
# ──────────────────────────────────────────────────────────────


class TestIntegrationThreeTasksTenPaths:
    def test_three_tasks_ten_paths(self):
        """3 tasks, 10 paths — one acquires first, others see conflicts
        on the overlap, non-overlapping paths stay free."""
        p_all = [f"src/file_{i}.c" for i in range(10)]
        t1_paths = p_all[:5]          # 0-4
        t2_paths = p_all[3:8]         # 3-7 (overlaps t1 on 3, 4)
        t3_paths = p_all[7:]          # 7-9 (overlaps t2 on 7)

        r1 = acquire_paths("t1", t1_paths, priority=20)
        assert r1.ok

        r2 = acquire_paths("t2", t2_paths, priority=10)
        assert not r2.ok
        assert set(r2.conflicts) == {"src/file_3.c", "src/file_4.c"}

        # t3 doesn't conflict with t1 at all → succeeds.
        r3 = acquire_paths("t3", t3_paths, priority=30)
        assert r3.ok
        assert get_lock_holder("src/file_9.c") == "t3"

        # Release t1 — now t2 should be able to acquire its 3 & 4 (and
        # the 5,6,7 paths except 7 which is now held by t3).
        release_paths("t1")
        r2b = acquire_paths("t2", t2_paths, priority=10)
        assert not r2b.ok
        assert set(r2b.conflicts) == {"src/file_7.c"}  # t3 holds 7

    def test_concurrent_acquire_only_one_winner(self):
        """Two threads race for the same path — exactly one wins."""
        winners: list[str] = []
        barrier = threading.Barrier(5)

        def worker(i: int) -> None:
            barrier.wait()
            res = acquire_paths(f"t{i}", ["hot/contended.c"], ttl_s=60)
            if res.ok:
                winners.append(f"t{i}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(winners) == 1

    def test_heartbeat_failure_releases_to_waiting_peer(self):
        """Integration: heartbeat dies → new worker claims after TTL."""
        acquire_paths("t_dying", ["shared.c"], ttl_s=0.1)
        # A new task waits briefly.
        deadline = time.time() + 0.5
        acquired_by = None
        while time.time() < deadline:
            res = acquire_paths("t_new", ["shared.c"], ttl_s=60)
            if res.ok:
                acquired_by = "t_new"
                break
            time.sleep(0.05)
        assert acquired_by == "t_new"


# ──────────────────────────────────────────────────────────────
#  8. Unique task-id helper
# ──────────────────────────────────────────────────────────────


class TestTaskIdHelper:
    def test_new_task_id_is_unique(self):
        ids = {new_task_id() for _ in range(50)}
        assert len(ids) == 50

    def test_new_task_id_respects_prefix(self):
        tid = new_task_id(prefix="worker-42")
        assert tid.startswith("worker-42-")


# ──────────────────────────────────────────────────────────────
#  9. Metrics wired
# ──────────────────────────────────────────────────────────────


class TestMetricsWired:
    def test_acquire_increments_held_total(self):
        """Just confirm the metric object exists and accepts the calls —
        the actual counter semantics are tested in metrics.py's own suite."""
        from backend import metrics
        assert hasattr(metrics, "dist_lock_wait_seconds")
        assert hasattr(metrics, "dist_lock_held_total")
        assert hasattr(metrics, "dist_lock_deadlock_kills_total")
        # Exercise each — no-op if prometheus_client is unavailable.
        r = acquire_paths("t-metric", ["x"])
        assert r.ok
        release_paths("t-metric")
