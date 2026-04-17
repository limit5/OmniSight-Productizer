"""P10 #295 — Sentry Mobile adapter.

Sentry's mobile SDKs (sentry-android / sentry-cocoa / sentry-flutter /
@sentry/react-native) speak the same envelope protocol as the browser
SDK — POST to ``/api/<project>/envelope/`` with NDJSON, authed via
``sentry_key`` query parameter (the public DSN key).

This adapter mirrors ``backend.observability.sentry`` but ships the
mobile-specific envelope items:

    * ``event`` (with ``platform: "android" | "cocoa" | "javascript"
      | "dart"``) — crashes go here.
    * ``event`` with custom ``tags.hang_kind`` — ANRs / watchdog
      terminations go here. Sentry doesn't have a separate "anr"
      envelope type; the convention is a normal ``event`` with extra
      tags so the search-by-tag UI buckets them.
    * ``transaction`` with ``measurements`` — render metrics go here,
      same shape as the W10 web vitals adapter (``measurements.fcp``
      / ``measurements.frame_draw`` / ``measurements.hang``).

The DSN is parsed lazily so callers can request a native init snippet
without configuring a DSN.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from backend.mobile_observability.base import (
    HangEvent,
    InvalidMobileTokenError,
    MissingMobileScopeError,
    MobileCrash,
    MobileObservabilityAdapter,
    MobileObservabilityError,
    MobilePayloadError,
    MobileRateLimitError,
    RenderMetric,
)

logger = logging.getLogger(__name__)


def _raise_for_sentry(resp: httpx.Response, provider: str = "sentry-mobile") -> None:
    if resp.status_code < 400:
        return
    try:
        body = resp.json()
        msg = body.get("detail") or body.get("error") or resp.text
    except Exception:
        msg = resp.text or "unknown error"
    if resp.status_code == 401:
        raise InvalidMobileTokenError(msg, status=401, provider=provider)
    if resp.status_code == 403:
        raise MissingMobileScopeError(msg, status=403, provider=provider)
    if resp.status_code == 400:
        raise MobilePayloadError(msg, status=400, provider=provider)
    if resp.status_code == 429:
        retry = int(resp.headers.get("Retry-After", "60"))
        raise MobileRateLimitError(msg, retry_after=retry, status=429, provider=provider)
    raise MobileObservabilityError(msg, status=resp.status_code, provider=provider)


_PLATFORM_TAG = {
    "android": "android",
    "ios": "cocoa",
    "flutter": "dart",
    "react-native": "javascript",
}


class SentryMobileAdapter(MobileObservabilityAdapter):
    """Sentry Mobile adapter (``provider='sentry-mobile'``)."""

    provider = "sentry-mobile"

    def _configure(
        self,
        *,
        sdk_version: str = "8.0.0",
        traces_sample_rate: float = 0.1,
        ingest_base: Optional[str] = None,
        **_: Any,
    ) -> None:
        self._sdk_version = sdk_version
        self._traces_sample_rate = traces_sample_rate
        self._ingest_base_override = ingest_base

    # ── DSN parsing ──

    def _dsn_parts(self) -> tuple[str, str, str]:
        if not self._dsn:
            raise MobileObservabilityError(
                "sentry-mobile adapter has no DSN configured",
                status=400, provider=self.provider,
            )
        parsed = urlparse(self._dsn)
        if parsed.scheme not in ("http", "https"):
            raise MobileObservabilityError(
                f"invalid Sentry DSN scheme: {parsed.scheme!r}",
                status=400, provider=self.provider,
            )
        public_key = parsed.username or ""
        if not public_key:
            raise MobileObservabilityError(
                "Sentry DSN missing public key",
                status=400, provider=self.provider,
            )
        host = parsed.hostname or ""
        if not host:
            raise MobileObservabilityError(
                "Sentry DSN missing host",
                status=400, provider=self.provider,
            )
        project_id = parsed.path.lstrip("/").split("/")[0]
        if not project_id:
            raise MobileObservabilityError(
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
            "sentry_client": f"omnisight-mobile/{self._sdk_version}",
        }
        return url, params

    def _envelope_headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/x-sentry-envelope",
            "Accept": "application/json",
        }

    def _build_envelope(self, items: list[tuple[dict[str, Any], dict[str, Any]]]) -> bytes:
        env_header = {
            "event_id": _new_event_id(),
            "sent_at": _iso_now(),
            "dsn": self._dsn,
        }
        lines: list[bytes] = [_dump_jsonl(env_header)]
        for item_header, item_body in items:
            body_bytes = _dump_jsonl(item_body)
            header = dict(item_header)
            header["length"] = len(body_bytes) - 1
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

    def _platform_tag(self, platform: str) -> str:
        return _PLATFORM_TAG.get(platform, platform or "android")

    # ── Public API ──

    async def send_crash(self, crash: MobileCrash) -> None:
        ts = crash.timestamp or time.time()
        body = {
            "event_id": _new_event_id(),
            "timestamp": ts,
            "platform": self._platform_tag(crash.platform),
            "level": "fatal" if crash.fatal else "error",
            "release": crash.app_version or self._release,
            "environment": self._environment,
            "logger": "omnisight.mobile",
            "fingerprint": [crash.fingerprint] if crash.fingerprint else [crash.message],
            "user": ({"id": crash.user_id} if crash.user_id else
                     {"id": crash.session_id} if crash.session_id else {}),
            "exception": {
                "values": [{
                    "type": crash.signal or "Crash",
                    "value": crash.message,
                    "stacktrace": _stack_to_sentry(crash.stacktrace),
                }],
            },
            "contexts": {
                "device": {
                    "model": crash.device_model,
                },
                "os": {
                    "name": crash.platform,
                    "version": crash.os_version,
                },
                "app": {
                    "app_version": crash.app_version,
                },
            },
            "tags": {
                "platform": crash.platform,
                "fatal": str(crash.fatal).lower(),
            },
            "breadcrumbs": list(crash.breadcrumbs),
        }
        envelope = self._build_envelope([({"type": "event"}, body)])
        await self._post_envelope(envelope)

    async def send_hang(self, hang: HangEvent) -> None:
        if hang.severity != "critical" and not self._should_sample():
            return
        ts = hang.timestamp or time.time()
        body = {
            "event_id": _new_event_id(),
            "timestamp": ts,
            "platform": self._platform_tag(hang.platform),
            "level": "fatal" if hang.kind == "watchdog_termination" else "warning",
            "release": hang.app_version or self._release,
            "environment": self._environment,
            "logger": "omnisight.mobile.hang",
            "fingerprint": [hang.fingerprint],
            "message": {
                "formatted": (
                    f"{hang.kind} on {hang.platform}: "
                    f"main thread blocked for {int(hang.duration_ms)}ms"
                ),
            },
            "exception": {
                "values": [{
                    "type": hang.kind.upper(),
                    "value": (
                        f"main thread blocked for {int(hang.duration_ms)}ms"
                    ),
                    "stacktrace": _stack_to_sentry(hang.main_thread_stack),
                }],
            },
            "contexts": {
                "device": {"model": hang.device_model},
                "os": {"name": hang.platform, "version": hang.os_version},
            },
            "tags": {
                "hang_kind": hang.kind,
                "hang_severity": hang.severity,
                "platform": hang.platform,
                "in_foreground": str(hang.in_foreground).lower(),
            },
        }
        envelope = self._build_envelope([({"type": "event"}, body)])
        await self._post_envelope(envelope)

    async def send_render(self, metric: RenderMetric) -> None:
        if not self._should_sample():
            return
        ts = metric.timestamp or time.time()
        unit = "millisecond"
        transaction_event = {
            "type": "transaction",
            "event_id": _new_event_id(),
            "transaction": metric.screen or "/",
            "transaction_info": {"source": "view"},
            "timestamp": ts,
            "start_timestamp": ts,
            "platform": self._platform_tag(metric.platform),
            "release": metric.app_version or self._release,
            "environment": self._environment,
            "user": {"id": metric.session_id} if metric.session_id else {},
            "tags": {
                "metric.name": metric.name,
                "metric.rating": metric.rating,
                "platform": metric.platform,
            },
            "measurements": {
                metric.name: {"value": metric.value, "unit": unit},
            },
            "contexts": {
                "device": {"model": metric.device_model},
            },
        }
        envelope = self._build_envelope([
            ({"type": "transaction"}, transaction_event),
        ])
        await self._post_envelope(envelope)

    # ── Native init snippets ──

    def native_snippet(self, platform: str) -> str:
        platform = (platform or "").lower()
        dsn_lit = json.dumps(self._dsn or "")
        if platform == "android":
            return (
                "// Sentry Mobile (P10 #295) — Android.\n"
                "// Drop into Application.onCreate(). The SDK auto-collects\n"
                "// crashes, ANRs (>= 5 s default), and frame drops via\n"
                "// AndroidX Tracing.\n"
                "import io.sentry.android.core.SentryAndroid\n"
                "SentryAndroid.init(this) { options ->\n"
                f"    options.dsn = {dsn_lit}\n"
                f"    options.environment = \"{self._environment}\"\n"
                f"    options.release = \"{self._release or ''}\"\n"
                f"    options.tracesSampleRate = {self._traces_sample_rate}\n"
                "    options.isAnrEnabled = true\n"
                "    options.anrTimeoutIntervalMillis = 5000\n"
                "    options.isEnableAutoActivityLifecycleTracing = true\n"
                "    options.isEnableFramesTracking = true\n"
                "}\n"
            )
        if platform == "ios":
            return (
                "// Sentry Mobile (P10 #295) — iOS.\n"
                "// Drop into AppDelegate application(_:didFinishLaunchingWithOptions:).\n"
                "// Watchdog terminations come through MetricKit; the SDK\n"
                "// auto-subscribes when ``enableWatchdogTerminationTracking``\n"
                "// is true (default).\n"
                "import Sentry\n"
                "SentrySDK.start { options in\n"
                f"    options.dsn = {dsn_lit}\n"
                f"    options.environment = \"{self._environment}\"\n"
                f"    options.releaseName = \"{self._release or ''}\"\n"
                f"    options.tracesSampleRate = {self._traces_sample_rate}\n"
                "    options.enableAutoPerformanceTracing = true\n"
                "    options.enableWatchdogTerminationTracking = true\n"
                "    options.enableAppHangTracking = true\n"
                "    options.appHangTimeoutInterval = 2.0\n"
                "}\n"
            )
        if platform == "flutter":
            return (
                "// Sentry Mobile (P10 #295) — Flutter.\n"
                "// Wrap runApp in SentryFlutter.init.\n"
                "import 'package:sentry_flutter/sentry_flutter.dart';\n"
                "await SentryFlutter.init((options) {\n"
                f"  options.dsn = {dsn_lit};\n"
                f"  options.environment = '{self._environment}';\n"
                f"  options.release = '{self._release or ''}';\n"
                f"  options.tracesSampleRate = {self._traces_sample_rate};\n"
                "  options.attachScreenshot = false;\n"
                "  options.enableAutoPerformanceTracing = true;\n"
                "});\n"
            )
        if platform == "react-native":
            return (
                "// Sentry Mobile (P10 #295) — React Native.\n"
                "// Drop into index.js before AppRegistry.registerComponent.\n"
                "import * as Sentry from '@sentry/react-native';\n"
                "Sentry.init({\n"
                f"  dsn: {dsn_lit},\n"
                f"  environment: '{self._environment}',\n"
                f"  release: '{self._release or ''}',\n"
                f"  tracesSampleRate: {self._traces_sample_rate},\n"
                "  enableAutoPerformanceTracing: true,\n"
                "  enableNativeCrashHandling: true,\n"
                "});\n"
            )
        raise ValueError(
            f"unknown platform {platform!r}; "
            f"expected android / ios / flutter / react-native"
        )


def _stack_to_sentry(stack: str) -> dict[str, Any]:
    if not stack:
        return {"frames": []}
    frames: list[dict[str, Any]] = []
    for line in reversed(stack.splitlines()):
        line = line.strip()
        if not line:
            continue
        frames.append({"filename": line, "in_app": True})
    return {"frames": frames}


def _new_event_id() -> str:
    import uuid
    return uuid.uuid4().hex


def _iso_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _dump_jsonl(obj: dict[str, Any]) -> bytes:
    return (json.dumps(obj, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")


__all__ = ["SentryMobileAdapter"]
