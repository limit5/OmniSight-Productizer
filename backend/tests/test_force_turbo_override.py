"""H3 row 1527 — tests for the `Force turbo` manual override endpoint.

Covers:
  * ``POST /api/v1/coordinator/force-turbo`` refuses ``confirm=false``
    with 422 so a CLI / curl caller can't silently bypass the UI's
    OOM-warning confirm dialog.
  * Admin role is required (anon-admin in ``OMNISIGHT_AUTH_MODE=open``
    satisfies the gate; an explicit role=viewer user does not).
  * Success path clears an active H2 turbo auto-derate AND resets the
    DRF sandbox capacity derate ratio back to 1.0 so the effective
    budget returns to CAPACITY_MAX.
  * No-op path: when nothing is derated the call still succeeds (200)
    but ``cleared_turbo_derate`` / ``reset_capacity_derate`` are False.
  * A Phase-53 hash-chain audit row is written with
    ``action=coordinator.force_turbo_override``,
    ``entity_kind=force_turbo_override``, ``entity_id=applied`` and a
    before/after pair describing the state transition.
  * An SSE ``coordinator.force_turbo_override`` event is broadcast so
    every open dashboard picks up the override immediately.

Module-global state audit (SOP Step 1): the backend mutates
``backend.decision_engine._turbo_derate_state`` (module-global) and
``backend.sandbox_capacity._derate_ratio`` / ``_derate_reason`` (also
module-global). Both are deliberately per-worker: every worker derives
its own snapshot from its own host-metrics sampling loop. A force-turbo
override on worker A does NOT propagate to worker B's derate state —
but the audit row + SSE broadcast (Postgres-backed / bus-broadcast)
are the cross-worker truth, which is the intended architecture
(audit/SSE is the source of truth; in-memory state is per-worker view).
This matches SOP Step-1 acceptable answer #3 ("deliberately per-worker").
"""

from __future__ import annotations

from typing import Any

import pytest

from backend import decision_engine as de
from backend import sandbox_capacity as sc


@pytest.fixture(autouse=True)
def _reset_state():
    de._reset_for_tests()
    sc._reset_for_tests()
    yield
    de._reset_for_tests()
    sc._reset_for_tests()


