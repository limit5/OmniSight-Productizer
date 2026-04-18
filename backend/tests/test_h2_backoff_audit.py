"""H2 — exponential backoff + sandbox.deferred audit emit.

Covers the second bullet of the H2 TODO block: when the host-load
precondition refuses to grant a slot, the waiter must:

  * not hold a slot (_shared_parallel stays at 0)
  * emit a ``sandbox.deferred`` SSE event with the breaching reason
    code (``host_cpu_high`` / ``host_mem_high`` / ``container_cap``)
  * write a ``sandbox.deferred`` audit row (via audit.log_sync)
  * sleep for an exponentially growing interval capped at
    ``H2_BACKOFF_CAP_S`` (30s)

The pure-function precondition semantics live in
``test_h2_precondition.py`` — this file only exercises the
backoff/audit layer on top.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from backend import decision_engine as de
from backend import host_metrics as hm


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _mk_snapshot(*, cpu: float = 10.0, mem: float = 20.0,
                 containers: int = 2) -> hm.HostSnapshot:
    now = time.time()
    host = hm.HostSample(
        cpu_percent=cpu,
        mem_percent=mem,
        mem_used_gb=mem * 0.64,
        mem_total_gb=64.0,
        disk_percent=10.0,
        disk_used_gb=51.2,
        disk_total_gb=512.0,
        loadavg_1m=1.0,
        loadavg_5m=1.0,
        loadavg_15m=1.0,
        sampled_at=now,
    )
    docker = hm.DockerSample(
        container_count=containers,
        total_mem_reservation_bytes=0,
        source="sdk",
        sampled_at=now,
    )
    return hm.HostSnapshot(host=host, docker=docker, sampled_at=now)


def _install_snapshot(snap: hm.HostSnapshot | None) -> None:
    hm._host_history.clear()
    if snap is not None:
        hm._host_history.append(snap)


class _EventCollector:
    """Drop-in replacement for events.bus that records publishes."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def publish(self, event: str, data: dict, **kwargs) -> None:
        merged = dict(data)
        merged.update({f"_{k}": v for k, v in kwargs.items()})
        self.events.append((event, merged))


class _AuditCollector:
    """Captures audit.log_sync calls without hitting the DB."""

    def __init__(self) -> None:
        self.rows: list[dict] = []

    def log_sync(self, **kwargs) -> None:
        self.rows.append(dict(kwargs))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fixtures
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.fixture(autouse=True)
def _reset():
    de._reset_for_tests()
    hm._reset_for_tests()
    yield
    de._reset_for_tests()
    hm._reset_for_tests()


@pytest.fixture
def fast_backoff(monkeypatch):
    """Scale backoff to ms-range so tests finish fast while still
    exercising the exponential ramp + cap behaviour."""
    monkeypatch.setattr(de, "H2_BACKOFF_BASE_S", 0.01)
    monkeypatch.setattr(de, "H2_BACKOFF_CAP_S", 0.05)
    yield


@pytest.fixture
def audit_capture(monkeypatch) -> _AuditCollector:
    """Replace backend.audit.log_sync so calls don't touch the DB."""
    collector = _AuditCollector()
    import backend.audit as _audit
    monkeypatch.setattr(_audit, "log_sync", collector.log_sync)
    yield collector


