"""P10 #295 — Firebase Crashlytics adapter tests (respx-mocked)."""

from __future__ import annotations

import json
import re

import httpx
import pytest
import respx

from backend.mobile_observability import (
    HangEvent,
    InvalidMobileTokenError,
    MissingMobileScopeError,
    MobileCrash,
    MobileObservabilityError,
    MobilePayloadError,
    MobileRateLimitError,
    RenderMetric,
)
from backend.mobile_observability.firebase_crashlytics import (
    CRASHLYTICS_INGEST,
    FirebaseCrashlyticsAdapter,
    PERF_MONITORING_INGEST,
)


def _mk(**kw):
    kw.setdefault("api_key", "ya29.test-token-1234")
    kw.setdefault("project_id", "omni-app-prod")
    kw.setdefault("google_app_id_android", "1:111:android:abc")
    kw.setdefault("google_app_id_ios", "1:111:ios:def")
    kw.setdefault("environment", "prod")
    kw.setdefault("release", "1.42.0")
    return FirebaseCrashlyticsAdapter(**kw)


class TestSendCrash:

    @respx.mock
    async def test_posts_crash_to_crashlytics_ingest(self):
        route = respx.post(CRASHLYTICS_INGEST).mock(
            return_value=httpx.Response(200, json={"id": "abc"}),
        )
        crash = MobileCrash(
            message="java.lang.NullPointerException",
            platform="android",
            signal="java.lang.NullPointerException",
            stacktrace="at app.MainActivity.onCreate(MainActivity.kt:42)",
            app_version="1.42.0",
            os_version="14",
            device_model="Pixel 8",
            session_id="sess-1",
        )
        await _mk().send_crash(crash)
        assert route.called
        req = route.calls.last.request
        body = json.loads(req.content)
        assert body["google_app_id"] == "1:111:android:abc"
        assert body["report"]["exception"]["type"].startswith("java.lang")
        assert body["report"]["exception"]["reason"] == crash.message
        assert body["report"]["session"]["fatal"] is True
        assert body["release"] == "1.42.0"
        # Auth header carries the token.
        assert req.headers["Authorization"].startswith("Bearer ya29.")

    @respx.mock
    async def test_ios_crash_uses_ios_app_id(self):
        route = respx.post(CRASHLYTICS_INGEST).mock(
            return_value=httpx.Response(200, json={"id": "abc"}),
        )
        crash = MobileCrash(message="EXC_BAD_ACCESS", platform="ios",
                            signal="EXC_BAD_ACCESS")
        await _mk().send_crash(crash)
        body = json.loads(route.calls.last.request.content)
        assert body["google_app_id"] == "1:111:ios:def"

    async def test_missing_api_key_raises(self):
        with pytest.raises(MobileObservabilityError) as ei:
            adapter = FirebaseCrashlyticsAdapter(
                api_key=None,
                project_id="x",
                google_app_id_android="x",
            )
            await adapter.send_crash(MobileCrash(message="boom"))
        assert "api_key" in str(ei.value)


class TestSendHang:

    @respx.mock
    async def test_posts_anr_as_non_fatal_crash(self):
        route = respx.post(CRASHLYTICS_INGEST).mock(
            return_value=httpx.Response(200),
        )
        hang = HangEvent(duration_ms=6_000, platform="android", kind="anr")
        await _mk().send_hang(hang)
        body = json.loads(route.calls.last.request.content)
        assert body["report"]["exception"]["type"] == "ANR"
        assert "main thread blocked" in body["report"]["exception"]["reason"]
        assert body["report"]["severity"] == "warning"
        # ANR is non-fatal in Crashlytics' model.
        assert body["report"]["session"]["fatal"] is False

    @respx.mock
    async def test_posts_watchdog_as_fatal(self):
        route = respx.post(CRASHLYTICS_INGEST).mock(
            return_value=httpx.Response(200),
        )
        hang = HangEvent(duration_ms=0, platform="ios",
                         kind="watchdog_termination")
        await _mk().send_hang(hang)
        body = json.loads(route.calls.last.request.content)
        assert body["report"]["session"]["fatal"] is True
        assert body["report"]["severity"] == "critical"

    @respx.mock
    async def test_critical_hangs_ignore_sample_rate(self):
        route = respx.post(CRASHLYTICS_INGEST).mock(
            return_value=httpx.Response(200),
        )
        a = _mk(sample_rate=0.0)
        await a.send_hang(HangEvent(duration_ms=15_000, kind="anr"))
        assert route.called

    @respx.mock
    async def test_non_critical_hangs_sample(self):
        route = respx.post(CRASHLYTICS_INGEST)
        a = _mk(sample_rate=0.0)
        # warning-severity hang under sample 0 → no POST.
        await a.send_hang(HangEvent(duration_ms=6_000, kind="anr"))
        assert not route.called