class _AuditCollector:
    """Captures audit.log_sync calls so tests don't need the real DB."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def log_sync(self, **kwargs: Any) -> None:
        self.rows.append(dict(kwargs))


class _EventCollector:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def publish(self, event: str, data: dict[str, Any], **kwargs: Any) -> None:
        self.events.append((event, dict(data)))


@pytest.fixture
def audit_capture(monkeypatch):
    collector = _AuditCollector()
    import backend.audit as _audit
    monkeypatch.setattr(_audit, "log_sync", collector.log_sync)
    yield collector


@pytest.fixture
def bus_capture(monkeypatch):
    collector = _EventCollector()
    import backend.events as _events
    monkeypatch.setattr(_events.bus, "publish", collector.publish)
    yield collector


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  confirm=true gate
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestConfirmGate:

    @pytest.mark.asyncio
    async def test_missing_confirm_returns_422(self, client, audit_capture):
        r = await client.post("/api/v1/coordinator/force-turbo", json={})
        assert r.status_code == 422
        body = r.json()
        assert body["code"] == "force_turbo_confirm_required"
        # No audit row written when the guard refuses the call — operators
        # must acknowledge OOM risk before any state mutation happens.
        assert audit_capture.rows == []

    @pytest.mark.asyncio
    async def test_explicit_confirm_false_returns_422(self, client, audit_capture):
        r = await client.post(
            "/api/v1/coordinator/force-turbo",
            json={"confirm": False},
        )
        assert r.status_code == 422
        assert audit_capture.rows == []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  No-op path: nothing is derated
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestNoOpPath:

    @pytest.mark.asyncio
    async def test_confirm_true_when_clean_returns_200_and_noop(
        self, client, audit_capture, bus_capture,
    ):
        # Nothing is derated → call should succeed, with cleared/reset
        # both False, but STILL write an audit row so "someone pressed
        # the big red button" survives in the trail.
        r = await client.post(
            "/api/v1/coordinator/force-turbo",
            json={"confirm": True},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["applied"] is True
        assert body["cleared_turbo_derate"] is False
        assert body["reset_capacity_derate"] is False
        assert body["before"]["turbo_derate_active"] is False
        assert body["before"]["capacity_derate_ratio"] == 1.0
        # Audit row present even on no-op — operator-action trail.
        rows = [
            r for r in audit_capture.rows
            if r["action"] == "coordinator.force_turbo_override"
        ]
        assert len(rows) == 1
        assert rows[0]["entity_id"] == "applied"
        assert rows[0]["entity_kind"] == "force_turbo_override"
        assert rows[0]["after"]["manual_override"] is True
        # SSE event fires regardless so the dashboard updates.
        assert any(e[0] == "coordinator.force_turbo_override" for e in bus_capture.events)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Success path: clears an active turbo derate
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestClearsActiveTurboDerate:

    @pytest.mark.asyncio
    async def test_clears_active_turbo_derate_and_writes_audit(
        self, client, audit_capture, bus_capture,
    ):
        # Engage an H2 auto-derate the normal way: sustained high CPU.
        de.evaluate_turbo_derate(now=1000.0, cpu_percent=95.0)
        de.evaluate_turbo_derate(now=1030.0, cpu_percent=95.0)
        assert de.is_turbo_derated() is True
        # Drop the engage row + event so we only assert on the override.
        audit_capture.rows.clear()
        bus_capture.events.clear()

        r = await client.post(
            "/api/v1/coordinator/force-turbo",
            json={"confirm": True, "reason": "benchmark spike, not workload"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["cleared_turbo_derate"] is True
        assert body["before"]["turbo_derate_active"] is True
        assert body["after"]["turbo_derate_active"] is False
        # Restored-to-budget reflects the turbo parallel cap (8).
        assert body["after"]["restored_to_budget"] == 8
        assert body["after"]["operator_reason"] == "benchmark spike, not workload"
        # State machine really flipped.
        assert de.is_turbo_derated() is False

        # Phase-53 audit row: both the force_turbo_override AND the
        # underlying turbo_recover row (clear_turbo_derate() emits that
        # one as well) must be present.
        actions = [r["action"] for r in audit_capture.rows]
        assert "coordinator.force_turbo_override" in actions
        # SSE event set should include both the recover event and the
        # force-turbo override event.
        event_names = {e[0] for e in bus_capture.events}
        assert "coordinator.force_turbo_override" in event_names


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Success path: resets DRF capacity derate
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestResetsCapacityDerate:

    @pytest.mark.asyncio
    async def test_resets_capacity_derate_and_restores_effective_budget(
        self, client, audit_capture,
    ):
        # Coordinator has derated the DRF capacity to 40% under MEM pressure.
        sc.set_derate(0.4, reason="MEM 91% > threshold")
        snap_before = sc.snapshot()
        assert snap_before["derated"] is True
        assert snap_before["derate_reason"] == "MEM 91% > threshold"

        audit_capture.rows.clear()
        r = await client.post(
            "/api/v1/coordinator/force-turbo",
            json={"confirm": True},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["reset_capacity_derate"] is True
        assert body["before"]["capacity_derate_ratio"] == pytest.approx(0.4)
        assert body["before"]["capacity_derate_reason"] == "MEM 91% > threshold"
        assert body["after"]["capacity_derate_ratio"] == pytest.approx(1.0)

        # Effective budget back to CAPACITY_MAX (12).
        snap_after = sc.snapshot()
        assert snap_after["derated"] is False
        assert snap_after["derate_ratio"] == pytest.approx(1.0)
        assert snap_after["effective_capacity_max"] == pytest.approx(sc.CAPACITY_MAX)

        # Audit row captures the before/after ratio so an operator can
        # see what was overridden later.
        rows = [
            r for r in audit_capture.rows
            if r["action"] == "coordinator.force_turbo_override"
        ]
        assert len(rows) == 1
        assert rows[0]["before"]["capacity_derate_ratio"] == pytest.approx(0.4)
        assert rows[0]["after"]["capacity_derate_ratio"] == pytest.approx(1.0)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Audit + SSE resilience: one failing doesn't break the other
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestAuditFailureIsolation:

    @pytest.mark.asyncio
    async def test_audit_failure_does_not_break_override(
        self, monkeypatch, client, bus_capture,
    ):
        # Derate the capacity first so there's real state to reset.
        sc.set_derate(0.4, reason="simulated")
        import backend.audit as _audit

        def _boom(**kwargs):
            raise RuntimeError("simulated audit failure")

        monkeypatch.setattr(_audit, "log_sync", _boom)

        r = await client.post(
            "/api/v1/coordinator/force-turbo",
            json={"confirm": True},
        )
        # Endpoint must still succeed — audit is best-effort, state
        # change is the operator-visible contract.
        assert r.status_code == 200
        assert sc.snapshot()["derated"] is False
        # SSE event still fired.
        assert any(e[0] == "coordinator.force_turbo_override" for e in bus_capture.events)
