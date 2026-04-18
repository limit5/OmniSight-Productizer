"""H2 — host-load-aware precondition on ``_ModeSlot.acquire()``.

Covers the first bullet of the H2 TODO block:
  * ``cpu_percent >= 85`` blocks acquire (reason ``host_cpu_high``)
  * ``mem_percent >= 85`` blocks acquire (reason ``host_mem_high``)
  * ``container_count >= K`` blocks acquire (reason ``container_cap``)
  * Clean host grants the slot immediately
  * No snapshot (cold start) grants the slot (fail-open)
  * Unblocking happens as soon as the host pressure clears
  * Reason-code precedence: CPU → mem → container

The backoff / audit-emit layer is a separate TODO item and is NOT
exercised here. This test file asserts precondition semantics only.
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
    """Replace the ring buffer's single entry with ``snap``.

    Passing ``None`` leaves the ring empty — the cold-start case.
    """
    hm._host_history.clear()
    if snap is not None:
        hm._host_history.append(snap)


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


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pure reason-code function
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestHostPreconditionReason:

    def test_clean_host_returns_none(self):
        _install_snapshot(_mk_snapshot(cpu=10.0, mem=20.0, containers=2))
        assert de._host_precondition_reason() is None

    def test_cold_start_no_snapshot_fails_open(self):
        _install_snapshot(None)
        assert de._host_precondition_reason() is None

    def test_cpu_over_threshold_flags_host_cpu_high(self):
        _install_snapshot(_mk_snapshot(cpu=de.H2_CPU_HIGH_PCT, mem=20.0))
        assert de._host_precondition_reason() == de.H2_REASON_CPU

    def test_cpu_just_above_threshold_flags_host_cpu_high(self):
        _install_snapshot(_mk_snapshot(cpu=de.H2_CPU_HIGH_PCT + 2.5, mem=20.0))
        assert de._host_precondition_reason() == de.H2_REASON_CPU

    def test_cpu_just_below_threshold_passes(self):
        # 84.99% is below the >=85 rule → no reason.
        _install_snapshot(_mk_snapshot(cpu=de.H2_CPU_HIGH_PCT - 0.01, mem=20.0))
        assert de._host_precondition_reason() is None

    def test_mem_over_threshold_flags_host_mem_high(self):
        _install_snapshot(_mk_snapshot(cpu=10.0, mem=de.H2_MEM_HIGH_PCT + 1.0))
        assert de._host_precondition_reason() == de.H2_REASON_MEM

    def test_container_cap_flags_container_cap(self):
        _install_snapshot(_mk_snapshot(
            cpu=10.0, mem=20.0, containers=de.H2_CONTAINER_CAP,
        ))
        assert de._host_precondition_reason() == de.H2_REASON_CONTAINER

    def test_cpu_precedence_over_mem_and_container(self):
        # All three breached → CPU wins (matches spec ordering).
        _install_snapshot(_mk_snapshot(
            cpu=99.0, mem=99.0, containers=de.H2_CONTAINER_CAP + 10,
        ))
        assert de._host_precondition_reason() == de.H2_REASON_CPU

    def test_mem_precedence_over_container(self):
        _install_snapshot(_mk_snapshot(
            cpu=10.0, mem=99.0, containers=de.H2_CONTAINER_CAP + 10,
        ))
        assert de._host_precondition_reason() == de.H2_REASON_MEM


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  End-to-end: acquire is blocked and unblocked
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestAcquireBlockedByPrecondition:
    """``_ModeSlot.acquire()`` must wait while the precondition is
    breached and proceed as soon as the host clears."""

    @pytest.mark.asyncio
    async def test_clean_host_acquires_immediately(self):
        _install_snapshot(_mk_snapshot(cpu=10.0, mem=20.0))
        slot = de._ModeSlot()
        # Should return inside a small timeout — no host pressure.
        await asyncio.wait_for(slot.acquire(), timeout=0.5)
        assert de.parallel_in_flight() == 1
        slot.release()
        assert de.parallel_in_flight() == 0

    @pytest.mark.asyncio
    async def test_high_cpu_blocks_then_releases(self):
        _install_snapshot(_mk_snapshot(cpu=95.0, mem=20.0))
        slot = de._ModeSlot()

        task = asyncio.create_task(slot.acquire())
        # Give the task a few ticks to attempt + block.
        await asyncio.sleep(0.1)
        assert not task.done(), "acquire should be blocked while CPU is hot"
        assert de.parallel_in_flight() == 0, "no slot must be consumed"

        # Clear pressure → acquire completes.
        _install_snapshot(_mk_snapshot(cpu=10.0, mem=20.0))
        await asyncio.wait_for(task, timeout=3.0)
        assert de.parallel_in_flight() == 1
        slot.release()

    @pytest.mark.asyncio
    async def test_high_mem_blocks_then_releases(self):
        _install_snapshot(_mk_snapshot(cpu=10.0, mem=97.5))
        slot = de._ModeSlot()
        task = asyncio.create_task(slot.acquire())
        await asyncio.sleep(0.1)
        assert not task.done()
        assert de.parallel_in_flight() == 0

        _install_snapshot(_mk_snapshot(cpu=10.0, mem=40.0))
        await asyncio.wait_for(task, timeout=3.0)
        slot.release()

    @pytest.mark.asyncio
    async def test_container_cap_blocks_then_releases(self):
        _install_snapshot(_mk_snapshot(
            cpu=10.0, mem=20.0, containers=de.H2_CONTAINER_CAP + 5,
        ))
        slot = de._ModeSlot()
        task = asyncio.create_task(slot.acquire())
        await asyncio.sleep(0.1)
        assert not task.done()
        assert de.parallel_in_flight() == 0

        _install_snapshot(_mk_snapshot(
            cpu=10.0, mem=20.0, containers=3,
        ))
        await asyncio.wait_for(task, timeout=3.0)
        slot.release()

    @pytest.mark.asyncio
    async def test_cold_start_does_not_block(self):
        """No snapshot yet (sampling loop hasn't fired) must not deadlock."""
        _install_snapshot(None)
        slot = de._ModeSlot()
        await asyncio.wait_for(slot.acquire(), timeout=0.5)
        slot.release()

    @pytest.mark.asyncio
    async def test_host_metrics_import_failure_fails_open(self, monkeypatch):
        """If host_metrics is unimportable the precondition must fail open
        — dev/test runners without psutil shouldn't deadlock acquire()."""
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "backend.host_metrics" or name.endswith(".host_metrics"):
                raise ImportError("simulated host_metrics unavailable")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        assert de._host_precondition_reason() is None
