"""OP-1163 — periodic Alembic image-DB drift probe."""

from __future__ import annotations

import importlib
import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

from backend import metrics

DEFAULT_INTERVAL_S = 60.0
DIRECTIONS = ("aligned", "forward", "backward")

logger = logging.getLogger(__name__)

Comparator = Callable[[str, Path], tuple[int, dict[str, Any]]]
DbUrlResolver = Callable[[], str | None]
ScriptDirResolver = Callable[[], Path]
Sleeper = Callable[[float], None]


class AlembicDriftProbe:
    """Probe Alembic drift through the boot-time gate comparator."""

    def __init__(
        self,
        *,
        interval_s: float = DEFAULT_INTERVAL_S,
        comparator: Comparator | None = None,
        db_url_resolver: DbUrlResolver | None = None,
        script_dir_resolver: ScriptDirResolver | None = None,
        sleeper: Sleeper | None = None,
    ) -> None:
        if interval_s <= 0:
            raise ValueError("interval_s must be positive")
        gate = self._load_gate()
        self.interval_s = float(interval_s)
        self._comparator = comparator or gate.check_drift
        self._db_url_resolver = db_url_resolver or gate._resolve_db_url
        self._script_dir_resolver = script_dir_resolver or gate._script_dir_path
        self._sleeper = sleeper or time.sleep
        self._publish("aligned")

    def probe_once(self) -> str:
        """Run one drift check and publish the labelled gauge state."""
        db_url = self._db_url_resolver()
        if db_url is None:
            self._publish("aligned")
            return "aligned"

        try:
            code, payload = self._comparator(db_url, self._script_dir_resolver())
        except Exception:
            metrics.alembic_drift_probe_errors_total.inc()
            logger.exception("alembic_drift_probe.collection_failed")
            return "error"

        if code == 2:
            metrics.alembic_drift_probe_errors_total.inc()
            logger.warning(
                "alembic_drift_probe.collection_failed reason=%s",
                payload.get("reason"),
            )
            return "error"

        direction = self._direction_from_payload(code, payload)
        self._publish(direction)
        return direction

    def run_forever(self, stop_event: threading.Event | None = None) -> None:
        """Probe periodically until ``stop_event`` is set."""
        while stop_event is None or not stop_event.is_set():
            self.probe_once()
            if stop_event is not None and stop_event.wait(self.interval_s):
                break
            if stop_event is None:
                self._sleeper(self.interval_s)

    @staticmethod
    def _load_gate() -> ModuleType:
        try:
            return importlib.import_module("backend.alembic_drift_gate")
        except ImportError:
            logger.exception("alembic_drift_probe.gate_import_failed")
            raise

    @staticmethod
    def _direction_from_payload(code: int, payload: dict[str, Any]) -> str:
        if code == 0:
            return "aligned"
        drift_direction = payload.get("drift_direction")
        if drift_direction == "image_ahead":
            return "forward"
        if drift_direction == "db_ahead":
            return "backward"
        return "backward"

    @staticmethod
    def _publish(active_direction: str) -> None:
        for direction in DIRECTIONS:
            metrics.alembic_drift.labels(direction=direction).set(
                1.0 if direction == active_direction and direction != "aligned" else 0.0
            )


__all__ = [
    "AlembicDriftProbe",
    "DEFAULT_INTERVAL_S",
    "DIRECTIONS",
]