class TestSendRender:

    @respx.mock
    async def test_posts_to_perf_monitoring_endpoint(self):
        url_re = re.compile(
            r"https://firebaseperformance\.googleapis\.com/.*omni-app-prod/events:batchCreate"
        )
        route = respx.post(url_re).mock(return_value=httpx.Response(200))
        m = RenderMetric(name="frame_draw", value=20, platform="android",
                         screen="/home", session_id="sess-1")
        await _mk().send_render(m)
        assert route.called
        body = json.loads(route.calls.last.request.content)
        assert body["appInstance"]["googleAppId"] == "1:111:android:abc"
        assert body["perfMetrics"][0]["screenTrace"]["attributes"]["metric_name"] == "frame_draw"

    @respx.mock
    async def test_zero_sample_rate_skips(self):
        route = respx.post(re.compile(r".*"))
        a = _mk(sample_rate=0.0)
        await a.send_render(RenderMetric(name="frame_draw", value=20))
        assert not route.called

    async def test_perf_endpoint_requires_project(self):
        a = FirebaseCrashlyticsAdapter(api_key="x")  # no project_id
        with pytest.raises(MobileObservabilityError):
            await a.send_render(RenderMetric(name="frame_draw", value=20))


class TestErrorMapping:

    @respx.mock
    async def test_401_maps_to_invalid_token(self):
        respx.post(CRASHLYTICS_INGEST).mock(
            return_value=httpx.Response(401, text="bad token"),
        )
        with pytest.raises(InvalidMobileTokenError):
            await _mk().send_crash(MobileCrash(message="boom"))

    @respx.mock
    async def test_403_maps_to_missing_scope(self):
        respx.post(CRASHLYTICS_INGEST).mock(
            return_value=httpx.Response(403, text="forbidden"),
        )
        with pytest.raises(MissingMobileScopeError):
            await _mk().send_crash(MobileCrash(message="boom"))

    @respx.mock
    async def test_400_maps_to_payload_error(self):
        respx.post(CRASHLYTICS_INGEST).mock(
            return_value=httpx.Response(400, text="bad payload"),
        )
        with pytest.raises(MobilePayloadError):
            await _mk().send_crash(MobileCrash(message="boom"))

    @respx.mock
    async def test_429_maps_to_rate_limit(self):
        respx.post(CRASHLYTICS_INGEST).mock(
            return_value=httpx.Response(
                429, headers={"Retry-After": "30"}, text="slow",
            ),
        )
        with pytest.raises(MobileRateLimitError) as ei:
            await _mk().send_crash(MobileCrash(message="boom"))
        assert ei.value.retry_after == 30


class TestNativeSnippets:

    def test_android_snippet_has_crashlytics_init(self):
        snippet = _mk().native_snippet("android")
        assert "FirebaseCrashlytics.getInstance()" in snippet
        assert "FirebasePerformance.getInstance()" in snippet
        assert "prod" in snippet  # environment
        assert "Application.onCreate()" in snippet

    def test_ios_snippet_has_firebaseapp_configure(self):
        snippet = _mk().native_snippet("ios")
        assert "FirebaseApp.configure()" in snippet
        assert "Crashlytics.crashlytics()" in snippet
        assert "Performance.sharedInstance()" in snippet

    def test_flutter_snippet_uses_recordError(self):
        snippet = _mk().native_snippet("flutter")
        assert "FirebaseCrashlytics.instance" in snippet
        assert "FlutterError.onError" in snippet
        assert "recordError" in snippet

    def test_react_native_snippet_uses_react_native_firebase(self):
        snippet = _mk().native_snippet("react-native")
        assert "@react-native-firebase/crashlytics" in snippet
        assert "@react-native-firebase/perf" in snippet

    def test_unknown_platform_rejected(self):
        with pytest.raises(ValueError):
            _mk().native_snippet("symbian")
