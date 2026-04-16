"""W9 #283 — Sanity CMS adapter.

Reads through Sanity's public query endpoint:

    GET /v{apiVersion}/data/query/{dataset}?query=<GROQ>&...params

Writes are out of scope (OmniSight is read-only against the CMS; editors
own the Studio). Previewing drafts lives behind ``SANITY_PREVIEW_TOKEN``
so the scaffold never leaks unpublished content to public traffic.

Webhook verification
--------------------
Sanity ships webhook payloads with a ``sanity-webhook-signature`` header:
an HMAC-SHA256 hex digest over the raw request body keyed on the secret
the operator configured in the Studio.

API version
-----------
Sanity's API requires a dated version pin (``YYYY-MM-DD``) — the default
is ``2024-10-01`` (Next.js 16 / Astro 5 contemporary) but callers can
override via ``api_version=...`` on construction.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional, Union

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

SANITY_API_BASE = "https://{project_id}.api.sanity.io"
_DEFAULT_API_VERSION = "2024-10-01"


def _raise_for_sanity(resp: httpx.Response, provider: str = "sanity") -> None:
    """Map Sanity error responses to typed exceptions."""
    if resp.status_code < 400:
        return
    try:
        body = resp.json()
    except Exception:
        body = {}
    err = body.get("error") or {}
    msg = err.get("description") or body.get("message") or resp.text or "unknown error"
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


class SanityCMSSource(CMSSource):
    """Sanity Headless CMS adapter (``provider='sanity'``)."""

    provider = "sanity"

    def _configure(
        self,
        *,
        project_id: str = "",
        dataset: str = "production",
        api_version: str = _DEFAULT_API_VERSION,
        api_base: Optional[str] = None,
        use_cdn: bool = True,
        **_: Any,
    ) -> None:
        if not project_id:
            raise ValueError("SanityCMSSource requires 'project_id'")
        self._project_id = project_id
        self._dataset = dataset
        self._api_version = api_version
        # ``apicdn`` is CDN-cached (faster, eventually consistent); fall
        # back to ``api`` when a token is present (drafts / preview).
        host_prefix = "apicdn" if (use_cdn and not self._token) else "api"
        base = api_base or f"https://{project_id}.{host_prefix}.sanity.io"
        self._api_base = base.rstrip("/")

    # ── HTTP plumbing ──

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def _get(self, path: str, *, params: Optional[dict] = None) -> dict:
        url = f"{self._api_base}{path}"
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            resp = await c.get(url, headers=self._headers(), params=params or {})
        _raise_for_sanity(resp)
        if not resp.content:
            return {}
        try:
            return resp.json()
        except Exception as exc:
            raise CMSError(
                f"sanity returned non-JSON response: {exc}",
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
        """Run a GROQ query against the configured dataset.

        ``query`` is a GROQ string (``*[_type == "post"]``). When
        ``content_type`` is passed *and* ``query`` is a raw dict with a
        ``groq`` key, the adapter wraps the query to scope by type —
        otherwise the caller is expected to embed the ``_type`` predicate
        themselves.
        """
        if isinstance(query, Mapping):
            groq = str(query.get("groq") or query.get("query") or "")
            if not groq:
                raise CMSQueryError(
                    "sanity fetch requires a GROQ string or {'groq': ...} mapping",
                    status=400, provider=self.provider,
                )
        else:
            groq = query
        if not groq:
            raise CMSQueryError(
                "sanity fetch requires a non-empty GROQ query",
                status=400, provider=self.provider,
            )
        if content_type and "_type ==" not in groq:
            groq = f'*[_type == "{content_type}"] | {{ ...{groq} }}' if groq.startswith("{") else \
                f'*[_type == "{content_type}"]{groq}' if not groq.startswith("*") else groq

        req_params: dict[str, Any] = {"query": groq}
        for k, v in (params or {}).items():
            req_params[f"$" + k] = v
        data = await self._get(
            f"/v{self._api_version}/data/query/{self._dataset}",
            params=req_params,
        )
        result = data.get("result") or []
        entries: list[CMSEntry] = []
        for doc in result:
            if not isinstance(doc, Mapping):
                continue
            doc_id = str(doc.get("_id") or "")
            doc_type = str(doc.get("_type") or content_type or "")
            fields = {k: v for k, v in doc.items() if not k.startswith("_")}
            entries.append(
                CMSEntry(
                    id=doc_id,
                    content_type=doc_type,
                    fields=fields,
                    created_at=str(doc.get("_createdAt")) if doc.get("_createdAt") else None,
                    updated_at=str(doc.get("_updatedAt")) if doc.get("_updatedAt") else None,
                    raw=dict(doc),
                )
            )
        logger.info(
            "sanity.fetch project=%s dataset=%s entries=%d fp=%s",
            self._project_id, self._dataset, len(entries), self.token_fp(),
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
        signature = normalised_headers.get("sanity-webhook-signature")
        if isinstance(payload, (str, bytes)):
            if not self.verify_signature(signature, payload, scheme="hmac-sha256"):
                raise CMSSignatureError(
                    "sanity webhook signature mismatch",
                    status=401, provider=self.provider,
                )
            import json as _json
            raw_text = payload.decode() if isinstance(payload, bytes) else payload
            try:
                data: Mapping[str, Any] = _json.loads(raw_text)
            except Exception as exc:
                raise CMSError(
                    f"sanity webhook body is not JSON: {exc}",
                    status=400, provider=self.provider,
                )
        else:
            data = payload
        # Sanity GROQ-powered webhooks let operators pick the projection;
        # we accept the common shapes: ``{_id, _type, operation}`` or
        # ``{ids: [...], transactionId}``.
        entry_id = str(data.get("_id") or "") or None
        content_type = str(data.get("_type") or "") or None
        op = str(data.get("operation") or data.get("transition") or "").lower()
        action = {
            "create": "create",
            "update": "update",
            "delete": "delete",
            "publish": "publish",
            "unpublish": "unpublish",
        }.get(op, op or "other")
        return CMSWebhookEvent(
            provider=self.provider,
            action=action,
            entry_id=entry_id,
            content_type=content_type,
            raw=dict(data),
        )


__all__ = ["SanityCMSSource"]
