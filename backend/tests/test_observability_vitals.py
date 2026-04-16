"""W10 #284 — Core Web Vitals aggregator tests."""

from __future__ import annotations

import threading
import time

import pytest

from backend.observability import (
    CoreWebVitalsAggregator,
    DashboardSnapshot,
    MetricStats,
    WebVital,
    get_default_aggregator,
    reset_default_aggregator,
)
from backend.observability.vitals import _normalise_page, _pct


class TestNormalisePage:

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("/", "/"),
            ("/blog", "/blog"),
            ("/blog/", "/blog"),
            ("/blog?page=2", "/blog"),
            ("/blog#anchor", "/blog"),
            ("/blog/?utm=xyz", "/blog"),
            ("*", "*"),
            ("", "/"),
        ],
    )
    def test_canonicalises(self, raw, expected):
        assert _normalise_page(raw) == expected


class TestPercentile:

    def test_empty_returns_zero(self):
        assert _pct([], 50) == 0.0

    def test_single_returns_self(self):
        assert _pct([42], 95) == 42.0

    def test_p50_is_median(self):
        assert _pct([1, 2, 3, 4, 5], 50) == 3.0

    def test_p95_uses_nearest_rank(self):
        # 100 values 1..100 → P95 → index = ceil(0.95 * 100) = 95 → value 95
        vals = list(range(1, 101))
        assert _pct(vals, 95) == 95.0

    def test_p75(self):
        vals = list(range(1, 101))
        assert _pct(vals, 75) == 75.0


class TestRecordAndSnapshot:

    def setup_method(self):
        self.agg = CoreWebVitalsAggregator(window_seconds=600)

    def test_records_a_sample_per_page_and_rollup(self):
        self.agg.record(WebVital(name="LCP", value=2200, page="/"))
        snap = self.agg.snapshot()
        # One per-page bucket + one site-wide rollup.
        assert len(snap.metrics) == 2
        pages = {m.page for m in snap.metrics}
        assert pages == {"/", "*"}
        assert all(m.name == "LCP" for m in snap.metrics)
        assert all(m.count == 1 for m in snap.metrics)

    def test_classification_counts_break_down_correctly(self):
        # 3 good, 2 needs-improvement, 1 poor for LCP at "/".
        for v in (1500, 2000, 2500, 3000, 3800, 5000):
            self.agg.record(WebVital(name="LCP", value=v, page="/"))
        snap = self.agg.snapshot(page="/")
        assert len(snap.metrics) == 1
        m = snap.metrics[0]
        assert m.count == 6
        assert m.good_count == 3
        assert m.needs_improvement_count == 2
        assert m.poor_count == 1
        assert m.good_ratio == pytest.approx(3 / 6, abs=0.001)
        assert m.poor_ratio == pytest.approx(1 / 6, abs=0.001)

    def test_p75_p95_computed(self):
        for v in range(1, 101):
            self.agg.record(WebVital(name="INP", value=float(v), page="/x"))
        snap = self.agg.snapshot(page="/x", metric="INP")
        assert len(snap.metrics) == 1
        m = snap.metrics[0]
        assert m.count == 100
        assert m.p50 == 50.5  # statistics.median
        assert m.p75 == 75.0
        assert m.p95 == 95.0

    def test_snapshot_filters_by_metric(self):
        self.agg.record(WebVital(name="LCP", value=2200, page="/"))
        self.agg.record(WebVital(name="INP", value=180, page="/"))
        snap = self.agg.snapshot(metric="LCP")
        assert all(m.name == "LCP" for m in snap.metrics)

    def test_snapshot_filters_by_page(self):
        self.agg.record(WebVital(name="LCP", value=2200, page="/blog"))
        self.agg.record(WebVital(name="LCP", value=1800, page="/about"))
        snap = self.agg.snapshot(page="/blog")
        assert all(m.page == "/blog" for m in snap.metrics)
        assert all(m.count == 1 for m in snap.metrics)

    def test_dashboard_snapshot_to_dict_round_trip(self):
        self.agg.record(WebVital(name="LCP", value=2200, page="/"))
        d = self.agg.snapshot().to_dict()
        assert "generated_at" in d
        assert "metrics" in d
        assert isinstance(d["metrics"], list)
        # Per-metric to_dict has expected keys.
        m = d["metrics"][0]
        for k in ("name", "page", "count", "p50", "p75", "p95",
                  "good_count", "needs_improvement_count", "poor_count",
                  "good_threshold", "poor_threshold",
                  "good_ratio", "poor_ratio"):
            assert k in m


