"""I6 — Tests for DRF per-tenant sandbox capacity.

Covers:
- Basic acquire/release
- Per-tenant guaranteed minimum (DRF)
- Idle capacity borrowing
- Grace period reclaim (30s)
- Turbo per-tenant cap
- Two-tenant load simulation
- Starvation prevention
- Snapshot / observability
"""

from __future__ import annotations

import asyncio
import time

import pytest

from backend import sandbox_capacity as sc


@pytest.fixture(autouse=True)
def _reset():
    sc._reset_for_tests()
    yield
    sc._reset_for_tests()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Basic acquire / release
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestBasicAcquireRelease:
    def test_acquire_single_token(self):
        assert sc.try_acquire("t-a", cost=1.0) is True
        snap = sc.snapshot()
        assert snap["total_used"] == 1.0
        assert snap["tenants"]["t-a"]["used"] == 1.0

    def test_acquire_up_to_capacity(self):
        for i in range(sc.CAPACITY_MAX):
            assert sc.try_acquire("t-a", cost=1.0) is True
        assert sc.try_acquire("t-a", cost=1.0) is False

    def test_release_frees_capacity(self):
        for _ in range(sc.CAPACITY_MAX):
            sc.try_acquire("t-a", cost=1.0)
        sc.release("t-a", cost=1.0)
        assert sc.try_acquire("t-a", cost=1.0) is True

    def test_release_nonexistent_tenant_noop(self):
        sc.release("t-nonexistent", cost=1.0)

    def test_default_tenant_fallback(self):
        assert sc.try_acquire(None, cost=1.0) is True
        snap = sc.snapshot()
        assert "t-default" in snap["tenants"]

    def test_weighted_cost(self):
        assert sc.try_acquire("t-a", cost=4.0) is True
        assert sc.snapshot()["total_used"] == 4.0
        assert sc.try_acquire("t-a", cost=9.0) is False
        assert sc.try_acquire("t-a", cost=8.0) is True
        assert sc.snapshot()["total_used"] == 12.0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  DRF guaranteed minimum
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestDRFGuaranteedMinimum:
    def test_single_tenant_gets_full_capacity(self):
        sc._ensure_bucket("t-a")
        sc._recalc_guarantees()
        snap = sc.snapshot()
        assert snap["tenants"]["t-a"]["guaranteed"] == sc.CAPACITY_MAX

    def test_two_tenants_split_evenly(self):
        with sc._lock:
            sc._ensure_bucket("t-a")
            sc._ensure_bucket("t-b")
            sc._recalc_guarantees()
        snap = sc.snapshot()
        assert snap["tenants"]["t-a"]["guaranteed"] == sc.CAPACITY_MAX / 2
        assert snap["tenants"]["t-b"]["guaranteed"] == sc.CAPACITY_MAX / 2

    def test_three_tenants_split_thirds(self):
        with sc._lock:
            sc._ensure_bucket("t-a")
            sc._ensure_bucket("t-b")
            sc._ensure_bucket("t-c")
            sc._recalc_guarantees()
        snap = sc.snapshot()
        expected = sc.CAPACITY_MAX / 3
        for tid in ("t-a", "t-b", "t-c"):
            assert abs(snap["tenants"][tid]["guaranteed"] - expected) < 0.01

    def test_guaranteed_recalculated_on_acquire(self):
        sc.try_acquire("t-a", cost=1.0)
        g1 = sc.snapshot()["tenants"]["t-a"]["guaranteed"]
        sc.try_acquire("t-b", cost=1.0)
        g2 = sc.snapshot()["tenants"]["t-a"]["guaranteed"]
        assert g2 < g1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Idle capacity borrowing
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestIdleCapacityBorrowing:
    def test_tenant_can_borrow_idle_capacity(self):
        sc.try_acquire("t-a", cost=1.0)
        sc.try_acquire("t-b", cost=1.0)
        guaranteed_a = sc.CAPACITY_MAX / 2
        for _ in range(int(guaranteed_a) + 2):
            sc.try_acquire("t-a", cost=1.0)
        snap = sc.snapshot()
        assert snap["tenants"]["t-a"]["used"] > guaranteed_a

    def test_borrowing_limited_by_global_capacity(self):
        sc.try_acquire("t-a", cost=1.0)
        sc.try_acquire("t-b", cost=1.0)
        count = 0
        while sc.try_acquire("t-a", cost=1.0):
            count += 1
            if count > sc.CAPACITY_MAX + 5:
                break
        assert sc.snapshot()["total_used"] == sc.CAPACITY_MAX


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Grace period reclaim
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestGracePeriodReclaim:
    def test_reclaim_sets_grace_deadline(self):
        """t-a fills all capacity, t-b registers — t-a is over guaranteed, reclaim fires."""
        for _ in range(sc.CAPACITY_MAX):
            sc.try_acquire("t-a", cost=1.0)
        sc.try_acquire("t-b", cost=1.0)  # fails but registers bucket

        reclaims = sc.reclaim_borrowed("t-b")
        assert len(reclaims) > 0
        assert reclaims[0][0] == "t-a"

    def test_grace_deadline_enforced(self):
        """t-a grabs all 12, t-b arrives — t-a is over guaranteed (6), reclaim works."""
        for _ in range(sc.CAPACITY_MAX):
            sc.try_acquire("t-a", cost=1.0)
        sc.try_acquire("t-b", cost=1.0)  # fails but registers bucket

        reclaims = sc.reclaim_borrowed("t-b")
        assert len(reclaims) > 0

        with sc._lock:
            for b in sc._buckets.values():
                for g in b.grants:
                    if g.grace_deadline is not None:
                        g.grace_deadline = time.time() - 1.0

        released = sc.enforce_grace_deadlines()
        assert len(released) > 0
        assert released[0][0] == "t-a"

    def test_no_reclaim_when_requester_at_capacity(self):
        """Both tenants at guaranteed share — no reclaim possible."""
        for _ in range(6):
            sc.try_acquire("t-a", cost=1.0)
        for _ in range(6):
            sc.try_acquire("t-b", cost=1.0)

        reclaims = sc.reclaim_borrowed("t-b")
        assert len(reclaims) == 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Turbo per-tenant cap
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestTurboPerTenantCap:
    def test_turbo_cap_limits_single_tenant(self):
        turbo_cap = sc.CAPACITY_MAX * sc.TURBO_TENANT_CAP_RATIO
        count = 0
        while sc.try_acquire("t-a", cost=1.0, is_turbo=True):
            count += 1
            if count > sc.CAPACITY_MAX + 5:
                break
        assert count == int(turbo_cap)

    def test_turbo_cap_allows_other_tenants(self):
        turbo_cap = int(sc.CAPACITY_MAX * sc.TURBO_TENANT_CAP_RATIO)
        for _ in range(turbo_cap):
            sc.try_acquire("t-a", cost=1.0, is_turbo=True)
        assert sc.try_acquire("t-a", cost=1.0, is_turbo=True) is False
        assert sc.try_acquire("t-b", cost=1.0, is_turbo=True) is True

    def test_non_turbo_ignores_tenant_cap(self):
        count = 0
        while sc.try_acquire("t-a", cost=1.0, is_turbo=False):
            count += 1
            if count > sc.CAPACITY_MAX + 5:
                break
        assert count == sc.CAPACITY_MAX


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Two-tenant load simulation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestTwoTenantLoadSimulation:
    def test_fair_share_under_contention(self):
        """Both tenants compete for all capacity — each should get ~6."""
        a_acquired = 0
        b_acquired = 0
        for _ in range(20):
            if sc.try_acquire("t-a", cost=1.0):
                a_acquired += 1
            if sc.try_acquire("t-b", cost=1.0):
                b_acquired += 1
        assert a_acquired == sc.CAPACITY_MAX // 2
        assert b_acquired == sc.CAPACITY_MAX // 2
        assert a_acquired + b_acquired == sc.CAPACITY_MAX

    def test_late_joiner_gets_fair_share(self):
        """t-a fills up, then t-b arrives — t-b can reclaim its share."""
        for _ in range(sc.CAPACITY_MAX):
            sc.try_acquire("t-a", cost=1.0)
        assert sc.try_acquire("t-b", cost=1.0) is False

        reclaims = sc.reclaim_borrowed("t-b")
        assert len(reclaims) > 0

        with sc._lock:
            for b in sc._buckets.values():
                for g in b.grants:
                    if g.grace_deadline is not None:
                        g.grace_deadline = time.time() - 1.0

        released = sc.enforce_grace_deadlines()
        assert len(released) > 0

        assert sc.try_acquire("t-b", cost=1.0) is True

    def test_weighted_cost_fairness(self):
        """Heavy sandbox (cost=4) vs light (cost=1): both respect capacity."""
        assert sc.try_acquire("t-a", cost=4.0) is True
        assert sc.try_acquire("t-a", cost=4.0) is True
        assert sc.try_acquire("t-b", cost=1.0) is True
        assert sc.try_acquire("t-b", cost=1.0) is True
        assert sc.try_acquire("t-b", cost=1.0) is True
        assert sc.try_acquire("t-b", cost=1.0) is True
        assert sc.snapshot()["total_used"] == 12.0
        assert sc.try_acquire("t-a", cost=1.0) is False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Starvation prevention
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestStarvationPrevention:
    def test_greedy_tenant_cannot_starve_others(self):
        """Even if t-a tries to grab everything in turbo mode, t-b still gets capacity."""
        turbo_cap = int(sc.CAPACITY_MAX * sc.TURBO_TENANT_CAP_RATIO)
        for _ in range(turbo_cap):
            sc.try_acquire("t-a", cost=1.0, is_turbo=True)
        remaining = sc.CAPACITY_MAX - turbo_cap
        for _ in range(remaining):
            assert sc.try_acquire("t-b", cost=1.0) is True

    def test_release_and_reacquire_cycle(self):
        """Simulate ongoing work: acquire, release, re-acquire — no leaks."""
        for cycle in range(5):
            for _ in range(6):
                sc.try_acquire("t-a", cost=1.0)
                sc.try_acquire("t-b", cost=1.0)
            assert sc.snapshot()["total_used"] == sc.CAPACITY_MAX
            for _ in range(6):
                sc.release("t-a", cost=1.0)
                sc.release("t-b", cost=1.0)
            assert sc.snapshot()["total_used"] == 0.0

    def test_three_tenants_guaranteed_minimum(self):
        """With 3 tenants, each guaranteed 4 tokens — verify all get their share."""
        for tid in ("t-a", "t-b", "t-c"):
            for _ in range(4):
                assert sc.try_acquire(tid, cost=1.0) is True
        assert sc.snapshot()["total_used"] == 12.0

        snap = sc.snapshot()
        for tid in ("t-a", "t-b", "t-c"):
            assert snap["tenants"][tid]["used"] == 4.0
            assert abs(snap["tenants"][tid]["guaranteed"] - 4.0) < 0.01


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Async acquire
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestAsyncAcquire:
    @pytest.mark.asyncio
    async def test_async_acquire_succeeds(self):
        ok = await sc.acquire("t-a", cost=1.0, timeout_s=1.0)
        assert ok is True

    @pytest.mark.asyncio
    async def test_async_acquire_timeout(self):
        for _ in range(sc.CAPACITY_MAX):
            sc.try_acquire("t-a", cost=1.0)
        ok = await sc.acquire("t-b", cost=1.0, timeout_s=0.5)
        assert ok is False

    @pytest.mark.asyncio
    async def test_async_acquire_with_release(self):
        for _ in range(sc.CAPACITY_MAX):
            sc.try_acquire("t-a", cost=1.0)

        async def delayed_release():
            await asyncio.sleep(0.2)
            sc.release("t-a", cost=1.0)

        asyncio.create_task(delayed_release())
        ok = await sc.acquire("t-b", cost=1.0, timeout_s=2.0)
        assert ok is True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Snapshot / observability
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestSnapshot:
    def test_snapshot_empty(self):
        snap = sc.snapshot()
        assert snap["capacity_max"] == sc.CAPACITY_MAX
        assert snap["total_used"] == 0
        assert snap["tenants"] == {}

    def test_snapshot_reflects_state(self):
        sc.try_acquire("t-a", cost=3.0)
        sc.try_acquire("t-b", cost=2.0)
        snap = sc.snapshot()
        assert snap["total_used"] == 5.0
        assert snap["total_free"] == sc.CAPACITY_MAX - 5.0
        assert snap["active_tenants"] == 2
        assert snap["tenants"]["t-a"]["used"] == 3.0
        assert snap["tenants"]["t-b"]["used"] == 2.0

    def test_tenant_usage_missing_tenant(self):
        usage = sc.tenant_usage("t-nonexistent")
        assert usage["used"] == 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Cost weight enum
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestCostWeights:
    def test_weight_values(self):
        assert sc.SandboxCostWeight.gvisor_lightweight == 1.0
        assert sc.SandboxCostWeight.docker_t2_networked == 2.0
        assert sc.SandboxCostWeight.phase64c_local_compile == 4.0
        assert sc.SandboxCostWeight.phase64c_qemu_aarch64 == 3.0
        assert sc.SandboxCostWeight.phase64c_ssh_remote == 0.5

    def test_acquire_with_weight_enum(self):
        assert sc.try_acquire(
            "t-a", cost=sc.SandboxCostWeight.phase64c_local_compile
        ) is True
        assert sc.snapshot()["total_used"] == 4.0

    def test_weight_metadata_matches_todo_spec(self):
        """H4a TODO row: initial cost-weight estimates per sandbox class.

        Mirrors the resource envelopes called out in the TODO.md H4a spec
        (unit-test / lint ~512MB / 1 core burst; integration ~1.5GB / 2 core;
        compile 2GB / 4 core sustained; QEMU 2GB / 2 core; ssh-remote 成本
        在對端). If the TODO text changes, this test must change with it —
        that's the whole point: the test pins the agreed initial estimates.
        """
        lw = sc.cost_estimate(sc.SandboxCostWeight.gvisor_lightweight)
        assert (lw.tokens, lw.memory_mb, lw.cpu_cores, lw.burst) == (
            1.0, 512, 1.0, True,
        )
        assert "unit test" in lw.use_case or "lint" in lw.use_case

        net = sc.cost_estimate(sc.SandboxCostWeight.docker_t2_networked)
        assert (net.tokens, net.memory_mb, net.cpu_cores, net.burst) == (
            2.0, 1536, 2.0, False,
        )

        cmp_ = sc.cost_estimate(sc.SandboxCostWeight.phase64c_local_compile)
        assert (cmp_.tokens, cmp_.memory_mb, cmp_.cpu_cores, cmp_.burst) == (
            4.0, 2048, 4.0, False,
        )
        assert "make -j4" in cmp_.use_case

        qemu = sc.cost_estimate(sc.SandboxCostWeight.phase64c_qemu_aarch64)
        assert (qemu.tokens, qemu.memory_mb, qemu.cpu_cores, qemu.burst) == (
            3.0, 2048, 2.0, False,
        )

        ssh = sc.cost_estimate(sc.SandboxCostWeight.phase64c_ssh_remote)
        assert (ssh.tokens, ssh.memory_mb, ssh.cpu_cores, ssh.burst) == (
            0.5, 256, 0.5, True,
        )

    def test_weight_metadata_matches_enum_values(self):
        """Drift guard — every enum member MUST have a metadata row, and
        the ``tokens`` field MUST equal the enum's float value.

        Prevents the classic bug of adding a new SandboxCostWeight member
        in one PR, forgetting the COST_WEIGHT_ESTIMATES row, and shipping
        a KeyError into prod the first time a caller passes the new member
        to ``cost_estimate()``.
        """
        for member in sc.SandboxCostWeight:
            assert member in sc.COST_WEIGHT_ESTIMATES, (
                f"SandboxCostWeight.{member.name} has no COST_WEIGHT_ESTIMATES row"
            )
            est = sc.COST_WEIGHT_ESTIMATES[member]
            assert est.tokens == float(member.value), (
                f"{member.name} enum value {member.value} != metadata tokens {est.tokens}"
            )

    def test_default_cost_is_lightweight(self):
        """DEFAULT_COST should remain the lightweight (1-token) weight so
        callers that don't pass a cost enum default to the cheapest class.
        """
        assert sc.DEFAULT_COST is sc.SandboxCostWeight.gvisor_lightweight
        assert sc.cost_estimate(sc.DEFAULT_COST).tokens == 1.0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Reset
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestReset:
    def test_reset_clears_all_state(self):
        sc.try_acquire("t-a", cost=5.0)
        sc.try_acquire("t-b", cost=3.0)
        sc._reset_for_tests()
        snap = sc.snapshot()
        assert snap["total_used"] == 0
        assert snap["tenants"] == {}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  H3 row 1524 — Coordinator transparency
