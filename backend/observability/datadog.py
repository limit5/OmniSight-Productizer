"""W10 #284 — Datadog RUM adapter.

Datadog's browser RUM SDK posts events to the regional intake gateway:

    POST https://browser-intake-{region}.datadoghq.{tld}/api/v2/rum
        ?dd-api-key=<client_token>
        &ddsource=browser
        &dd-evp-origin=browser

Each request contains a newline-delimited JSON document — one JSON
object per line — typed by ``type`` field (``view`` / ``action`` /
``error`` / ``vital``).

Auth model
----------
Datadog separates **client tokens** (safe to embed in the browser
snippet, write-only event ingest) from **API keys** (server-side,
read + write metric/log API). The RUM beacon endpoint takes the
client token via ``dd-api-key`` query param. We treat the public
client token as the ``dsn`` (lowercase ``ddrum_pub_…``) and the
optional server API key as ``api_key``.

Sites
-----
Datadog has multiple regional sites: ``datadoghq.com`` (US1),
``datadoghq.eu`` (EU), ``us3.datadoghq.com``, ``us5.datadoghq.com``,
``ap1.datadoghq.com``, ``ddog-gov.com``. The intake host is
constructed from ``site``.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

import httpx

from backend.observability.base import (
    ErrorEvent,
    InvalidRUMTokenError,
    MissingRUMScopeError,
    RUMAdapter,
    RUMError,
    RUMPayloadError,
    RUMRateLimitError,
    WebVital,
)

logger = logging.getLogger(__name__)

DEFAULT_DD_SITE = "datadoghq.com"


def _raise_for_datadog(resp: httpx.Response, provider: str = "datadog") -> None:
    if resp.status_code < 400:
        return
    try:
        body = resp.json()
        msg = body.get("errors") or body.get("message") or resp.text
        if isinstance(msg, list):
            msg = "; ".join(str(m) for m in msg)
    except Exception:
        msg = resp.text or "unknown error"
    if resp.status_code == 401:
        raise InvalidRUMTokenError(msg, status=401, provider=provider)
    if resp.status_code == 403:
        raise MissingRUMScopeError(msg, status=403, provider=provider)
    if resp.status_code == 400:
        raise RUMPayloadError(msg, status=400, provider=provider)
    if resp.status_code == 429:
        retry = int(resp.headers.get("Retry-After", "60"))
        raise RUMRateLimitError(msg, retry_after=retry, status=429, provider=provider)
    raise RUMError(msg, status=resp.status_code, provider=provider)


class DatadogRUMAdapter(RUMAdapter):
    """Datadog browser RUM adapter (``provider='datadog'``)."""

    provider = "datadog"

    def _configure(
        self,
        *,
        site: str = DEFAULT_DD_SITE,
        application_id: str = "",
        service: str = "",
        sdk_version: str = "5.21.0",
        intake_base: Optional[str] = None,
        **_: Any,
    ) -> None:
        if not application_id and not self._application_id:
            # ``application_id`` is the RUM application UUID — distinct
            # from the client token. Both required for the beacon.
            raise ValueError(
                "DatadogRUMAdapter requires 'application_id' "
                "(create the RUM Application in Datadog UI first)"
            )
        self._site = site
        if application_id:
            self._application_id = application_id
        self._service = service or "omnisight-web"
        self._sdk_version = sdk_version
        self._intake_base_override = intake_base

    # ── URL ──

    def _intake_url(self) -> str:
        if self._intake_base_override:
            return f"{self._intake_base_override.rstrip('/')}/api/v2/rum"
        # Region prefix: us1 → "datadoghq.com"; us3 → "us3.datadoghq.com";
        # the browser intake hostname follows the convention
        # ``browser-intake-<site>``.
        return f"https://browser-intake-{self._site}/api/v2/rum"

    def _intake_params(self) -> dict[str, str]:
        if not self._dsn:
            raise RUMError(
                "datadog adapter has no client token configured (dsn=)",
                status=400, provider=self.provider,
            )
        return {
            "dd-api-key": self._dsn,
            "ddsource": "browser",
            "dd-evp-origin": "browser",
            "dd-evp-origin-version": self._sdk_version,
            "dd-request-id": _new_request_id(),
        }

    # ── Event helpers ──

    def _common_event_fields(self) -> dict[str, Any]:
        return {
            "application": {"id": self._application_id},
            "session": {"type": "user"},
            "view": {"url": ""},
            "service": self._service,
            "version": self._release or "0.0.0",
            "env": self._environment,
            "source": "browser",
        }

    async def _post_events(self, events: list[dict[str, Any]]) -> None:
        body = "\n".join(
            json.dumps(e, separators=(",", ":"), sort_keys=True) for e in events
        ).encode("utf-8")
        url = self._intake_url()
        params = self._intake_params()
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            resp = await c.post(
                url, params=params,
                headers={
                    "Content-Type": "text/plain;charset=UTF-8",
                    "Accept": "application/json",
                },
                content=body,
            )
        _raise_for_datadog(resp, provider=self.provider)

    # ── Public API ──

    async def send_vital(self, vital: WebVital) -> None:
        if not self._should_sample():
            return
        ts_ns = int((vital.timestamp or time.time()) * 1_000_000_000)
        # CLS is unitless; LCP/INP/TTFB/FCP are ms — Datadog stores RUM
        # vitals on the ``view`` event under per-name attributes.
        event = self._common_event_fields()
        event.update({
            "type": "view",
            "date": ts_ns // 1_000_000,  # ms epoch
            "view": {
                "id": _new_request_id(),
                "url": _full_page_url(vital.page),
                "url_path": vital.page,
                "referrer": "",
                "name": vital.page,
                f"{vital.name.lower()}": vital.value,
                f"{vital.name.lower()}_rating": vital.rating,
            },
            "session": {
                "id": vital.session_id or _new_request_id(),
                "type": "user",
            },
            "vital": {
                "name": vital.name,
                "value": vital.value,
                "rating": vital.rating,
                "nav_type": vital.nav_type,
            },
            "user_agent": vital.user_agent,
            "locale": vital.locale,
        })
        await self._post_events([event])

    async def send_error(self, event_in: ErrorEvent) -> None:
        ts_ns = int((event_in.timestamp or time.time()) * 1_000_000_000)
        event = self._common_event_fields()
        event.update({
            "type": "error",
            "date": ts_ns // 1_000_000,
            "view": {
                "url": _full_page_url(event_in.page),
                "url_path": event_in.page,
                "name": event_in.page,
            },
            "session": {
                "id": event_in.session_id or _new_request_id(),
                "type": "user",
            },
            "error": {
                "message": event_in.message,
                "stack": event_in.stack,
                "source": "source",
                "fingerprint": event_in.fingerprint,
                "type": "Error",
                "handling": "unhandled",
            },
            "version": event_in.release or self._release or "0.0.0",
            "env": event_in.environment or self._environment,
            "user_agent": event_in.user_agent,
        })
        await self._post_events([event])

    # ── Browser snippet ──

    def browser_snippet(self, *, include_dsn: bool = True) -> str:
        """Return JS that initialises ``@datadog/browser-rum``.

        Like Sentry's snippet, the client token is **safe** to embed —
        it's a write-only ingest credential designed to ride along in
        page source.
        """
        client_token = "process.env.DD_CLIENT_TOKEN"
        if include_dsn and self._dsn:
            client_token = json.dumps(self._dsn)
        return (
            f'import {{ datadogRum }} from "@datadog/browser-rum";\n'
            f'import {{ onLCP, onINP, onCLS, onTTFB, onFCP }} from "web-vitals";\n'
            f'datadogRum.init({{\n'
            f'  applicationId: {json.dumps(self._application_id)},\n'
            f'  clientToken: {client_token},\n'
            f'  site: {json.dumps(self._site)},\n'
            f'  service: {json.dumps(self._service)},\n'
            f'  env: {json.dumps(self._environment)},\n'
            f'  version: {json.dumps(self._release or "0.0.0")},\n'
            f'  sessionSampleRate: {self._sample_rate * 100:.0f},\n'
            f'  trackUserInteractions: true,\n'
            f'  trackResources: true,\n'
            f'  trackLongTasks: true,\n'
            f'  defaultPrivacyLevel: "mask-user-input",\n'
            f'}});\n'
            f'function reportVital(metric) {{\n'
            f'  datadogRum.addAction("web-vital", '
            f'{{ name: metric.name, value: metric.value, rating: metric.rating }});\n'
            f'  navigator.sendBeacon("/api/v1/rum/vitals", JSON.stringify({{\n'
            f'    name: metric.name, value: metric.value, page: location.pathname,\n'
            f'    rating: metric.rating, navType: metric.navigationType\n'
            f'  }}));\n'
            f'}}\n'
            f'onLCP(reportVital); onINP(reportVital); onCLS(reportVital);\n'
            f'onTTFB(reportVital); onFCP(reportVital);\n'
        )


def _new_request_id() -> str:
    import uuid
    return str(uuid.uuid4())


def _full_page_url(path: str) -> str:
    if not path:
        return "https://app/"
    if path.startswith(("http://", "https://")):
        return path
    return f"https://app{path if path.startswith('/') else '/' + path}"


__all__ = ["DatadogRUMAdapter"]
