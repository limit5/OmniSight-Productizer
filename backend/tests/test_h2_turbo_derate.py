"""H2 row 1513 — Turbo auto-derate on sustained high CPU.

Covers the state machine that temporarily drops a turbo-mode session's
parallel budget (8 → 2, supervised) when host CPU stays above 80% for
at least 30s, and auto-restores it once CPU drops below threshold and
holds there for 2 minutes.

The state machine is driven by :func:`decision_engine.evaluate_turbo_derate`
— called from both the host sampling loop (5s cadence) and every
``_ModeSlot.acquire()``. Here we drive it directly with synthetic
``now`` + ``cpu_percent`` values so the test stays deterministic and
does not need wall-clock delays.
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
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def publish(self, event: str, data: dict, **kwargs) -> None:
        merged = dict(data)
        merged.update({f"_{k}": v for k, v in kwargs.items()})
        self.events.append((event, merged))


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
def bus_capture(monkeypatch) -> _EventCollector:
    collector = _EventCollector()
    import backend.events as _events
    monkeypatch.setattr(_events.bus, "publish", collector.publish)
    yield collector


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pure state machine: evaluate_turbo_derate
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestEvaluateTurboDerate:

    def test_clean_host_stays_inactive(self):
        assert de.evaluate_turbo_derate(now=1000.0, cpu_percent=30.0) is False
        assert de.is_turbo_derated() is False

    def test_below_or_equal_threshold_does_not_arm(self):
        """Threshold is strict >80; 80 exactly should not arm sustain."""
        assert de.evaluate_turbo_derate(now=1000.0, cpu_percent=80.0) is False
        # 30s later still at 80 → no derate
        assert de.evaluate_turbo_derate(now=1030.0, cpu_percent=80.0) is False
        snap = de.turbo_derate_snapshot()
        assert snap["derate_active"] is False
        assert snap["high_cpu_since"] is None

    def test_brief_spike_under_30s_does_not_derate(self):
        assert de.evaluate_turbo_derate(now=1000.0, cpu_percent=95.0) is False
        # 29.9s later — still under the sustained window
        assert de.evaluate_turbo_derate(now=1029.9, cpu_percent=95.0) is False
        assert de.is_turbo_derated() is False

    def test_sustained_30s_triggers_derate(self):
        de.evaluate_turbo_derate(now=1000.0, cpu_percent=95.0)
        # Exactly 30s of continuous above-threshold → derate engaged.
        active = de.evaluate_turbo_derate(now=1030.0, cpu_percent=95.0)
        assert active is True
        assert de.is_turbo_derated() is True
        snap = de.turbo_derate_snapshot()
        assert snap["last_transition_at"] == pytest.approx(1030.0)

    def test_drop_below_threshold_clears_sustain_timer(self):
        """CPU dip before the 30s window elapses resets the counter."""
        de.evaluate_turbo_derate(now=1000.0, cpu_percent=95.0)
        # Dip to 50% at t+20s — clears sustain timer
        de.evaluate_turbo_derate(now=1020.0, cpu_percent=50.0)
        # Climb back to 95% at t+25s — timer starts over here
        de.evaluate_turbo_derate(now=1025.0, cpu_percent=95.0)
        # Original t+40s is only 15s of continuous hotness → no derate
        assert de.evaluate_turbo_derate(now=1040.0, cpu_percent=95.0) is False
        # Need another full sustain window from t+25s → t+55s
        assert de.evaluate_turbo_derate(now=1055.0, cpu_percent=95.0) is True


class TestRecoveryCooldown:

    def _engage_derate(self, *, at: float) -> None:
        de.evaluate_turbo_derate(now=at, cpu_percent=95.0)
        de.evaluate_turbo_derate(now=at + 30.0, cpu_percent=95.0)
        assert de.is_turbo_derated() is True

    def test_recover_only_after_full_2min_cooldown(self):
        self._engage_derate(at=1000.0)
        # CPU cleared at t+30s (engage time)
        de.evaluate_turbo_derate(now=1031.0, cpu_percent=50.0)
        # 119s into cooldown — not yet
        assert de.evaluate_turbo_derate(now=1150.0, cpu_percent=50.0) is True
        # Full 120s cooldown since t+31 → t+151s
        assert de.evaluate_turbo_derate(now=1151.0, cpu_percent=50.0) is False
        assert de.is_turbo_derated() is False

    def test_cpu_spike_during_cooldown_resets_cooldown(self):
        self._engage_derate(at=1000.0)
        # CPU drops at t+31s — cooldown timer starts
        de.evaluate_turbo_derate(now=1031.0, cpu_percent=50.0)
        # Brief spike at t+60s — cooldown resets, still derated
        de.evaluate_turbo_derate(now=1060.0, cpu_percent=95.0)
        assert de.is_turbo_derated() is True
        # CPU drops again at t+65s — cooldown restarts from here
        de.evaluate_turbo_derate(now=1065.0, cpu_percent=50.0)
        # At t+150s (original) it would have recovered; with restart it's
        # only 85s into the new cooldown — still derated.
        assert de.evaluate_turbo_derate(now=1150.0, cpu_percent=50.0) is True
        # Must wait until t+185s (65 + 120) for auto-recover.
        assert de.evaluate_turbo_derate(now=1185.0, cpu_percent=50.0) is False

    def test_recover_at_exactly_120s_cooldown(self):
        self._engage_derate(at=1000.0)
        de.evaluate_turbo_derate(now=1030.0, cpu_percent=50.0)
        # Cooldown started at t+30 → recover at t+30+120=t+150
        assert de.evaluate_turbo_derate(now=1150.0, cpu_percent=50.0) is False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SSE events on transitions
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestTransitionEvents:

    def test_derate_emits_coordinator_turbo_derate(self, bus_capture):
        de.evaluate_turbo_derate(now=1000.0, cpu_percent=95.0)
        assert not [e for e in bus_capture.events
                    if e[0] == "coordinator.turbo_derate"]
        de.evaluate_turbo_derate(now=1030.0, cpu_percent=95.0)
        derates = [e for e in bus_capture.events
                   if e[0] == "coordinator.turbo_derate"]
        assert len(derates) == 1
        payload = derates[0][1]
        assert payload["cpu_percent"] == 95.0
        assert payload["threshold_pct"] == 80.0
        assert payload["derated_to_budget"] == 2
        assert payload["from_budget"] == 8

    def test_recover_emits_coordinator_turbo_recover(self, bus_capture):
        de.evaluate_turbo_derate(now=1000.0, cpu_percent=95.0)
        de.evaluate_turbo_derate(now=1030.0, cpu_percent=95.0)
        de.evaluate_turbo_derate(now=1031.0, cpu_percent=50.0)
        de.evaluate_turbo_derate(now=1151.0, cpu_percent=50.0)
        recovers = [e for e in bus_capture.events
                    if e[0] == "coordinator.turbo_recover"]
        assert len(recovers) == 1
        payload = recovers[0][1]
        assert payload["restored_to_budget"] == 8
        assert payload["cooldown_required_s"] == 120.0

    def test_no_event_while_stable(self, bus_capture):
        # Staying below threshold should produce zero transition events.
        for t in range(1000, 1060, 5):
            de.evaluate_turbo_derate(now=float(t), cpu_percent=30.0)
        transition_events = [
            e for e in bus_capture.events
            if e[0] in ("coordinator.turbo_derate", "coordinator.turbo_recover")
        ]
        assert transition_events == []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Integration with _ModeSlot parallel budget
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestEffectiveBudgetInSlot:

    def test_effective_budget_helper_tracks_state(self):
        # Non-turbo modes are never affected.
        assert de._effective_budget(de.OperationMode.supervised) == 2
        assert de._effective_budget(de.OperationMode.full_auto) == 4
        assert de._effective_budget(de.OperationMode.turbo) == 8
        # Force derate on.
        with de._state_lock:
            de._turbo_derate_state.derate_active = True
        assert de._effective_budget(de.OperationMode.turbo) == 2
        assert de._effective_budget(de.OperationMode.full_auto) == 4
        assert de._effective_budget(de.OperationMode.supervised) == 2
        assert de._effective_budget(de.OperationMode.manual) == 1

    @pytest.mark.asyncio
    async def test_turbo_slot_respects_derated_cap(self):
        de.set_mode(de.OperationMode.turbo)
        _install_snapshot(_mk_snapshot(cpu=10.0))
        # Force derate active via direct state mutation (pure-logic path
        # is already covered — this test exercises the _ModeSlot cap).
        with de._state_lock:
            de._turbo_derate_state.derate_active = True

        slot = de._ModeSlot()
        # Cap is now supervised=2, not turbo=8.
        assert slot._get_cap() == 2

        # Fill two slots — third must block.
        held = [de._ModeSlot() for _ in range(3)]
        await held[0].acquire()
        await held[1].acquire()
        assert de.parallel_in_flight() == 2

        task = asyncio.create_task(held[2].acquire())
        await asyncio.sleep(0.05)
        assert not task.done(), "third slot must block while derated"
        assert de.parallel_in_flight() == 2

        # Release one via __aexit__ (which notifies) — waiter proceeds.
        await held[0].__aexit__(None, None, None)
        await asyncio.wait_for(task, timeout=1.0)
        assert de.parallel_in_flight() == 2
        await held[1].__aexit__(None, None, None)
        await held[2].__aexit__(None, None, None)

    @pytest.mark.asyncio
    async def test_recovery_lets_cap_grow_back_to_turbo(self):
        de.set_mode(de.OperationMode.turbo)
        # Force derate ON to establish the lower cap.
        with de._state_lock:
            de._turbo_derate_state.derate_active = True
        _install_snapshot(_mk_snapshot(cpu=10.0))
        slot = de._ModeSlot()
        assert slot._get_cap() == 2
        # Now simulate recovery by clearing the flag.
        with de._state_lock:
            de._turbo_derate_state.derate_active = False
        # Cap snaps back to turbo=8 for new acquirers.
        assert slot._get_cap() == 8

    @pytest.mark.asyncio
    async def test_non_turbo_unaffected_by_derate(self):
        de.set_mode(de.OperationMode.full_auto)
        _install_snapshot(_mk_snapshot(cpu=10.0))
        with de._state_lock:
            de._turbo_derate_state.derate_active = True
        slot = de._ModeSlot()
        # full_auto = 4 regardless of derate.
        assert slot._get_cap() == 4


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Snapshot-driven evaluation (reads host_metrics)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestSnapshotDrivenEvaluation:
    """``evaluate_turbo_derate()`` without explicit cpu_percent reads
    the latest HostSnapshot from host_metrics."""

    def test_reads_latest_snapshot_when_cpu_omitted(self):
        _install_snapshot(_mk_snapshot(cpu=95.0))
        de.evaluate_turbo_derate(now=1000.0)
        # Second sample still hot → triggers sustained-derate path
        de.evaluate_turbo_derate(now=1030.0)
        assert de.is_turbo_derated() is True

    def test_cold_start_no_snapshot_does_not_mutate(self):
        _install_snapshot(None)
        de.evaluate_turbo_derate(now=1000.0)
        snap = de.turbo_derate_snapshot()
        assert snap["derate_active"] is False
        assert snap["high_cpu_since"] is None

    def test_host_metrics_import_failure_fails_open(self, monkeypatch):
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name.endswith("host_metrics"):
                raise ImportError("simulated host_metrics unavailable")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        # Should not raise; state stays the same.
        de.evaluate_turbo_derate(now=1000.0)
        assert de.is_turbo_derated() is False


class TestTurboDerateSnapshot:

    def test_snapshot_exposes_thresholds(self):
        snap = de.turbo_derate_snapshot()
        assert snap["threshold_pct"] == 80.0
        assert snap["sustain_required_s"] == 30.0
        assert snap["cooldown_required_s"] == 120.0
        assert snap["derate_active"] is False

    def test_snapshot_reflects_live_state(self):
        de.evaluate_turbo_derate(now=1000.0, cpu_percent=95.0)
        snap = de.turbo_derate_snapshot()
        assert snap["high_cpu_since"] == 1000.0
        assert snap["derate_active"] is False

        de.evaluate_turbo_derate(now=1030.0, cpu_percent=95.0)
        snap = de.turbo_derate_snapshot()
        assert snap["derate_active"] is True
        assert snap["last_transition_at"] == 1030.0
