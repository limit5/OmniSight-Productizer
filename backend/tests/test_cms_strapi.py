"""W9 #283 — Strapi CMS adapter tests (respx-mocked)."""

from __future__ import annotations

import json
import re

import httpx
import pytest
import respx

from backend.cms import (
    CMSError,
    CMSQueryError,
    CMSSignatureError,
    InvalidCMSTokenError,
    hmac_sha256_hex,
)
from backend.cms.strapi import StrapiCMSSource, _flatten_filters


def _ok(payload=None, status=200):
    return httpx.Response(status, json=payload if payload is not None else {"data": []})


def _mk(**kw):
    kw.setdefault("base_url", "https://cms.example.com")
    kw.setdefault("default_collection", None)
    return StrapiCMSSource(
        token=kw.pop("token", "tok_strapi_ABCD1234"),
        webhook_secret=kw.pop("webhook_secret", "whsec_strapi_xyz"),
        **kw,
    )


class TestConfigure:

    def test_requires_base_url(self):
        with pytest.raises(ValueError):
            StrapiCMSSource(token="t", base_url="")

    def test_rejects_invalid_base_url(self):
        with pytest.raises(ValueError):
            StrapiCMSSource(token="t", base_url="not-a-url")


class TestFlattenFilters:

    def test_flat_equality(self):
        out = _flatten_filters({"title": {"$eq": "hi"}})
        assert ("filters[title][$eq]", "hi") in out

    def test_nested_and(self):
        out = _flatten_filters({"$and": [{"a": 1}, {"b": 2}]})
        keys = {k for k, _ in out}
        assert "filters[$and][0][a]" in keys
        assert "filters[$and][1][b]" in keys

    def test_bool_coerced(self):
        out = _flatten_filters({"published": True})
        assert ("filters[published]", "true") in out


class TestFetch:

    @respx.mock
    async def test_fetch_collection_by_string(self):
        route = respx.get("https://cms.example.com/api/articles").mock(
            return_value=_ok({"data": [
                {"id": 1, "attributes": {
                    "title": "Hi", "slug": "hi", "createdAt": "2026-01-01T00:00:00Z",
                    "updatedAt": "2026-01-02T00:00:00Z", "publishedAt": "2026-01-03T00:00:00Z",
                    "locale": "en",
                }},
            ]}),
        )
        src = _mk()
        entries = await src.fetch("articles")
        assert route.called
        assert entries[0].id == "1"
        assert entries[0].content_type == "articles"
        assert entries[0].fields == {"title": "Hi", "slug": "hi"}
        assert entries[0].created_at == "2026-01-01T00:00:00Z"
        assert entries[0].locale == "en"

    @respx.mock
    async def test_fetch_filter_dict_requires_collection(self):
        src = _mk()
        with pytest.raises(CMSQueryError):
            await src.fetch({"title": {"$eq": "x"}})

    @respx.mock
    async def test_fetch_filter_dict_with_content_type(self):
        route = respx.get(re.compile(r"https://cms\.example\.com/api/articles.*")).mock(
            return_value=_ok({"data": []}),
        )
        src = _mk()
        await src.fetch({"title": {"$eq": "hi"}}, content_type="articles")
        assert route.called
        url = str(route.calls.last.request.url)
        assert "filters%5Btitle%5D%5B%24eq%5D=hi" in url or "filters[title][$eq]=hi" in url

    @respx.mock
    async def test_fetch_flat_v5_row_shape(self):
        """Strapi v5 often returns flat rows (no ``attributes`` wrapper)."""
        respx.get(re.compile(r"https://cms\.example\.com/api/articles.*")).mock(
            return_value=_ok({"data": [
                {"id": 7, "title": "Flat", "slug": "flat"},
            ]}),
        )
        src = _mk(default_collection="articles")
        entries = await src.fetch({})
        assert entries[0].id == "7"
        assert entries[0].fields == {"title": "Flat", "slug": "flat"}

    @respx.mock
    async def test_fetch_passes_pagination_params(self):
        route = respx.get(re.compile(r"https://cms\.example\.com/api/articles.*")).mock(
            return_value=_ok(),
        )
        src = _mk()
        await src.fetch("articles", params={"pagination": {"page": 2, "pageSize": 10}, "populate": "*"})
        url = str(route.calls.last.request.url)
        assert "pagination" in url
        assert "page" in url
        assert "populate=%2A" in url or "populate=*" in url

    @respx.mock
    async def test_fetch_401_maps_to_invalid_token(self):
        respx.get("https://cms.example.com/api/articles").mock(
            return_value=httpx.Response(401, json={"error": {"message": "Unauthorized"}}),
        )
        with pytest.raises(InvalidCMSTokenError):
            await _mk(token="bad").fetch("articles")


class TestWebhook:

    async def test_webhook_hmac_header_verifies(self):
        body = json.dumps({
            "event": "entry.publish",
            "model": "article",
            "entry": {"id": 42, "title": "Hi"},
        })
        sig = hmac_sha256_hex("whsec_strapi_xyz", body)
        src = _mk()
        event = await src.webhook_handler(body, headers={"x-strapi-signature": sig})
        assert event.action == "publish"
        assert event.entry_id == "42"
        assert event.content_type == "article"

    async def test_webhook_authorization_bearer_scheme(self):
        body = json.dumps({"event": "entry.update", "model": "article", "entry": {"id": 3}})
        src = _mk()
        event = await src.webhook_handler(
            body,
            headers={"Authorization": "Bearer whsec_strapi_xyz"},
        )
        assert event.action == "update"

    async def test_webhook_custom_header_name(self):
        body = json.dumps({"event": "entry.delete", "model": "article", "entry": {"id": 1}})
        sig = hmac_sha256_hex("whsec_strapi_xyz", body)
        src = _mk(webhook_header="X-Custom-Sig")
        event = await src.webhook_handler(body, headers={"x-custom-sig": sig})
        assert event.action == "delete"

    async def test_webhook_rejects_bad_signature(self):
        src = _mk()
        with pytest.raises(CMSSignatureError):
            await src.webhook_handler(
                json.dumps({"event": "entry.create"}),
                headers={"x-strapi-signature": "wrong"},
            )

    async def test_webhook_rejects_bad_bearer(self):
        src = _mk()
        with pytest.raises(CMSSignatureError):
            await src.webhook_handler(
                json.dumps({"event": "entry.create"}),
                headers={"Authorization": "Bearer not-the-secret"},
            )

    async def test_webhook_invalid_json(self):
        body = b"not-json"
        sig = hmac_sha256_hex("whsec_strapi_xyz", body)
        src = _mk()
        with pytest.raises(CMSError):
            await src.webhook_handler(body, headers={"x-strapi-signature": sig})
