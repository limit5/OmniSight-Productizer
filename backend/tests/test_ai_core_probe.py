"""OP-1160 contract tests for the ai-core auxiliary-service probe."""

from __future__ import annotations

import httpx
import pytest

from backend import metrics
from backend.agents import ai_core_probe as probe_mod
from backend.agents.ai_core_probe import AiCoreProbe


pytestmark = pytest.mark.skipif(
    not metrics.is_available(), reason="prometheus_client not installed"
)


class Clock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _metric_text() -> str:
    from prometheus_client import generate_latest

    return generate_latest(metrics.REGISTRY).decode()


def setup_function() -> None:
    metrics.reset_for_tests()
    probe_mod.AI_CORE_AVAILABLE = False
    probe_mod.AUX_SERVICE_AVAILABLE[probe_mod.SERVICE_LABEL] = False


def _http_from(observations: list[bool]) -> probe_mod.HttpGet:
    """Build an http_get returning 200 for True and 503 for False, in order."""
    it = iter(observations)

    def _get(_url: str, _timeout_s: float) -> int:
        return 200 if next(it) else 503

    return _get


def test_probe_returns_available_when_health_200() -> None:
    probe = AiCoreProbe(
        flap_threshold=1,
        http_get=lambda _url, _timeout: 200,
    )

    assert probe.probe_once() is True
    assert probe_mod.AI_CORE_AVAILABLE is True


def test_probe_returns_unavailable_when_404() -> None:
    probe = AiCoreProbe(
        flap_threshold=1,
        initial_available=True,
        http_get=lambda _url, _timeout: 404,
    )

    assert probe.probe_once() is False
    assert probe_mod.AI_CORE_AVAILABLE is False


def test_probe_returns_unavailable_when_timeout() -> None:
    def _timeout(_url: str, _timeout_s: float) -> int:
        raise httpx.TimeoutException("timed out")

    probe = AiCoreProbe(
        flap_threshold=1,
        initial_available=True,
        http_get=_timeout,
    )

    assert probe.probe_once() is False


def test_probe_returns_unavailable_when_connection_error() -> None:
    def _connection_error(_url: str, _timeout_s: float) -> int:
        raise httpx.ConnectError("connection failed")

    probe = AiCoreProbe(
        flap_threshold=1,
        initial_available=True,
        http_get=_connection_error,
    )

    assert probe.probe_once() is False


def test_flap_threshold_prevents_premature_flip() -> None:
    probe = AiCoreProbe(
        flap_threshold=3,
        initial_available=False,
        http_get=lambda _url, _timeout: 200,
    )

    assert probe.probe_once() is False
    assert probe_mod.AI_CORE_AVAILABLE is False


def test_flap_threshold_flips_after_3_consecutive() -> None:
    probe = AiCoreProbe(
        flap_threshold=3,
        initial_available=False,
        http_get=lambda _url, _timeout: 200,
    )

    assert probe.probe_once() is False
    assert probe.probe_once() is False
    assert probe.probe_once() is True
    assert probe_mod.AI_CORE_AVAILABLE is True


def test_prometheus_gauge_reflects_state() -> None:
    probe = AiCoreProbe(
        flap_threshold=1,
        http_get=lambda _url, _timeout: 200,
    )

    probe.probe_once()

    assert 'omnisight_aux_service_available{service="ai_core"} 1.0' in _metric_text()


def test_long_outage_log_emitted_after_24h(caplog: pytest.LogCaptureFixture) -> None:
    clock = Clock()
    probe = AiCoreProbe(
        flap_threshold=1,
        initial_available=True,
        clock=clock,
        http_get=lambda _url, _timeout: 503,
    )

    probe.probe_once()
    clock.now = probe_mod.LONG_OUTAGE_S + 1
    with caplog.at_level("ERROR", logger=probe_mod.__name__):
        probe.probe_once()

    assert any("aux_service.long_outage" in record.message for record in caplog.records)


# ── OP-1749 §3.6 cases ──────────────────────────────────────────────────────


def test_three_of_five_flip_non_consecutive() -> None:
    """§3.6 #5: 3 down-observations within a 5-probe window flip down even when
    they are interleaved with up-observations (non-consecutive)."""
    # available=True, observe: down, up, down, up, down → 3 downs in last 5.
    probe = AiCoreProbe(
        flap_window=5,
        flap_threshold=3,
        initial_available=True,
        http_get=_http_from([False, True, False, True, False]),
    )

    results = [probe.probe_once() for _ in range(5)]

    assert results == [True, True, True, True, False]
    assert probe_mod.AI_CORE_AVAILABLE is False
    assert probe_mod.AUX_SERVICE_AVAILABLE["ai_core"] is False


