"""W9 #283 — Contentful CMS adapter.

Reads through the Contentful Delivery API (published) or Preview API
(drafts):

    GET https://{host}/spaces/{space}/environments/{env}/entries
        ?content_type={type}&<filters>
        Authorization: Bearer {accessToken}

Hosts:

  * ``cdn.contentful.com`` — Delivery API (uses CONTENTFUL_DELIVERY_TOKEN)
  * ``preview.contentful.com`` — Preview API (uses CONTENTFUL_PREVIEW_TOKEN)

The adapter never talks to the Management API — that surface is reserved
for editors in Contentful's own web app.

Webhook verification
--------------------
Contentful signs webhooks with a shared secret the operator pastes
into the webhook config. The secret is delivered back on every request
via a header (default ``x-contentful-webhook-signature``) as a plain
value — we compare in constant time.
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

CONTENTFUL_DELIVERY_HOST = "cdn.contentful.com"
CONTENTFUL_PREVIEW_HOST = "preview.contentful.com"


def _raise_for_contentful(resp: httpx.Response, provider: str = "contentful") -> None:
    if resp.status_code < 400:
        return
    try:
        body = resp.json()
    except Exception:
        body = {}
    msg = body.get("message") or resp.text or "unknown error"
    if resp.status_code == 401:
        raise InvalidCMSTokenError(msg, status=401, provider=provider)
    if resp.status_code == 403:
        raise MissingCMSScopeError(msg, status=403, provider=provider)
    if resp.status_code == 404:
        raise CMSNotFoundError(msg, status=404, provider=provider)
    if resp.status_code == 400:
        raise CMSQueryError(msg, status=400, provider=provider)
    if resp.status_code == 429:
        retry = int(resp.headers.get("X-Contentful-RateLimit-Reset") or
                    resp.headers.get("Retry-After", "60"))
        raise CMSRateLimitError(msg, retry_after=retry, status=429, provider=provider)
    raise CMSError(msg, status=resp.status_code, provider=provider)


class ContentfulCMSSource(CMSSource):
    """Contentful Delivery / Preview API adapter (``provider='contentful'``)."""

    provider = "contentful"

    def _configure(
        self,
        *,
        space_id: str = "",
        environment: str = "master",
        preview: bool = False,
        api_base: Optional[str] = None,
        signature_header: str = "x-contentful-webhook-signature",
        **_: Any,
    ) -> None:
        if not space_id:
            raise ValueError("ContentfulCMSSource requires 'space_id'")
        if not self._token:
            raise ValueError("ContentfulCMSSource requires a delivery / preview token")
        self._space_id = space_id
        self._environment = environment
        self._preview = preview
        host = CONTENTFUL_PREVIEW_HOST if preview else CONTENTFUL_DELIVERY_HOST
        self._api_base = (api_base or f"https://{host}").rstrip("/")
        self._signature_header = signature_header.lower()

    # ── HTTP plumbing ──

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        }

    async def _get(self, path: str, *, params: Optional[dict] = None) -> dict:
        url = f"{self._api_base}{path}"
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            resp = await c.get(url, headers=self._headers(), params=params or {})
        _raise_for_contentful(resp)
        if not resp.content:
            return {}
        try:
            return resp.json()
        except Exception as exc:
            raise CMSError(
                f"contentful returned non-JSON response: {exc}",
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
        """Fetch entries from the Delivery / Preview API.

        ``query`` is a filter dict (``{"fields.slug": "hello"}``) or a
        content-type string (``"post"``). When a dict, the adapter
        automatically includes ``content_type`` if set.
        """
        req_params: dict[str, Any] = {}
        if isinstance(query, str) and query:
            req_params["content_type"] = query
        elif isinstance(query, Mapping):
            for k, v in query.items():
                req_params[k] = _coerce_contentful_value(v)
        else:
            raise CMSQueryError(
                "contentful fetch requires a filter dict or content-type string",
                status=400, provider=self.provider,
            )
        if content_type and "content_type" not in req_params:
            req_params["content_type"] = content_type
        for k, v in (params or {}).items():
            req_params[k] = _coerce_contentful_value(v)

        data = await self._get(
            f"/spaces/{self._space_id}/environments/{self._environment}/entries",
            params=req_params,
        )
        items = data.get("items") or []
        entries: list[CMSEntry] = []
        for item in items:
            if not isinstance(item, Mapping):
                continue
            sys = item.get("sys") if isinstance(item.get("sys"), Mapping) else {}
            entry_id = str(sys.get("id") or "")
            entry_type = str(
                (sys.get("contentType") or {}).get("sys", {}).get("id")
                if isinstance(sys.get("contentType"), Mapping) else ""
            ) or req_params.get("content_type") or ""
            fields = dict(item.get("fields") or {})
            entries.append(
                CMSEntry(
                    id=entry_id,
                    content_type=entry_type,
                    fields=fields,
                    created_at=sys.get("createdAt"),
                    updated_at=sys.get("updatedAt"),
                    locale=sys.get("locale"),
                    raw=dict(item),
                )
            )
        logger.info(
            "contentful.fetch space=%s env=%s preview=%s entries=%d fp=%s",
            self._space_id, self._environment, self._preview, len(entries), self.token_fp(),
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
        signature = normalised_headers.get(self._signature_header)
        if not self.verify_signature(signature, b"", scheme="shared-secret"):
            raise CMSSignatureError(
                "contentful webhook signature mismatch",
                status=401, provider=self.provider,
            )
        if isinstance(payload, (str, bytes)):
            import json as _json
            raw_text = payload.decode() if isinstance(payload, bytes) else payload
            try:
                data: Mapping[str, Any] = _json.loads(raw_text)
            except Exception as exc:
                raise CMSError(
                    f"contentful webhook body is not JSON: {exc}",
                    status=400, provider=self.provider,
                )
        else:
            data = payload
        # Contentful surfaces the event type via the ``X-Contentful-Topic``
        # header. Value shape: ``ContentManagement.Entry.publish``.
        topic = normalised_headers.get("x-contentful-topic") or ""
        action = _contentful_action_from_topic(topic)
        sys = data.get("sys") if isinstance(data.get("sys"), Mapping) else {}
        entry_id = str(sys.get("id") or "") or None
        entry_type = None
        ct = sys.get("contentType") if isinstance(sys.get("contentType"), Mapping) else None
        if ct and isinstance(ct.get("sys"), Mapping):
            entry_type = ct["sys"].get("id")
        return CMSWebhookEvent(
            provider=self.provider,
            action=action,
            entry_id=entry_id,
            content_type=entry_type,
            raw=dict(data),
        )


def _coerce_contentful_value(v: Any) -> Any:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (list, tuple)):
        return ",".join(str(x) for x in v)
    return v


def _contentful_action_from_topic(topic: str) -> str:
    """Map ``ContentManagement.Entry.publish`` → ``publish``.

    Unknown topics fall through as ``other`` so callers can audit the
    raw event without the adapter swallowing it.
    """
    if not topic:
        return "other"
    last = topic.split(".")[-1].lower()
    return {
        "create": "create",
        "save": "update",
        "auto_save": "update",
        "archive": "update",
        "unarchive": "update",
        "publish": "publish",
        "unpublish": "unpublish",
        "delete": "delete",
    }.get(last, last or "other")


__all__ = ["ContentfulCMSSource"]
