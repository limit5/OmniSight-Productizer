"""H2 row 1517 — End-to-end integration test.

Drives the full H2 lifecycle through a mocked ``host_metrics`` ring
buffer in one flow:

  1. Install a high-pressure snapshot → ``_ModeSlot.acquire()`` must
     block on the precondition (no slot consumed, ``sandbox.deferred``
     SSE events fire with the right reason code).
  2. Hold sustained high CPU for the 30s sustain window (virtual clock)
     → ``coordinator.turbo_derate`` fires, the effective turbo budget
     drops to the supervised cap (8 → 2), and the state-machine flag
     flips to ``derate_active=True``.
  3. Clear the high-pressure snapshot AND hold CPU low for the full
     120s cooldown → ``coordinator.turbo_recover`` fires, the effective
     budget snaps back to 8, and the previously blocked acquire
     completes against the recovered cap.

Individual facets (precondition reason codes, backoff schedule,
derate state machine edge cases) are covered by the dedicated H2
test files. This file asserts that all three phases play correctly
together through the same host_metrics mock the production sampling
loop would feed.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from backend import decision_engine as de
from backend import host_metrics as hm


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Helpers — mock host_metrics snapshots
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _mk_snapshot(
    *,
    cpu: float = 10.0,
    mem: float = 20.0,
    containers: int = 2,
    sampled_at: float | None = None,
) -> hm.HostSnapshot:
    t = sampled_at if sampled_at is not None else time.time()
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
        sampled_at=t,
    )
    docker = hm.DockerSample(
        container_count=containers,
        total_mem_reservation_bytes=0,
        source="sdk",
        sampled_at=t,
    )
    return hm.HostSnapshot(host=host, docker=docker, sampled_at=t)


def _install_snapshot(snap: hm.HostSnapshot | None) -> None:
    hm._host_history.clear()
    if snap is not None:
        hm._host_history.append(snap)


class _EventCollector:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def publish(self, event: str, data: dict, **kwargs) -> None:
        merged = dict(data)
        merged.update({f"_{k}": v for k, v in kwargs.items()})
        self.events.append((event, merged))


class _AuditCollector:
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
    """Shrink the precondition backoff so the async acquire loop spins
    in ms rather than seconds — keeps the integration test wall-clock
    bounded without changing the logical schedule."""
    monkeypatch.setattr(de, "H2_BACKOFF_BASE_S", 0.005)
    monkeypatch.setattr(de, "H2_BACKOFF_CAP_S", 0.02)
    yield


@pytest.fixture
def bus_capture(monkeypatch) -> _EventCollector:
    collector = _EventCollector()
    import backend.events as _events
    monkeypatch.setattr(_events.bus, "publish", collector.publish)
    yield collector


@pytest.fixture
def audit_capture(monkeypatch) -> _AuditCollector:
    collector = _AuditCollector()
    import backend.audit as _audit
    monkeypatch.setattr(_audit, "log_sync", collector.log_sync)
    yield collector


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Full lifecycle — acquire block → derate → recover
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestFullPressureLifecycle:
    """One flow that touches all three H2 behaviours through the same
    mocked host_metrics surface production would use."""

    @pytest.mark.asyncio
    async def test_acquire_blocks_then_derate_engages_then_recovers(
        self, fast_backoff, bus_capture, audit_capture,
    ):
        # ─── Phase 1 — High pressure → acquire is blocked ────────────
        # Install a snapshot above the precondition CPU threshold. The
        # precondition must refuse to grant the slot and fire
        # sandbox.deferred events while the pressure persists.
        de.set_mode(de.OperationMode.turbo)
        _install_snapshot(_mk_snapshot(cpu=95.0))

        slot = de._ModeSlot()
        task = asyncio.create_task(slot.acquire())

        # Let the backoff loop iterate a few times. The slot counter
        # must stay at 0 — the waiter is deferred, not occupying.
        await asyncio.sleep(0.05)
        assert not task.done(), "acquire must block while CPU is hot"
        assert de.parallel_in_flight() == 0, \
            "deferred waiter must not consume a slot"

        deferred_events = [
            e for e in bus_capture.events if e[0] == "sandbox.deferred"
        ]
        assert deferred_events, "expected sandbox.deferred SSE events"
        assert any(
            e[1]["reason"] == de.H2_REASON_CPU for e in deferred_events
        ), "expected host_cpu_high reason in deferred payloads"
        # Audit rows mirror the SSE side — one per deferral attempt.
        assert any(
            r["action"] == "sandbox.deferred" for r in audit_capture.rows
        ), "expected sandbox.deferred audit rows"

        # ─── Phase 2 — Sustain high CPU → turbo derate engages ───────
        # Drive the derate state machine directly with a virtual clock
        # so we don't have to wait 30s of wall-clock. This mirrors how
        # the sampling loop would tick evaluate_turbo_derate() every 5s.
        t0 = 10_000.0
        assert de.evaluate_turbo_derate(now=t0, cpu_percent=95.0) is False
        # 30s later, still above threshold → engage derate.
        active = de.evaluate_turbo_derate(now=t0 + 30.0, cpu_percent=95.0)
        assert active is True, "sustained high CPU must engage derate"
        assert de.is_turbo_derated() is True

        derate_events = [
            e for e in bus_capture.events
            if e[0] == "coordinator.turbo_derate"
        ]
        assert len(derate_events) == 1
        payload = derate_events[0][1]
        assert payload["cpu_percent"] == 95.0
        assert payload["threshold_pct"] == de.H2_TURBO_DERATE_CPU_PCT
        assert payload["derated_to_budget"] == 2
        assert payload["from_budget"] == 8

        # The Phase-53 hash-chain audit row is written in the same
        # transition. Mirrors the SSE payload for reconstruction.
        derate_rows = [
            r for r in audit_capture.rows
            if r["action"] == "coordinator.turbo_derate"
        ]
        assert len(derate_rows) == 1
        assert derate_rows[0]["entity_kind"] == "turbo_derate"
        assert derate_rows[0]["entity_id"] == "engaged"
        assert derate_rows[0]["after"]["derate_active"] is True

        # While derated, the effective turbo budget drops to supervised.
        assert de._effective_budget(de.OperationMode.turbo) == 2

        # The precondition task is still blocked — high CPU is still
        # breaching the >=85% CPU rule regardless of derate state.
        assert not task.done()
        assert de.parallel_in_flight() == 0

        # ─── Phase 3 — Clear pressure → acquire unblocks, derate recovers
        # Swap in a clean snapshot. The precondition loop picks it up on
        # its next retry and the blocked acquire completes.
        _install_snapshot(_mk_snapshot(cpu=10.0, mem=20.0))
        await asyncio.wait_for(task, timeout=2.0)
        assert de.parallel_in_flight() == 1, \
            "slot must be held once the host clears"

        # The acquire path reads a wall-clock timestamp inside
        # evaluate_turbo_derate() on its way out. That would seed
        # low_cpu_since with the current wall-clock which skews the
        # virtual-clock cooldown arithmetic below. Reset it so this
        # phase drives the recovery path purely on the simulated clock.
        with de._state_lock:
            de._turbo_derate_state.low_cpu_since = None

        # Advance the virtual clock to exercise the full 120s recovery
        # cooldown. CPU must stay below threshold continuously — any
        # spike resets the cooldown (covered by the unit tests; here we
        # just drive the happy path).
        cooldown_start = t0 + 31.0  # 1s after engage
        assert de.evaluate_turbo_derate(
            now=cooldown_start, cpu_percent=50.0,
        ) is True, "cooldown starts — derate still active"
        # One tick before 120s elapses — still derated.
        assert de.evaluate_turbo_derate(
            now=cooldown_start + 119.0, cpu_percent=50.0,
        ) is True
        # Full 120s cooldown met → recover.
        recovered = de.evaluate_turbo_derate(
            now=cooldown_start + 120.0, cpu_percent=50.0,
        )
        assert recovered is False, "full cooldown must recover derate"
        assert de.is_turbo_derated() is False

        recover_events = [
            e for e in bus_capture.events
            if e[0] == "coordinator.turbo_recover"
        ]
        assert len(recover_events) == 1
        rpayload = recover_events[0][1]
        assert rpayload["restored_to_budget"] == 8
        assert rpayload["cooldown_required_s"] == de.H2_TURBO_RECOVER_COOLDOWN_S

        recover_rows = [
            r for r in audit_capture.rows
            if r["action"] == "coordinator.turbo_recover"
        ]
        assert len(recover_rows) == 1
        assert recover_rows[0]["entity_id"] == "recovered"
        assert recover_rows[0]["after"]["derate_active"] is False

        # Effective budget is back to turbo.
        assert de._effective_budget(de.OperationMode.turbo) == 8

        # Audit transcript is a complete engage → recover cycle in order.
        transition_actions = [
            r["action"] for r in audit_capture.rows
            if r["action"] in (
                "coordinator.turbo_derate", "coordinator.turbo_recover",
            )
        ]
        assert transition_actions == [
            "coordinator.turbo_derate",
            "coordinator.turbo_recover",
        ]

        slot.release()
        assert de.parallel_in_flight() == 0


class TestMemPressureBlocksAcquireSymmetry:
    """Parallel flow exercising mem_percent as the breaching axis — the
    precondition path is symmetrical across CPU / mem / container
    reasons, so we sanity-check the mem branch end-to-end once."""

    @pytest.mark.asyncio
    async def test_high_mem_blocks_then_releases_on_clear(
        self, fast_backoff, bus_capture, audit_capture,
    ):
        _install_snapshot(_mk_snapshot(cpu=10.0, mem=97.0))
        slot = de._ModeSlot()
        task = asyncio.create_task(slot.acquire())

        await asyncio.sleep(0.04)
        assert not task.done()
        assert de.parallel_in_flight() == 0

        reasons = [
            e[1]["reason"] for e in bus_capture.events
            if e[0] == "sandbox.deferred"
        ]
        assert de.H2_REASON_MEM in reasons

        _install_snapshot(_mk_snapshot(cpu=10.0, mem=30.0))
        await asyncio.wait_for(task, timeout=2.0)
        assert de.parallel_in_flight() == 1
        slot.release()


class TestContainerCapBlocksAcquireSymmetry:
    """Same lifecycle but tripped by the running-container cap."""

    @pytest.mark.asyncio
    async def test_over_container_cap_blocks_then_releases_on_clear(
        self, fast_backoff, bus_capture, audit_capture,
    ):
        _install_snapshot(_mk_snapshot(
            cpu=10.0, mem=20.0, containers=de.H2_CONTAINER_CAP + 5,
        ))
        slot = de._ModeSlot()
        task = asyncio.create_task(slot.acquire())

        await asyncio.sleep(0.04)
        assert not task.done()
        assert de.parallel_in_flight() == 0

        reasons = [
            e[1]["reason"] for e in bus_capture.events
            if e[0] == "sandbox.deferred"
        ]
        assert de.H2_REASON_CONTAINER in reasons

        _install_snapshot(_mk_snapshot(cpu=10.0, mem=20.0, containers=3))
        await asyncio.wait_for(task, timeout=2.0)
        assert de.parallel_in_flight() == 1
        slot.release()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Snapshot-driven derate via host_metrics (no explicit cpu_percent)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestSnapshotDrivenDerateRecoverCycle:
    """The production sampling loop calls ``evaluate_turbo_derate()``
    without passing *cpu_percent* — it reads the latest snapshot from
    ``host_metrics``. This exercises that path over a full cycle to
    prove the mock surface feeds the state machine correctly."""

    def test_snapshot_driven_engage_and_recover(self, bus_capture, audit_capture):
        # Install a hot snapshot; evaluate once — arms the sustain timer.
        _install_snapshot(_mk_snapshot(cpu=95.0))
        assert de.evaluate_turbo_derate(now=1000.0) is False

        # 30s later, still the same hot snapshot → engage.
        assert de.evaluate_turbo_derate(now=1030.0) is True
        assert de.is_turbo_derated() is True

        # Swap in a cool snapshot; the state machine should recover
        # after the 120s cooldown.
        _install_snapshot(_mk_snapshot(cpu=25.0))
        de.evaluate_turbo_derate(now=1031.0)  # starts cooldown
        assert de.is_turbo_derated() is True
        assert de.evaluate_turbo_derate(now=1151.0) is False
        assert de.is_turbo_derated() is False

        derate = [e for e in bus_capture.events if e[0] == "coordinator.turbo_derate"]
        recover = [e for e in bus_capture.events if e[0] == "coordinator.turbo_recover"]
        assert len(derate) == 1 and len(recover) == 1
