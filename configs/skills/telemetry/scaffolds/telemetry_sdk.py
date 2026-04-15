"""OmniSight Telemetry SDK — Python client stub.

C17 L4-CORE-17 Telemetry backend.
"""

from __future__ import annotations

import gzip
import json
import logging
import signal
import threading
import time
import traceback
from collections import deque
from typing import Any, Optional

logger = logging.getLogger(__name__)


class TelemetryClient:
    def __init__(
        self,
        endpoint_url: str,
        device_id: str,
        profile: str = "default",
        opt_in: bool = False,
        batch_size: int = 50,
        flush_interval: float = 60.0,
        max_queue_size: int = 5000,
        offline_queue_enabled: bool = True,
        compression: str = "gzip",
    ):
        self.endpoint_url = endpoint_url
        self.device_id = device_id
        self.profile = profile
        self.opt_in = opt_in
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self.max_queue_size = max_queue_size
        self.offline_queue_enabled = offline_queue_enabled
        self.compression = compression

        self._queue: deque[dict[str, Any]] = deque(maxlen=max_queue_size)
        self._lock = threading.Lock()
        self._connected = True
        self._running = True

        self._flush_thread = threading.Thread(
            target=self._flush_loop, daemon=True
        )
        self._flush_thread.start()

        self._install_crash_handlers()

    def _install_crash_handlers(self) -> None:
        original_hook = getattr(signal, "getsignal", lambda _: None)
        try:
            signal.signal(signal.SIGSEGV, self._crash_handler)
            signal.signal(signal.SIGABRT, self._crash_handler)
        except (OSError, ValueError):
            pass

    def _crash_handler(self, sig: int, frame: Any) -> None:
        tb = traceback.format_stack(frame) if frame else []
        self.send_crash(
            signal_name=signal.Signals(sig).name,
            stack_trace="".join(tb),
        )
        self.flush()

    def send_crash(
        self,
        signal_name: str,
        stack_trace: str,
        extra: Optional[dict[str, Any]] = None,
    ) -> bool:
        event = {
            "event_type": "crash_dump",
            "device_id": self.device_id,
            "timestamp": time.time(),
            "crash_signal": signal_name,
            "stack_trace": stack_trace,
        }
        if extra:
            event.update(extra)
        return self._enqueue(event)

    def send_usage(
        self,
        event_name: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> bool:
        event = {
            "event_type": "usage_event",
            "device_id": self.device_id,
            "timestamp": time.time(),
            "event_name": event_name,
        }
        if metadata:
            event["metadata"] = metadata
        return self._enqueue(event)

    def send_metric(
        self,
        metric_name: str,
        value: float,
        unit: str = "",
        tags: Optional[dict[str, str]] = None,
    ) -> bool:
        event = {
            "event_type": "perf_metric",
            "device_id": self.device_id,
            "timestamp": time.time(),
            "metric_name": metric_name,
            "metric_value": value,
        }
        if unit:
            event["metric_unit"] = unit
        if tags:
            event["metric_tags"] = tags
        return self._enqueue(event)

    def _enqueue(self, event: dict[str, Any]) -> bool:
        if not self.opt_in:
            return False
        with self._lock:
            if len(self._queue) >= self.max_queue_size:
                return False
            self._queue.append(event)
        return True

    def flush(self) -> int:
        with self._lock:
            events = list(self._queue)
            self._queue.clear()

        if not events:
            return 0

        for i in range(0, len(events), self.batch_size):
            batch = events[i : i + self.batch_size]
            self._send_batch(batch)

        return len(events)

    def _send_batch(self, batch: list[dict[str, Any]]) -> bool:
        if not self._connected and not self.offline_queue_enabled:
            return False

        payload = json.dumps({
            "device_id": self.device_id,
            "events": batch,
            "opt_in": self.opt_in,
        }).encode()

        if self.compression == "gzip":
            payload = gzip.compress(payload)

        # Stub: POST payload to self.endpoint_url
        logger.debug("Would POST %d events (%d bytes) to %s",
                      len(batch), len(payload), self.endpoint_url)
        return True

    def _flush_loop(self) -> None:
        while self._running:
            time.sleep(self.flush_interval)
            try:
                self.flush()
            except Exception:
                logger.exception("Flush failed")

    def set_connected(self, connected: bool) -> None:
        was_disconnected = not self._connected
        self._connected = connected
        if connected and was_disconnected and self.offline_queue_enabled:
            self.flush()

    def shutdown(self) -> None:
        self._running = False
        self.flush()

    @property
    def queue_size(self) -> int:
        return len(self._queue)