#  queue_depth, deferred_5m, effective budget, derate
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestCoordinatorTransparency:
    def test_fresh_snapshot_has_zero_pressure_and_no_derate(self):
        snap = sc.snapshot()
        assert snap["queue_depth"] == 0
        assert snap["deferred_5m"] == 0
        assert snap["derated"] is False
        assert snap["derate_ratio"] == 1.0
        assert snap["derate_reason"] is None
        assert snap["effective_capacity_max"] == sc.CAPACITY_MAX

    def test_set_derate_shrinks_effective_budget(self):
        sc.set_derate(0.5, reason="CPU 87% > threshold")
        snap = sc.snapshot()
        assert snap["derated"] is True
        assert snap["derate_ratio"] == 0.5
        assert snap["derate_reason"] == "CPU 87% > threshold"
        assert snap["effective_capacity_max"] == sc.CAPACITY_MAX * 0.5

    def test_derate_caps_try_acquire_at_effective_budget(self):
        sc.set_derate(0.5)  # effective budget = 6.0
        # Should accept up to 6 tokens worth of work
        assert sc.try_acquire("t-a", cost=6.0) is True
        # Anything above the effective budget must be rejected even
        # though CAPACITY_MAX (12) has spare room.
        assert sc.try_acquire("t-b", cost=1.0) is False

    def test_clearing_derate_restores_full_capacity(self):
        sc.set_derate(0.5, reason="cpu hot")
        sc.set_derate(1.0)  # clear
        snap = sc.snapshot()
        assert snap["derated"] is False
        assert snap["derate_reason"] is None
        assert sc.try_acquire("t-a", cost=12.0) is True

    def test_derate_ratio_floor_never_zero(self):
        # A fully clamped ratio must still leave at least 1 effective
        # token so waiters can't deadlock forever.
        sc.set_derate(0.0)
        snap = sc.snapshot()
        assert snap["effective_capacity_max"] >= 1.0

    def test_deferred_counter_records_wait_events(self):
        # Fill capacity so every subsequent acquire() is deferred.
        for _ in range(sc.CAPACITY_MAX):
            sc.try_acquire("t-a", cost=1.0)

        async def _attempt():
            # Very short timeout → acquire returns False, but the
            # attempt still counts as a deferral.
            return await sc.acquire("t-b", cost=1.0, timeout_s=0.05)

        results = asyncio.get_event_loop().run_until_complete(
            asyncio.gather(_attempt(), _attempt())
        )
        assert results == [False, False]
        assert sc.deferred_count_recent() == 2
        # queue_depth must drop back to 0 once the waiters finish.
        assert sc.queue_depth() == 0

    def test_deferred_events_expire_after_window(self):
        # Stuff in an old deferral beyond the 5-min window and verify
        # the rolling trim drops it from the count.
        sc._deferred_events.append(time.time() - (sc.DEFERRED_WINDOW_S + 1))
        assert sc.deferred_count_recent() == 0
        snap = sc.snapshot()
        assert snap["deferred_5m"] == 0

    def test_queue_depth_reflects_waiter_during_wait(self):
        # Saturate capacity.
        for _ in range(sc.CAPACITY_MAX):
            sc.try_acquire("t-a", cost=1.0)

        async def _driver():
            async def _wait():
                return await sc.acquire("t-b", cost=1.0, timeout_s=0.3)
            task = asyncio.create_task(_wait())
            # Give the task a chance to fail the fast path and register.
            await asyncio.sleep(0.05)
            depth_during = sc.queue_depth()
            result = await task
            return depth_during, result

        depth_during, result = asyncio.get_event_loop().run_until_complete(_driver())
        assert depth_during == 1
        assert result is False
        assert sc.queue_depth() == 0
