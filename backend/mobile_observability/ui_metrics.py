"""P10 #295 — Online UI metric aggregator (render time / frame drop).

Mobile counterpart of ``backend.observability.vitals``. Keeps a rolling
in-memory window of render samples bucketed by ``(metric, screen,
platform)`` and exposes P50 / P75 / P95 + slow-frame / frozen-frame
counts on demand.

Why bucket by platform?
    * Android Choreographer reports one sample per frame; iOS
      CADisplayLink reports one per refresh tick — collapsing the two
      hides which platform is jankier.
    * Flutter / RN apps report on top of the host platform; bucketing
      by ``react-native`` lets operators see the JS-thread overhead
      separately from the native render path.

Capacity bound
--------------
``max_samples_per_bucket`` (default 10 000) caps memory; once full,
older entries drop FIFO so the rolling window naturally shifts. Mobile
fleets emit higher event volumes than browser RUM (one sample per frame
× 60 FPS × N concurrent devices), so the default bucket size is the
same but the operator may want to reduce ``window_seconds`` from 600 to
something smaller.

Concurrency
-----------
A single ``threading.Lock`` guards the ``deque`` writes. Mirrors the
W10 vitals aggregator on purpose — operators reading either dashboard
should see the same lock semantics.
"""

from __future__ import annotations

import logging
import statistics
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Optional

from backend.mobile_observability.base import (
    GOOD_RENDER_MS,
    POOR_RENDER_MS,
    RenderMetric,
    classify_render,
)

logger = logging.getLogger(__name__)


# ── Snapshot data models ─────────────────────────────────────────


@dataclass
class RenderStats:
    """Stats for one render metric within one (platform, screen) bucket."""

    name: str
    platform: str
    screen: str
    count: int
    p50: float
    p75: float
    p95: float
    good_count: int
    needs_improvement_count: int
    poor_count: int
    good_threshold: float
    poor_threshold: float

    @property
    def good_ratio(self) -> float:
        if self.count == 0:
            return 0.0
        return self.good_count / self.count

    @property
    def poor_ratio(self) -> float:
        if self.count == 0:
            return 0.0
        return self.poor_count / self.count

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "platform": self.platform,
            "screen": self.screen,
            "count": self.count,
            "p50": round(self.p50, 3),
            "p75": round(self.p75, 3),
            "p95": round(self.p95, 3),
            "good_count": self.good_count,
            "needs_improvement_count": self.needs_improvement_count,
            "poor_count": self.poor_count,
            "good_threshold": self.good_threshold,
            "poor_threshold": self.poor_threshold,
            "good_ratio": round(self.good_ratio, 4),
            "poor_ratio": round(self.poor_ratio, 4),
        }


@dataclass
class RenderDashboardSnapshot:
    """A point-in-time view of every (metric, platform, screen) bucket."""

    generated_at: float
    window_seconds: int
    total_samples: int
    metrics: list[RenderStats] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "window_seconds": self.window_seconds,
            "total_samples": self.total_samples,
            "metrics": [m.to_dict() for m in self.metrics],
        }

    def metric(
        self,
        name: str,
        platform: str = "android",
        screen: str = "*",
    ) -> Optional[RenderStats]:
        nm = name.lower()
        plat = platform.lower()
        for m in self.metrics:
            if m.name == nm and m.platform == plat and m.screen == screen:
                return m
        return None


# ── Aggregator ───────────────────────────────────────────────────