class TestRollingWindow:

    def test_old_samples_pruned_outside_window(self):
        # Inject monotonic clock so we can fast-forward.
        clock = [1000.0]
        agg = CoreWebVitalsAggregator(window_seconds=10, clock=lambda: clock[0])

        agg.record(WebVital(name="LCP", value=2200, page="/", timestamp=clock[0]))
        # advance 15s — past window
        clock[0] += 15
        snap = agg.snapshot()
        assert snap.metrics == []
        assert snap.total_samples == 1  # cumulative counter, not pruned

    def test_recent_samples_kept(self):
        clock = [1000.0]
        agg = CoreWebVitalsAggregator(window_seconds=60, clock=lambda: clock[0])
        agg.record(WebVital(name="LCP", value=2200, page="/", timestamp=clock[0]))
        clock[0] += 30
        agg.record(WebVital(name="LCP", value=2400, page="/", timestamp=clock[0]))
        clock[0] += 20  # 50s elapsed since first; both still in window
        snap = agg.snapshot(page="/")
        assert len(snap.metrics) == 1
        assert snap.metrics[0].count == 2

    def test_older_partial_pruning(self):
        clock = [1000.0]
        agg = CoreWebVitalsAggregator(window_seconds=10, clock=lambda: clock[0])
        agg.record(WebVital(name="LCP", value=2000, page="/", timestamp=clock[0]))
        clock[0] += 5
        agg.record(WebVital(name="LCP", value=2200, page="/", timestamp=clock[0]))
        clock[0] += 8  # first ts 1000, now=1013, cutoff=1003 → drop first
        snap = agg.snapshot(page="/")
        m = snap.metrics[0]
        assert m.count == 1
        assert m.p50 == 2200


class TestCapacityCap:

    def test_max_samples_per_bucket_caps_memory(self):
        agg = CoreWebVitalsAggregator(
            window_seconds=600, max_samples_per_bucket=10,
        )
        for v in range(20):
            agg.record(WebVital(name="LCP", value=float(v), page="/"))
        snap = agg.snapshot(page="/")
        # Only the most recent 10 survive.
        assert snap.metrics[0].count == 10

    def test_zero_max_samples_rejected(self):
        with pytest.raises(ValueError):
            CoreWebVitalsAggregator(max_samples_per_bucket=0)

    def test_zero_window_rejected(self):
        with pytest.raises(ValueError):
            CoreWebVitalsAggregator(window_seconds=0)


class TestReset:

    def test_reset_wipes_state(self):
        agg = CoreWebVitalsAggregator()
        for _ in range(5):
            agg.record(WebVital(name="LCP", value=2000, page="/"))
        agg.reset()
        snap = agg.snapshot()
        assert snap.metrics == []
        assert snap.total_samples == 0


class TestDefaultSingleton:

    def setup_method(self):
        reset_default_aggregator()

    def teardown_method(self):
        reset_default_aggregator()

    def test_singleton_returns_same_instance(self):
        a = get_default_aggregator()
        b = get_default_aggregator()
        assert a is b

    def test_reset_creates_new_instance(self):
        a = get_default_aggregator()
        reset_default_aggregator()
        b = get_default_aggregator()
        assert a is not b


class TestThreadSafety:

    def test_concurrent_record_doesnt_lose_samples(self):
        agg = CoreWebVitalsAggregator(window_seconds=600)
        N = 200

        def worker():
            for v in range(N):
                agg.record(WebVital(name="LCP", value=float(v), page="/"))

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        snap = agg.snapshot(page="/")
        assert snap.metrics[0].count == 4 * N
