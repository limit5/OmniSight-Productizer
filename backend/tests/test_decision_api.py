"""Tests for Phase 47D: Decision API (approve/reject/undo) + timeout sweep."""

from __future__ import annotations

import time

import pytest

from backend import decision_engine as de


class TestApprove:

    def setup_method(self):
        de._reset_for_tests()

    @pytest.mark.asyncio
    async def test_approve_happy_path(self, client):
        de.set_mode("manual")
        d = de.propose("k", "t", severity="routine",
                       options=[{"id": "a", "label": "A"}, {"id": "b", "label": "B"}])
        r = await client.post(f"/api/v1/decisions/{d.id}/approve", json={"option_id": "b"})
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "approved"
        assert body["chosen_option_id"] == "b"
        # now it's history, and pending queue is empty
        pending = await client.get("/api/v1/decisions?status=pending")
        assert d.id not in [x["id"] for x in pending.json()["items"]]

    @pytest.mark.asyncio
    async def test_approve_missing(self, client):
        r = await client.post("/api/v1/decisions/dec-zzz/approve", json={"option_id": "a"})
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_approve_rejects_unknown_option(self, client):
        de.set_mode("manual")
        d = de.propose("k", "t", severity="routine",
                       options=[{"id": "a", "label": "A"}])
        r = await client.post(f"/api/v1/decisions/{d.id}/approve", json={"option_id": "not_there"})
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_approve_already_resolved_409(self, client):
        de.set_mode("supervised")  # auto-executes
        d = de.propose("k", "t", severity="routine")
        r = await client.post(f"/api/v1/decisions/{d.id}/approve", json={"option_id": d.default_option_id})
        assert r.status_code == 409


class TestReject:

    def setup_method(self):
        de._reset_for_tests()

    @pytest.mark.asyncio
    async def test_reject_pending(self, client):
        de.set_mode("manual")
        d = de.propose("k", "t", severity="routine")
        r = await client.post(f"/api/v1/decisions/{d.id}/reject")
        assert r.status_code == 200
        assert r.json()["status"] == "rejected"

    @pytest.mark.asyncio
    async def test_reject_missing(self, client):
        r = await client.post("/api/v1/decisions/nope/reject")
        assert r.status_code == 404


class TestUndo:

    def setup_method(self):
        de._reset_for_tests()

    @pytest.mark.asyncio
    async def test_undo_auto_executed(self, client):
        de.set_mode("supervised")
        d = de.propose("k", "t", severity="routine")
        r = await client.post(f"/api/v1/decisions/{d.id}/undo")
        assert r.status_code == 200
        assert r.json()["status"] == "undone"

    @pytest.mark.asyncio
    async def test_undo_pending_404(self, client):
        # undo() only acts on resolved decisions
        de.set_mode("manual")
        d = de.propose("k", "t", severity="routine")
        r = await client.post(f"/api/v1/decisions/{d.id}/undo")
        assert r.status_code == 404


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Timeout sweep
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestTimeoutSweep:

    def setup_method(self):
        de._reset_for_tests()

    def test_sweep_resolves_expired(self):
        de.set_mode("manual")
        d = de.propose("k", "t", severity="routine", timeout_s=1.0,
                       options=[{"id": "safe", "label": "S"}, {"id": "risky", "label": "R"}])
        # simulate time passing
        resolved = de.sweep_timeouts(now=time.time() + 2.0)
        assert len(resolved) == 1
        assert resolved[0].id == d.id
        assert resolved[0].status == de.DecisionStatus.timeout_default
        assert resolved[0].chosen_option_id == "safe"
        assert resolved[0].resolver == "timeout"

    def test_sweep_ignores_unexpired(self):
        de.set_mode("manual")
        de.propose("k", "t", severity="routine", timeout_s=60)
        resolved = de.sweep_timeouts()
        assert resolved == []

    def test_sweep_ignores_no_deadline(self):
        de.set_mode("manual")
        de.propose("k", "t", severity="routine", timeout_s=None)
        resolved = de.sweep_timeouts(now=time.time() + 99999)
        assert resolved == []

    @pytest.mark.asyncio
    async def test_sweep_endpoint(self, client):
        de.set_mode("manual")
        d = de.propose("k", "t", severity="routine", timeout_s=0.1)
        # allow deadline to pass
        import asyncio
        await asyncio.sleep(0.15)
        r = await client.post("/api/v1/decisions/sweep")
        assert r.status_code == 200
        assert d.id in r.json()["ids"]
