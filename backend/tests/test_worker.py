"""O3 (#266) — Stateless Agent Worker Pool tests.

Covers the full spec checklist:

  * pull → lock → sandbox → execute → commit → push → ack → release
  * Heartbeat thread refreshes the Redis-equivalent ``alive`` key.
  * ``--capacity N`` lets a single worker drain N tasks concurrently.
  * ``--tenant-filter`` / ``--capability-filter`` reject non-matching CATC
    cards back to the queue without touching the sandbox.
  * Graceful shutdown (SIGTERM-equivalent ``stop()``) waits for in-flight,
    releases locks, and deregisters from the active set.
  * ``workers:active`` registration on start + deregistration on stop.
  * Sandbox bind-mount enforcement: only ``impact_scope.allowed`` paths
    are visible inside the workspace.
  * Gerrit push includes ``Change-Id`` + ``CATC-Ticket`` trailer.
  * Crash recovery, heartbeat-loss, push retries, multi-worker fan-out.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from backend import dist_lock
from backend import queue_backend as qb
from backend import worker as wkr
from backend.catc import TaskCard
from backend.queue_backend import (
    InMemoryQueueBackend,
    PriorityLevel,
    TaskState,
    set_backend_for_tests,
)
from backend.dist_lock import InMemoryLockBackend, set_backend_for_tests as set_lock_backend


# ──────────────────────────────────────────────────────────────
#  Fixtures
# ──────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _fresh_backends():
    set_backend_for_tests(InMemoryQueueBackend())
    set_lock_backend(InMemoryLockBackend())
    wkr.set_heartbeat_store_for_tests(wkr._MemoryHeartbeatStore())
    yield
    set_backend_for_tests(None)
    set_lock_backend(None)
    wkr.set_heartbeat_store_for_tests(None)


@pytest.fixture()
def project_root(tmp_path: Path) -> Path:
    """A fake project tree we can use as the worker's project root."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.c").write_text("// main\n")
    (tmp_path / "src" / "util.c").write_text("// util\n")
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "api.key").write_text("DO-NOT-LEAK")
    return tmp_path


def _card(ticket: str = "PROJ-1",
          allowed: list[str] | None = None,
          forbidden: list[str] | None = None,
          tenant: str = "",
          handoff: list[str] | None = None) -> TaskCard:
    return TaskCard.from_dict({
        "jira_ticket": ticket,
        "acceptance_criteria": "do the thing",
        "navigation": {
            "entry_point": "src/main.c",
            "impact_scope": {
                "allowed": allowed or ["src/main.c"],
                "forbidden": forbidden or [],
            },
        },
        "domain_context": tenant,
        "handoff_protocol": handoff or [],
    })


def _make_worker(project_root: Path, *,
                 worker_id: str = "w1",
                 capacity: int = 1,
                 max_messages: int | None = None,
                 sandbox: wkr.SandboxRuntime | None = None,
                 executor: wkr.AgentExecutor | None = None,
                 gerrit: wkr.GerritPusher | None = None,
                 tenant_filter: list[str] | None = None,
                 capability_filter: list[str] | None = None,
                 ) -> wkr.Worker:
    cfg = wkr.WorkerConfig(
        worker_id=worker_id,
        capacity=capacity,
        max_messages=max_messages,
        project_root=project_root,
        loop_idle_s=0.01,
        heartbeat_interval_s=0.05,
        heartbeat_ttl_s=1,
        visibility_timeout_s=10.0,
        tenant_filter=tenant_filter or [],
        capability_filter=capability_filter or [],
    )
    return wkr.Worker(
        cfg,
        sandbox_runtime=sandbox or wkr.LocalSandboxRuntime(
            workdir_root=project_root / ".sandboxes",
        ),
        agent_executor=executor or wkr._StubAgentExecutor(),
        gerrit_pusher=gerrit or wkr.StubGerritPusher(),
    )


# ──────────────────────────────────────────────────────────────
#  1. Sub-helpers
# ──────────────────────────────────────────────────────────────


