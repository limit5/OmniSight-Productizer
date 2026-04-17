"""P10 #295 — Sentry Mobile adapter tests (respx-mocked)."""

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
from backend.mobile_observability.sentry_mobile import SentryMobileAdapter


_TEST_DSN = "https://pubkey0123@o42.ingest.sentry.io/777"


def _mk(**kw):
    kw.setdefault("dsn", _TEST_DSN)
    kw.setdefault("environment", "prod")
    kw.setdefault("release", "1.42.0")
    return SentryMobileAdapter(**kw)


class TestDSNParsing:

    def test_parses_standard_dsn(self):
        a = _mk()
        public_key, ingest_base, project_id = a._dsn_parts()
        assert public_key == "pubkey0123"
        assert ingest_base == "https://o42.ingest.sentry.io"
        assert project_id == "777"

    def test_missing_dsn_raises_on_use(self):
        a = SentryMobileAdapter(dsn=None)
        with pytest.raises(MobileObservabilityError):
            a._dsn_parts()

    def test_dsn_without_public_key_raises(self):
        a = SentryMobileAdapter(dsn="https://o1.ingest.sentry.io/1")
        with pytest.raises(MobileObservabilityError):
            a._dsn_parts()


class TestSendCrash:

    @respx.mock
    async def test_posts_crash_envelope_with_platform_tag(self):
        route = respx.post(re.compile(
            r"https://o42\.ingest\.sentry\.io/api/777/envelope/"
        )).mock(return_value=httpx.Response(200, json={"id": "abc"}))
        crash = MobileCrash(
            message="java.lang.NPE",
            platform="android",
            signal="java.lang.NullPointerException",
            stacktrace="at MainActivity.onCreate(MainActivity.kt:42)",
            app_version="1.42.0",
            session_id="sess-1",
        )
        await _mk().send_crash(crash)
        assert route.called
        body_text = route.calls.last.request.content.decode("utf-8")
        lines = [l for l in body_text.split("\n") if l]
        item_header = json.loads(lines[1])
        body = json.loads(lines[2])
        assert item_header["type"] == "event"
        assert body["platform"] == "android"
        assert body["level"] == "fatal"
        assert body["fingerprint"] == [crash.fingerprint]
        assert body["exception"]["values"][0]["value"] == crash.message
        assert body["release"] == "1.42.0"
        assert body["tags"]["platform"] == "android"

    @respx.mock
    async def test_ios_crash_uses_cocoa_platform_tag(self):
        route = respx.post(re.compile(r".*/envelope/")).mock(
            return_value=httpx.Response(200),
        )
        await _mk().send_crash(MobileCrash(message="boom", platform="ios"))
        body = json.loads(
            route.calls.last.request.content.decode("utf-8").split("\n")[2]
        )
        assert body["platform"] == "cocoa"

    @respx.mock
    async def test_flutter_crash_uses_dart_platform_tag(self):
        route = respx.post(re.compile(r".*/envelope/")).mock(
            return_value=httpx.Response(200),
        )
        await _mk().send_crash(MobileCrash(message="boom", platform="flutter"))
        body = json.loads(
            route.calls.last.request.content.decode("utf-8").split("\n")[2]
        )
        assert body["platform"] == "dart"

    @respx.mock
    async def test_non_fatal_crash_emits_error_level(self):
        route = respx.post(re.compile(r".*/envelope/")).mock(
            return_value=httpx.Response(200),
        )
        await _mk().send_crash(MobileCrash(message="recoverable", fatal=False))
        body = json.loads(
            route.calls.last.request.content.decode("utf-8").split("\n")[2]
        )
        assert body["level"] == "error"


class TestSendHang:

    @respx.mock
    async def test_anr_sends_event_with_hang_kind_tag(self):
        route = respx.post(re.compile(r".*/envelope/")).mock(
            return_value=httpx.Response(200),
        )
        await _mk().send_hang(HangEvent(duration_ms=6_000, kind="anr"))
        body = json.loads(
            route.calls.last.request.content.decode("utf-8").split("\n")[2]
        )
        assert body["tags"]["hang_kind"] == "anr"
        assert body["tags"]["hang_severity"] == "warning"
        assert body["level"] == "warning"

    @respx.mock
    async def test_watchdog_emits_fatal_level(self):
        route = respx.post(re.compile(r".*/envelope/")).mock(
            return_value=httpx.Response(200),
        )
        await _mk().send_hang(HangEvent(
            duration_ms=0, kind="watchdog_termination", platform="ios",
        ))
        body = json.loads(
            route.calls.last.request.content.decode("utf-8").split("\n")[2]
        )
        assert body["level"] == "fatal"
        assert body["tags"]["hang_severity"] == "critical"

    @respx.mock
    async def test_critical_hang_ignores_sample_rate(self):
        route = respx.post(re.compile(r".*/envelope/")).mock(
            return_value=httpx.Response(200),
        )
        a = _mk(sample_rate=0.0)
        await a.send_hang(HangEvent(duration_ms=15_000, kind="anr"))
        assert route.called

    @respx.mock
    async def test_warning_hang_respects_sample_rate(self):
        route = respx.post(re.compile(r".*/envelope/"))
        a = _mk(sample_rate=0.0)
        await a.send_hang(HangEvent(duration_ms=6_000, kind="anr"))
        assert not route.called


