"""W10 #284 — Sentry RUM adapter tests (respx-mocked)."""

from __future__ import annotations

import json
import re

import httpx
import pytest
import respx

from backend.observability import (
    ErrorEvent,
    InvalidRUMTokenError,
    MissingRUMScopeError,
    RUMError,
    RUMPayloadError,
    RUMRateLimitError,
    WebVital,
)
from backend.observability.sentry import SentryRUMAdapter


_TEST_DSN = "https://pubkey0123@o42.ingest.sentry.io/777"


def _mk(**kw):
    kw.setdefault("dsn", _TEST_DSN)
    kw.setdefault("environment", "prod")
    kw.setdefault("release", "1.42.0")
    return SentryRUMAdapter(**kw)


class TestDSNParsing:

    def test_parses_standard_dsn(self):
        a = _mk()
        public_key, ingest_base, project_id = a._dsn_parts()
        assert public_key == "pubkey0123"
        assert ingest_base == "https://o42.ingest.sentry.io"
        assert project_id == "777"

    def test_missing_dsn_raises_on_use(self):
        a = SentryRUMAdapter(dsn=None)
        with pytest.raises(RUMError):
            a._dsn_parts()

    def test_dsn_without_public_key_raises(self):
        a = SentryRUMAdapter(dsn="https://o1.ingest.sentry.io/1")
        with pytest.raises(RUMError):
            a._dsn_parts()

    def test_dsn_without_project_raises(self):
        a = SentryRUMAdapter(dsn="https://k@o1.ingest.sentry.io/")
        with pytest.raises(RUMError):
            a._dsn_parts()

    def test_invalid_scheme_raises(self):
        a = SentryRUMAdapter(dsn="ftp://k@o1.ingest.sentry.io/1")
        with pytest.raises(RUMError):
            a._dsn_parts()

    def test_ingest_base_override(self):
        a = SentryRUMAdapter(dsn=_TEST_DSN,
                             ingest_base="https://onprem-sentry.example.com")
        _, ingest_base, _ = a._dsn_parts()
        assert ingest_base == "https://onprem-sentry.example.com"


class TestSendVital:

    @respx.mock
    async def test_posts_envelope_with_measurement(self):
        route = respx.post(re.compile(
            r"https://o42\.ingest\.sentry\.io/api/777/envelope/"
        )).mock(return_value=httpx.Response(200, json={"id": "abc"}))
        a = _mk()
        await a.send_vital(WebVital(name="LCP", value=2200, page="/blog"))
        assert route.called
        req = route.calls.last.request
        # Query params include sentry_key.
        assert "sentry_key=pubkey0123" in str(req.url)
        # Body is NDJSON envelope.
        body_text = req.content.decode("utf-8")
        lines = [l for l in body_text.split("\n") if l]
        # Envelope header + item header + item body = 3 lines.
        assert len(lines) == 3
        env_header = json.loads(lines[0])
        assert "event_id" in env_header and "sent_at" in env_header
        item_header = json.loads(lines[1])
        assert item_header["type"] == "transaction"
        item_body = json.loads(lines[2])
        assert item_body["type"] == "transaction"
        assert item_body["measurements"]["lcp"]["value"] == 2200
        assert item_body["measurements"]["lcp"]["unit"] == "millisecond"
        assert item_body["transaction"] == "/blog"
        assert item_body["release"] == "1.42.0"
        assert item_body["environment"] == "prod"
        assert item_body["tags"]["vital.name"] == "LCP"

    @respx.mock
    async def test_cls_uses_unitless(self):
        route = respx.post(re.compile(r".*/envelope/")).mock(
            return_value=httpx.Response(200, json={"id": "abc"}),
        )
        await _mk().send_vital(WebVital(name="CLS", value=0.05, page="/"))
        body = route.calls.last.request.content.decode("utf-8")
        body_obj = json.loads(body.split("\n")[2])
        assert body_obj["measurements"]["cls"]["unit"] == "none"

    @respx.mock
    async def test_zero_sample_rate_skips(self):
        route = respx.post(re.compile(r".*/envelope/"))
        a = _mk(sample_rate=0.0)
        await a.send_vital(WebVital(name="LCP", value=2200))
        assert not route.called

    @respx.mock
    async def test_401_maps_to_invalid_token(self):
        respx.post(re.compile(r".*/envelope/")).mock(
            return_value=httpx.Response(401, text="bad key"),
        )
        with pytest.raises(InvalidRUMTokenError):
            await _mk().send_vital(WebVital(name="LCP", value=2200))

    @respx.mock
    async def test_403_maps_to_missing_scope(self):
        respx.post(re.compile(r".*/envelope/")).mock(
            return_value=httpx.Response(403, text="forbidden"),
        )
        with pytest.raises(MissingRUMScopeError):
            await _mk().send_vital(WebVital(name="LCP", value=2200))

    @respx.mock
    async def test_400_maps_to_payload_error(self):
        respx.post(re.compile(r".*/envelope/")).mock(
            return_value=httpx.Response(400, text="bad envelope"),
        )
        with pytest.raises(RUMPayloadError):
            await _mk().send_vital(WebVital(name="LCP", value=2200))

    @respx.mock
    async def test_429_maps_to_rate_limit(self):
        respx.post(re.compile(r".*/envelope/")).mock(
            return_value=httpx.Response(
                429, headers={"Retry-After": "73"}, text="slow down",
            ),
        )
        with pytest.raises(RUMRateLimitError) as ei:
            await _mk().send_vital(WebVital(name="LCP", value=2200))
        assert ei.value.retry_after == 73


