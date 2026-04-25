"""H4a row 2581 — token-based ``_ModeSlot.acquire(cost)`` contract tests.

Complements :mod:`test_decision_engine.TestParallelSlot` (which already
locks mode-cap behaviour at ``cost=1``) by exercising the new shapes
introduced by row 2581:

* ``acquire(cost=N)`` charges ``N`` slots against the shared counter
  (not always 1), so a cost=3 acquire in a supervised session
  (cap=2) gets *clamped to 2* rather than deadlocking.
* On first peek-failure (capacity or DRF), ``sandbox.deferred``
  fires exactly once per acquire — not per condvar wakeup — with a
  reason code drawn from ``H4A_REASON_*``.
* The mode cap is composed with
  :func:`backend.adaptive_budget.effective_budget`, so AIMD pressure
  (MD halving) tightens the effective cap for non-turbo modes even if
  the static ``_PARALLEL_BUDGET`` would otherwise be higher.
* Release decrements the shared counter by the clamped cost that was
  actually reserved (stack-LIFO), not by the caller-supplied cost —
  a cost=3→clamped=2 acquire must return exactly 2 tokens to the pool.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from backend import adaptive_budget as ab
from backend import decision_engine as de


@pytest.fixture(autouse=True)
def _reset():
    """Reset decision_engine + adaptive_budget between tests so the
    shared slot counter + AIMD budget start from a known state."""
    de._reset_for_tests()
    ab._reset_for_tests()
    yield
    de._reset_for_tests()
    ab._reset_for_tests()


@pytest.fixture
def bus_capture(monkeypatch):
    """Intercept ``events.bus.publish`` so tests can assert the
    ``sandbox.deferred`` SSE payload without a live Redis / SSE bus."""
    events: list[tuple[str, dict[str, Any]]] = []

    def _fake_publish(event: str, data: dict[str, Any], **kwargs):
        events.append((event, data))

    from backend import events as events_mod
    monkeypatch.setattr(events_mod.bus, "publish", _fake_publish)
    return events


@pytest.fixture
def audit_capture(monkeypatch):
    """Intercept ``audit.log_sync`` so tests avoid the DB write-path
    while still recording what would have been persisted."""
    rows: list[dict[str, Any]] = []

    def _fake_log_sync(**kwargs):
        rows.append(kwargs)

    from backend import audit as audit_mod
    monkeypatch.setattr(audit_mod, "log_sync", _fake_log_sync)
    return rows


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Token cost — signature + basic consumption
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestTokenCost:

    @pytest.mark.asyncio
    async def test_acquire_accepts_cost_parameter(self):
        """``acquire(cost=N)`` is the row-2581 public signature — the
        parameter must flow into ``_cost`` for the rest of the acquire."""
        de.set_mode("turbo")  # cap=8, plenty of room
        slot = de.parallel_slot()
        await slot.acquire(cost=2)
        assert slot._cost == 2
        slot.release()

    @pytest.mark.asyncio
    async def test_default_cost_still_one(self):
        """Legacy zero-arg ``acquire()`` keeps the existing cost=1
        contract — existing callers must not see behavioural changes."""
        de.set_mode("full_auto")  # cap=4
        slot = de.parallel_slot()
        await slot.acquire()
        assert de.parallel_in_flight() == 1
        slot.release()
        assert de.parallel_in_flight() == 0

    @pytest.mark.asyncio
    async def test_cost_n_consumes_n_tokens(self):
        """A ``cost=3`` acquire in turbo (cap=8) must bump the shared
        counter by 3, not 1 — the counter is the token-bucket."""
        de.set_mode("turbo")
        slot = de.parallel_slot()
        await slot.acquire(cost=3)
        assert de.parallel_in_flight() == 3
        slot.release()
        assert de.parallel_in_flight() == 0

    @pytest.mark.asyncio
    async def test_cost_greater_than_cap_clamps_to_cap(self):
        """A cost > current cap must *clamp* (take the whole cap) rather
        than deadlock — matches the anti-deadlock convention from
        ``adaptive_budget.effective_budget``. Otherwise any oversized
        acquire would wait forever against a fixed-window scheduler."""
        de.set_mode("supervised")  # cap=2
        slot = de.parallel_slot()
        # Cap might be tightened further by AIMD — read it after the
        # composition so the assertion is deterministic.
        cap = de._compose_effective_cap(de.OperationMode.supervised)
        await asyncio.wait_for(slot.acquire(cost=99), timeout=0.5)
        assert de.parallel_in_flight() == cap
        slot.release()
        assert de.parallel_in_flight() == 0

    @pytest.mark.asyncio
    async def test_cost_zero_floors_at_one(self):
        """Defensive lower bound — a caller passing cost=0 still
        consumes one token, so a buggy caller can't infinitely loop
        without contributing to the shared-counter accounting."""
        de.set_mode("turbo")
        slot = de.parallel_slot()
        await slot.acquire(cost=0)
        assert de.parallel_in_flight() == 1
        slot.release()
        assert de.parallel_in_flight() == 0

    @pytest.mark.asyncio
    async def test_release_matches_reserved_cost_not_supplied_cost(self):
        """If ``_cost`` is mutated after acquire (or clamped inside
        ``__aenter__``), release must still decrement by the amount
        actually reserved on the shared counter — tracked via the
        internal reservation stack."""
        de.set_mode("supervised")  # cap=2
        slot = de.parallel_slot()
        await slot.acquire(cost=10)  # clamped to cap (2)
        assert de.parallel_in_flight() == 2
        slot._cost = 99  # caller mutates after acquire — irrelevant
        slot.release()
        assert de.parallel_in_flight() == 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  sandbox.deferred emission on queue
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestSandboxDeferredEmission:

    @pytest.mark.asyncio
    async def test_emit_on_mode_cap_saturation(self, bus_capture, audit_capture):
        """When mode-cap is saturated, the first waiter must fire one
        ``sandbox.deferred`` SSE + audit row with reason
        ``mode_cap_saturated``. Fast-success acquires must NOT emit."""
        de.set_mode("manual")  # cap=1
        holder = de.parallel_slot()
        await holder.acquire()
        assert de.parallel_in_flight() == 1

        # Spawn a waiter that will queue behind the held slot.
        waiter = de.parallel_slot()
        async def _wait():
            try:
                await asyncio.wait_for(waiter.acquire(), timeout=0.1)
            except asyncio.TimeoutError:
                pass

        await _wait()

        # Exactly one sandbox.deferred event with the mode-cap reason.
        deferred = [e for e in bus_capture if e[0] == "sandbox.deferred"]
        mode_cap_events = [
            e for e in deferred if e[1].get("reason") == de.H4A_REASON_MODE_CAP
        ]
        assert len(mode_cap_events) == 1, (
            f"expected 1 mode_cap emission, got {len(mode_cap_events)}: {deferred}"
        )
        _, payload = mode_cap_events[0]
        assert payload["cost"] == 1
        assert payload["cap"] == de._compose_effective_cap(de.OperationMode.manual)
        assert payload["in_flight"] == 1

        # Audit row mirrors the SSE payload.
        audit_rows = [r for r in audit_capture if r.get("action") == "sandbox.deferred"]
        mode_cap_audits = [
            r for r in audit_rows if r.get("entity_id") == de.H4A_REASON_MODE_CAP
        ]
        assert len(mode_cap_audits) == 1

        holder.release()

    @pytest.mark.asyncio
    async def test_fast_success_does_not_emit(self, bus_capture):
        """A cap-free acquire is the golden path — no
        ``sandbox.deferred`` event should be emitted when there's no
        wait. Prevents audit-log spam on every INVOKE."""
        de.set_mode("turbo")  # cap=8, lots of room
        slot = de.parallel_slot()
        await slot.acquire(cost=1)
        slot.release()

        deferred = [
            e for e in bus_capture
            if e[0] == "sandbox.deferred"
            and e[1].get("reason") in (de.H4A_REASON_MODE_CAP, de.H4A_REASON_DRF)
        ]
        assert deferred == [], f"expected no deferral, got: {deferred}"

    @pytest.mark.asyncio
    async def test_emit_exactly_once_per_acquire(self, bus_capture):
        """Even with many condvar wakeups before capacity frees, a
        single waiter must emit ``sandbox.deferred`` exactly once — the
        deferred flag is local to this acquire's stack frame."""
        de.set_mode("manual")  # cap=1
        holder = de.parallel_slot()
        await holder.acquire()

        waiter = de.parallel_slot()
        async def _try_and_give_up():
            try:
                await asyncio.wait_for(waiter.acquire(), timeout=0.15)
            except asyncio.TimeoutError:
                pass

        # Tick the condvar a few times by releasing-and-reacquiring the
        # holder — each cycle wakes the waiter, who fails the peek and
        # must NOT re-emit sandbox.deferred.
        async def _churn():
            for _ in range(3):
                await asyncio.sleep(0.02)
                holder.release()
                await holder.acquire()

        await asyncio.gather(_try_and_give_up(), _churn())

        mode_cap_events = [
            e for e in bus_capture
            if e[0] == "sandbox.deferred"
            and e[1].get("reason") == de.H4A_REASON_MODE_CAP
        ]
        # One waiter → exactly one emission, even across multiple wakeups.
        assert len(mode_cap_events) == 1, (
            f"expected exactly 1 emission, got {len(mode_cap_events)}"
        )

        holder.release()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Composition with adaptive_budget
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestAimdComposition:

    def test_compose_takes_min_of_mode_budget_and_aimd(self):
        """``_compose_effective_cap`` takes the tighter of
        ``_effective_budget(mode)`` (static, turbo-derate-aware) and
        ``adaptive_budget.effective_budget(mode)`` (AIMD-shaped).
        In cold-start state (AIMD=6), supervised (2) < AIMD mode_cap —
        the static budget wins. In turbo (8) > AIMD budget (6) — AIMD
        wins."""
        ab.reset(initial_budget=6)
        # Supervised static cap is 2; AIMD floor mode_cap for supervised
        # is min(0.4*CAPACITY_MAX, 6) which is >= 2 on the 16c/64GB rig.
        assert de._compose_effective_cap(de.OperationMode.supervised) == 2
        # Turbo static cap is 8; AIMD current budget is 6 → AIMD wins.
        assert de._compose_effective_cap(de.OperationMode.turbo) == 6

    def test_aimd_md_halving_tightens_turbo_cap(self):
        """After an AIMD MD cycle shrinks the budget, a turbo acquire
        sees the tighter cap even without a turbo-derate transition —
        this is the whole point of wiring AIMD into the mode-cap
        path."""
        ab.reset(initial_budget=6)
        # Simulate sustained CPU pressure → MD halves (6 → 3).
        now = time.time()
        ab.tick(cpu_percent=95.0, mem_percent=50.0, deferred_count=0, now=now)
        ab.tick(cpu_percent=95.0, mem_percent=50.0, deferred_count=0,
                now=now + ab.MD_PERSISTENCE_S + 0.01)
        assert ab.current_budget() == 3
        # Turbo static is 8, AIMD now 3 → composition = 3.
        assert de._compose_effective_cap(de.OperationMode.turbo) == 3

    @pytest.mark.asyncio
    async def test_aimd_tightening_queues_new_acquires(self, bus_capture):
        """End-to-end: hold turbo-level in-flight at AIMD ceiling, then
        verify the next acquire queues and emits sandbox.deferred."""
        ab.reset(initial_budget=2)  # force a small AIMD ceiling
        de.set_mode("turbo")  # static cap=8, AIMD=2 → effective=2

        s1 = de.parallel_slot()
        s2 = de.parallel_slot()
        await s1.acquire()
        await s2.acquire()
        assert de.parallel_in_flight() == 2

        waiter = de.parallel_slot()
        try:
            await asyncio.wait_for(waiter.acquire(), timeout=0.1)
            pytest.fail("waiter should have blocked behind AIMD-tightened cap")
        except asyncio.TimeoutError:
            pass

        mode_cap_events = [
            e for e in bus_capture
            if e[0] == "sandbox.deferred"
            and e[1].get("reason") == de.H4A_REASON_MODE_CAP
        ]
        assert len(mode_cap_events) >= 1
        assert mode_cap_events[0][1]["cap"] == 2

        s1.release()
        s2.release()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Reservation-stack semantics (singleton reuse)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestReservationStack:

    @pytest.mark.asyncio
    async def test_singleton_concurrent_acquires_each_reserve(self):
        """``parallel_slot()`` with no args returns the module
        singleton — multiple concurrent-nested acquires against it must
        each reserve their own token slot and each release must pop
        exactly one reservation off the stack, never trampling."""
        de.set_mode("full_auto")  # cap=4
        s = de.parallel_slot()
        await s.acquire(cost=1)
        await s.acquire(cost=2)
        # Two reservations on the stack: [1, 2]
        assert list(s._reservations) == [1, 2]
        assert de.parallel_in_flight() == 3
        s.release()  # pops 2
        assert de.parallel_in_flight() == 1
        s.release()  # pops 1
        assert de.parallel_in_flight() == 0
        assert s._reservations == []

    @pytest.mark.asyncio
    async def test_release_is_noop_when_stack_empty(self):
        """A release() without a prior acquire must not underflow the
        shared counter (prevents flaky tests + operator-initiated
        release-twice from going negative)."""
        de.set_mode("supervised")
        s = de.parallel_slot()
        s.release()  # never acquired
        assert de.parallel_in_flight() == 0
