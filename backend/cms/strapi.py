"""W9 #283 — Strapi CMS adapter.

Reads through Strapi's public REST API (v4 / v5 compatible):

    GET {base_url}/api/{collection}?filters[...]&populate=*&pagination[page]=1

Strapi's filter syntax is dict-style (``filters[field][$eq]=value``),
the adapter accepts a nested dict and flattens it into the Strapi query
param shape.

Auth is a single bearer token (``STRAPI_API_TOKEN``). Token-less reads
are also supported when the content type is publicly readable.

Webhook verification
--------------------
Strapi's "Webhooks" feature lets operators configure a shared secret
that Strapi sends either via a custom header (default ``Authorization:
Bearer <secret>``) or via the first custom header the operator sets.
We accept both:

  * ``x-strapi-signature`` — HMAC-SHA256 hex over raw body (community
    convention; many Strapi webhook plugins adopt it).
  * ``authorization: Bearer <secret>`` — the built-in scheme.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional, Union
from urllib.parse import urlparse

import httpx

from backend.cms.base import (
    CMSEntry,
    CMSError,
    CMSNotFoundError,
    CMSQueryError,
    CMSRateLimitError,
    CMSSignatureError,
    CMSSource,
    CMSWebhookEvent,
    InvalidCMSTokenError,
    MissingCMSScopeError,
)

logger = logging.getLogger(__name__)


def _raise_for_strapi(resp: httpx.Response, provider: str = "strapi") -> None:
    if resp.status_code < 400:
        return
    try:
        body = resp.json()
    except Exception:
        body = {}
    err = body.get("error") or {}
    msg = err.get("message") or body.get("message") or resp.text or "unknown error"
    if resp.status_code == 401:
        raise InvalidCMSTokenError(msg, status=401, provider=provider)
    if resp.status_code == 403:
        raise MissingCMSScopeError(msg, status=403, provider=provider)
    if resp.status_code == 404:
        raise CMSNotFoundError(msg, status=404, provider=provider)
    if resp.status_code == 400:
        raise CMSQueryError(msg, status=400, provider=provider)
    if resp.status_code == 429:
        retry = int(resp.headers.get("Retry-After", "60"))
        raise CMSRateLimitError(msg, retry_after=retry, status=429, provider=provider)
    raise CMSError(msg, status=resp.status_code, provider=provider)


def _flatten_filters(node: Mapping[str, Any], prefix: str = "filters") -> list[tuple[str, str]]:
    """Flatten a nested filter dict into ``filters[a][b]=c`` pairs.

    Strapi expects ``filters[title][$eq]=hello`` — we convert a
    natural dict ``{"title": {"$eq": "hello"}}`` into that shape so
    callers don't have to build the query string by hand.
    """
    out: list[tuple[str, str]] = []
    for k, v in node.items():
        key = f"{prefix}[{k}]"
        if isinstance(v, Mapping):
            out.extend(_flatten_filters(v, key))
        elif isinstance(v, (list, tuple)):
            for i, item in enumerate(v):
                if isinstance(item, Mapping):
                    out.extend(_flatten_filters(item, f"{key}[{i}]"))
                else:
                    out.append((f"{key}[{i}]", str(item)))
        elif isinstance(v, bool):
            out.append((key, "true" if v else "false"))
        else:
            out.append((key, str(v)))
    return out


class StrapiCMSSource(CMSSource):
    """Strapi Headless CMS adapter (``provider='strapi'``)."""

    provider = "strapi"

    def _configure(
        self,
        *,
        base_url: str = "",
        default_collection: Optional[str] = None,
        webhook_header: str = "x-strapi-signature",
        **_: Any,
    ) -> None:
        if not base_url:
            raise ValueError("StrapiCMSSource requires 'base_url'")
        parsed = urlparse(base_url)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError(f"Invalid Strapi base_url: {base_url!r}")
        self._base_url = base_url.rstrip("/")
        self._default_collection = default_collection
        self._webhook_header = webhook_header.lower()

    # ── HTTP plumbing ──

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def _get(self, path: str, *, params: Optional[list] = None) -> dict:
        url = f"{self._base_url}{path}"
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            resp = await c.get(url, headers=self._headers(), params=params or [])
        _raise_for_strapi(resp)
        if not resp.content:
            return {}
        try:
            return resp.json()
        except Exception as exc:
            raise CMSError(
                f"strapi returned non-JSON response: {exc}",
                status=resp.status_code, provider=self.provider,
            )

    # ── fetch ──

    async def fetch(
        self,
        query: Union[str, Mapping[str, Any]],
        *,
        params: Optional[Mapping[str, Any]] = None,
        content_type: Optional[str] = None,
    ) -> list[CMSEntry]:
        """Fetch entries from a Strapi collection.

        ``query`` is a filter dict (``{"title": {"$eq": "hello"}}``) or
        a collection path string (``"articles"``). ``content_type`` or
        ``default_collection`` (from construction) is used to resolve
        the REST path when ``query`` is a dict.
        """
        collection = content_type or self._default_collection
        filter_params: list[tuple[str, str]] = []
        if isinstance(query, str) and query:
            # Allow ``query="articles"`` as a collection shortcut.
            if "/" in query or query.startswith("api/"):
                path = "/" + query.lstrip("/")
            else:
                collection = collection or query
                path = f"/api/{collection}"
        elif isinstance(query, Mapping):
            if query:
                filter_params.extend(_flatten_filters(query))
            if not collection:
                raise CMSQueryError(
                    "strapi fetch requires 'content_type' (or default_collection) "
                    "when 'query' is a filter dict",
                    status=400, provider=self.provider,
                )
            path = f"/api/{collection}"
        else:
            raise CMSQueryError(
                "strapi fetch requires a collection string or filter dict",
                status=400, provider=self.provider,
            )

        # Merge pagination / population / sort params.
        extra_params: list[tuple[str, str]] = []
        for k, v in (params or {}).items():
            if isinstance(v, Mapping):
                extra_params.extend(_flatten_filters(v, k))
            else:
                extra_params.append((k, "true" if v is True else ("false" if v is False else str(v))))

        data = await self._get(path, params=filter_params + extra_params)
        rows = data.get("data") or []
        if isinstance(rows, Mapping):
            rows = [rows]
        entries: list[CMSEntry] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            # Strapi v4 wraps in ``{id, attributes: {...}}`` / v5 flattens.
            attrs = row.get("attributes") if isinstance(row.get("attributes"), Mapping) else row
            raw_id = row.get("id")
            entry_id = str(raw_id) if raw_id is not None else ""
            fields = {k: v for k, v in attrs.items() if k not in {"id", "createdAt", "updatedAt", "publishedAt", "locale"}}
            entries.append(
                CMSEntry(
                    id=entry_id,
                    content_type=collection or "",
                    fields=fields,
                    created_at=attrs.get("createdAt") or row.get("createdAt"),
                    updated_at=attrs.get("updatedAt") or row.get("updatedAt"),
                    locale=attrs.get("locale") or row.get("locale"),
                    raw=dict(row),
                )
            )
        logger.info(
            "strapi.fetch base=%s collection=%s entries=%d fp=%s",
            self._base_url, collection, len(entries), self.token_fp(),
        )
        return entries

    # ── webhook ──

    async def webhook_handler(
        self,
        payload: Union[str, bytes, Mapping[str, Any]],
        *,
        headers: Optional[Mapping[str, str]] = None,
    ) -> CMSWebhookEvent:
        normalised_headers = {k.lower(): v for k, v in (headers or {}).items()}
        ok = self._verify_strapi_headers(normalised_headers, payload)
        if not ok:
            raise CMSSignatureError(
                "strapi webhook signature mismatch",
                status=401, provider=self.provider,
            )
        if isinstance(payload, (str, bytes)):
            import json as _json
            raw_text = payload.decode() if isinstance(payload, bytes) else payload
            try:
                data: Mapping[str, Any] = _json.loads(raw_text)
            except Exception as exc:
                raise CMSError(
                    f"strapi webhook body is not JSON: {exc}",
                    status=400, provider=self.provider,
                )
        else:
            data = payload
        event = str(data.get("event") or "").lower()
        action = {
            "entry.create": "create",
            "entry.update": "update",
            "entry.delete": "delete",
            "entry.publish": "publish",
            "entry.unpublish": "unpublish",
        }.get(event, event or "other")
        entry_block = data.get("entry") if isinstance(data.get("entry"), Mapping) else {}
        entry_id = entry_block.get("id") if entry_block else None
        return CMSWebhookEvent(
            provider=self.provider,
            action=action,
            entry_id=str(entry_id) if entry_id is not None else None,
            content_type=data.get("model") or None,
            raw=dict(data),
        )

    def _verify_strapi_headers(
        self,
        headers: Mapping[str, str],
        payload: Union[str, bytes, Mapping[str, Any]],
    ) -> bool:
        """Strapi supports two schemes; try both with the single configured secret."""
        if not self._webhook_secret:
            return False
        hmac_sig = headers.get(self._webhook_header)
        if hmac_sig and isinstance(payload, (str, bytes)):
            if self.verify_signature(hmac_sig, payload, scheme="hmac-sha256"):
                return True
        auth = headers.get("authorization")
        if auth and auth.lower().startswith("bearer "):
            candidate = auth.split(" ", 1)[1].strip()
            return self.verify_signature(candidate, b"", scheme="shared-secret")
        return False


__all__ = ["StrapiCMSSource"]
