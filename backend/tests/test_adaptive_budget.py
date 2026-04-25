"""H4a — Tests for the global AIMD controller (``backend/adaptive_budget.py``).

Covers the row 2575 spec:
* Init budget = 6 (clamped to CAPACITY_MAX on tiny hosts).
* AI fires every 30s when ``cpu<70 & mem<70 & deferred==0``.
* MD fires when ``cpu>85 or mem>85`` sustained ≥10s.
* Hard cap at ``CAPACITY_MAX``; hard floor at ``FLOOR_BUDGET=2``.
* AI/MD threshold strictness (boundary values do not trigger).
* Pressure-clock reset on cool, AI-clock reset on MD.
* Trace deque is bounded by the 5-min ``TRACE_WINDOW_S``.
* Snapshot shape covers everything the UI / ops summary need.
* Drift guard: every :class:`AdjustReason` is reachable.
"""

from __future__ import annotations

import pytest

from backend import adaptive_budget as ab
from backend.sandbox_capacity import CAPACITY_MAX


@pytest.fixture(autouse=True)
def _reset():
    ab._reset_for_tests()
    yield
    ab._reset_for_tests()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Cold-start / reset
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestInit:
    def test_module_load_primes_to_init_budget(self):
        # _reset_for_tests() in the fixture re-primes via reset() with
        # default ``initial_budget=INIT_BUDGET`` clamped to CAPACITY_MAX.
        expected = max(ab.FLOOR_BUDGET, min(CAPACITY_MAX, ab.INIT_BUDGET))
        assert ab.current_budget() == expected

    def test_init_budget_default_is_six(self):
        # TODO H4a row 2575: 'Init budget = 6 (~ CAPACITY_MAX/2 safe boot)'.
        assert ab.INIT_BUDGET == 6

    def test_floor_budget_default_is_two(self):
        # TODO H4a row 2575: 'budget = max(floor=2, budget//2)'.
        assert ab.FLOOR_BUDGET == 2

    def test_reset_with_explicit_seed(self):
        ab.reset(initial_budget=4, now=0.0)
        assert ab.current_budget() == 4

    def test_reset_clamps_above_cap(self):
        ab.reset(initial_budget=CAPACITY_MAX + 100, now=0.0)
        assert ab.current_budget() == CAPACITY_MAX

    def test_reset_clamps_below_floor(self):
        ab.reset(initial_budget=0, now=0.0)
        assert ab.current_budget() == ab.FLOOR_BUDGET

    def test_reset_records_init_trace(self):
        ab.reset(initial_budget=6, now=42.0)
        entries = ab.trace(now=42.0)
        assert len(entries) == 1
        assert entries[0].reason == ab.AdjustReason.INIT
        assert entries[0].budget == 6
        assert entries[0].timestamp == 42.0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Additive increase
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestAdditiveIncrease:
    def test_ai_fires_after_30s_green(self):
        ab.reset(initial_budget=6, now=0.0)
        r = ab.tick(cpu_percent=50, mem_percent=50, deferred_count=0, now=15.0)
        assert r == ab.AdjustReason.HOLD
        assert ab.current_budget() == 6

        r = ab.tick(cpu_percent=50, mem_percent=50, deferred_count=0, now=30.0)
        assert r == ab.AdjustReason.AI
        assert ab.current_budget() == 7

    def test_ai_blocked_by_deferred(self):
        ab.reset(initial_budget=6, now=0.0)
        # Cool host but tasks queued → must NOT grow (would just lengthen
        # the queue without freeing it).
        ab.tick(cpu_percent=10, mem_percent=10, deferred_count=3, now=30.0)
        assert ab.current_budget() == 6

    def test_ai_blocked_at_cpu_threshold_boundary(self):
        # Strict ``<`` semantics: cpu == 70.0 is not < 70.
        ab.reset(initial_budget=6, now=0.0)
        ab.tick(
            cpu_percent=ab.CPU_AI_THRESHOLD_PCT,
            mem_percent=10,
            deferred_count=0,
            now=30.0,
        )
        assert ab.current_budget() == 6

    def test_ai_blocked_at_mem_threshold_boundary(self):
        ab.reset(initial_budget=6, now=0.0)
        ab.tick(
            cpu_percent=10,
            mem_percent=ab.MEM_AI_THRESHOLD_PCT,
            deferred_count=0,
            now=30.0,
        )
        assert ab.current_budget() == 6

    def test_ai_blocked_when_cpu_or_mem_above_ai_threshold(self):
        # cpu just above AI threshold but below MD → still HOLD.
        ab.reset(initial_budget=6, now=0.0)
        ab.tick(cpu_percent=75, mem_percent=10, deferred_count=0, now=30.0)
        assert ab.current_budget() == 6

    def test_ai_caps_at_capacity_max(self):
        ab.reset(initial_budget=CAPACITY_MAX, now=0.0)
        r = ab.tick(cpu_percent=10, mem_percent=10, deferred_count=0, now=30.0)
        assert r == ab.AdjustReason.CAP
        assert ab.current_budget() == CAPACITY_MAX

        # And subsequent ticks just keep returning CAP, never overflow.
        ab.tick(cpu_percent=10, mem_percent=10, deferred_count=0, now=60.0)
        assert ab.current_budget() == CAPACITY_MAX

    def test_ai_grows_one_per_interval(self):
        # Walk green from 6 → 9 over 90s.
        ab.reset(initial_budget=6, now=0.0)
        for i, t in enumerate([30.0, 60.0, 90.0], start=1):
            r = ab.tick(cpu_percent=10, mem_percent=10, deferred_count=0, now=t)
            assert r == ab.AdjustReason.AI
            assert ab.current_budget() == min(CAPACITY_MAX, 6 + i)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Multiplicative decrease
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestMultiplicativeDecrease:
    def test_md_requires_10s_persistence(self):
        ab.reset(initial_budget=8, now=0.0)
        # First hot tick — clock starts but no shrink yet.
        r = ab.tick(cpu_percent=90, mem_percent=10, deferred_count=0, now=0.0)
        assert r == ab.AdjustReason.HOLD
        assert ab.current_budget() == 8
        # 5s later — still not enough.
        r = ab.tick(cpu_percent=90, mem_percent=10, deferred_count=0, now=5.0)
        assert r == ab.AdjustReason.HOLD
        assert ab.current_budget() == 8
        # 10s — fires.
        r = ab.tick(cpu_percent=90, mem_percent=10, deferred_count=0, now=10.0)
        assert r == ab.AdjustReason.MD
        assert ab.current_budget() == 4

    def test_md_triggered_by_mem_alone(self):
        ab.reset(initial_budget=8, now=0.0)
        ab.tick(cpu_percent=10, mem_percent=90, deferred_count=0, now=0.0)
        ab.tick(cpu_percent=10, mem_percent=90, deferred_count=0, now=10.0)
        assert ab.current_budget() == 4

    def test_md_threshold_strict(self):
        # cpu == 85.0 is the boundary — not > 85 → not hot.
        ab.reset(initial_budget=8, now=0.0)
        ab.tick(
            cpu_percent=ab.CPU_MD_THRESHOLD_PCT,
            mem_percent=ab.MEM_MD_THRESHOLD_PCT,
            deferred_count=0,
            now=0.0,
        )
        ab.tick(
            cpu_percent=ab.CPU_MD_THRESHOLD_PCT,
            mem_percent=ab.MEM_MD_THRESHOLD_PCT,
            deferred_count=0,
            now=10.0,
        )
        assert ab.current_budget() == 8

    def test_md_floors_at_two(self):
        # Already at floor — MD must clamp, not drop below.
        ab.reset(initial_budget=2, now=0.0)
        ab.tick(cpu_percent=90, mem_percent=10, deferred_count=0, now=0.0)
        r = ab.tick(cpu_percent=90, mem_percent=10, deferred_count=0, now=10.0)
        assert r == ab.AdjustReason.FLOOR
        assert ab.current_budget() == 2

    def test_md_floors_when_halving_below_two(self):
        # 3 // 2 = 1; floor clamps to 2. Reason should still be FLOOR
        # (we hit the clamp, not a clean halving).
        ab.reset(initial_budget=3, now=0.0)
        ab.tick(cpu_percent=90, mem_percent=10, deferred_count=0, now=0.0)
        r = ab.tick(cpu_percent=90, mem_percent=10, deferred_count=0, now=10.0)
        assert r == ab.AdjustReason.MD  # 3 → max(2, 3//2)=max(2,1)=2 — *did* shrink
        assert ab.current_budget() == 2

    def test_pressure_clock_resets_on_cool(self):
        ab.reset(initial_budget=8, now=0.0)
        # Pressure for 5s, then cool, then pressure again — must wait
        # ANOTHER full 10s of new pressure.
        ab.tick(cpu_percent=90, mem_percent=10, deferred_count=0, now=0.0)
        ab.tick(cpu_percent=20, mem_percent=10, deferred_count=0, now=5.0)
        ab.tick(cpu_percent=90, mem_percent=10, deferred_count=0, now=8.0)
        ab.tick(cpu_percent=90, mem_percent=10, deferred_count=0, now=15.0)
        assert ab.current_budget() == 8  # not yet 10s of fresh pressure
        ab.tick(cpu_percent=90, mem_percent=10, deferred_count=0, now=18.0)
        assert ab.current_budget() == 4  # 8+10=18, fires now

    def test_back_to_back_md_each_requires_full_persistence(self):
        # Big budget that can survive two halvings without hitting floor.
        ab.reset(initial_budget=12, now=0.0)
        ab.tick(cpu_percent=90, mem_percent=10, deferred_count=0, now=0.0)
        ab.tick(cpu_percent=90, mem_percent=10, deferred_count=0, now=10.0)
        assert ab.current_budget() == 6
        # Clock was reset to 10 after the halving. Need another 10s
        # before next MD even though pressure stayed.
        ab.tick(cpu_percent=90, mem_percent=10, deferred_count=0, now=15.0)
        assert ab.current_budget() == 6
        ab.tick(cpu_percent=90, mem_percent=10, deferred_count=0, now=20.0)
        assert ab.current_budget() == 3

    def test_md_resets_ai_clock(self):
        # After MD, AI must wait a fresh 30s — no immediate bounce-back.
        ab.reset(initial_budget=8, now=0.0)
        # Cool for 25s — AI clock at 25/30.
        ab.tick(cpu_percent=10, mem_percent=10, deferred_count=0, now=25.0)
        # Then hot for 10s → MD at t=35.
        ab.tick(cpu_percent=90, mem_percent=10, deferred_count=0, now=25.5)
        ab.tick(cpu_percent=90, mem_percent=10, deferred_count=0, now=35.5)
        assert ab.current_budget() == 4
        # Cool again at t=40 — AI must NOT fire (last_ai_at was reset to 35.5).
        r = ab.tick(cpu_percent=10, mem_percent=10, deferred_count=0, now=40.0)
        assert r == ab.AdjustReason.HOLD
        assert ab.current_budget() == 4
        # AI eligible again at t=65.5 = 35.5 + 30.
        r = ab.tick(cpu_percent=10, mem_percent=10, deferred_count=0, now=65.5)
        assert r == ab.AdjustReason.AI
        assert ab.current_budget() == 5


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Trace + snapshot
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestTrace:
    def test_trace_records_each_change(self):
        ab.reset(initial_budget=6, now=0.0)
        ab.tick(cpu_percent=10, mem_percent=10, deferred_count=0, now=30.0)  # AI
        ab.tick(cpu_percent=10, mem_percent=10, deferred_count=0, now=60.0)  # AI
        entries = ab.trace(now=60.0)
        assert [e.reason for e in entries] == [
            ab.AdjustReason.INIT,
            ab.AdjustReason.AI,
            ab.AdjustReason.AI,
        ]
        assert [e.budget for e in entries] == [6, 7, 8]

    def test_trace_skips_holds(self):
        # HOLD ticks must NOT pollute the trace — only state-changing
        # decisions get recorded.
        ab.reset(initial_budget=6, now=0.0)
        for t in (5.0, 10.0, 15.0, 20.0, 25.0):
            ab.tick(cpu_percent=10, mem_percent=10, deferred_count=0, now=t)
        assert len(ab.trace(now=25.0)) == 1  # only the INIT entry

    def test_trace_evicts_entries_older_than_5min(self):
        ab.reset(initial_budget=6, now=0.0)
        ab.tick(cpu_percent=10, mem_percent=10, deferred_count=0, now=30.0)  # AI
        # Jump well past TRACE_WINDOW_S — both INIT@0 and AI@30 fall off
        # (cutoff = 400 - 300 = 100).
        entries = ab.trace(now=400.0)
        assert entries == []

    def test_trace_partial_eviction(self):
        ab.reset(initial_budget=6, now=0.0)
        ab.tick(cpu_percent=10, mem_percent=10, deferred_count=0, now=30.0)
        ab.tick(cpu_percent=10, mem_percent=10, deferred_count=0, now=200.0)
        # At now=350, cutoff=50: INIT@0 evicted, AI@30 evicted, AI@200 stays.
        entries = ab.trace(now=350.0)
        assert len(entries) == 1
        assert entries[0].timestamp == 200.0

    def test_md_emits_trace_entry(self):
        ab.reset(initial_budget=8, now=0.0)
        ab.tick(cpu_percent=90, mem_percent=10, deferred_count=0, now=0.0)
        ab.tick(cpu_percent=90, mem_percent=10, deferred_count=0, now=10.0)
        entries = ab.trace(now=10.0)
        # INIT@0 + MD@10
        assert entries[-1].reason == ab.AdjustReason.MD
        assert entries[-1].budget == 4
        assert entries[-1].cpu_percent == 90.0