@pytest.fixture
def bus_capture(monkeypatch) -> _EventCollector:
    """Replace events.bus.publish with a recorder."""
    collector = _EventCollector()
    import backend.events as _events
    monkeypatch.setattr(_events.bus, "publish", collector.publish)
    yield collector


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pure backoff schedule
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestBackoffSchedule:

    def test_first_attempt_uses_base(self, monkeypatch):
        monkeypatch.setattr(de, "H2_BACKOFF_BASE_S", 1.0)
        monkeypatch.setattr(de, "H2_BACKOFF_CAP_S", 30.0)
        assert de._h2_backoff_delay(1) == 1.0

    def test_doubles_each_attempt(self, monkeypatch):
        monkeypatch.setattr(de, "H2_BACKOFF_BASE_S", 1.0)
        monkeypatch.setattr(de, "H2_BACKOFF_CAP_S", 30.0)
        assert de._h2_backoff_delay(2) == 2.0
        assert de._h2_backoff_delay(3) == 4.0
        assert de._h2_backoff_delay(4) == 8.0
        assert de._h2_backoff_delay(5) == 16.0

    def test_capped_at_30s(self, monkeypatch):
        monkeypatch.setattr(de, "H2_BACKOFF_BASE_S", 1.0)
        monkeypatch.setattr(de, "H2_BACKOFF_CAP_S", 30.0)
        # 2^5 = 32 > 30 → cap
        assert de._h2_backoff_delay(6) == 30.0
        # later attempts stay at the cap
        assert de._h2_backoff_delay(100) == 30.0

    def test_zero_or_negative_attempt_is_zero(self):
        assert de._h2_backoff_delay(0) == 0.0
        assert de._h2_backoff_delay(-1) == 0.0

    def test_cap_env_override(self, monkeypatch):
        # A tighter cap caps earlier.
        monkeypatch.setattr(de, "H2_BACKOFF_BASE_S", 1.0)
        monkeypatch.setattr(de, "H2_BACKOFF_CAP_S", 5.0)
        assert de._h2_backoff_delay(10) == 5.0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  End-to-end: acquire emits sandbox.deferred + doesn't hold a slot
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestAcquireBackoffBehavior:

    @pytest.mark.asyncio
    async def test_does_not_occupy_slot_while_deferred(
        self, fast_backoff, audit_capture, bus_capture,
    ):
        _install_snapshot(_mk_snapshot(cpu=95.0))
        slot = de._ModeSlot()
        task = asyncio.create_task(slot.acquire())

        # While still deferred, no slot consumed.
        await asyncio.sleep(0.03)
        assert not task.done()
        assert de.parallel_in_flight() == 0

        # At least one deferred event was emitted.
        deferred_events = [e for e in bus_capture.events
                           if e[0] == "sandbox.deferred"]
        assert deferred_events, \
            "expected at least one sandbox.deferred event before clearing"

        # Clear pressure → acquire completes.
        _install_snapshot(_mk_snapshot(cpu=10.0))
        await asyncio.wait_for(task, timeout=1.0)
        assert de.parallel_in_flight() == 1
        slot.release()

    @pytest.mark.asyncio
    async def test_cpu_reason_surfaced_in_event(
        self, fast_backoff, audit_capture, bus_capture,
    ):
        _install_snapshot(_mk_snapshot(cpu=95.0))
        slot = de._ModeSlot()
        task = asyncio.create_task(slot.acquire())
        await asyncio.sleep(0.03)
        _install_snapshot(_mk_snapshot(cpu=5.0))
        await asyncio.wait_for(task, timeout=1.0)
        slot.release()

        reasons = [e[1]["reason"] for e in bus_capture.events
                   if e[0] == "sandbox.deferred"]
        assert de.H2_REASON_CPU in reasons

    @pytest.mark.asyncio
    async def test_mem_reason_surfaced_in_event(
        self, fast_backoff, audit_capture, bus_capture,
    ):
        _install_snapshot(_mk_snapshot(cpu=10.0, mem=97.5))
        slot = de._ModeSlot()
        task = asyncio.create_task(slot.acquire())
        await asyncio.sleep(0.03)
        _install_snapshot(_mk_snapshot(cpu=10.0, mem=30.0))
        await asyncio.wait_for(task, timeout=1.0)
        slot.release()

        reasons = [e[1]["reason"] for e in bus_capture.events
                   if e[0] == "sandbox.deferred"]
        assert de.H2_REASON_MEM in reasons

    @pytest.mark.asyncio
    async def test_container_reason_surfaced_in_event(
        self, fast_backoff, audit_capture, bus_capture,
    ):
        _install_snapshot(_mk_snapshot(
            cpu=10.0, mem=20.0, containers=de.H2_CONTAINER_CAP + 5,
        ))
        slot = de._ModeSlot()
        task = asyncio.create_task(slot.acquire())
        await asyncio.sleep(0.03)
        _install_snapshot(_mk_snapshot(cpu=10.0, mem=20.0, containers=2))
        await asyncio.wait_for(task, timeout=1.0)
        slot.release()

        reasons = [e[1]["reason"] for e in bus_capture.events
                   if e[0] == "sandbox.deferred"]
        assert de.H2_REASON_CONTAINER in reasons

    @pytest.mark.asyncio
    async def test_audit_row_written_per_defer(
        self, fast_backoff, audit_capture, bus_capture,
    ):
        _install_snapshot(_mk_snapshot(cpu=95.0))
        slot = de._ModeSlot()
        task = asyncio.create_task(slot.acquire())
        await asyncio.sleep(0.03)
        _install_snapshot(_mk_snapshot(cpu=5.0))
        await asyncio.wait_for(task, timeout=1.0)
        slot.release()

        assert audit_capture.rows, \
            "expected sandbox.deferred audit rows"
        row = audit_capture.rows[0]
        assert row["action"] == "sandbox.deferred"
        assert row["entity_kind"] == "sandbox_slot"
        assert row["entity_id"] == de.H2_REASON_CPU
        assert "reason" in row["after"]
        assert "attempt" in row["after"]
        assert "delay_s" in row["after"]
        assert row["after"]["backoff_cap_s"] == de.H2_BACKOFF_CAP_S

    @pytest.mark.asyncio
    async def test_attempts_increment_monotonically(
        self, fast_backoff, audit_capture, bus_capture,
    ):
        """Each successive deferral carries attempt=N, N+1, N+2 …"""
        _install_snapshot(_mk_snapshot(cpu=95.0))
        slot = de._ModeSlot()
        task = asyncio.create_task(slot.acquire())
        # Let several backoff cycles run before clearing.
        await asyncio.sleep(0.15)
        _install_snapshot(_mk_snapshot(cpu=5.0))
        await asyncio.wait_for(task, timeout=1.0)
        slot.release()

        attempts = [e[1]["attempt"] for e in bus_capture.events
                    if e[0] == "sandbox.deferred"]
        assert attempts == list(range(1, len(attempts) + 1)), \
            f"attempts must be 1,2,3,… got {attempts}"

    @pytest.mark.asyncio
    async def test_delay_respects_cap(
        self, fast_backoff, audit_capture, bus_capture,
    ):
        """Even with many failed attempts the reported delay never
        exceeds H2_BACKOFF_CAP_S."""
        _install_snapshot(_mk_snapshot(cpu=95.0))
        slot = de._ModeSlot()
        task = asyncio.create_task(slot.acquire())
        await asyncio.sleep(0.5)  # plenty of cycles at 0.01s base
        _install_snapshot(_mk_snapshot(cpu=5.0))
        await asyncio.wait_for(task, timeout=1.0)
        slot.release()

        delays = [e[1]["delay_s"] for e in bus_capture.events
                  if e[0] == "sandbox.deferred"]
        assert delays, "expected at least one deferred event"
        assert max(delays) <= de.H2_BACKOFF_CAP_S + 1e-9

    @pytest.mark.asyncio
    async def test_30s_cap_default(self, monkeypatch, audit_capture, bus_capture):
        """The default cap is 30s per TODO spec."""
        # Run at default thresholds but stub sleep to avoid waiting.
        sleeps: list[float] = []

        async def _fake_sleep(d):
            sleeps.append(d)
            # break out of loop after a handful of attempts by
            # clearing the pressure
            if len(sleeps) >= 8:
                _install_snapshot(_mk_snapshot(cpu=5.0))

        monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
        _install_snapshot(_mk_snapshot(cpu=95.0))
        slot = de._ModeSlot()
        await slot.acquire()
        slot.release()

        # attempt 1..7 = 1,2,4,8,16,32→30,30  — verify the 30s cap
        assert 30.0 in sleeps, f"expected 30s cap in the schedule, got {sleeps}"
        assert all(d <= 30.0 + 1e-9 for d in sleeps)

    @pytest.mark.asyncio
    async def test_clean_host_emits_no_deferred_event(
        self, fast_backoff, audit_capture, bus_capture,
    ):
        _install_snapshot(_mk_snapshot(cpu=5.0))
        slot = de._ModeSlot()
        await slot.acquire()
        slot.release()
        deferred = [e for e in bus_capture.events
                    if e[0] == "sandbox.deferred"]
        assert not deferred
        assert not audit_capture.rows

    @pytest.mark.asyncio
    async def test_audit_failure_does_not_break_acquire(
        self, fast_backoff, bus_capture, monkeypatch,
    ):
        """A broken audit module must NOT stall or crash acquire."""
        import backend.audit as _audit

        def _boom(**kwargs):
            raise RuntimeError("simulated audit failure")

        monkeypatch.setattr(_audit, "log_sync", _boom)

        _install_snapshot(_mk_snapshot(cpu=95.0))
        slot = de._ModeSlot()
        task = asyncio.create_task(slot.acquire())
        await asyncio.sleep(0.03)
        _install_snapshot(_mk_snapshot(cpu=5.0))
        await asyncio.wait_for(task, timeout=1.0)
        assert de.parallel_in_flight() == 1
        slot.release()
