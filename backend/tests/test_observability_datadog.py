"""W10 #284 — Datadog RUM adapter tests (respx-mocked)."""

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
    RUMRateLimitError,
    WebVital,
)
from backend.observability.datadog import DatadogRUMAdapter


_DSN = "ddrum_pub_clienttoken_ABCD"
_APP_ID = "app-uuid-1"


def _mk(**kw):
    kw.setdefault("dsn", _DSN)
    kw.setdefault("application_id", _APP_ID)
    kw.setdefault("environment", "prod")
    kw.setdefault("release", "1.42.0")
    kw.setdefault("service", "omnisight-test")
    return DatadogRUMAdapter(**kw)


class TestConfigure:

    def test_requires_application_id(self):
        with pytest.raises(ValueError):
            DatadogRUMAdapter(dsn=_DSN, application_id="", environment="prod")

    def test_default_site_constructs_us1_intake(self):
        a = _mk()
        assert a._intake_url() == "https://browser-intake-datadoghq.com/api/v2/rum"

    def test_eu_site_constructs_eu_intake(self):
        a = _mk(site="datadoghq.eu")
        assert a._intake_url() == "https://browser-intake-datadoghq.eu/api/v2/rum"

    def test_intake_base_override(self):
        a = _mk(intake_base="https://onprem-dd.example.com")
        assert a._intake_url() == "https://onprem-dd.example.com/api/v2/rum"

    def test_missing_dsn_intake_params_raises(self):
        a = DatadogRUMAdapter(dsn=None, application_id=_APP_ID,
                              environment="prod")
        with pytest.raises(RUMError):
            a._intake_params()


class TestSendVital:

    @respx.mock
    async def test_posts_vital_event(self):
        route = respx.post(re.compile(
            r"https://browser-intake-datadoghq\.com/api/v2/rum.*"
        )).mock(return_value=httpx.Response(202, json={}))
        await _mk().send_vital(WebVital(name="LCP", value=2200, page="/blog"))
        assert route.called
        req = route.calls.last.request
        # Auth via dd-api-key query param.
        assert f"dd-api-key={_DSN}" in str(req.url)
        body_text = req.content.decode("utf-8")
        events = [json.loads(l) for l in body_text.split("\n") if l]
        assert len(events) == 1
        ev = events[0]
        assert ev["type"] == "view"
        assert ev["application"]["id"] == _APP_ID
        assert ev["env"] == "prod"
        assert ev["version"] == "1.42.0"
        assert ev["view"]["url_path"] == "/blog"
        assert ev["view"]["lcp"] == 2200
        assert ev["view"]["lcp_rating"] == "good"

    @respx.mock
    async def test_zero_sample_rate_skips(self):
        route = respx.post(re.compile(r".*"))
        a = _mk(sample_rate=0.0)
        await a.send_vital(WebVital(name="LCP", value=2200))
        assert not route.called

    @respx.mock
    async def test_401_maps_to_invalid_token(self):
        respx.post(re.compile(r".*")).mock(
            return_value=httpx.Response(401, json={"errors": ["bad token"]}),
        )
        with pytest.raises(InvalidRUMTokenError):
            await _mk().send_vital(WebVital(name="LCP", value=2200))

    @respx.mock
    async def test_403_maps_to_missing_scope(self):
        respx.post(re.compile(r".*")).mock(
            return_value=httpx.Response(403, json={"errors": ["forbidden"]}),
        )
        with pytest.raises(MissingRUMScopeError):
            await _mk().send_vital(WebVital(name="LCP", value=2200))

    @respx.mock
    async def test_429_maps_to_rate_limit(self):
        respx.post(re.compile(r".*")).mock(
            return_value=httpx.Response(
                429, headers={"Retry-After": "13"},
                json={"errors": ["slow"]},
            ),
        )
        with pytest.raises(RUMRateLimitError) as ei:
            await _mk().send_vital(WebVital(name="LCP", value=2200))
        assert ei.value.retry_after == 13


class TestSendError:

    @respx.mock
    async def test_posts_error_event(self):
        route = respx.post(re.compile(r".*")).mock(
            return_value=httpx.Response(202, json={}),
        )
        ev = ErrorEvent(message="TypeError: x", page="/x", level="error",
                        release="1.42.0", environment="prod",
                        stack="at app.js:1:2")
        await _mk().send_error(ev)
        body = route.calls.last.request.content.decode("utf-8")
        out = json.loads([l for l in body.split("\n") if l][0])
        assert out["type"] == "error"
        assert out["error"]["message"] == "TypeError: x"
        assert out["error"]["fingerprint"] == ev.fingerprint
        assert out["view"]["url_path"] == "/x"
        # Event-level environment wins over adapter default.
        assert out["env"] == "prod"

    @respx.mock
    async def test_errors_ignore_sample_rate(self):
        route = respx.post(re.compile(r".*")).mock(
            return_value=httpx.Response(202, json={}),
        )
        a = _mk(sample_rate=0.0)
        await a.send_error(ErrorEvent(message="boom"))
        assert route.called


class TestBrowserSnippet:

    def test_includes_client_token_by_default(self):
        snippet = _mk().browser_snippet()
        assert _DSN in snippet
        assert _APP_ID in snippet
        assert "@datadog/browser-rum" in snippet
        assert "web-vitals" in snippet
        assert "onLCP" in snippet
        assert "onINP" in snippet
        assert "onCLS" in snippet
        assert "/api/v1/rum/vitals" in snippet

    def test_omit_dsn_uses_env_var(self):
        snippet = _mk().browser_snippet(include_dsn=False)
        assert _DSN not in snippet
        assert "process.env.DD_CLIENT_TOKEN" in snippet

    def test_sample_rate_baked_in(self):
        snippet = _mk(sample_rate=0.25).browser_snippet()
        # 0.25 → "25" in sessionSampleRate (Datadog uses 0–100).
        assert "sessionSampleRate: 25" in snippet

    def test_site_baked_in(self):
        snippet = _mk(site="datadoghq.eu").browser_snippet()
        assert '"datadoghq.eu"' in snippet
