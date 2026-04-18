"""W10 #284 — Core Web Vitals in-process aggregator.

Operators want a "live" dashboard of how the production site is
performing right now without paying Sentry / Datadog ingest dollars to
look at Q-of-Q trends. This module keeps the last N vital samples in
memory and computes per-metric P50 / P75 / P95 + good / poor counts on
demand.

Why in-memory?
  * Web Vitals are high-frequency low-value events — persisting every
    one to Postgres swamps the audit log and serves nothing operators
    need at 5-second granularity.
  * The vendor adapter (Sentry / Datadog) is the long-term store.
  * The dashboard is a cockpit, not a system of record.

Capacity bound
--------------
``max_samples`` (default 10 000 per metric+page bucket) caps memory
without throwing samples away invisibly — once full, older entries
drop with FIFO so the rolling window naturally shifts.

Concurrency
-----------
A single ``threading.Lock`` guards the ``deque`` writes. The aggregator
expects to be called from FastAPI request handlers (async + threadpool
mix) — a lock is cheaper than ``asyncio.Lock`` and the critical section
is microseconds.
"""

from __future__ import annotations

import logging
import statistics
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Optional

from backend.observability.base import (
    GOOD_THRESHOLDS,
    POOR_THRESHOLDS,
    WebVital,
    classify_vital,
)

logger = logging.getLogger(__name__)


# ── Snapshot data models ─────────────────────────────────────────


@dataclass
class MetricStats:
    """Stats for one CWV metric within one page bucket."""

    name: str
    page: str
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
            "page": self.page,
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
class DashboardSnapshot:
    """A point-in-time view of every metric × page bucket."""

    generated_at: float
    window_seconds: int
    total_samples: int
    metrics: list[MetricStats] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "window_seconds": self.window_seconds,
            "total_samples": self.total_samples,
            "metrics": [m.to_dict() for m in self.metrics],
        }

    def metric(self, name: str, page: str = "*") -> Optional[MetricStats]:
        """Convenience getter — returns the bucket matching name+page."""
        nm = name.upper()
        for m in self.metrics:
            if m.name == nm and m.page == page:
                return m
        return None


# ── Aggregator ───────────────────────────────────────────────────


class CoreWebVitalsAggregator:
    """Rolling window of vital samples bucketed by ``(metric, page)``.

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
        self._buckets: dict[tuple[str, str], deque[tuple[float, float, str]]] = (
            defaultdict(lambda: deque(maxlen=self.max_samples_per_bucket))
        )
        self._lock = threading.Lock()
        self._total_seen = 0

    def record(self, vital: WebVital) -> None:
        """Add a sample. Unknown metric names are accepted but bucketed
        separately — they show up in the snapshot under their own row.
        """
        with self._lock:
            ts = vital.timestamp or self._clock()
            page = _normalise_page(vital.page)
            key = (vital.name, page)
            self._buckets[key].append((ts, float(vital.value), vital.rating or
                                       classify_vital(vital.name, vital.value)))
            # Per-page bucket also feeds a "*" rollup for the dashboard's
            # site-wide view.
            self._buckets[(vital.name, "*")].append(
                (ts, float(vital.value), vital.rating or
                 classify_vital(vital.name, vital.value))
            )
            self._total_seen += 1

    def snapshot(
        self,
        *,
        page: Optional[str] = None,
        metric: Optional[str] = None,
    ) -> DashboardSnapshot:
        """Compute stats for every ``(metric, page)`` bucket.

        ``page=None`` (default) returns every page including the ``*``
        rollup. ``page='/'`` returns only the home-page bucket. ``metric``
        filters to one CWV name (LCP / INP / CLS / TTFB / FCP).
        """
        now = self._clock()
        cutoff = now - self.window_seconds
        with self._lock:
            self._prune_locked(cutoff)
            metrics: list[MetricStats] = []
            target_metric = (metric or "").upper() or None
            for (name, p), samples in sorted(self._buckets.items()):
                if not samples:
                    continue
                if target_metric and name != target_metric:
                    continue
                if page is not None and p != _normalise_page(page):
                    continue
                values = [v for _ts, v, _r in samples]
                ratings = [r for _ts, _v, r in samples]
                metrics.append(MetricStats(
                    name=name,
                    page=p,
                    count=len(values),
                    p50=_pct(values, 50),
                    p75=_pct(values, 75),
                    p95=_pct(values, 95),
                    good_count=ratings.count("good"),
                    needs_improvement_count=ratings.count("needs-improvement"),
                    poor_count=ratings.count("poor"),
                    good_threshold=GOOD_THRESHOLDS.get(name, 0.0),
                    poor_threshold=POOR_THRESHOLDS.get(name, 0.0),
                ))
            return DashboardSnapshot(
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
        """Drop samples older than ``cutoff``. Caller must hold the lock."""
        empty_keys: list[tuple[str, str]] = []
        for key, dq in self._buckets.items():
            while dq and dq[0][0] < cutoff:
                dq.popleft()
            if not dq:
                empty_keys.append(key)
        for k in empty_keys:
            self._buckets.pop(k, None)


# ── Module-level singleton ───────────────────────────────────────
#
# A single aggregator process-wide is the right default — every
# request handler dumps into the same in-memory store; the dashboard
# router reads it back. Operators wanting per-tenant aggregation can
# instantiate a second one and pass it explicitly.

_default: Optional[CoreWebVitalsAggregator] = None


def get_default_aggregator() -> CoreWebVitalsAggregator:
    """Lazy-init module-level aggregator with default settings."""
    global _default
    if _default is None:
        _default = CoreWebVitalsAggregator()
    return _default


def reset_default_aggregator() -> None:
    """Test helper — wipes the module-level singleton state."""
    global _default
    _default = None


# ── Helpers ──────────────────────────────────────────────────────


def _normalise_page(page: str) -> str:
    """Canonicalise the page key — drop query string + trailing slash.

    Two queries against ``/blog?page=2`` and ``/blog?page=3`` should
    bucket the same; ``/blog/`` and ``/blog`` should bucket the same
    (except the root path stays as ``/``).
    """
    if not page or page == "*":
        return page or "/"
    p = page.split("?", 1)[0]
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
    # Nearest-rank method (Wikipedia: Percentile § Nearest-rank method)
    k = max(1, int(round((pct / 100.0) * len(sorted_vals))))
    return float(sorted_vals[k - 1])


__all__ = [
    "CoreWebVitalsAggregator",
    "DashboardSnapshot",
    "MetricStats",
    "get_default_aggregator",
    "reset_default_aggregator",
]