class TestSendRender:

    @respx.mock
    async def test_render_posts_transaction_with_measurement(self):
        route = respx.post(re.compile(r".*/envelope/")).mock(
            return_value=httpx.Response(200),
        )
        m = RenderMetric(name="frame_draw", value=20, platform="android",
                         screen="/feed", session_id="sess-1")
        await _mk().send_render(m)
        body_text = route.calls.last.request.content.decode("utf-8")
        lines = [l for l in body_text.split("\n") if l]
        header = json.loads(lines[1])
        body = json.loads(lines[2])
        assert header["type"] == "transaction"
        assert body["type"] == "transaction"
        assert body["measurements"]["frame_draw"]["value"] == 20
        assert body["measurements"]["frame_draw"]["unit"] == "millisecond"
        assert body["transaction"] == "/feed"
        assert body["tags"]["metric.name"] == "frame_draw"

    @respx.mock
    async def test_zero_sample_rate_skips(self):
        route = respx.post(re.compile(r".*/envelope/"))
        a = _mk(sample_rate=0.0)
        await a.send_render(RenderMetric(name="frame_draw", value=20))
        assert not route.called


class TestErrorMapping:

    @respx.mock
    async def test_401_maps_to_invalid_token(self):
        respx.post(re.compile(r".*/envelope/")).mock(
            return_value=httpx.Response(401, text="bad key"),
        )
        with pytest.raises(InvalidMobileTokenError):
            await _mk().send_crash(MobileCrash(message="boom"))

    @respx.mock
    async def test_429_maps_to_rate_limit(self):
        respx.post(re.compile(r".*/envelope/")).mock(
            return_value=httpx.Response(
                429, headers={"Retry-After": "73"}, text="slow",
            ),
        )
        with pytest.raises(MobileRateLimitError) as ei:
            await _mk().send_crash(MobileCrash(message="boom"))
        assert ei.value.retry_after == 73

    @respx.mock
    async def test_400_maps_to_payload_error(self):
        respx.post(re.compile(r".*/envelope/")).mock(
            return_value=httpx.Response(400, text="bad envelope"),
        )
        with pytest.raises(MobilePayloadError):
            await _mk().send_crash(MobileCrash(message="boom"))

    @respx.mock
    async def test_403_maps_to_missing_scope(self):
        respx.post(re.compile(r".*/envelope/")).mock(
            return_value=httpx.Response(403, text="forbidden"),
        )
        with pytest.raises(MissingMobileScopeError):
            await _mk().send_crash(MobileCrash(message="boom"))


class TestNativeSnippets:

    def test_android_snippet_includes_dsn_and_anr_config(self):
        snippet = _mk().native_snippet("android")
        assert _TEST_DSN in snippet
        assert "io.sentry.android.core.SentryAndroid" in snippet
        assert "isAnrEnabled" in snippet
        assert "isEnableFramesTracking" in snippet
        assert "anrTimeoutIntervalMillis" in snippet

    def test_ios_snippet_enables_watchdog_tracking(self):
        snippet = _mk().native_snippet("ios")
        assert _TEST_DSN in snippet
        assert "SentrySDK.start" in snippet
        assert "enableWatchdogTerminationTracking" in snippet
        assert "enableAppHangTracking" in snippet

    def test_flutter_snippet_wraps_runApp(self):
        snippet = _mk().native_snippet("flutter")
        assert "SentryFlutter.init" in snippet
        assert _TEST_DSN in snippet

    def test_react_native_snippet_uses_sentry_react_native(self):
        snippet = _mk().native_snippet("react-native")
        assert "@sentry/react-native" in snippet
        assert _TEST_DSN in snippet

    def test_unknown_platform_rejected(self):
        with pytest.raises(ValueError):
            _mk().native_snippet("symbian")