class RenderMetricAggregator:
    """Rolling window of render samples bucketed by ``(metric, platform, screen)``.

    Samples older than ``window_seconds`` are pruned lazily on read —
    write-path is O(1).
    """

    def __init__(
        self,
        *,
        window_seconds: int = 600,
        max_samples_per_bucket: int = 10_000,
        clock=time.time,
    ) -> None:
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        if max_samples_per_bucket <= 0:
            raise ValueError("max_samples_per_bucket must be > 0")
        self.window_seconds = window_seconds
        self.max_samples_per_bucket = max_samples_per_bucket
        self._clock = clock
        # bucket key → deque[(timestamp, value, rating)]
        self._buckets: dict[
            tuple[str, str, str], deque[tuple[float, float, str]]
        ] = defaultdict(lambda: deque(maxlen=self.max_samples_per_bucket))
        self._lock = threading.Lock()
        self._total_seen = 0

    def record(self, metric: RenderMetric) -> None:
        """Add a sample. Unknown metric names are accepted but bucketed
        under their own row.
        """
        with self._lock:
            ts = metric.timestamp or self._clock()
            screen = _normalise_screen(metric.screen)
            plat = (metric.platform or "android").lower()
            key = (metric.name, plat, screen)
            rating = metric.rating or classify_render(metric.name, metric.value)
            self._buckets[key].append((ts, float(metric.value), rating))
            # Per-screen bucket also feeds a "*" rollup for the
            # platform-wide view.
            self._buckets[(metric.name, plat, "*")].append(
                (ts, float(metric.value), rating)
            )
            self._total_seen += 1

    def snapshot(
        self,
        *,
        platform: Optional[str] = None,
        screen: Optional[str] = None,
        metric: Optional[str] = None,
    ) -> RenderDashboardSnapshot:
        now = self._clock()
        cutoff = now - self.window_seconds
        with self._lock:
            self._prune_locked(cutoff)
            metrics: list[RenderStats] = []
            target_metric = (metric or "").lower() or None
            target_platform = (platform or "").lower() or None
            target_screen = _normalise_screen(screen) if screen else None
            for (name, plat, scr), samples in sorted(self._buckets.items()):
                if not samples:
                    continue
                if target_metric and name != target_metric:
                    continue
                if target_platform and plat != target_platform:
                    continue
                if target_screen is not None and scr != target_screen:
                    continue
                values = [v for _ts, v, _r in samples]
                ratings = [r for _ts, _v, r in samples]
                metrics.append(RenderStats(
                    name=name,
                    platform=plat,
                    screen=scr,
                    count=len(values),
                    p50=_pct(values, 50),
                    p75=_pct(values, 75),
                    p95=_pct(values, 95),
                    good_count=ratings.count("good"),
                    needs_improvement_count=ratings.count("needs-improvement"),
                    poor_count=ratings.count("poor"),
                    good_threshold=GOOD_RENDER_MS.get(name, 0.0),
                    poor_threshold=POOR_RENDER_MS.get(name, 0.0),
                ))
            return RenderDashboardSnapshot(
                generated_at=now,
                window_seconds=self.window_seconds,
                total_samples=self._total_seen,
                metrics=metrics,
            )

    def reset(self) -> None:
        """Drop every sample. Test helper / operator panic-button."""
        with self._lock:
            self._buckets.clear()
            self._total_seen = 0

    def _prune_locked(self, cutoff: float) -> None:
        empty_keys: list[tuple[str, str, str]] = []
        for key, dq in self._buckets.items():
            while dq and dq[0][0] < cutoff:
                dq.popleft()
            if not dq:
                empty_keys.append(key)
        for k in empty_keys:
            self._buckets.pop(k, None)


# ── Module-level singleton ───────────────────────────────────────

_default: Optional[RenderMetricAggregator] = None


def get_default_aggregator() -> RenderMetricAggregator:
    global _default
    if _default is None:
        _default = RenderMetricAggregator()
    return _default


def reset_default_aggregator() -> None:
    global _default
    _default = None


# ── Helpers ──────────────────────────────────────────────────────


def _normalise_screen(screen: str) -> str:
    """Canonicalise the screen key — drop query string + trailing slash.

    Mobile screens are usually route paths or view-controller names;
    different fragments / query strings should bucket the same.
    """
    if not screen or screen == "*":
        return screen or "/"
    p = screen.split("?", 1)[0]
    p = p.split("#", 1)[0]
    if len(p) > 1 and p.endswith("/"):
        p = p[:-1]
    return p


def _pct(values: list[float], pct: int) -> float:
    """Inclusive percentile — small-sample friendly (no numpy)."""
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    sorted_vals = sorted(values)
    if pct == 50:
        return float(statistics.median(sorted_vals))
    k = max(1, int(round((pct / 100.0) * len(sorted_vals))))
    return float(sorted_vals[k - 1])


__all__ = [
    "RenderDashboardSnapshot",
    "RenderMetricAggregator",
    "RenderStats",
    "get_default_aggregator",
    "reset_default_aggregator",
]
