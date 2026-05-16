"""OP-1160 — periodic ai-core availability probe."""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable

import httpx

from backend import metrics

DEFAULT_PROBE_URL = "http://ai-core:8080/health"
DEFAULT_INTERVAL_S = 60.0
DEFAULT_TIMEOUT_S = 5.0
DEFAULT_FLAP_THRESHOLD = 3
LONG_OUTAGE_S = 24 * 60 * 60
SERVICE_LABEL = "ai_core"

AI_CORE_AVAILABLE = False

logger = logging.getLogger(__name__)


HttpGet = Callable[[str, float], int]
Clock = Callable[[], float]
Sleeper = Callable[[float], None]


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("ai_core_probe.invalid_env name=%s value=%r", name, raw)
        return default
    return value if value > 0 else default


def _default_http_get(url: str, timeout_s: float) -> int:
    with httpx.Client(trust_env=False, timeout=timeout_s) as client:
        response = client.get(url)
    return response.status_code


class AiCoreProbe:
    """Probe ai-core health and expose stable availability state."""

    def __init__(
        self,
        *,
        probe_url: str | None = None,
        interval_s: float | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        flap_threshold: int = DEFAULT_FLAP_THRESHOLD,
        http_get: HttpGet | None = None,
        clock: Clock | None = None,
        sleeper: Sleeper | None = None,
        initial_available: bool = False,
    ) -> None:
        self.probe_url = probe_url or os.getenv(
            "OMNISIGHT_AI_CORE_HEALTH_URL",
            DEFAULT_PROBE_URL,
        )
        self.interval_s = (
            _env_float("OMNISIGHT_AI_CORE_PROBE_INTERVAL_S", DEFAULT_INTERVAL_S)
            if interval_s is None
            else interval_s
        )
        self.timeout_s = timeout_s
        self.flap_threshold = max(1, int(flap_threshold))
        self._http_get = http_get or _default_http_get
        self._clock = clock or time.time
        self._sleeper = sleeper or time.sleep
        self._available = bool(initial_available)
        self._candidate: bool | None = None
        self._candidate_count = 0
        self._unavailable_since: float | None = None
        self._long_outage_logged = False
        self._publish_state()
        self._set_global(self._available)

    def probe_once(self) -> bool:
        """Run one health check and return the stable availability flag."""
        observed_available = self._observe_available()
        self._apply_observation(observed_available)
        self._publish_state()
        self._maybe_log_long_outage()
        return self._available

    def run_forever(self, stop_event: threading.Event | None = None) -> None:
        """Probe periodically until ``stop_event`` is set."""
        while stop_event is None or not stop_event.is_set():
            self.probe_once()
            if stop_event is not None and stop_event.wait(self.interval_s):
                break
            if stop_event is None:
                self._sleeper(self.interval_s)

    def _observe_available(self) -> bool:
        try:
            status_code = self._http_get(self.probe_url, self.timeout_s)
        except (httpx.TimeoutException, httpx.RequestError, OSError):
            return False
        return status_code == 200

    def _apply_observation(self, observed_available: bool) -> None:
        if observed_available == self._available:
            self._candidate = None
            self._candidate_count = 0
            self._track_unavailability()
            return

        if observed_available == self._candidate:
            self._candidate_count += 1
        else:
            self._candidate = observed_available
            self._candidate_count = 1

        if self._candidate_count >= self.flap_threshold:
            previous = self._available
            self._available = observed_available
            self._candidate = None
            self._candidate_count = 0
            self._set_global(self._available)
            self._track_unavailability()
            logger.info(
                "aux_service.state_change service=%s previous=%s current=%s",
                SERVICE_LABEL,
                previous,
                self._available,
            )

    def _track_unavailability(self) -> None:
        if self._available:
            self._unavailable_since = None
            self._long_outage_logged = False
        elif self._unavailable_since is None:
            self._unavailable_since = self._clock()

    def _maybe_log_long_outage(self) -> None:
        if self._available or self._unavailable_since is None:
            return
        if self._long_outage_logged:
            return
        outage_s = self._clock() - self._unavailable_since
        if outage_s > LONG_OUTAGE_S:
            self._long_outage_logged = True
            logger.error(
                "aux_service.long_outage service=%s outage_s=%.0f",
                SERVICE_LABEL,
                outage_s,
            )

    def _publish_state(self) -> None:
        metrics.aux_service_available.labels(service=SERVICE_LABEL).set(
            1.0 if self._available else 0.0
        )

    @staticmethod
    def _set_global(value: bool) -> None:
        global AI_CORE_AVAILABLE
        AI_CORE_AVAILABLE = bool(value)


__all__ = [
    "AI_CORE_AVAILABLE",
    "AiCoreProbe",
    "DEFAULT_PROBE_URL",
    "DEFAULT_INTERVAL_S",
    "DEFAULT_TIMEOUT_S",
    "DEFAULT_FLAP_THRESHOLD",
]
