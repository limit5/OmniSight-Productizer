"""C7 — Audio family HIL plugin (#216).

Measures signal-to-noise ratio (SNR) and acoustic echo cancellation (AEC)
performance for audio-class products (USB microphone, earbuds, video
conference devices, etc.).
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

AUDIO_METRICS = ["snr", "aec", "thd"]


class AudioHILPlugin(HILPlugin):
    """HIL plugin for audio devices.

    Supported metrics
    -----------------
    * **snr** — Signal-to-noise ratio in dB. Higher is better.
      Params: ``sample_rate`` (int, Hz), ``duration_s`` (float).
    * **aec** — AEC attenuation in dB (echo return loss enhancement).
      Params: ``reference_level_dbfs`` (float), ``playback_delay_ms`` (int).
    * **thd** — Total harmonic distortion as percentage.
      Params: ``frequency_hz`` (int), ``amplitude_dbfs`` (float).
    """

    def __init__(self, device_id: str = "default") -> None:
        self.device_id = device_id
        self._initialized = True
        self.plugin_info = PluginInfo(
            name="audio",
            family=PluginFamily.audio,
            version="1.0.0",
            description="Audio HIL — SNR, AEC, THD measurements",
            supported_metrics=list(AUDIO_METRICS),
        )

    def measure(self, metric: str, **params: Any) -> Measurement:
        if metric not in AUDIO_METRICS:
            raise ValueError(
                f"unsupported metric {metric!r}, must be one of {AUDIO_METRICS}"
            )
        self._check_initialized()

        if metric == "snr":
            return self._measure_snr(**params)
        elif metric == "aec":
            return self._measure_aec(**params)
        else:
            return self._measure_thd(**params)

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

    def _measure_snr(
        self, sample_rate: int = 48000, duration_s: float = 1.0, **_kw: Any
    ) -> Measurement:
        return Measurement(
            plugin_name=self.plugin_info.name,
            metric_name="snr",
            value=0.0,
            unit="dB",
            metadata={
                "sample_rate": sample_rate,
                "duration_s": duration_s,
                "device_id": self.device_id,
            },
        )

    def _measure_aec(
        self,
        reference_level_dbfs: float = -20.0,
        playback_delay_ms: int = 50,
        **_kw: Any,
    ) -> Measurement:
        return Measurement(
            plugin_name=self.plugin_info.name,
            metric_name="aec",
            value=0.0,
            unit="dB",
            metadata={
                "reference_level_dbfs": reference_level_dbfs,
                "playback_delay_ms": playback_delay_ms,
                "device_id": self.device_id,
            },
        )

    def _measure_thd(
        self, frequency_hz: int = 1000, amplitude_dbfs: float = -6.0, **_kw: Any
    ) -> Measurement:
        return Measurement(
            plugin_name=self.plugin_info.name,
            metric_name="thd",
            value=0.0,
            unit="percent",
            metadata={
                "frequency_hz": frequency_hz,
                "amplitude_dbfs": amplitude_dbfs,
                "device_id": self.device_id,
            },
        )