def test_single_down_observation_is_suppressed() -> None:
    """§3.6 #6: a lone down-blip surrounded by ups never reaches the 3-of-5
    threshold, so the stable flag stays available."""
    probe = AiCoreProbe(
        flap_window=5,
        flap_threshold=3,
        initial_available=True,
        http_get=_http_from([True, False, True, True, True]),
    )

    for _ in range(5):
        assert probe.probe_once() is True

    assert probe_mod.AI_CORE_AVAILABLE is True
    assert probe_mod.AUX_SERVICE_AVAILABLE["ai_core"] is True


def test_gauge_initialises_to_zero_on_construction() -> None:
    """§3.6 #8: constructing the probe publishes the gauge as 0.0 (fail-closed),
    not the pre-probe NaN sentinel."""
    AiCoreProbe(
        flap_window=5,
        flap_threshold=3,
        http_get=lambda _url, _timeout: 200,
    )

    assert 'omnisight_aux_service_available{service="ai_core"} 0.0' in _metric_text()


def test_bootstrap_state_is_fail_closed_false() -> None:
    """§3.6 #10: with no observations yet, the probe and the AUX dict both report
    unavailable (fail-closed bootstrap)."""
    probe = AiCoreProbe(
        flap_window=5,
        flap_threshold=3,
        http_get=lambda _url, _timeout: 200,
    )

    assert probe._available is False  # constructed but not yet probed
    assert probe_mod.AI_CORE_AVAILABLE is False
    assert probe_mod.AUX_SERVICE_AVAILABLE["ai_core"] is False


def test_aux_dict_tracks_flip_up_and_down() -> None:
    """AUX_SERVICE_AVAILABLE mirrors the stable flag across a full up→down cycle."""
    probe = AiCoreProbe(
        flap_window=5,
        flap_threshold=3,
        initial_available=False,
        http_get=_http_from([True, True, True, False, False, False]),
    )

    for _ in range(3):
        probe.probe_once()
    assert probe_mod.AUX_SERVICE_AVAILABLE["ai_core"] is True

    for _ in range(3):
        probe.probe_once()
    assert probe_mod.AUX_SERVICE_AVAILABLE["ai_core"] is False


def test_probe_url_prefers_new_env_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(probe_mod.LEGACY_HEALTH_URL_ENV, "http://legacy/health")
    monkeypatch.setenv(probe_mod.PROBE_URL_ENV, "http://probe/health")

    probe = AiCoreProbe(http_get=lambda _url, _timeout: 200)

    assert probe.probe_url == "http://probe/health"


def test_disable_env_forces_unavailable_without_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(probe_mod.AUX_SERVICE_DISABLE_ENV, "vision_core,ai_core")
    calls = 0

    def _http_get(_url: str, _timeout_s: float) -> int:
        nonlocal calls
        calls += 1
        return 200

    probe = AiCoreProbe(
        flap_threshold=1,
        initial_available=True,
        http_get=_http_get,
    )

    assert probe.probe_once() is False
    assert calls == 0
    assert probe_mod.AUX_SERVICE_AVAILABLE["ai_core"] is False


def test_chaos_up_down_up_cycle_logs_transition_and_flips_gauge(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """OP-1757 AC: controllable up→down→up samples flip log + gauge."""
    probe = AiCoreProbe(
        flap_window=5,
        flap_threshold=3,
        initial_available=False,
        http_get=_http_from([
            True, True, True,
            False, False, False,
            True, True, True,
        ]),
    )

    with caplog.at_level("INFO", logger=probe_mod.__name__):
        for _ in range(3):
            probe.probe_once()
        assert probe_mod.AUX_SERVICE_AVAILABLE["ai_core"] is True
        assert (
            'omnisight_aux_service_available{service="ai_core"} 1.0'
            in _metric_text()
        )

        for _ in range(3):
            probe.probe_once()
        assert probe_mod.AUX_SERVICE_AVAILABLE["ai_core"] is False
        assert (
            'omnisight_aux_service_available{service="ai_core"} 0.0'
            in _metric_text()
        )

        for _ in range(3):
            probe.probe_once()
        assert probe_mod.AUX_SERVICE_AVAILABLE["ai_core"] is True
        assert (
            'omnisight_aux_service_available{service="ai_core"} 1.0'
            in _metric_text()
        )

    transitions = [
        record.message
        for record in caplog.records
        if "aux_service.state_change service=ai_core" in record.message
    ]
    assert len(transitions) == 3
    assert "previous=False current=True" in transitions[0]
    assert "previous=True current=False" in transitions[1]
    assert "previous=False current=True" in transitions[2]
