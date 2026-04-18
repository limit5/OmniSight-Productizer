"""H2 row 1516 — Phase 53 hash-chain audit for turbo derate / recover.

Every transition through ``_emit_turbo_transition`` must:

  * publish the SSE event on ``coordinator.turbo_derate`` /
    ``coordinator.turbo_recover`` (already covered by
    ``test_h2_turbo_derate.py``)
  * write a hash-chained audit row via ``audit.log_sync`` so an operator
    can later reconstruct *why* a turbo session was throttled

The audit row mirrors the SSE payload (cpu_percent, sustain elapsed,
budget swap) and uses ``entity_kind=turbo_derate`` with
``entity_id=engaged|recovered`` so the chain is queryable by direction.
The hash chain is owned by ``backend.audit`` (Phase 53) — we only assert
the call shape here; chain integrity itself is exercised in
``test_audit.py``.
"""

from __future__ import annotations

import pytest

from backend import decision_engine as de


class _AuditCollector:
    """Captures audit.log_sync calls without hitting the DB."""

    def __init__(self) -> None:
        self.rows: list[dict] = []

    def log_sync(self, **kwargs) -> None:
        self.rows.append(dict(kwargs))


class _EventCollector:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def publish(self, event: str, data: dict, **kwargs) -> None:
        self.events.append((event, dict(data)))


@pytest.fixture(autouse=True)
def _reset():
    de._reset_for_tests()
    yield
    de._reset_for_tests()


@pytest.fixture
def audit_capture(monkeypatch) -> _AuditCollector:
    collector = _AuditCollector()
    import backend.audit as _audit
    monkeypatch.setattr(_audit, "log_sync", collector.log_sync)
    yield collector


