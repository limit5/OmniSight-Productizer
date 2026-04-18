"""C7 — L4-CORE-07 HIL plugin API (#216).

Hardware-in-the-Loop (HIL) plugin protocol and base classes.

Every HIL plugin must implement three lifecycle methods:

* ``measure(**params)`` — acquire a measurement from hardware
* ``verify(measurement, criteria)`` — check measurement against pass/fail criteria
* ``teardown()`` — release hardware resources, reset state

Plugins are grouped into *families* (camera, audio, display, …).
Each family provides domain-specific measurement types and default criteria.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class PluginFamily(str, Enum):
    camera = "camera"
    audio = "audio"
    display = "display"


class MeasurementStatus(str, Enum):
    ok = "ok"
    fail = "fail"
    error = "error"
    skipped = "skipped"


@dataclass
class Measurement:
    plugin_name: str
    metric_name: str
    value: Any
    unit: str = ""
    timestamp: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class VerifyResult:
    passed: bool
    metric_name: str
    measured_value: Any
    criterion: str = ""
    message: str = ""


@dataclass
class PluginInfo:
    name: str
    family: PluginFamily
    version: str
    description: str = ""
    supported_metrics: list[str] = field(default_factory=list)


class HILPlugin(ABC):
    """Base class for all HIL plugins.

    Subclasses must implement ``measure``, ``verify``, and ``teardown``.
    They must also set ``plugin_info`` describing their capabilities.
    """

    plugin_info: PluginInfo

    @abstractmethod
    def measure(self, metric: str, **params: Any) -> Measurement:
        """Acquire a single measurement from hardware.

        Parameters
        ----------
        metric : str
            Which metric to measure (must be in ``plugin_info.supported_metrics``).
        **params
            Metric-specific parameters (e.g. exposure, gain, channel).

        Returns
        -------
        Measurement
            The acquired measurement with value and metadata.
        """

    @abstractmethod
    def verify(self, measurement: Measurement, criteria: dict[str, Any]) -> VerifyResult:
        """Verify a measurement against pass/fail criteria.

        Parameters
        ----------
        measurement : Measurement
            A previously acquired measurement.
        criteria : dict
            Key-value criteria (e.g. ``{"min": 0.8, "max": 1.2}``).

        Returns
        -------
        VerifyResult
            Whether the measurement passes the given criteria.
        """

    @abstractmethod
    def teardown(self) -> None:
        """Release hardware resources and reset plugin state.

        Called after a test run completes (pass or fail). Must be idempotent —
        calling teardown on an already-torn-down plugin must not raise.
        """

    def measure_and_verify(
        self, metric: str, criteria: dict[str, Any], **params: Any
    ) -> tuple[Measurement, VerifyResult]:
        """Convenience: measure then verify in one call."""
        m = self.measure(metric, **params)
        v = self.verify(m, criteria)
        return m, v

    def supports_metric(self, metric: str) -> bool:
        return metric in self.plugin_info.supported_metrics


@dataclass
class PluginRunSummary:
    plugin_name: str
    family: str
    measurements: list[Measurement] = field(default_factory=list)
    results: list[VerifyResult] = field(default_factory=list)
    status: MeasurementStatus = MeasurementStatus.ok
    error_message: str = ""
    duration_s: float = 0.0

    @property
    def all_passed(self) -> bool:
        return all(r.passed for r in self.results)

    @property
    def pass_count(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def fail_count(self) -> int:
        return sum(1 for r in self.results if not r.passed)


def run_plugin_lifecycle(
    plugin: HILPlugin,
    metrics_and_criteria: list[tuple[str, dict[str, Any], dict[str, Any]]],
) -> PluginRunSummary:
    """Execute a full HIL plugin lifecycle: measure → verify → teardown.

    Parameters
    ----------
    plugin : HILPlugin
        The plugin instance to run.
    metrics_and_criteria : list of (metric, params, criteria)
        Each tuple is (metric_name, measure_params, verify_criteria).

    Returns
    -------
    PluginRunSummary
        Aggregated results for all metrics.
    """
    summary = PluginRunSummary(
        plugin_name=plugin.plugin_info.name,
        family=plugin.plugin_info.family.value,
    )
    t0 = time.monotonic()
    try:
        for metric, params, criteria in metrics_and_criteria:
            m = plugin.measure(metric, **params)
            summary.measurements.append(m)
            v = plugin.verify(m, criteria)
            summary.results.append(v)
    except Exception as exc:
        summary.status = MeasurementStatus.error
        summary.error_message = str(exc)
    else:
        summary.status = (
            MeasurementStatus.ok if summary.all_passed else MeasurementStatus.fail
        )
    finally:
        try:
            plugin.teardown()
        except Exception as exc:
            if summary.status != MeasurementStatus.error:
                summary.status = MeasurementStatus.error
                summary.error_message = f"teardown failed: {exc}"
        summary.duration_s = time.monotonic() - t0
    return summary