class TestHelpers:
    def test_new_worker_id_unique(self):
        ids = {wkr.new_worker_id() for _ in range(50)}
        assert len(ids) == 50

    def test_new_change_id_format(self):
        cid = wkr._new_change_id()
        # ``I`` + 32 hex (uuid4) + 8 hex (uuid4 trim) = 41 chars.
        assert cid.startswith("I") and len(cid) == 41
        assert all(c in "0123456789abcdef" for c in cid[1:])

    def test_build_commit_message_appends_trailers(self):
        card = _card()
        msg = wkr._build_commit_message(
            card=card, base="agent did the thing\n",
            change_id="Iabc", worker_id="w1",
        )
        assert "agent did the thing" in msg
        assert "Change-Id: Iabc" in msg
        assert "CATC-Ticket: PROJ-1" in msg
        assert "Worker-Id: w1" in msg

    def test_build_commit_message_falls_back_when_empty(self):
        card = _card("PROJ-2")
        msg = wkr._build_commit_message(
            card=card, base="", change_id="Ixyz", worker_id="w7",
        )
        assert msg.startswith("agent: PROJ-2")

    def test_capability_extraction_from_handoff_and_domain_context(self):
        card = _card(
            handoff=["cap:firmware", "cap:vision"],
            tenant="cap:platform tenant=acme",
        )
        push_id = qb.push(card)
        msg = qb.get(push_id)
        caps = wkr._msg_capabilities(msg)
        assert "firmware" in caps and "vision" in caps and "platform" in caps

    def test_resolve_glob_rejects_parent_escape(self, tmp_path: Path):
        with pytest.raises(ValueError):
            wkr._resolve_glob(tmp_path, "../etc/passwd")


# ──────────────────────────────────────────────────────────────
#  2. Sandbox bind-mount enforcement (impact_scope.allowed)
# ──────────────────────────────────────────────────────────────


class TestSandboxBindMount:
    def test_only_allowed_paths_visible(self, project_root: Path):
        sb = wkr.LocalSandboxRuntime(workdir_root=project_root / ".sb")
        card = _card(allowed=["src/main.c"])
        h = sb.start(worker_id="w1", task_id="t1", card=card,
                     project_root=project_root)
        try:
            assert (h.workspace / "src" / "main.c").is_file()
            assert not (h.workspace / "src" / "util.c").exists()
            assert not (h.workspace / "secrets").exists()
        finally:
            sb.stop(h)

    def test_glob_dir_pulls_subtree(self, project_root: Path):
        sb = wkr.LocalSandboxRuntime(workdir_root=project_root / ".sb")
        card = _card(allowed=["src"])
        h = sb.start(worker_id="w1", task_id="t1", card=card,
                     project_root=project_root)
        try:
            assert (h.workspace / "src" / "main.c").is_file()
            assert (h.workspace / "src" / "util.c").is_file()
            assert not (h.workspace / "secrets").exists()
        finally:
            sb.stop(h)

    def test_path_escape_rejected(self, project_root: Path):
        sb = wkr.LocalSandboxRuntime(workdir_root=project_root / ".sb")
        bad = _card(allowed=["../etc/passwd"])
        with pytest.raises(ValueError):
            sb.start(worker_id="w1", task_id="t1", card=bad,
                     project_root=project_root)

    def test_commit_returns_sha(self, project_root: Path):
        sb = wkr.LocalSandboxRuntime(workdir_root=project_root / ".sb")
        card = _card(allowed=["src/main.c"])
        h = sb.start(worker_id="w1", task_id="t1", card=card,
                     project_root=project_root)
        try:
            (h.workspace / "src" / "main.c").write_text("// changed\n")
            sha = sb.commit(h, commit_message="test commit")
            assert len(sha) == 40
        finally:
            sb.stop(h)


# ──────────────────────────────────────────────────────────────
#  3. Single-task happy-path (pull → lock → run → push → ack)
# ──────────────────────────────────────────────────────────────


class TestSingleTaskHappyPath:
    def test_acks_and_pushes(self, project_root: Path):
        gerrit = wkr.StubGerritPusher()
        w = _make_worker(project_root, gerrit=gerrit, max_messages=1)
        mid = qb.push(_card("PROJ-100", allowed=["src/main.c"]))

        w.start()
        try:
            outcomes = w.run()
        finally:
            w.stop()

        assert len(outcomes) == 1
        out = outcomes[0]
        assert out.status == "acked"
        assert out.message_id == mid
        assert out.gerrit and out.gerrit.ok
        assert out.gerrit.change_id.startswith("I")

        # Queue is empty (acked → removed from in-memory backend).
        assert qb.depth() == 0
        # Gerrit pusher saw exactly one push with the right ticket.
        assert len(gerrit.pushed) == 1
        assert gerrit.pushed[0]["ticket"] == "PROJ-100"

    def test_dist_lock_released_after_ack(self, project_root: Path):
        w = _make_worker(project_root, max_messages=1)
        qb.push(_card("PROJ-101", allowed=["src/main.c"]))
        w.start()
        try:
            w.run()
        finally:
            w.stop()
        # Lock should be released — re-acquiring should succeed.
        res = dist_lock.acquire_paths("probe", ["src/main.c"])
        assert res.ok
        dist_lock.release_paths("probe")


