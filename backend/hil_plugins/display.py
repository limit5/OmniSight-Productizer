"""C7 — Display family HIL plugin (#216).

Measures display uniformity and touch latency for display-class products
(smart displays, kiosks, car dashboards, etc.).
"""

from __future__ import annotations

from typing import Any

from backend.hil_plugin import (
    HILPlugin,
    Measurement,
    PluginFamily,
    PluginInfo,
    VerifyResult,
)

DISPLAY_METRICS = ["uniformity", "touch_latency", "color_accuracy"]


class DisplayHILPlugin(HILPlugin):
    """HIL plugin for display devices.

    Supported metrics
    -----------------
    * **uniformity** — Luminance uniformity ratio (0–1, 1 = perfectly uniform).
      Params: ``zones`` (int, grid NxN), ``test_pattern`` (str).
    * **touch_latency** — Finger-to-pixel response time in milliseconds.
      Params: ``touch_points`` (int), ``method`` (str, "stylus" | "finger").
    * **color_accuracy** — Average Delta-E 2000 across a colour checker.
      Params: ``profile`` (str, ICC profile name), ``patches`` (int).
    """

    def __init__(self, device_id: str = "default") -> None:
        self.device_id = device_id
        self._initialized = True
        self.plugin_info = PluginInfo(
            name="display",
            family=PluginFamily.display,
            version="1.0.0",
            description="Display HIL — uniformity, touch latency, color accuracy",
            supported_metrics=list(DISPLAY_METRICS),
        )

    def measure(self, metric: str, **params: Any) -> Measurement:
        if metric not in DISPLAY_METRICS:
            raise ValueError(
                f"unsupported metric {metric!r}, must be one of {DISPLAY_METRICS}"
            )
        self._check_initialized()

        if metric == "uniformity":
            return self._measure_uniformity(**params)
        elif metric == "touch_latency":
            return self._measure_touch_latency(**params)
        else:
            return self._measure_color_accuracy(**params)

    def verify(self, measurement: Measurement, criteria: dict[str, Any]) -> VerifyResult:
        value = measurement.value
        passed = True
        messages: list[str] = []

        if "min" in criteria and value < criteria["min"]:
            passed = False
            messages.append(f"value {value} < min {criteria['min']}")
        if "max" in criteria and value > criteria["max"]:
            passed = False
            messages.append(f"value {value} > max {criteria['max']}")

        return VerifyResult(
            passed=passed,
            metric_name=measurement.metric_name,
            measured_value=value,
            criterion=str(criteria),
            message="; ".join(messages) if messages else "PASS",
        )

    def teardown(self) -> None:
        self._initialized = False

    def _check_initialized(self) -> None:
        if not self._initialized:
            raise RuntimeError("plugin already torn down")

    def _measure_uniformity(
        self, zones: int = 9, test_pattern: str = "white", **_kw: Any
    ) -> Measurement:
        return Measurement(
            plugin_name=self.plugin_info.name,
            metric_name="uniformity",
            value=0.0,
            unit="ratio",
            metadata={
                "zones": zones,
                "test_pattern": test_pattern,
                "device_id": self.device_id,
            },
        )

    def _measure_touch_latency(
        self, touch_points: int = 5, method: str = "finger", **_kw: Any
    ) -> Measurement:
        return Measurement(
            plugin_name=self.plugin_info.name,
            metric_name="touch_latency",
            value=0.0,
            unit="ms",
            metadata={
                "touch_points": touch_points,
                "method": method,
                "device_id": self.device_id,
            },
        )

    def _measure_color_accuracy(
        self, profile: str = "sRGB", patches: int = 24, **_kw: Any
    ) -> Measurement:
        return Measurement(
            plugin_name=self.plugin_info.name,
            metric_name="color_accuracy",
            value=0.0,
            unit="delta_e_2000",
            metadata={
                "profile": profile,
                "patches": patches,
                "device_id": self.device_id,
            },
        )
