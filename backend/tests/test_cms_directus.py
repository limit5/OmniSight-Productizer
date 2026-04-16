"""W9 #283 — Directus CMS adapter tests (respx-mocked)."""

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
    hmac_sha256_hex,
)
from backend.cms.directus import DirectusCMSSource


def _ok(payload=None, status=200):
    return httpx.Response(status, json=payload if payload is not None else {"data": []})


BASE = "https://directus.example.com"


def _mk(**kw):
    kw.setdefault("base_url", BASE)
    return DirectusCMSSource(
        token=kw.pop("token", "static_token_ABCD1234"),
        webhook_secret=kw.pop("webhook_secret", "whsec_directus_xyz"),
        **kw,
    )


class TestConfigure:

    def test_requires_base_url(self):
        with pytest.raises(ValueError):
            DirectusCMSSource(token="t", base_url="")

    def test_rejects_invalid_base_url(self):
        with pytest.raises(ValueError):
            DirectusCMSSource(token="t", base_url="bad-url")


class TestFetch:

    @respx.mock
    async def test_fetch_collection_by_string(self):
        route = respx.get(f"{BASE}/items/articles").mock(
            return_value=_ok({"data": [
                {"id": 1, "title": "Hi", "slug": "hi",
                 "date_created": "2026-01-01", "date_updated": "2026-01-02",
                 "status": "published"},
            ]}),
        )
        src = _mk()
        entries = await src.fetch("articles")
        assert route.called
        assert entries[0].id == "1"
        assert entries[0].content_type == "articles"
        assert entries[0].fields == {"title": "Hi", "slug": "hi"}
        assert entries[0].created_at == "2026-01-01"

    @respx.mock
    async def test_fetch_filter_dict_requires_collection(self):
        src = _mk()
        with pytest.raises(CMSQueryError):
            await src.fetch({"title": {"_eq": "x"}})

    @respx.mock
    async def test_fetch_filter_dict_json_encoded(self):
        route = respx.get(re.compile(re.escape(f"{BASE}/items/articles") + r".*")).mock(
            return_value=_ok(),
        )
        src = _mk(default_collection="articles")
        await src.fetch({"title": {"_eq": "hi"}})
        url = str(route.calls.last.request.url)
        # ``filter=`` should be present and its value should be JSON.
        assert "filter=" in url
        # Decode to confirm JSON payload made it through.
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(url).query)
        assert "filter" in qs
        parsed = json.loads(qs["filter"][0])
        assert parsed == {"title": {"_eq": "hi"}}

    @respx.mock
    async def test_fetch_params_list_coerced(self):
        route = respx.get(re.compile(re.escape(f"{BASE}/items/articles") + r".*")).mock(
            return_value=_ok(),
        )
        src = _mk()
        await src.fetch("articles", params={"fields": ["id", "title"], "limit": 10, "deep": {"related": {"fields": ["x"]}}})
        url = str(route.calls.last.request.url)
        assert "fields=id%2Ctitle" in url or "fields=id,title" in url
        assert "limit=10" in url
        assert "deep=" in url

    @respx.mock
    async def test_fetch_raw_path_escape_hatch(self):
        """If caller passes ``items/x`` or ``/foo`` string, it's a raw path."""
        route = respx.get(f"{BASE}/items/things").mock(return_value=_ok())
        src = _mk()
        await src.fetch("items/things")
        assert route.called

    @respx.mock
    async def test_fetch_401_maps_to_invalid_token(self):
        respx.get(f"{BASE}/items/articles").mock(
            return_value=httpx.Response(401, json={"errors": [{"message": "Unauthorized"}]}),
        )
        with pytest.raises(InvalidCMSTokenError):
            await _mk().fetch("articles")


class TestWebhook:

    async def test_webhook_hmac_header(self):
        body = json.dumps({
            "event": "items.create",
            "collection": "articles",
            "keys": [42],
        })
        sig = hmac_sha256_hex("whsec_directus_xyz", body)
        src = _mk()
        event = await src.webhook_handler(body, headers={"x-directus-signature": sig})
        assert event.action == "create"
        assert event.entry_id == "42"
        assert event.content_type == "articles"

    async def test_webhook_shared_secret_header(self):
        body = json.dumps({"event": "items.update", "collection": "posts", "keys": [7]})
        src = _mk()
        event = await src.webhook_handler(
            body,
            headers={"x-directus-secret": "whsec_directus_xyz"},
        )
        assert event.action == "update"
        assert event.entry_id == "7"

    async def test_webhook_action_field_fallback(self):
        """Flow-style payloads may use ``action`` instead of ``event``."""
        body = json.dumps({"action": "delete", "collection": "posts", "key": 99})
        sig = hmac_sha256_hex("whsec_directus_xyz", body)
        src = _mk()
        event = await src.webhook_handler(body, headers={"x-directus-signature": sig})
        assert event.action == "delete"
        assert event.entry_id == "99"

    async def test_webhook_rejects_bad_signature(self):
        src = _mk()
        with pytest.raises(CMSSignatureError):
            await src.webhook_handler(
                json.dumps({"event": "items.create"}),
                headers={"x-directus-signature": "nope"},
            )

    async def test_webhook_custom_header_names(self):
        body = json.dumps({"event": "items.create", "collection": "c", "keys": [1]})
        sig = hmac_sha256_hex("whsec_directus_xyz", body)
        src = _mk(hmac_header="X-Sig", shared_secret_header="X-Token")
        # Either header should work.
        ev1 = await src.webhook_handler(body, headers={"x-sig": sig})
        assert ev1.action == "create"
        ev2 = await src.webhook_handler(body, headers={"x-token": "whsec_directus_xyz"})
        assert ev2.action == "create"