# ──────────────────────────────────────────────────────────────
#  4. Filters: tenant + capability
# ──────────────────────────────────────────────────────────────


class TestFilters:
    def test_tenant_filter_matches_passes(self, project_root: Path):
        w = _make_worker(project_root, tenant_filter=["acme"],
                         max_messages=1)
        qb.push(_card("PROJ-200", tenant="acme"))
        w.start()
        try:
            outs = w.run()
        finally:
            w.stop()
        assert outs and outs[0].status == "acked"

    def test_tenant_filter_mismatch_returns_to_queue(self, project_root: Path):
        w = _make_worker(project_root, tenant_filter=["acme"],
                         max_messages=1)
        qb.push(_card("PROJ-201", tenant="other"))
        w.start()
        try:
            outs = w.run()
        finally:
            w.stop()
        assert outs and outs[0].status == "nacked"
        # Message goes back into Queued state.
        assert qb.depth(state=TaskState.Queued) == 1

    def test_capability_filter_match(self, project_root: Path):
        w = _make_worker(project_root, capability_filter=["firmware"],
                         max_messages=1)
        qb.push(_card("PROJ-202", handoff=["cap:firmware"]))
        w.start()
        try:
            outs = w.run()
        finally:
            w.stop()
        assert outs[0].status == "acked"

    def test_capability_filter_miss_requeues(self, project_root: Path):
        w = _make_worker(project_root, capability_filter=["firmware"],
                         max_messages=1)
        qb.push(_card("PROJ-203", handoff=["cap:vision"]))
        w.start()
        try:
            outs = w.run()
        finally:
            w.stop()
        assert outs[0].status == "nacked"
        assert qb.depth(state=TaskState.Queued) == 1


# ──────────────────────────────────────────────────────────────
#  5. Heartbeat + registration
# ──────────────────────────────────────────────────────────────


class TestHeartbeatRegistration:
    def test_register_on_start_deregister_on_stop(self, project_root: Path):
        store = wkr._MemoryHeartbeatStore()
        wkr.set_heartbeat_store_for_tests(store)
        w = _make_worker(project_root, worker_id="hb1", max_messages=0)
        w.config.loop_idle_s = 0.01
        w.start()
        try:
            assert "hb1" in store.list_active()
            info = store.get_info("hb1")
            assert info and info["worker_id"] == "hb1"
            assert info["capacity"] == 1
        finally:
            w.stop()
        assert "hb1" not in store.list_active()

    def test_heartbeat_refreshes_ttl(self, project_root: Path):
        store = wkr._MemoryHeartbeatStore()
        wkr.set_heartbeat_store_for_tests(store)
        w = _make_worker(project_root, worker_id="hb2", max_messages=0)
        w.config.heartbeat_interval_s = 0.05
        w.config.heartbeat_ttl_s = 1
        w.start()
        try:
            time.sleep(0.2)
            # Multiple heartbeats should have fired.
            info = store.get_info("hb2")
            assert info and info["status"] == "alive"
        finally:
            w.stop()

    def test_heartbeat_loss_drops_from_list_active(self, project_root: Path):
        store = wkr._MemoryHeartbeatStore()
        # Register manually with a tiny TTL — simulate a worker that
        # crashed between heartbeats.
        store.register("ghost", {"worker_id": "ghost"}, ttl_s=0)
        time.sleep(0.05)
        assert "ghost" not in store.list_active()


# ──────────────────────────────────────────────────────────────
#  6. Capacity > 1 — one worker drains N concurrently
# ──────────────────────────────────────────────────────────────


class _SlowExecutor:
    """Executor that sleeps so the test can catch concurrent in-flight."""

    def __init__(self, delay: float = 0.2) -> None:
        self.delay = delay
        self.peak_inflight = 0
        self._inflight = 0
        self._lock = threading.Lock()

    def run(self, *, handle: wkr.SandboxHandle, card: TaskCard,
            worker_id: str) -> wkr.AgentResult:
        with self._lock:
            self._inflight += 1
            self.peak_inflight = max(self.peak_inflight, self._inflight)
        time.sleep(self.delay)
        with self._lock:
            self._inflight -= 1
        return wkr.AgentResult(
            ok=True, commit_message=f"agent: {card.jira_ticket}",
        )


