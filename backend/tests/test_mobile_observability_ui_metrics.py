"""P10 #295 — Render-metric aggregator tests."""

from __future__ import annotations

import pytest

from backend.mobile_observability import (
    GOOD_RENDER_MS,
    POOR_RENDER_MS,
    RenderDashboardSnapshot,
    RenderMetric,
    RenderMetricAggregator,
    RenderStats,
    get_default_aggregator,
    reset_default_aggregator,
)


class _Clock:
    """Deterministic monotonic clock for tests."""

    def __init__(self, start: float = 1_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def agg():
    return RenderMetricAggregator(window_seconds=60)


@pytest.fixture
def clock():
    return _Clock()


@pytest.fixture
def agg_with_clock(clock):
    return RenderMetricAggregator(window_seconds=60, clock=clock)


class TestConstruction:

    def test_invalid_window_seconds_rejected(self):
        with pytest.raises(ValueError):
            RenderMetricAggregator(window_seconds=0)
        with pytest.raises(ValueError):
            RenderMetricAggregator(window_seconds=-1)

    def test_invalid_max_samples_rejected(self):
        with pytest.raises(ValueError):
            RenderMetricAggregator(max_samples_per_bucket=0)


class TestRecordAndSnapshot:

    def test_single_sample_appears_in_snapshot(self, agg):
        agg.record(RenderMetric(name="frame_draw", value=20, screen="/home"))
        snap = agg.snapshot()
        # One per (metric, platform, screen) bucket + the platform-rollup "*".
        assert snap.total_samples == 1
        assert any(m.screen == "/home" for m in snap.metrics)
        assert any(m.screen == "*" for m in snap.metrics)

    def test_per_screen_and_rollup_counts(self, agg):
        for v in [10, 20, 30]:
            agg.record(RenderMetric(name="frame_draw", value=v, screen="/home"))
        for v in [40, 50]:
            agg.record(RenderMetric(name="frame_draw", value=v, screen="/feed"))
        snap = agg.snapshot()
        home = snap.metric("frame_draw", platform="android", screen="/home")
        feed = snap.metric("frame_draw", platform="android", screen="/feed")
        rollup = snap.metric("frame_draw", platform="android", screen="*")
        assert home.count == 3
        assert feed.count == 2
        assert rollup.count == 5

    def test_p50_and_p95(self, agg):
        for v in range(1, 101):  # 1..100
            agg.record(RenderMetric(name="frame_draw", value=v, screen="/x"))
        snap = agg.snapshot()
        bucket = snap.metric("frame_draw", platform="android", screen="/x")
        # statistics.median(1..100) = (50+51)/2 = 50.5
        assert bucket.p50 == 50.5
        # Nearest-rank P95 of 100 sorted values picks index 95.
        assert bucket.p95 == 95
        assert bucket.count == 100

    def test_rating_counts_align(self, agg):
        # Mix good + poor + needs-improvement.
        agg.record(RenderMetric(name="frame_draw", value=10))   # good
        agg.record(RenderMetric(name="frame_draw", value=20))   # needs
        agg.record(RenderMetric(name="frame_draw", value=50))   # poor
        snap = agg.snapshot(metric="frame_draw")
        bucket = snap.metric("frame_draw", platform="android", screen="*")
        assert bucket.good_count == 1
        assert bucket.needs_improvement_count == 1
        assert bucket.poor_count == 1
        assert bucket.good_threshold == GOOD_RENDER_MS["frame_draw"]
        assert bucket.poor_threshold == POOR_RENDER_MS["frame_draw"]


class TestPruning:

    def test_old_samples_drop_after_window(self, agg_with_clock, clock):
        agg_with_clock.record(RenderMetric(
            name="frame_draw", value=20, timestamp=clock.now,
        ))
        clock.advance(120)  # > 60 s window
        snap = agg_with_clock.snapshot()
        assert snap.metrics == []  # everything pruned

    def test_partial_pruning(self, agg_with_clock, clock):
        agg_with_clock.record(RenderMetric(
            name="frame_draw", value=20, timestamp=clock.now,
        ))
        clock.advance(30)
        agg_with_clock.record(RenderMetric(
            name="frame_draw", value=40, timestamp=clock.now,
        ))
        clock.advance(40)  # first sample now > 60 s old
        snap = agg_with_clock.snapshot()
        bucket = snap.metric("frame_draw", platform="android", screen="*")
        assert bucket is not None
        assert bucket.count == 1
        assert bucket.p50 == 40


class TestFiltering:

    def test_metric_filter(self, agg):
        agg.record(RenderMetric(name="frame_draw", value=20))
        agg.record(RenderMetric(name="hang", value=500))
        snap = agg.snapshot(metric="hang")
        names = {m.name for m in snap.metrics}
        assert names == {"hang"}

    def test_platform_filter(self, agg):
        agg.record(RenderMetric(name="frame_draw", value=20, platform="android"))
        agg.record(RenderMetric(name="frame_draw", value=20, platform="ios"))
        snap = agg.snapshot(platform="ios")
        platforms = {m.platform for m in snap.metrics}
        assert platforms == {"ios"}

    def test_screen_filter(self, agg):
        agg.record(RenderMetric(name="frame_draw", value=20, screen="/home"))
        agg.record(RenderMetric(name="frame_draw", value=20, screen="/feed"))
        snap = agg.snapshot(screen="/home")
        screens = {m.screen for m in snap.metrics}
        assert screens == {"/home"}


class TestScreenNormalisation:

    def test_query_string_stripped(self, agg):
        agg.record(RenderMetric(name="frame_draw", value=20, screen="/blog?p=1"))
        agg.record(RenderMetric(name="frame_draw", value=30, screen="/blog?p=2"))
        snap = agg.snapshot()
        bucket = snap.metric("frame_draw", platform="android", screen="/blog")
        assert bucket is not None and bucket.count == 2

    def test_trailing_slash_stripped(self, agg):
        agg.record(RenderMetric(name="frame_draw", value=20, screen="/blog/"))
        agg.record(RenderMetric(name="frame_draw", value=30, screen="/blog"))
        snap = agg.snapshot()
        bucket = snap.metric("frame_draw", platform="android", screen="/blog")
        assert bucket is not None and bucket.count == 2

    def test_root_preserved(self, agg):
        agg.record(RenderMetric(name="frame_draw", value=20, screen="/"))
        snap = agg.snapshot()
        bucket = snap.metric("frame_draw", platform="android", screen="/")
        assert bucket is not None


class TestStatsModel:

    def test_good_ratio_zero_when_empty(self):
        s = RenderStats(name="x", platform="android", screen="/",
                        count=0, p50=0, p75=0, p95=0,
                        good_count=0, needs_improvement_count=0, poor_count=0,
                        good_threshold=0, poor_threshold=0)
        assert s.good_ratio == 0.0
        assert s.poor_ratio == 0.0

    def test_to_dict_serialisable(self):
        s = RenderStats(name="frame_draw", platform="android", screen="/",
                        count=10, p50=15, p75=20, p95=30,
                        good_count=5, needs_improvement_count=3, poor_count=2,
                        good_threshold=16, poor_threshold=33)
        d = s.to_dict()
        assert d["good_ratio"] == 0.5
        assert d["poor_ratio"] == 0.2
        assert d["p50"] == 15.0


class TestReset:

    def test_reset_clears_all_buckets(self, agg):
        agg.record(RenderMetric(name="frame_draw", value=20))
        agg.reset()
        snap = agg.snapshot()
        assert snap.metrics == []
        assert snap.total_samples == 0


class TestDefaultAggregator:

    def setup_method(self):
        reset_default_aggregator()

    def teardown_method(self):
        reset_default_aggregator()

    def test_lazy_instantiation(self):
        a = get_default_aggregator()
        b = get_default_aggregator()
        assert a is b

    def test_reset_drops_singleton(self):
        a = get_default_aggregator()
        reset_default_aggregator()
        b = get_default_aggregator()
        assert a is not b
