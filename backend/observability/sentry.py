"""W10 #284 — Sentry RUM adapter.

Sentry's browser SDK posts events to the project's "envelope" endpoint:

    POST https://o<org>.ingest.sentry.io/api/<project>/envelope/

Each envelope is a newline-delimited JSON document — first line a header
identifying the event type (``transaction`` / ``event``), then the
event body. The API authenticates via a ``sentry_key`` query parameter
(the public part of the DSN — safe to ship to the browser).

DSN parsing
-----------
A Sentry DSN looks like
``https://<public_key>@o<org>.ingest.sentry.io/<project>``. We split it
into ``public_key`` / ``host`` / ``project_id`` lazily on first use so
construction doesn't fail when the operator just wants to render the
browser snippet.

Web Vitals
----------
Sentry models Web Vitals as ``measurements`` on a ``transaction`` event
(``measurements.lcp`` / ``measurements.cls`` / ``measurements.inp`` …).
The adapter wraps each vital in a minimal transaction envelope so the
vendor UI shows it under the project's Performance tab.

Errors
------
Errors POST as a standard ``event`` envelope with ``exception.values``.
The fingerprint is sent verbatim so Sentry's own grouping respects the
caller's dedup choice.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional
from urllib.parse import urlparse

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


def _raise_for_sentry(resp: httpx.Response, provider: str = "sentry") -> None:
    """Map Sentry error responses to typed exceptions."""
    if resp.status_code < 400:
        return
    try:
        body = resp.json()
        msg = body.get("detail") or body.get("error") or resp.text
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


class SentryRUMAdapter(RUMAdapter):
    """Sentry browser RUM adapter (``provider='sentry'``)."""

    provider = "sentry"

    def _configure(
        self,
        *,
        sdk_version: str = "8.0.0",
        traces_sample_rate: float = 0.1,
        replays_sample_rate: float = 0.0,
        ingest_base: Optional[str] = None,
        **_: Any,
    ) -> None:
        self._sdk_version = sdk_version
        self._traces_sample_rate = traces_sample_rate
        self._replays_sample_rate = replays_sample_rate
        # ``ingest_base`` exists so tests / on-prem Sentry installs can
        # override the host derived from the DSN.
        self._ingest_base_override = ingest_base

    # ── DSN parsing ──

    def _dsn_parts(self) -> tuple[str, str, str]:
        """Return (public_key, ingest_base, project_id).

        Raises ``RUMError`` when the DSN is missing or malformed — but
        defers the failure to the first network use so callers can call
        ``browser_snippet()`` for ``include_dsn=False`` without a DSN
        being mandatory.
        """
        if not self._dsn:
            raise RUMError("sentry adapter has no DSN configured",
                           status=400, provider=self.provider)
        parsed = urlparse(self._dsn)
        if parsed.scheme not in ("http", "https"):
            raise RUMError(
                f"invalid Sentry DSN scheme: {parsed.scheme!r}",
                status=400, provider=self.provider,
            )
        public_key = parsed.username or ""
        if not public_key:
            raise RUMError(
                "Sentry DSN missing public key (expected https://<key>@host/project)",
                status=400, provider=self.provider,
            )
        host = parsed.hostname or ""
        if not host:
            raise RUMError(
                "Sentry DSN missing host",
                status=400, provider=self.provider,
            )
        project_id = parsed.path.lstrip("/").split("/")[0]
        if not project_id:
            raise RUMError(
                "Sentry DSN missing project id",
                status=400, provider=self.provider,
            )
        port = f":{parsed.port}" if parsed.port else ""
        ingest_base = self._ingest_base_override or f"{parsed.scheme}://{host}{port}"
        return public_key, ingest_base.rstrip("/"), project_id

    # ── Envelope helpers ──

    def _envelope_url(self) -> tuple[str, dict[str, str]]:
        public_key, ingest_base, project_id = self._dsn_parts()
        url = f"{ingest_base}/api/{project_id}/envelope/"
        params = {
            "sentry_key": public_key,
            "sentry_version": "7",
            "sentry_client": f"omnisight-rum/{self._sdk_version}",
        }
        return url, params

    def _envelope_headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/x-sentry-envelope",
            "Accept": "application/json",
        }

    def _build_envelope(self, items: list[tuple[dict[str, Any], dict[str, Any]]]) -> bytes:
        """Build a Sentry NDJSON envelope.

        ``items`` is a list of ``(item_header, item_body)`` pairs. Each
        item is serialised as two lines: header JSON, then body JSON.
        The envelope header (one line at the top) carries event_id +
        sent_at metadata.
        """
        env_header = {
            "event_id": _new_event_id(),
            "sent_at": _iso_now(),
            "dsn": self._dsn,
        }
        lines: list[bytes] = [_dump_jsonl(env_header)]
        for item_header, item_body in items:
            body_bytes = _dump_jsonl(item_body)
            header = dict(item_header)
            # Sentry requires ``length`` to match the body byte count.
            header["length"] = len(body_bytes) - 1  # exclude trailing \n
            lines.append(_dump_jsonl(header))
            lines.append(body_bytes)
        return b"".join(lines)

    async def _post_envelope(self, body: bytes) -> None:
        url, params = self._envelope_url()
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            resp = await c.post(
                url, params=params, headers=self._envelope_headers(), content=body,
            )
        _raise_for_sentry(resp, provider=self.provider)

    # ── Public API ──

    async def send_vital(self, vital: WebVital) -> None:
        if not self._should_sample():
            return
        # Sentry requires ms-based timestamps in float-seconds.
        ts = vital.timestamp or time.time()
        # The CWV is conveyed as ``measurements.<name>``; LCP/INP/TTFB/FCP
        # use unit "millisecond", CLS uses "none".
        unit = "none" if vital.name == "CLS" else "millisecond"
        transaction_event = {
            "type": "transaction",
            "event_id": _new_event_id(),
            "transaction": vital.page or "/",
            "transaction_info": {"source": "url"},
            "timestamp": ts,
            "start_timestamp": ts,
            "platform": "javascript",
            "release": self._release,
            "environment": self._environment,
            "user": {"id": vital.session_id} if vital.session_id else {},
            "tags": {
                "vital.name": vital.name,
                "vital.rating": vital.rating,
                "vital.nav_type": vital.nav_type,
                "vital.locale": vital.locale,
            },
            "measurements": {
                vital.name.lower(): {"value": vital.value, "unit": unit},
            },
            "contexts": {
                "browser": {"name": "user-agent",
                            "user_agent": vital.user_agent},
            },
        }
        envelope = self._build_envelope([(
            {"type": "transaction"},
            transaction_event,
        )])
        await self._post_envelope(envelope)

    async def send_error(self, event: ErrorEvent) -> None:
        # Errors are NEVER sampled.
        ts = event.timestamp or time.time()
        body = {
            "event_id": _new_event_id(),
            "timestamp": ts,
            "platform": "javascript",
            "level": event.level,
            "release": event.release or self._release,
            "environment": event.environment or self._environment,
            "logger": "omnisight.rum",
            "fingerprint": [event.fingerprint] if event.fingerprint else [event.message],
            "transaction": event.page,
            "user": {"id": event.session_id} if event.session_id else {},
            "exception": {
                "values": [{
                    "type": "Error",
                    "value": event.message,
                    "stacktrace": _stack_to_sentry(event.stack),
                }],
            },
            "contexts": {
                "browser": {"name": "user-agent",
                            "user_agent": event.user_agent},
            },
            "tags": {
                "page": event.page,
                "level": event.level,
            },
        }
        envelope = self._build_envelope([(
            {"type": "event"},
            body,
        )])
        await self._post_envelope(envelope)

    # ── Browser snippet ──

    def browser_snippet(self, *, include_dsn: bool = True) -> str:
        """Return the JS snippet that loads the Sentry Browser SDK.

        ``include_dsn=True`` (default) bakes the DSN public key into the
        snippet — this is **safe** because the public key is designed to
        be exposed to the browser. ``include_dsn=False`` lets the
        scaffold inject the DSN later via env var.
        """
        dsn_literal = "process.env.SENTRY_DSN"
        if include_dsn and self._dsn:
            dsn_literal = json.dumps(self._dsn)
        traces = self._traces_sample_rate
        replays = self._replays_sample_rate
        env = json.dumps(self._environment)
        release = json.dumps(self._release or "")
        return (
            f'import * as Sentry from "@sentry/browser";\n'
            f'import {{ onLCP, onINP, onCLS, onTTFB, onFCP }} from "web-vitals";\n'
            f'Sentry.init({{\n'
            f'  dsn: {dsn_literal},\n'
            f'  environment: {env},\n'
            f'  release: {release},\n'
            f'  tracesSampleRate: {traces},\n'
            f'  replaysSessionSampleRate: {replays},\n'
            f'}});\n'
            f'function reportVital(metric) {{\n'
            f'  Sentry.setMeasurement(metric.name, metric.value, '
            f'metric.name === "CLS" ? "none" : "millisecond");\n'
            f'  navigator.sendBeacon("/api/v1/rum/vitals", JSON.stringify({{\n'
            f'    name: metric.name, value: metric.value, page: location.pathname,\n'
            f'    rating: metric.rating, navType: metric.navigationType\n'
            f'  }}));\n'
            f'}}\n'
            f'onLCP(reportVital); onINP(reportVital); onCLS(reportVital);\n'
            f'onTTFB(reportVital); onFCP(reportVital);\n'
        )


def _stack_to_sentry(stack: str) -> dict[str, Any]:
    """Convert a raw stack-trace string into Sentry's stacktrace shape.

    The Sentry parser is forgiving — when we can't parse line/col,
    we ship a single frame with the raw line as ``filename`` so the
    UI still shows something useful.
    """
    if not stack:
        return {"frames": []}
    frames: list[dict[str, Any]] = []
    for line in reversed(stack.splitlines()):  # Sentry wants oldest-first
        line = line.strip()
        if not line:
            continue
        frames.append({"filename": line, "in_app": True})
    return {"frames": frames}


def _new_event_id() -> str:
    """32-char hex id (Sentry requires 32 chars without dashes)."""
    import uuid
    return uuid.uuid4().hex


def _iso_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _dump_jsonl(obj: dict[str, Any]) -> bytes:
    """JSON-encode + trailing newline, in deterministic key order."""
    return (json.dumps(obj, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")


__all__ = ["SentryRUMAdapter"]
