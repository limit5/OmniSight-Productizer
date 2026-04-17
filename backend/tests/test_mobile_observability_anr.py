"""P10 #295 — ANR detector / iOS watchdog classifier tests."""

from __future__ import annotations

import pytest

from backend.mobile_observability import (
    ANRDetector,
    ANRDetectorConfig,
    DEFAULT_ANR_CRITICAL_MS,
    DEFAULT_ANR_WARNING_MS,
    HangEvent,
    android_anr_snippet,
    ios_watchdog_snippet,
)


class TestConfig:

    def test_defaults_match_published_thresholds(self):
        c = ANRDetectorConfig()
        assert c.platform == "android"
        assert c.warning_ms == DEFAULT_ANR_WARNING_MS == 5_000.0
        assert c.critical_ms == DEFAULT_ANR_CRITICAL_MS == 10_000.0

    def test_unknown_platform_rejected(self):
        with pytest.raises(ValueError):
            ANRDetectorConfig(platform="symbian")

    def test_warning_above_critical_rejected(self):
        with pytest.raises(ValueError):
            ANRDetectorConfig(warning_ms=10_000, critical_ms=1_000)

    def test_negative_threshold_rejected(self):
        with pytest.raises(ValueError):
            ANRDetectorConfig(warning_ms=-1)

    def test_ios_platform_accepted(self):
        c = ANRDetectorConfig(platform="ios", warning_ms=250, critical_ms=1_000)
        assert c.platform == "ios"


class TestClassify:

    @pytest.fixture
    def det(self):
        return ANRDetector(ANRDetectorConfig(
            platform="android",
            warning_ms=5_000,
            critical_ms=10_000,
        ))

    @pytest.mark.parametrize(
        "duration,verdict",
        [
            (-1, "ignored"),
            (0, "info"),
            (4_999, "info"),
            (5_000, "warning"),
            (9_999, "warning"),
            (10_000, "critical"),
            (50_000, "critical"),
        ],
    )
    def test_thresholds(self, det, duration, verdict):
        assert det.classify(duration_ms=duration) == verdict

    def test_background_anr_ignored(self, det):
        # Per Google's Play Vitals guidance, background ANR is suppressed.
        assert det.classify(duration_ms=20_000, in_foreground=False) == "ignored"


class TestToEvent:

    def test_warning_event_emitted(self):
        det = ANRDetector(ANRDetectorConfig(platform="android"))
        ev = det.to_event(duration_ms=6_000)
        assert ev is not None
        assert ev.severity == "warning"
        assert ev.kind == "anr"
        assert ev.platform == "android"

    def test_below_threshold_returns_none(self):
        det = ANRDetector(ANRDetectorConfig(platform="android"))
        assert det.to_event(duration_ms=100) is None

    def test_background_returns_none(self):
        det = ANRDetector(ANRDetectorConfig(platform="android"))
        assert det.to_event(duration_ms=20_000, in_foreground=False) is None

    def test_ios_default_kind_is_watchdog(self):
        det = ANRDetector(ANRDetectorConfig(
            platform="ios", warning_ms=250, critical_ms=1_000,
        ))
        ev = det.to_event(duration_ms=2_000)
        assert ev.kind == "watchdog_termination"
        assert ev.platform == "ios"
        # Watchdog severity is always critical regardless of duration.
        assert ev.severity == "critical"

    def test_explicit_kind_overrides_default(self):
        det = ANRDetector(ANRDetectorConfig(platform="android"))
        ev = det.to_event(duration_ms=6_000, kind="watchdog_termination")
        assert ev.kind == "watchdog_termination"

    def test_metadata_propagates_to_event(self):
        det = ANRDetector(ANRDetectorConfig(platform="android"))
        ev = det.to_event(
            duration_ms=6_000,
            main_thread_stack="at MainActivity.onCreate",
            app_version="1.42.0",
            os_version="14",
            device_model="Pixel 8",
            session_id="sess-1",
        )
        assert ev.app_version == "1.42.0"
        assert ev.os_version == "14"
        assert ev.device_model == "Pixel 8"
        assert ev.session_id == "sess-1"
        assert ev.main_thread_stack.startswith("at MainActivity")


class TestSnippets:

    def test_android_snippet_uses_threshold(self):
        snippet = android_anr_snippet(threshold_ms=3_000)
        assert "ANRWatchDog(3000)" in snippet
        assert "setReportMainThreadOnly" in snippet
        assert "Application.onCreate" in snippet

    def test_android_snippet_default_threshold(self):
        snippet = android_anr_snippet()
        assert "ANRWatchDog(5000)" in snippet

    def test_ios_snippet_subscribes_to_metrickit(self):
        snippet = ios_watchdog_snippet()
        assert "MetricKit" in snippet
        assert "MXMetricManager.shared.add(self)" in snippet
        assert "MXMetricManagerSubscriber" in snippet
        assert "didReceive" in snippet
        assert "MXDiagnosticPayload" in snippet