class TestCapacity:
    def test_capacity_limits_concurrent_inflight(self, project_root: Path):
        executor = _SlowExecutor(delay=0.15)
        w = _make_worker(project_root, capacity=3, max_messages=5,
                         executor=executor)
        for i in range(5):
            qb.push(_card(f"PROJ-{300 + i}",
                          allowed=[f"src/main_{i}.c"]))
        # Pre-create allowed files so the sandbox copy succeeds.
        for i in range(5):
            (project_root / "src" / f"main_{i}.c").write_text("//\n")

        w.start()
        try:
            outs = w.run()
        finally:
            w.stop(timeout_s=5)

        assert len(outs) == 5
        assert all(o.status == "acked" for o in outs)
        # Worker should have run several tasks in parallel — peak in-flight
        # should reach > 1 (at least 2 of the 3 capacity slots filled).
        assert executor.peak_inflight >= 2


# ──────────────────────────────────────────────────────────────
#  7. Graceful shutdown
# ──────────────────────────────────────────────────────────────


class TestGracefulShutdown:
    def test_stop_releases_locks_and_deregisters(self, project_root: Path):
        store = wkr._MemoryHeartbeatStore()
        wkr.set_heartbeat_store_for_tests(store)
        w = _make_worker(project_root, worker_id="gs1", max_messages=1)
        qb.push(_card("PROJ-400", allowed=["src/main.c"]))
        w.start()
        try:
            w.run()
        finally:
            w.stop()
        assert "gs1" not in store.list_active()
        # Lock free after shutdown.
        res = dist_lock.acquire_paths("probe", ["src/main.c"])
        assert res.ok
        dist_lock.release_paths("probe")

    def test_signal_handler_install_idempotent(self, project_root: Path):
        w = _make_worker(project_root, max_messages=0)
        w.install_signal_handlers()
        w.install_signal_handlers()  # second call should no-op
        assert w._signal_handlers_installed


# ──────────────────────────────────────────────────────────────
#  8. Lock conflict path (Blocked_by_Mutex)
# ──────────────────────────────────────────────────────────────


class TestLockConflict:
    def test_lock_conflict_returns_message_to_queue(self, project_root: Path):
        # Pre-acquire the lock in another "worker".
        held = dist_lock.acquire_paths(
            "external", ["src/main.c"], ttl_s=60,
        )
        assert held.ok

        try:
            w = _make_worker(project_root, max_messages=1)
            qb.push(_card("PROJ-500", allowed=["src/main.c"]))
            w.start()
            try:
                outs = w.run()
            finally:
                w.stop()
        finally:
            dist_lock.release_paths("external")

        assert outs and outs[0].status == "locked_skipped"
        # Message is back in queue.
        assert qb.depth() == 1


# ──────────────────────────────────────────────────────────────
#  9. Gerrit push retry
# ──────────────────────────────────────────────────────────────


class TestGerritRetry:
    def test_pusher_retries_then_succeeds(self):
        attempts = {"n": 0}

        def runner(_cwd: Path, _args: list[str]) -> tuple[int, str, str]:
            attempts["n"] += 1
            if attempts["n"] < 3:
                return (1, "", "transient: connection refused")
            return (0, "remote: https://gerrit/changes/123\n", "")

        pusher = wkr.GerritCommandPusher(
            runner=runner, max_retries=5, backoff_s=(0.001, 0.001, 0.001),
        )
        h = wkr.SandboxHandle(workspace=Path("."))
        res = pusher.push(handle=h, card=_card(), commit_sha="abc123",
                          change_id="Ifoo", worker_id="w1")
        assert res.ok and res.attempts == 3
        assert res.review_url == "https://gerrit/changes/123"

    def test_pusher_gives_up_after_max_retries(self):
        def runner(_cwd: Path, _args: list[str]) -> tuple[int, str, str]:
            return (1, "", "permanent failure")

        pusher = wkr.GerritCommandPusher(
            runner=runner, max_retries=2, backoff_s=(0.001,),
        )
        h = wkr.SandboxHandle(workspace=Path("."))
        res = pusher.push(handle=h, card=_card(), commit_sha="abc",
                          change_id="Ibar", worker_id="w1")
        assert not res.ok and res.attempts == 2
        assert "permanent failure" in res.reason

    def test_failed_push_nacks_message(self, project_root: Path):
        class _AlwaysFailGerrit:
            def push(self, **_kw):
                return wkr.GerritPushResult(
                    ok=False, change_id="Ifail", reason="503 unavailable",
                )

        w = _make_worker(project_root, gerrit=_AlwaysFailGerrit(),
                         max_messages=1)
        qb.push(_card("PROJ-600", allowed=["src/main.c"]))
        w.start()
        try:
            outs = w.run()
        finally:
            w.stop()
        assert outs[0].status == "nacked"
        assert "503" in (outs[0].error or "")
        # Message back to Queued (delivery_count=1, still under 3-strike).
        assert qb.depth() == 1


