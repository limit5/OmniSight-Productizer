"""W9 #283 — Contentful CMS adapter tests (respx-mocked)."""

from __future__ import annotations

import json
import re

import httpx
import pytest
import respx

from backend.cms import (
    CMSQueryError,
    CMSSignatureError,
    InvalidCMSTokenError,
    MissingCMSScopeError,
    CMSRateLimitError,
)
from backend.cms.contentful import ContentfulCMSSource


def _ok(payload=None, status=200):
    return httpx.Response(status, json=payload if payload is not None else {"items": []})


SPACE = "spaceXYZ"
ENV = "master"
BASE = "https://cdn.contentful.com"
ENDPOINT = f"{BASE}/spaces/{SPACE}/environments/{ENV}/entries"


def _mk(**kw):
    kw.setdefault("space_id", SPACE)
    kw.setdefault("environment", ENV)
    return ContentfulCMSSource(
        token=kw.pop("token", "CFPAT-delivery-token-1234"),
        webhook_secret=kw.pop("webhook_secret", "whsec_contentful_0001"),
        **kw,
    )


class TestConfigure:

    def test_requires_space_id(self):
        with pytest.raises(ValueError):
            ContentfulCMSSource(token="t", space_id="")

    def test_requires_token(self):
        with pytest.raises(ValueError):
            ContentfulCMSSource(token=None, space_id=SPACE)

    def test_preview_toggle_routes_to_preview_host(self):
        src = ContentfulCMSSource(token="t", space_id=SPACE, preview=True)
        assert "preview.contentful.com" in src._api_base


class TestFetch:

    @respx.mock
    async def test_fetch_by_content_type_string(self):
        route = respx.get(re.compile(re.escape(ENDPOINT) + r".*")).mock(
            return_value=_ok({"items": [
                {
                    "sys": {
                        "id": "e1", "createdAt": "2026-01-01T00:00:00Z",
                        "updatedAt": "2026-01-02T00:00:00Z", "locale": "en-US",
                        "contentType": {"sys": {"id": "post"}},
                    },
                    "fields": {"title": "Hi", "slug": "hi"},
                },
            ]}),
        )
        src = _mk()
        entries = await src.fetch("post")
        assert route.called
        assert entries[0].id == "e1"
        assert entries[0].content_type == "post"
        assert entries[0].fields == {"title": "Hi", "slug": "hi"}
        assert entries[0].locale == "en-US"
        assert "content_type=post" in str(route.calls.last.request.url)

    @respx.mock
    async def test_fetch_filter_dict_merges_content_type(self):
        route = respx.get(re.compile(re.escape(ENDPOINT) + r".*")).mock(
            return_value=_ok(),
        )
        src = _mk()
        await src.fetch({"fields.slug": "hi"}, content_type="post")
        url = str(route.calls.last.request.url)
        assert "content_type=post" in url
        assert "fields.slug=hi" in url

    @respx.mock
    async def test_fetch_rejects_non_string_non_mapping_query(self):
        src = _mk()
        with pytest.raises(CMSQueryError):
            await src.fetch(123)  # type: ignore[arg-type]

    @respx.mock
    async def test_fetch_401_maps_to_invalid_token(self):
        respx.get(re.compile(re.escape(ENDPOINT) + r".*")).mock(
            return_value=httpx.Response(401, json={"message": "AccessTokenInvalid"}),
        )
        with pytest.raises(InvalidCMSTokenError):
            await _mk().fetch("post")

    @respx.mock
    async def test_fetch_403_maps_to_missing_scope(self):
        respx.get(re.compile(re.escape(ENDPOINT) + r".*")).mock(
            return_value=httpx.Response(403, json={"message": "AccessDenied"}),
        )
        with pytest.raises(MissingCMSScopeError):
            await _mk().fetch("post")

    @respx.mock
    async def test_fetch_429_maps_to_rate_limit(self):
        respx.get(re.compile(re.escape(ENDPOINT) + r".*")).mock(
            return_value=httpx.Response(
                429,
                headers={"X-Contentful-RateLimit-Reset": "30"},
                json={"message": "Throttled"},
            ),
        )
        with pytest.raises(CMSRateLimitError) as ei:
            await _mk().fetch("post")
        assert ei.value.retry_after == 30

    @respx.mock
    async def test_fetch_list_value_coerced_to_csv(self):
        route = respx.get(re.compile(re.escape(ENDPOINT) + r".*")).mock(return_value=_ok())
        src = _mk()
        await src.fetch({"sys.id[in]": ["a", "b", "c"]}, content_type="post")
        url = str(route.calls.last.request.url)
        assert "a%2Cb%2Cc" in url or "a,b,c" in url


class TestWebhook:

    async def test_webhook_verifies_shared_secret(self):
        body = json.dumps({"sys": {"id": "abc", "contentType": {"sys": {"id": "post"}}}})
        src = _mk()
        event = await src.webhook_handler(
            body,
            headers={
                "x-contentful-webhook-signature": "whsec_contentful_0001",
                "X-Contentful-Topic": "ContentManagement.Entry.publish",
            },
        )
        assert event.action == "publish"
        assert event.entry_id == "abc"
        assert event.content_type == "post"

    async def test_webhook_unknown_topic_falls_through_to_other(self):
        body = json.dumps({"sys": {"id": "x"}})
        src = _mk()
        event = await src.webhook_handler(
            body,
            headers={"x-contentful-webhook-signature": "whsec_contentful_0001"},
        )
        assert event.action == "other"

    async def test_webhook_rejects_bad_signature(self):
        src = _mk()
        with pytest.raises(CMSSignatureError):
            await src.webhook_handler(
                json.dumps({}),
                headers={
                    "x-contentful-webhook-signature": "wrong",
                    "x-contentful-topic": "ContentManagement.Entry.publish",
                },
            )

    async def test_webhook_custom_signature_header(self):
        body = json.dumps({"sys": {"id": "x"}})
        src = _mk(signature_header="X-My-Sig")
        event = await src.webhook_handler(
            body,
            headers={"x-my-sig": "whsec_contentful_0001", "x-contentful-topic": "ContentManagement.Entry.save"},
        )
        assert event.action == "update"

    async def test_webhook_archive_topic_maps_to_update(self):
        body = json.dumps({"sys": {"id": "x"}})
        src = _mk()
        event = await src.webhook_handler(
            body,
            headers={
                "x-contentful-webhook-signature": "whsec_contentful_0001",
                "x-contentful-topic": "ContentManagement.Entry.archive",
            },
        )
        assert event.action == "update"