@pytest.fixture
def bus_capture(monkeypatch) -> _EventCollector:
    collector = _EventCollector()
    import backend.events as _events
    monkeypatch.setattr(_events.bus, "publish", collector.publish)
    yield collector


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Auto-engage path
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestAutoDerateAuditRow:

    def test_sustained_high_cpu_writes_audit_row(self, audit_capture):
        de.evaluate_turbo_derate(now=1000.0, cpu_percent=95.0)
        # Pre-transition: no audit row yet.
        assert audit_capture.rows == []
        de.evaluate_turbo_derate(now=1030.0, cpu_percent=95.0)

        derate_rows = [
            r for r in audit_capture.rows
            if r["action"] == "coordinator.turbo_derate"
        ]
        assert len(derate_rows) == 1
        row = derate_rows[0]
        assert row["entity_kind"] == "turbo_derate"
        assert row["entity_id"] == "engaged"
        assert row["before"] == {"derate_active": False}
        assert row["after"]["derate_active"] is True
        # SSE payload is mirrored verbatim into the audit `after` dict.
        assert row["after"]["cpu_percent"] == 95.0
        assert row["after"]["threshold_pct"] == 80.0
        assert row["after"]["sustain_required_s"] == 30.0
        assert row["after"]["derated_to_budget"] == 2
        assert row["after"]["from_budget"] == 8
        assert row["after"]["at"] == 1030.0

    def test_brief_spike_writes_no_audit_row(self, audit_capture):
        # 29.9s < sustain window — no transition, no audit row.
        de.evaluate_turbo_derate(now=1000.0, cpu_percent=95.0)
        de.evaluate_turbo_derate(now=1029.9, cpu_percent=95.0)
        assert audit_capture.rows == []

    def test_clean_host_writes_no_audit_row(self, audit_capture):
        de.evaluate_turbo_derate(now=1000.0, cpu_percent=30.0)
        de.evaluate_turbo_derate(now=1030.0, cpu_percent=30.0)
        assert audit_capture.rows == []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Auto-recover path
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestAutoRecoverAuditRow:

    def _engage(self, audit_capture):
        de.evaluate_turbo_derate(now=1000.0, cpu_percent=95.0)
        de.evaluate_turbo_derate(now=1030.0, cpu_percent=95.0)
        # Drop the engage row so each test asserts on the recover row alone.
        audit_capture.rows.clear()

    def test_full_cooldown_writes_recover_audit_row(self, audit_capture):
        self._engage(audit_capture)
        de.evaluate_turbo_derate(now=1031.0, cpu_percent=50.0)
        de.evaluate_turbo_derate(now=1151.0, cpu_percent=50.0)

        recover_rows = [
            r for r in audit_capture.rows
            if r["action"] == "coordinator.turbo_recover"
        ]
        assert len(recover_rows) == 1
        row = recover_rows[0]
        assert row["entity_kind"] == "turbo_derate"
        assert row["entity_id"] == "recovered"
        assert row["before"] == {"derate_active": True}
        assert row["after"]["derate_active"] is False
        assert row["after"]["restored_to_budget"] == 8
        assert row["after"]["cooldown_required_s"] == 120.0
        assert row["after"]["at"] == 1151.0

    def test_cpu_spike_resets_cooldown_no_recover_row(self, audit_capture):
        self._engage(audit_capture)
        de.evaluate_turbo_derate(now=1031.0, cpu_percent=50.0)
        # Spike interrupts cooldown.
        de.evaluate_turbo_derate(now=1060.0, cpu_percent=95.0)
        de.evaluate_turbo_derate(now=1100.0, cpu_percent=50.0)
        # Original recover-time would have been 1151; with the reset it
        # has only been 51s into the new cooldown — no recover row yet.
        assert not [
            r for r in audit_capture.rows
            if r["action"] == "coordinator.turbo_recover"
        ]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Manual operator clear
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestManualClearAuditRow:

    def test_clear_turbo_derate_writes_recover_audit_row(self, audit_capture):
        # Engage manually so the engage row is captured + cleared first.
        de.evaluate_turbo_derate(now=1000.0, cpu_percent=95.0)
        de.evaluate_turbo_derate(now=1030.0, cpu_percent=95.0)
        audit_capture.rows.clear()

        changed = de.clear_turbo_derate()
        assert changed is True

        recover_rows = [
            r for r in audit_capture.rows
            if r["action"] == "coordinator.turbo_recover"
        ]
        assert len(recover_rows) == 1
        row = recover_rows[0]
        assert row["entity_id"] == "recovered"
        assert row["after"]["manual_clear"] is True
        assert row["after"]["restored_to_budget"] == 8

    def test_clear_when_inactive_writes_nothing(self, audit_capture):
        # Already inactive — clear is a no-op, no row written.
        changed = de.clear_turbo_derate()
        assert changed is False
        assert audit_capture.rows == []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Resilience — audit failure must not break the transition
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestAuditFailureIsolation:

    def test_audit_log_sync_failure_does_not_break_state_machine(
        self, monkeypatch, bus_capture,
    ):
        import backend.audit as _audit

        def _boom(**kwargs):
            raise RuntimeError("simulated audit failure")

        monkeypatch.setattr(_audit, "log_sync", _boom)

        de.evaluate_turbo_derate(now=1000.0, cpu_percent=95.0)
        active = de.evaluate_turbo_derate(now=1030.0, cpu_percent=95.0)
        # State machine still flips even though audit blew up.
        assert active is True
        assert de.is_turbo_derated() is True
        # SSE bus still received the transition.
        assert any(
            e[0] == "coordinator.turbo_derate" for e in bus_capture.events
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Cycle: engage → recover writes both rows in order
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestCycleAuditTrail:

    def test_engage_then_recover_writes_two_rows_in_order(self, audit_capture):
        de.evaluate_turbo_derate(now=1000.0, cpu_percent=95.0)
        de.evaluate_turbo_derate(now=1030.0, cpu_percent=95.0)
        de.evaluate_turbo_derate(now=1031.0, cpu_percent=50.0)
        de.evaluate_turbo_derate(now=1151.0, cpu_percent=50.0)

        actions = [r["action"] for r in audit_capture.rows]
        assert actions == [
            "coordinator.turbo_derate",
            "coordinator.turbo_recover",
        ]
        # Direction tags are inverses of each other.
        ids = [r["entity_id"] for r in audit_capture.rows]
        assert ids == ["engaged", "recovered"]
