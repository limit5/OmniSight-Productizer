"""W9 #283 — Directus CMS adapter.

Reads through the Directus public REST API (v10 / v11 compatible):

    GET {base_url}/items/{collection}?filter[...]&fields=*&limit=N
        Authorization: Bearer {static_token}

Directus uses a richer filter grammar than Strapi — the adapter accepts
dict filters and JSON-encodes them onto the ``?filter=`` query param as
Directus expects (single JSON blob, not bracket-flattened).

Webhook verification
--------------------
Directus ships a "Flow" / "Webhook" integration; the default authorised
scheme is either a custom header containing a shared secret *or* an
``X-Directus-Signature`` HMAC. We support both:

  * ``x-directus-signature`` — HMAC-SHA256 hex over raw body.
  * configurable shared-secret header (default ``x-directus-secret``).
"""

from __future__ import annotations

import json
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


def _raise_for_directus(resp: httpx.Response, provider: str = "directus") -> None:
    if resp.status_code < 400:
        return
    try:
        body = resp.json()
    except Exception:
        body = {}
    errors = body.get("errors") or []
    msg = (errors[0].get("message") if errors else None) or resp.text or "unknown error"
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


class DirectusCMSSource(CMSSource):
    """Directus CMS adapter (``provider='directus'``)."""

    provider = "directus"

    def _configure(
        self,
        *,
        base_url: str = "",
        default_collection: Optional[str] = None,
        hmac_header: str = "x-directus-signature",
        shared_secret_header: str = "x-directus-secret",
        **_: Any,
    ) -> None:
        if not base_url:
            raise ValueError("DirectusCMSSource requires 'base_url'")
        parsed = urlparse(base_url)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError(f"Invalid Directus base_url: {base_url!r}")
        self._base_url = base_url.rstrip("/")
        self._default_collection = default_collection
        self._hmac_header = hmac_header.lower()
        self._shared_secret_header = shared_secret_header.lower()

    # ── HTTP plumbing ──

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def _get(self, path: str, *, params: Optional[dict] = None) -> dict:
        url = f"{self._base_url}{path}"
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            resp = await c.get(url, headers=self._headers(), params=params or {})
        _raise_for_directus(resp)
        if not resp.content:
            return {}
        try:
            return resp.json()
        except Exception as exc:
            raise CMSError(
                f"directus returned non-JSON response: {exc}",
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
        collection = content_type or self._default_collection
        req_params: dict[str, Any] = {}
        if isinstance(query, str) and query:
            if "/" in query or query.startswith("items/"):
                path = "/" + query.lstrip("/")
            else:
                collection = collection or query
                path = f"/items/{collection}"
        elif isinstance(query, Mapping):
            if query:
                req_params["filter"] = json.dumps(dict(query))
            if not collection:
                raise CMSQueryError(
                    "directus fetch requires 'content_type' (or default_collection) "
                    "when 'query' is a filter dict",
                    status=400, provider=self.provider,
                )
            path = f"/items/{collection}"
        else:
            raise CMSQueryError(
                "directus fetch requires a collection string or filter dict",
                status=400, provider=self.provider,
            )

        for k, v in (params or {}).items():
            if isinstance(v, bool):
                req_params[k] = "true" if v else "false"
            elif isinstance(v, (list, tuple)):
                req_params[k] = ",".join(str(x) for x in v)
            elif isinstance(v, Mapping):
                req_params[k] = json.dumps(dict(v))
            else:
                req_params[k] = v

        data = await self._get(path, params=req_params)
        rows = data.get("data") or []
        if isinstance(rows, Mapping):
            rows = [rows]
        entries: list[CMSEntry] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            raw_id = row.get("id")
            entry_id = str(raw_id) if raw_id is not None else ""
            fields = {
                k: v for k, v in row.items()
                if k not in {"id", "date_created", "date_updated", "status"}
            }
            entries.append(
                CMSEntry(
                    id=entry_id,
                    content_type=collection or "",
                    fields=fields,
                    created_at=row.get("date_created"),
                    updated_at=row.get("date_updated"),
                    raw=dict(row),
                )
            )
        logger.info(
            "directus.fetch base=%s collection=%s entries=%d fp=%s",
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
        if not self._verify_directus_headers(normalised_headers, payload):
            raise CMSSignatureError(
                "directus webhook signature mismatch",
                status=401, provider=self.provider,
            )
        if isinstance(payload, (str, bytes)):
            raw_text = payload.decode() if isinstance(payload, bytes) else payload
            try:
                data: Mapping[str, Any] = json.loads(raw_text)
            except Exception as exc:
                raise CMSError(
                    f"directus webhook body is not JSON: {exc}",
                    status=400, provider=self.provider,
                )
        else:
            data = payload
        # Directus Flows publish payloads with ``event`` + ``collection`` +
        # ``keys`` fields; Flow "Trigger — Webhook" carries the original
        # action under ``action``.
        action_raw = str(data.get("event") or data.get("action") or "").lower()
        action = {
            "items.create": "create",
            "items.update": "update",
            "items.delete": "delete",
            "create": "create",
            "update": "update",
            "delete": "delete",
        }.get(action_raw, action_raw or "other")
        keys = data.get("keys") if isinstance(data.get("keys"), list) else []
        entry_id = str(keys[0]) if keys else (
            str(data.get("key")) if data.get("key") is not None else None
        )
        return CMSWebhookEvent(
            provider=self.provider,
            action=action,
            entry_id=entry_id,
            content_type=data.get("collection") or None,
            raw=dict(data),
        )

    def _verify_directus_headers(
        self,
        headers: Mapping[str, str],
        payload: Union[str, bytes, Mapping[str, Any]],
    ) -> bool:
        if not self._webhook_secret:
            return False
        hmac_sig = headers.get(self._hmac_header)
        if hmac_sig and isinstance(payload, (str, bytes)):
            if self.verify_signature(hmac_sig, payload, scheme="hmac-sha256"):
                return True
        shared = headers.get(self._shared_secret_header)
        if shared and self.verify_signature(shared, b"", scheme="shared-secret"):
            return True
        return False


__all__ = ["DirectusCMSSource"]
