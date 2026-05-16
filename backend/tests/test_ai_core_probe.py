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
