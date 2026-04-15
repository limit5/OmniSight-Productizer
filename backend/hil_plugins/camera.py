"""C7 — Camera family HIL plugin (#216).

Measures focus sharpness, white balance accuracy, and stream latency
for camera-class products (UVC, IP camera, dashcam, etc.).

In production these delegate to real hardware (USB capture + analysis);
the base implementation provides the protocol skeleton that concrete
adapters override.
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

CAMERA_METRICS = ["focus_sharpness", "white_balance", "stream_latency"]


class CameraHILPlugin(HILPlugin):
    """HIL plugin for camera devices.

    Supported metrics
    -----------------
    * **focus_sharpness** — Laplacian variance of captured frame (higher = sharper).
      Params: ``resolution`` (str, e.g. "1920x1080"), ``roi`` (tuple, optional).
    * **white_balance** — Delta-E from reference white under D65 illuminant.
      Params: ``illuminant`` (str, default "D65").
    * **stream_latency** — End-to-end capture-to-display latency in milliseconds.
      Params: ``codec`` (str, e.g. "h264"), ``frames`` (int, sample count).
    """

    def __init__(self, device_id: str = "default") -> None:
        self.device_id = device_id
        self._initialized = True
        self.plugin_info = PluginInfo(
            name="camera",
            family=PluginFamily.camera,
            version="1.0.0",
            description="Camera HIL — focus, white balance, stream latency",
            supported_metrics=list(CAMERA_METRICS),
        )

    def measure(self, metric: str, **params: Any) -> Measurement:
        if metric not in CAMERA_METRICS:
            raise ValueError(
                f"unsupported metric {metric!r}, must be one of {CAMERA_METRICS}"
            )
        self._check_initialized()

        if metric == "focus_sharpness":
            return self._measure_focus(**params)
        elif metric == "white_balance":
            return self._measure_wb(**params)
        else:
            return self._measure_stream_latency(**params)

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

    def _measure_focus(self, resolution: str = "1920x1080", **_kw: Any) -> Measurement:
        return Measurement(
            plugin_name=self.plugin_info.name,
            metric_name="focus_sharpness",
            value=0.0,
            unit="laplacian_variance",
            metadata={"resolution": resolution, "device_id": self.device_id},
        )

    def _measure_wb(self, illuminant: str = "D65", **_kw: Any) -> Measurement:
        return Measurement(
            plugin_name=self.plugin_info.name,
            metric_name="white_balance",
            value=0.0,
            unit="delta_e",
            metadata={"illuminant": illuminant, "device_id": self.device_id},
        )

    def _measure_stream_latency(
        self, codec: str = "h264", frames: int = 30, **_kw: Any
    ) -> Measurement:
        return Measurement(
            plugin_name=self.plugin_info.name,
            metric_name="stream_latency",
            value=0.0,
            unit="ms",
            metadata={
                "codec": codec,
                "frames": frames,
                "device_id": self.device_id,
            },
        )
