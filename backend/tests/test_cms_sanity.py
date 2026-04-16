"""W9 #283 — Sanity CMS adapter tests (respx-mocked)."""

from __future__ import annotations

import json
import re

import httpx
import pytest
import respx

from backend.cms import (
    CMSNotFoundError,
    CMSQueryError,
    CMSRateLimitError,
    CMSSignatureError,
    InvalidCMSTokenError,
    MissingCMSScopeError,
    hmac_sha256_hex,
)
from backend.cms.sanity import SanityCMSSource


def _ok(result=None, status=200):
    return httpx.Response(status, json=result if result is not None else {})


def _err(status, code="", msg="err"):
    return httpx.Response(status, json={"error": {"code": code, "description": msg}})


def _mk(**kw):
    kw.setdefault("project_id", "proj_abc")
    kw.setdefault("dataset", "production")
    return SanityCMSSource(
        token=kw.pop("token", None),
        webhook_secret=kw.pop("webhook_secret", "whsec_1234567890"),
        **kw,
    )


class TestConfigure:

    def test_requires_project_id(self):
        with pytest.raises(ValueError):
            SanityCMSSource(token="t", project_id="", dataset="production")

    def test_cdn_host_when_no_token(self):
        src = SanityCMSSource(project_id="xyz", dataset="p")
        assert "apicdn.sanity.io" in src._api_base

    def test_authoritative_host_when_token_present(self):
        src = SanityCMSSource(token="t", project_id="xyz", dataset="p")
        assert "api.sanity.io" in src._api_base
        assert "apicdn" not in src._api_base


class TestFetch:

    @respx.mock
    async def test_fetches_groq_and_normalises(self):
        route = respx.get(re.compile(r"https://proj_abc\..*\.sanity\.io/v[^/]+/data/query/production")).mock(
            return_value=_ok({
                "result": [
                    {
                        "_id": "doc1", "_type": "post",
                        "_createdAt": "2026-01-01T00:00:00Z",
                        "_updatedAt": "2026-01-02T00:00:00Z",
                        "title": "Hello",
                        "slug": {"current": "hello"},
                    },
                    {"_id": "doc2", "_type": "post", "title": "Two"},
                ],
            }),
        )
        src = _mk()
        entries = await src.fetch('*[_type == "post"]')
        assert route.called
        assert [e.id for e in entries] == ["doc1", "doc2"]
        assert entries[0].content_type == "post"
        assert entries[0].fields == {"title": "Hello", "slug": {"current": "hello"}}
        assert entries[0].created_at == "2026-01-01T00:00:00Z"
        # Ensure query was sent as ?query= param.
        req = route.calls.last.request
        assert "query=" in str(req.url)

    @respx.mock
    async def test_fetch_accepts_groq_mapping(self):
        route = respx.get(re.compile(r"https://proj_abc\..*\.sanity\.io/v[^/]+/data/query/production")).mock(
            return_value=_ok({"result": []}),
        )
        src = _mk()
        entries = await src.fetch({"groq": '*[_type == "post"]'})
        assert entries == []
        assert route.called

    @respx.mock
    async def test_fetch_rejects_empty_query(self):
        src = _mk()
        with pytest.raises(CMSQueryError):
            await src.fetch("")
        with pytest.raises(CMSQueryError):
            await src.fetch({})

    @respx.mock
    async def test_fetch_401_maps_to_invalid_token(self):
        respx.get(re.compile(r"https://proj_abc\..*\.sanity\.io/.*")).mock(
            return_value=_err(401, msg="Invalid token"),
        )
        with pytest.raises(InvalidCMSTokenError):
            await _mk(token="bad").fetch('*[_type == "post"]')

    @respx.mock
    async def test_fetch_403_maps_to_missing_scope(self):
        respx.get(re.compile(r"https://proj_abc\..*\.sanity\.io/.*")).mock(
            return_value=_err(403, msg="Forbidden"),
        )
        with pytest.raises(MissingCMSScopeError):
            await _mk(token="t").fetch('*[_type == "post"]')

    @respx.mock
    async def test_fetch_404_maps_to_not_found(self):
        respx.get(re.compile(r"https://proj_abc\..*\.sanity\.io/.*")).mock(
            return_value=_err(404, msg="Not Found"),
        )
        with pytest.raises(CMSNotFoundError):
            await _mk().fetch('*[_type == "post"]')

    @respx.mock
    async def test_fetch_429_maps_to_rate_limit(self):
        respx.get(re.compile(r"https://proj_abc\..*\.sanity\.io/.*")).mock(
            return_value=httpx.Response(
                429,
                headers={"Retry-After": "42"},
                json={"error": {"description": "rate limited"}},
            ),
        )
        with pytest.raises(CMSRateLimitError) as ei:
            await _mk().fetch('*[_type == "post"]')
        assert ei.value.retry_after == 42


class TestWebhook:

    async def test_webhook_verifies_hmac_and_normalises(self):
        body = json.dumps({"_id": "doc-xyz", "_type": "post", "operation": "update"})
        sig = hmac_sha256_hex("whsec_1234567890", body)
        src = _mk()
        event = await src.webhook_handler(body, headers={"Sanity-Webhook-Signature": sig})
        assert event.provider == "sanity"
        assert event.action == "update"
        assert event.entry_id == "doc-xyz"
        assert event.content_type == "post"

    async def test_webhook_rejects_bad_signature(self):
        body = json.dumps({"_id": "doc-xyz"})
        src = _mk()
        with pytest.raises(CMSSignatureError):
            await src.webhook_handler(body, headers={"sanity-webhook-signature": "nope"})

    async def test_webhook_rejects_missing_header(self):
        src = _mk()
        with pytest.raises(CMSSignatureError):
            await src.webhook_handler("{}", headers={})

    async def test_webhook_rejects_invalid_json(self):
        body = b"not-json"
        sig = hmac_sha256_hex("whsec_1234567890", body)
        from backend.cms import CMSError
        src = _mk()
        with pytest.raises(CMSError):
            await src.webhook_handler(body, headers={"sanity-webhook-signature": sig})

    async def test_webhook_accepts_prebaked_dict_without_signature(self):
        """Test-only path: dict payloads skip signature verification."""
        src = _mk()
        event = await src.webhook_handler(
            {"_id": "doc1", "_type": "post", "operation": "publish"},
        )
        assert event.action == "publish"

    async def test_webhook_unknown_operation_is_other(self):
        body = json.dumps({"_id": "x", "operation": "weird"})
        sig = hmac_sha256_hex("whsec_1234567890", body)
        event = await _mk().webhook_handler(body, headers={"sanity-webhook-signature": sig})
        assert event.action == "weird"
