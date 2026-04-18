"""H2 row 1514 — `auto_derate` config switch + turbo manual-confirm gate.

Covers the operator opt-out for the turbo auto-derate safety net:

* With ``h2_auto_derate=True`` (default) the state machine behaves as
  in row 1513 — the existing ``test_h2_turbo_derate.py`` owns those
  assertions.
* With ``h2_auto_derate=False`` the state machine is frozen:
  ``evaluate_turbo_derate()`` neither engages nor recovers on its own.
* With the safety net off, :func:`set_mode` / :func:`set_session_mode`
  refuse to enter turbo without ``confirm_turbo=True``, raising
  :class:`TurboConfirmRequired`. The HTTP route surfaces this as 409.
* ``clear_turbo_derate()`` lets an operator who flipped the flag
  mid-derate manually restore the full turbo budget.
"""

from __future__ import annotations

import pytest

from backend import decision_engine as de
from backend import host_metrics as hm
from backend.config import settings


class _EventCollector:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def publish(self, event: str, data: dict, **kwargs) -> None:
        merged = dict(data)
        merged.update({f"_{k}": v for k, v in kwargs.items()})
        self.events.append((event, merged))


@pytest.fixture(autouse=True)
def _reset():
    de._reset_for_tests()
    hm._reset_for_tests()
    original = settings.h2_auto_derate
    yield
    settings.h2_auto_derate = original
    de._reset_for_tests()
    hm._reset_for_tests()


@pytest.fixture
def bus_capture(monkeypatch) -> _EventCollector:
    collector = _EventCollector()
    import backend.events as _events
    monkeypatch.setattr(_events.bus, "publish", collector.publish)
    yield collector


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Config flag plumbing
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestAutoDerateFlag:

    def test_default_enabled(self):
        assert settings.h2_auto_derate is True
        assert de.is_auto_derate_enabled() is True

    def test_flag_read_live(self):
        settings.h2_auto_derate = False
        assert de.is_auto_derate_enabled() is False
        settings.h2_auto_derate = True
        assert de.is_auto_derate_enabled() is True

    def test_snapshot_exposes_flag(self):
        settings.h2_auto_derate = False
        snap = de.turbo_derate_snapshot()
        assert snap["auto_derate_enabled"] is False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  State machine is frozen when flag is off
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestStateMachineGated:

    def test_disabled_flag_blocks_engage(self, bus_capture):
        settings.h2_auto_derate = False
        de.evaluate_turbo_derate(now=1000.0, cpu_percent=95.0)
        de.evaluate_turbo_derate(now=1030.0, cpu_percent=95.0)
        assert de.is_turbo_derated() is False
        assert not [
            e for e in bus_capture.events
            if e[0] == "coordinator.turbo_derate"
        ]

    def test_disabled_flag_keeps_existing_state(self):
        """Flipping the flag off while already derated leaves state as-is
        — operators who need immediate recovery call clear_turbo_derate()."""
        de.evaluate_turbo_derate(now=1000.0, cpu_percent=95.0)
        de.evaluate_turbo_derate(now=1030.0, cpu_percent=95.0)
        assert de.is_turbo_derated() is True

        settings.h2_auto_derate = False
        # Auto-recover path is disabled — CPU dropping does nothing.
        de.evaluate_turbo_derate(now=1200.0, cpu_percent=10.0)
        assert de.is_turbo_derated() is True

    def test_clear_turbo_derate_manual_release(self, bus_capture):
        de.evaluate_turbo_derate(now=1000.0, cpu_percent=95.0)
        de.evaluate_turbo_derate(now=1030.0, cpu_percent=95.0)
        assert de.is_turbo_derated() is True

        changed = de.clear_turbo_derate()
        assert changed is True
        assert de.is_turbo_derated() is False
        recovers = [
            e for e in bus_capture.events
            if e[0] == "coordinator.turbo_recover"
        ]
        assert recovers, "manual clear must emit turbo_recover event"
        assert recovers[-1][1].get("manual_clear") is True

    def test_clear_turbo_derate_noop_when_inactive(self):
        assert de.clear_turbo_derate() is False

    def test_enabled_flag_restores_normal_behavior(self):
        settings.h2_auto_derate = False
        de.evaluate_turbo_derate(now=1000.0, cpu_percent=95.0)
        de.evaluate_turbo_derate(now=1030.0, cpu_percent=95.0)
        assert de.is_turbo_derated() is False

        settings.h2_auto_derate = True
        # Re-arms from this point (sustain timer starts fresh).
        de.evaluate_turbo_derate(now=2000.0, cpu_percent=95.0)
        de.evaluate_turbo_derate(now=2030.0, cpu_percent=95.0)
        assert de.is_turbo_derated() is True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Manual-confirm gate on set_mode / set_session_mode
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestTurboConfirmGate:

    def test_default_flag_allows_turbo_without_confirm(self):
        settings.h2_auto_derate = True
        m = de.set_mode("turbo")
        assert m == de.OperationMode.turbo

    def test_disabled_flag_rejects_turbo_without_confirm(self):
        settings.h2_auto_derate = False
        with pytest.raises(de.TurboConfirmRequired):
            de.set_mode("turbo")
        # Mode must NOT have changed — caller stays on previous mode.
        assert de.get_mode() != de.OperationMode.turbo

    def test_disabled_flag_accepts_turbo_with_confirm(self):
        settings.h2_auto_derate = False
        m = de.set_mode("turbo", confirm_turbo=True)
        assert m == de.OperationMode.turbo

    def test_disabled_flag_does_not_gate_other_modes(self):
        settings.h2_auto_derate = False
        for mode in ("manual", "supervised", "full_auto"):
            m = de.set_mode(mode)
            assert m.value == mode

    @pytest.mark.asyncio
    async def test_session_mode_gate(self, monkeypatch):
        # Stub auth session layer — we only care about the gate logic.
        from backend import auth as _auth

        async def fake_update(token, meta):
            return None

        async def fake_get(token):
            return None

        monkeypatch.setattr(_auth, "update_session_metadata", fake_update)
        monkeypatch.setattr(_auth, "get_session", fake_get)

        settings.h2_auto_derate = False
        with pytest.raises(de.TurboConfirmRequired):
            await de.set_session_mode("sess-token-abc123", "turbo")
        m = await de.set_session_mode(
            "sess-token-abc123", "turbo", confirm_turbo=True
        )
        assert m == de.OperationMode.turbo