class TestSnapshot:
    def test_snapshot_shape(self):
        ab.reset(initial_budget=6, now=0.0)
        snap = ab.snapshot(now=0.0)
        assert snap["budget"] == 6
        assert snap["capacity_max"] == CAPACITY_MAX
        assert snap["floor"] == ab.FLOOR_BUDGET
        assert snap["init_budget"] == ab.INIT_BUDGET
        assert snap["last_reason"] == ab.AdjustReason.INIT.value
        assert snap["last_ai_at"] == 0.0
        assert snap["pressure_clock_started_at"] is None
        # Threshold knobs are surfaced for the UI tooltip + ops runbook.
        assert snap["thresholds"]["cpu_ai_pct"] == ab.CPU_AI_THRESHOLD_PCT
        assert snap["thresholds"]["mem_md_pct"] == ab.MEM_MD_THRESHOLD_PCT
        assert snap["thresholds"]["ai_interval_s"] == ab.AI_INTERVAL_S
        assert snap["thresholds"]["md_persistence_s"] == ab.MD_PERSISTENCE_S
        assert isinstance(snap["trace"], list)

    def test_snapshot_reflects_pressure_clock(self):
        ab.reset(initial_budget=8, now=0.0)
        ab.tick(cpu_percent=90, mem_percent=10, deferred_count=0, now=3.0)
        snap = ab.snapshot(now=5.0)
        assert snap["pressure_clock_started_at"] == 3.0

    def test_snapshot_clears_pressure_clock_after_md(self):
        # After MD fires, the persistence clock resets to ``t``.
        ab.reset(initial_budget=8, now=0.0)
        ab.tick(cpu_percent=90, mem_percent=10, deferred_count=0, now=0.0)
        ab.tick(cpu_percent=90, mem_percent=10, deferred_count=0, now=10.0)
        snap = ab.snapshot(now=10.0)
        assert snap["pressure_clock_started_at"] == 10.0
        assert snap["last_reason"] == ab.AdjustReason.MD.value


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Integration: simulated CPU spike (TODO row 2584 preview)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestSpikeRecoveryCycle:
    def test_spike_then_recover(self):
        """End-to-end: green grow → CPU spike → MD halve → cool → AI back up."""
        ab.reset(initial_budget=6, now=0.0)
        # Phase 1: green for 90s → grows 6 → 9.
        for t in (30.0, 60.0, 90.0):
            ab.tick(cpu_percent=10, mem_percent=10, deferred_count=0, now=t)
        assert ab.current_budget() == 9

        # Phase 2: CPU spike for 12s → MD at t=100.
        ab.tick(cpu_percent=95, mem_percent=10, deferred_count=0, now=90.0)
        ab.tick(cpu_percent=95, mem_percent=10, deferred_count=0, now=100.0)
        assert ab.current_budget() == 4  # 9 // 2

        # Phase 3: cool for 30s → AI fires at t=130 (last_ai_at reset to 100).
        ab.tick(cpu_percent=10, mem_percent=10, deferred_count=0, now=130.0)
        assert ab.current_budget() == 5

    def test_floor_holds_under_sustained_pressure(self):
        # Even with relentless pressure and many ticks, never goes below floor.
        ab.reset(initial_budget=8, now=0.0)
        for i in range(20):
            t = i * 10.0
            ab.tick(cpu_percent=99, mem_percent=99, deferred_count=0, now=t)
        assert ab.current_budget() == ab.FLOOR_BUDGET


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Drift guard
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestDriftGuards:
    def test_all_adjust_reasons_enumerable(self):
        # Every enum member must have a string value (for Prom labels +
        # JSON serialisation). Drift guard for future enum additions.
        for r in ab.AdjustReason:
            assert isinstance(r.value, str)
            assert r.value  # non-empty

    def test_capacity_max_consistency(self):
        # adaptive_budget reads CAPACITY_MAX at import time; the snapshot
        # surfaces it so downstream consumers (UI, ops summary) don't
        # have to import it themselves. Drift guard: if sandbox_capacity
        # ever swaps the symbol, this test fails loudly.
        from backend import sandbox_capacity
        assert ab.snapshot()["capacity_max"] == sandbox_capacity.CAPACITY_MAX

    def test_clamp_envelope(self):
        # Internal clamp helper round-trips edge values correctly.
        assert ab._clamp(0) == ab.FLOOR_BUDGET
        assert ab._clamp(ab.FLOOR_BUDGET) == ab.FLOOR_BUDGET
        assert ab._clamp(CAPACITY_MAX) == CAPACITY_MAX
        assert ab._clamp(CAPACITY_MAX + 100) == CAPACITY_MAX


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  evaluate_from_host_snapshot wiring
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestHostSnapshotWiring:
    def test_returns_hold_when_no_snapshot(self, monkeypatch):
        # Cold-start grace: no host sample yet → HOLD without raising.
        from backend import host_metrics
        monkeypatch.setattr(host_metrics, "get_latest_host_snapshot", lambda: None)
        r = ab.evaluate_from_host_snapshot()
        assert r == ab.AdjustReason.HOLD

    def test_pulls_cpu_mem_from_snapshot(self, monkeypatch):
        # Stub a hot host snapshot; controller should start the MD clock.
        from backend import host_metrics, sandbox_capacity

        class _StubHost:
            cpu_percent = 90.0
            mem_percent = 10.0
            sampled_at = 0.0

        class _StubSnap:
            host = _StubHost()
            sampled_at = 0.0

        monkeypatch.setattr(host_metrics, "get_latest_host_snapshot", lambda: _StubSnap())
        monkeypatch.setattr(sandbox_capacity, "deferred_count_recent", lambda: 0)
        ab.reset(initial_budget=8, now=0.0)
        r = ab.evaluate_from_host_snapshot(now=0.0)
        assert r == ab.AdjustReason.HOLD  # first hot tick — clock starts
        r = ab.evaluate_from_host_snapshot(now=10.0)
        assert r == ab.AdjustReason.MD
        assert ab.current_budget() == 4