# ──────────────────────────────────────────────────────────────
# 10. Multi-worker fan-out + crash recovery
# ──────────────────────────────────────────────────────────────


class TestMultiWorkerFanout:
    def test_two_workers_share_queue_no_double_delivery(self, project_root: Path):
        # Push 6 distinct tasks (different impact_scope so no lock contention).
        ids = []
        for i in range(6):
            (project_root / "src" / f"file_{i}.c").write_text("//\n")
            ids.append(qb.push(_card(f"PROJ-{700 + i}",
                                     allowed=[f"src/file_{i}.c"])))

        w_a = _make_worker(project_root, worker_id="A", max_messages=3)
        w_b = _make_worker(project_root, worker_id="B", max_messages=3)
        w_a.start()
        w_b.start()
        try:
            t_a = threading.Thread(target=w_a.run)
            t_b = threading.Thread(target=w_b.run)
            t_a.start(); t_b.start()
            t_a.join(timeout=10); t_b.join(timeout=10)
        finally:
            w_a.stop()
            w_b.stop()

        all_outs = w_a._processed + w_b._processed
        assert len(all_outs) == 6
        # No message processed twice.
        assert len({o.message_id for o in all_outs}) == 6
        assert all(o.status == "acked" for o in all_outs)
        assert qb.depth() == 0

    def test_visibility_recovery_after_simulated_crash(self, project_root: Path):
        # Push a task, claim it (don't ack), force-expire visibility,
        # and verify another worker picks it up.
        mid = qb.push(_card("PROJ-800", allowed=["src/main.c"]))
        first = qb.pull("crashed-worker", count=1, visibility_timeout_s=0.01)
        assert first and first[0].message_id == mid
        time.sleep(0.05)
        sweep = qb.sweep_visibility()
        assert mid in sweep.requeued_message_ids

        w = _make_worker(project_root, worker_id="recover", max_messages=1)
        w.start()
        try:
            outs = w.run()
        finally:
            w.stop()
        assert outs[0].status == "acked"


# ──────────────────────────────────────────────────────────────
# 11. CLI surface (argparse + smoke)
# ──────────────────────────────────────────────────────────────


class TestCli:
    def test_arg_parser_has_run_and_list(self):
        p = wkr._build_arg_parser()
        # Both subcommands present:
        ns = p.parse_args(["run", "--capacity", "4",
                           "--tenant-filter", "acme,foo",
                           "--capability-filter", "vision"])
        assert ns.cmd == "run"
        assert ns.capacity == 4
        assert ns.tenant_filter == "acme,foo"
        assert ns.capability_filter == "vision"

        ns2 = p.parse_args(["list"])
        assert ns2.cmd == "list"

    def test_csv_parser_handles_blanks(self):
        assert wkr._parse_csv("") == []
        assert wkr._parse_csv("a, b ,, c") == ["a", "b", "c"]


# ──────────────────────────────────────────────────────────────
# 12. Integration — full E2E with metrics wired
# ──────────────────────────────────────────────────────────────


class TestE2E:
    def test_metrics_objects_exist_and_dont_crash(self, project_root: Path):
        from backend import metrics

        # Worker run should bump worker_task_total at least once.
        w = _make_worker(project_root, max_messages=1)
        qb.push(_card("PROJ-900", allowed=["src/main.c"]))
        w.start()
        try:
            w.run()
        finally:
            w.stop()

        # No-op stub OR real Counter — both must accept .labels(...).inc().
        metrics.worker_task_total.labels(outcome="acked").inc()
        metrics.worker_active.set(0)
        metrics.worker_inflight.set(0)
        metrics.worker_heartbeat_total.inc()
        metrics.worker_lifecycle_total.labels(event="start").inc()
        metrics.worker_task_seconds.observe(0.1)

    def test_priority_drained_first(self, project_root: Path):
        # P3 first, P0 second — worker should drain P0 first.
        (project_root / "src" / "p3.c").write_text("//\n")
        (project_root / "src" / "p0.c").write_text("//\n")
        qb.push(_card("PROJ-901", allowed=["src/p3.c"]),
                priority=PriorityLevel.P3)
        qb.push(_card("PROJ-902", allowed=["src/p0.c"]),
                priority=PriorityLevel.P0)

        w = _make_worker(project_root, max_messages=2)
        w.start()
        try:
            outs = w.run()
        finally:
            w.stop()
        assert outs[0].jira_ticket == "PROJ-902"
        assert outs[1].jira_ticket == "PROJ-901"