class TestSendError:

    @respx.mock
    async def test_posts_event_envelope_with_fingerprint(self):
        route = respx.post(re.compile(r".*/envelope/")).mock(
            return_value=httpx.Response(200, json={"id": "abc"}),
        )
        ev = ErrorEvent(message="TypeError: x is undefined",
                        page="/blog", level="error",
                        release="1.42.0",
                        stack="at app.js:1:2\nat react-dom.js:5:6")
        await _mk().send_error(ev)
        assert route.called
        req = route.calls.last.request
        body_text = req.content.decode("utf-8")
        lines = [l for l in body_text.split("\n") if l]
        item_header = json.loads(lines[1])
        body = json.loads(lines[2])
        assert item_header["type"] == "event"
        assert body["level"] == "error"
        assert body["fingerprint"] == [ev.fingerprint]
        assert body["exception"]["values"][0]["value"] == ev.message
        assert body["release"] == "1.42.0"
        assert body["transaction"] == "/blog"
        # Stack frames present.
        frames = body["exception"]["values"][0]["stacktrace"]["frames"]
        assert any("app.js" in f["filename"] for f in frames)

    @respx.mock
    async def test_errors_ignore_sample_rate(self):
        route = respx.post(re.compile(r".*/envelope/")).mock(
            return_value=httpx.Response(200, json={"id": "abc"}),
        )
        # Sample 0 — but errors must STILL fire.
        a = _mk(sample_rate=0.0)
        await a.send_error(ErrorEvent(message="boom"))
        assert route.called

    @respx.mock
    async def test_default_message_when_no_fingerprint_falls_back_to_message(self):
        route = respx.post(re.compile(r".*/envelope/")).mock(
            return_value=httpx.Response(200, json={"id": "abc"}),
        )
        ev = ErrorEvent(message="boom", fingerprint="custom-fp")
        await _mk().send_error(ev)
        body = json.loads(route.calls.last.request.content.decode("utf-8").split("\n")[2])
        assert body["fingerprint"] == ["custom-fp"]


class TestBrowserSnippet:

    def test_includes_dsn_by_default(self):
        snippet = _mk().browser_snippet()
        assert _TEST_DSN in snippet
        assert "@sentry/browser" in snippet
        assert "web-vitals" in snippet
        assert "onLCP" in snippet
        assert "onINP" in snippet
        assert "onCLS" in snippet
        assert "onTTFB" in snippet
        assert "onFCP" in snippet
        # Beacon endpoint matches our router path.
        assert "/api/v1/rum/vitals" in snippet

    def test_omit_dsn_uses_env_var(self):
        snippet = _mk().browser_snippet(include_dsn=False)
        assert _TEST_DSN not in snippet
        assert "process.env.SENTRY_DSN" in snippet

    def test_release_baked_in(self):
        snippet = _mk(release="2.5.0").browser_snippet()
        assert "2.5.0" in snippet

    def test_sample_rate_in_snippet(self):
        snippet = _mk().browser_snippet()
        # tracesSampleRate is a Sentry-specific knob.
        assert "tracesSampleRate" in snippet
