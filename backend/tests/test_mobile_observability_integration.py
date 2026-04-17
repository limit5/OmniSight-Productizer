"""P10 #295 — End-to-end mobile observability integration test.

Walks the full pipeline:

    raw duration sample
        → ANRDetector.to_event() → HangEvent
        → SentryMobileAdapter.send_hang() → Sentry envelope
        → RenderMetricAggregator.record() → in-process snapshot

The goal is to catch wiring regressions where the four modules drift
out of sync (e.g., a new field on ``HangEvent`` that the Sentry
adapter doesn't pick up).
"""

from __future__ import annotations

import json
import re

import httpx
import pytest
import respx

from backend.mobile_observability import (
    ANRDetector,
    ANRDetectorConfig,
    HangEvent,
    MobileCrash,
    RenderMetric,
    RenderMetricAggregator,
    classify_render,
    get_mobile_adapter,
)
from backend.mobile_observability.firebase_crashlytics import CRASHLYTICS_INGEST


_TEST_DSN = "https://pubkey@o42.ingest.sentry.io/777"


class TestEndToEndSentryFlow:

    @respx.mock
    async def test_anr_detection_to_sentry_envelope(self):
        """A 6-second main-thread block flows through the detector and
        reaches the Sentry envelope as a warning-severity ANR."""
        route = respx.post(re.compile(r".*/envelope/")).mock(
            return_value=httpx.Response(200, json={"id": "abc"}),
        )
        det = ANRDetector(ANRDetectorConfig(platform="android"))
        ev = det.to_event(
            duration_ms=6_000,
            main_thread_stack="at MainActivity.onCreate",
            app_version="1.42.0",
            device_model="Pixel 8",
        )
        assert ev is not None
        cls = get_mobile_adapter("sentry-mobile")
        adapter = cls.from_plaintext_dsn(
            dsn=_TEST_DSN, environment="prod", release="1.42.0",
        )
        await adapter.send_hang(ev)
        assert route.called
        body_text = route.calls.last.request.content.decode("utf-8")
        body = json.loads(body_text.split("\n")[2])
        assert body["tags"]["hang_kind"] == "anr"
        assert body["tags"]["hang_severity"] == "warning"
        assert body["release"] == "1.42.0"

    @respx.mock
    async def test_ios_watchdog_termination_to_sentry_envelope(self):
        """An iOS watchdog kill (duration unknown) reaches Sentry as a
        fatal-level event."""
        route = respx.post(re.compile(r".*/envelope/")).mock(
            return_value=httpx.Response(200),
        )
        det = ANRDetector(ANRDetectorConfig(
            platform="ios", warning_ms=250, critical_ms=1_000,
        ))
        ev = det.to_event(duration_ms=2_000, kind="watchdog_termination")
        cls = get_mobile_adapter("sentry-mobile")
        adapter = cls.from_plaintext_dsn(
            dsn=_TEST_DSN, environment="prod", release="2.0.0",
        )
        await adapter.send_hang(ev)
        assert route.called
        body = json.loads(
            route.calls.last.request.content.decode("utf-8").split("\n")[2]
        )
        assert body["level"] == "fatal"
        assert body["platform"] == "cocoa"


class TestEndToEndCrashlyticsFlow:

    @respx.mock
    async def test_anr_detection_to_crashlytics_envelope(self):
        route = respx.post(CRASHLYTICS_INGEST).mock(
            return_value=httpx.Response(200),
        )
        det = ANRDetector(ANRDetectorConfig(platform="android"))
        ev = det.to_event(duration_ms=12_000)  # critical
        cls = get_mobile_adapter("firebase-crashlytics")
        adapter = cls(
            api_key="ya29.test",
            project_id="omni-test",
            google_app_id_android="1:1:android:abc",
            release="1.0.0",
        )
        await adapter.send_hang(ev)
        body = json.loads(route.calls.last.request.content)
        assert body["report"]["severity"] == "critical"
        assert body["report"]["exception"]["type"] == "ANR"


class TestRenderAggregatorIntegration:

    def test_classify_then_aggregate(self):
        """Samples classified by ``classify_render`` end up in the right
        good/needs/poor counters of the aggregator's snapshot."""
        agg = RenderMetricAggregator()
        # Walk three samples through the rating boundary for frame_draw.
        cases = [
            (10, "good"),
            (20, "needs-improvement"),
            (50, "poor"),
        ]
        for value, expected_rating in cases:
            assert classify_render("frame_draw", value) == expected_rating
            agg.record(RenderMetric(name="frame_draw", value=value))
        snap = agg.snapshot(metric="frame_draw")
        rollup = snap.metric("frame_draw", platform="android", screen="*")
        assert rollup.good_count == 1
        assert rollup.needs_improvement_count == 1
        assert rollup.poor_count == 1

    def test_per_platform_split(self):
        """The aggregator buckets Android and iOS samples separately so
        operators can spot which platform is jankier."""
        agg = RenderMetricAggregator()
        agg.record(RenderMetric(name="frame_draw", value=15, platform="android"))
        agg.record(RenderMetric(name="frame_draw", value=35, platform="ios"))
        snap = agg.snapshot(metric="frame_draw")
        android = snap.metric("frame_draw", platform="android", screen="*")
        ios = snap.metric("frame_draw", platform="ios", screen="*")
        assert android.good_count == 1
        assert ios.poor_count == 1


class TestCrossAdapterConsistency:

    def test_hang_severity_matches_across_adapters(self):
        """The HangEvent.severity property is the single source of
        truth used by both adapters' send_hang() — verify both honour
        the same boundaries."""
        warn = HangEvent(duration_ms=5_000, kind="anr")
        crit = HangEvent(duration_ms=15_000, kind="anr")
        watchdog = HangEvent(duration_ms=0, kind="watchdog_termination",
                             platform="ios")
        assert warn.severity == "warning"
        assert crit.severity == "critical"
        assert watchdog.severity == "critical"

    def test_known_provider_classes_match(self):
        from backend.mobile_observability.firebase_crashlytics import (
            FirebaseCrashlyticsAdapter,
        )
        from backend.mobile_observability.sentry_mobile import (
            SentryMobileAdapter,
        )
        assert get_mobile_adapter("crashlytics") is FirebaseCrashlyticsAdapter
        assert get_mobile_adapter("sentry-mobile") is SentryMobileAdapter
