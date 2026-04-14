"""Tests for Phase 47A: OperationMode + DecisionEngine skeleton + parallel invoke."""

from __future__ import annotations

import asyncio

import pytest

from backend import decision_engine as de


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  OperationMode
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestMode:

    def setup_method(self):
        de._reset_for_tests()

    def test_default_mode_is_supervised(self):
        assert de.get_mode() == de.OperationMode.supervised

    def test_set_mode_by_enum(self):
        de.set_mode(de.OperationMode.full_auto)
        assert de.get_mode() == de.OperationMode.full_auto

    def test_set_mode_by_string(self):
        de.set_mode("turbo")
        assert de.get_mode() == de.OperationMode.turbo

    def test_set_mode_invalid(self):
        with pytest.raises(ValueError):
            de.set_mode("ludicrous")

    def test_parallel_budget_per_mode(self):
        assert de._PARALLEL_BUDGET[de.OperationMode.manual] == 1
        assert de._PARALLEL_BUDGET[de.OperationMode.supervised] == 2
        assert de._PARALLEL_BUDGET[de.OperationMode.full_auto] == 4
        assert de._PARALLEL_BUDGET[de.OperationMode.turbo] == 8


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  should_auto_execute
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestAutoExecutePolicy:

    def setup_method(self):
        de._reset_for_tests()

    def test_info_always_auto(self):
        for m in de.OperationMode:
            de.set_mode(m)
            assert de.should_auto_execute(de.DecisionSeverity.info)

    def test_routine_requires_supervised_or_higher(self):
        de.set_mode("manual")
        assert not de.should_auto_execute("routine")
        de.set_mode("supervised")
        assert de.should_auto_execute("routine")

    def test_risky_requires_full_auto(self):
        de.set_mode("supervised")
        assert not de.should_auto_execute("risky")
        de.set_mode("full_auto")
        assert de.should_auto_execute("risky")

    def test_destructive_requires_turbo(self):
        de.set_mode("full_auto")
        assert not de.should_auto_execute("destructive")
        de.set_mode("turbo")
        assert de.should_auto_execute("destructive")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  propose / resolve / undo
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDecisionFlow:

    def setup_method(self):
        de._reset_for_tests()

    def test_propose_routine_in_manual_is_pending(self):
        de.set_mode("manual")
        d = de.propose("spawn_agent", "Need coder?", severity="routine",
                       options=[{"id": "yes", "label": "Spawn"}, {"id": "no", "label": "Skip"}])
        assert d.status == de.DecisionStatus.pending
        assert de.get(d.id) is d
        assert d in de.list_pending()

    def test_propose_routine_in_supervised_auto_executes(self):
        de.set_mode("supervised")
        d = de.propose("spawn_agent", "auto", severity="routine")
        assert d.status == de.DecisionStatus.auto_executed
        assert d.chosen_option_id == d.default_option_id
        assert d.resolver == "auto"
        # Auto-executed decisions go straight to history, not pending
        assert d not in de.list_pending()
        assert d in de.list_history()

    def test_destructive_pending_in_full_auto(self):
        de.set_mode("full_auto")
        d = de.propose("delete_artifacts", "nuke?", severity="destructive")
        assert d.status == de.DecisionStatus.pending

    def test_resolve_approve(self):
        de.set_mode("manual")
        d = de.propose("k", "t", severity="routine",
                       options=[{"id": "a", "label": "A"}, {"id": "b", "label": "B"}])
        out = de.resolve(d.id, "b", resolver="user")
        assert out is not None
        assert out.status == de.DecisionStatus.approved
        assert out.chosen_option_id == "b"
        assert out.resolver == "user"
        assert out.id not in [p.id for p in de.list_pending()]

    def test_resolve_unknown_returns_none(self):
        assert de.resolve("dec-nope", "x") is None

    def test_undo_auto_executed(self):
        de.set_mode("supervised")
        d = de.propose("k", "t", severity="routine")
        out = de.undo(d.id)
        assert out is not None
        assert out.status == de.DecisionStatus.undone

    def test_undo_pending_rejects(self):
        de.set_mode("manual")
        d = de.propose("k", "t", severity="routine")
        # undo only acts on resolved decisions
        assert de.undo(d.id) is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Parallel semaphore resizes on mode change
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestParallelSlot:

    def setup_method(self):
        de._reset_for_tests()

    @pytest.mark.asyncio
    async def test_slot_respects_mode(self):
        de.set_mode("manual")
        async with de.parallel_slot():
            # second acquire would block under cap=1 — use short timeout
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(de.parallel_slot().acquire(), timeout=0.05)

    @pytest.mark.asyncio
    async def test_slot_cap_is_read_at_acquire_time(self):
        """Mode change between acquires must take immediate effect for
        new acquirers (N4). We hold one slot under manual (cap=1), then
        switch to full_auto (cap=4) — three more slots must be grabbable
        without waiting."""
        de.set_mode("manual")
        async with de.parallel_slot():
            de.set_mode("full_auto")
            # Three more slots should be available immediately now.
            slots = [de.parallel_slot() for _ in range(3)]
            for s in slots:
                await asyncio.wait_for(s.acquire(), timeout=0.1)
            for s in slots:
                s.release()

    @pytest.mark.asyncio
    async def test_full_auto_allows_multiple_concurrent(self):
        de.set_mode("full_auto")  # cap=4
        held = []
        for _ in range(3):
            s = de.parallel_slot()
            await s.acquire()
            held.append(s)
        # 4th slot still free
        extra = de.parallel_slot()
        await asyncio.wait_for(extra.acquire(), timeout=0.1)
        extra.release()
        for s in held:
            s.release()

    def test_singleton_slot_is_stable_across_mode(self):
        """Phase 47A originally returned a new Semaphore on mode change;
        now the slot is a single object that reads cap on each acquire."""
        de.set_mode("manual")
        s1 = de.parallel_slot()
        de.set_mode("full_auto")
        s2 = de.parallel_slot()
        assert s1 is s2


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTTP endpoints
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestRouter:

    def setup_method(self):
        de._reset_for_tests()

    @pytest.mark.asyncio
    async def test_get_mode(self, client):
        r = await client.get("/api/v1/operation-mode")
        assert r.status_code == 200
        body = r.json()
        assert body["mode"] == "supervised"
        assert body["parallel_cap"] == 2
        assert "manual" in body["modes"]

    @pytest.mark.asyncio
    async def test_put_mode_valid(self, client):
        r = await client.put("/api/v1/operation-mode", json={"mode": "turbo"})
        assert r.status_code == 200
        assert r.json()["mode"] == "turbo"
        assert r.json()["parallel_cap"] == 8

    @pytest.mark.asyncio
    async def test_put_mode_invalid(self, client):
        r = await client.put("/api/v1/operation-mode", json={"mode": "godmode"})
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_list_decisions_empty(self, client):
        r = await client.get("/api/v1/decisions")
        assert r.status_code == 200
        assert r.json()["items"] == []

    @pytest.mark.asyncio
    async def test_list_decisions_pending(self, client):
        de.set_mode("manual")
        d = de.propose("k", "hello", severity="routine")
        r = await client.get("/api/v1/decisions?status=pending")
        assert r.status_code == 200
        ids = [item["id"] for item in r.json()["items"]]
        assert d.id in ids

    @pytest.mark.asyncio
    async def test_get_single_decision(self, client):
        de.set_mode("manual")
        d = de.propose("k", "hi", severity="routine")
        r = await client.get(f"/api/v1/decisions/{d.id}")
        assert r.status_code == 200
        assert r.json()["id"] == d.id

    @pytest.mark.asyncio
    async def test_get_missing_decision(self, client):
        r = await client.get("/api/v1/decisions/dec-missing")
        assert r.status_code == 404
