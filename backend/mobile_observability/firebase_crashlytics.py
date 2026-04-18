"""P10 #295 — Firebase Crashlytics adapter.

Firebase Crashlytics ingests crash / non-fatal exception / log records
through the Reporting API:

    POST https://firebasecrashlyticsreports-pa.googleapis.com/v1/reports

The on-device SDK normally batches and uploads these envelopes itself.
For server-side relay (testing, custom event injection from the
backend, Crashlytics-as-a-vendor for non-Firebase apps), this adapter
posts the same envelope shape using a Google service-account access
token (or, in self-hosted dev, a Firebase Cloud Messaging server key).

Two notes about the wire format:

    * Crashlytics envelopes are ND-JSON: top-level metadata followed
      by per-thread frames. We assemble that here so the adapter can
      be unit-tested without pulling in the closed-source Firebase
      Android SDK.
    * Render metrics route to Firebase Performance Monitoring rather
      than Crashlytics — the vendor splits crash data from perf data
      on different endpoints. The adapter abstracts this so the caller
      sees one ``send_render()`` method.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

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

CRASHLYTICS_INGEST = (
    "https://firebasecrashlyticsreports-pa.googleapis.com/v1/reports"
)
PERF_MONITORING_INGEST = (
    "https://firebaseperformance.googleapis.com/v1/projects/{project}/events:batchCreate"
)


def _raise_for_firebase(resp: httpx.Response, provider: str = "firebase-crashlytics") -> None:
    if resp.status_code < 400:
        return
    try:
        body = resp.json()
        msg = body.get("error", {}).get("message") or body.get("message") or resp.text
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


class FirebaseCrashlyticsAdapter(MobileObservabilityAdapter):
    """Firebase Crashlytics adapter (``provider='firebase-crashlytics'``)."""

    provider = "firebase-crashlytics"

    def _configure(
        self,
        *,
        project_id: Optional[str] = None,
        google_app_id_android: Optional[str] = None,
        google_app_id_ios: Optional[str] = None,
        crashlytics_endpoint: Optional[str] = None,
        perf_endpoint: Optional[str] = None,
        sdk_version: str = "19.0.0",
        **_: Any,
    ) -> None:
        self._project_id = project_id
        self._google_app_id_android = google_app_id_android
        self._google_app_id_ios = google_app_id_ios
        self._crashlytics_endpoint = crashlytics_endpoint or CRASHLYTICS_INGEST
        self._perf_endpoint = perf_endpoint
        self._sdk_version = sdk_version

    # ── Endpoint helpers ──

    def _crash_url(self) -> str:
        return self._crashlytics_endpoint

    def _perf_url(self) -> str:
        if self._perf_endpoint:
            return self._perf_endpoint
        if not self._project_id:
            raise MobileObservabilityError(
                "Firebase project_id required for Performance Monitoring",
                status=400, provider=self.provider,
            )
        return PERF_MONITORING_INGEST.format(project=self._project_id)

    def _auth_header(self) -> dict[str, str]:
        # The on-device SDK uses an unattended Google service-account
        # access token; ``api_key`` (server key) is the simpler stand-in
        # for backend / test paths.
        if not self._api_key:
            raise MobileObservabilityError(
                "Firebase Crashlytics adapter requires api_key (server access token)",
                status=400, provider=self.provider,
            )
        return {"Authorization": f"Bearer {self._api_key}"}

    def _google_app_id(self, platform: str) -> str:
        if platform == "ios":
            return self._google_app_id_ios or ""
        return self._google_app_id_android or ""

    # ── Wire-format helpers ──

    def _build_crash_envelope(self, crash: MobileCrash) -> dict[str, Any]:
        return {
            "google_app_id": self._google_app_id(crash.platform),
            "session_id": crash.session_id,
            "report": {
                "header": {
                    "bundle_short_version": crash.app_version,
                    "platform": crash.platform,
                    "os_version": crash.os_version,
                    "device_model": crash.device_model,
                    "is_root": False,
                },
                "session": {
                    "fatal": crash.fatal,
                    "started_at": int(crash.timestamp),
                },
                "exception": {
                    "type": crash.signal or "Throwable",
                    "reason": crash.message,
                    "frames": _split_stack(crash.stacktrace),
                },
                "fingerprint": crash.fingerprint,
                "user": {"user_id": crash.user_id} if crash.user_id else {},
                "breadcrumbs": list(crash.breadcrumbs),
            },
            "sdk_version": self._sdk_version,
            "environment": self._environment,
            "release": crash.app_version or self._release,
        }

    def _build_hang_envelope(self, hang: HangEvent) -> dict[str, Any]:
        # Crashlytics models ANR / watchdog termination as a non-fatal
        # crash with ``exception.type`` set to the hang kind so the
        # vendor UI buckets them under the dedicated ANR tab.
        return {
            "google_app_id": self._google_app_id(hang.platform),
            "session_id": hang.session_id,
            "report": {
                "header": {
                    "bundle_short_version": hang.app_version,
                    "platform": hang.platform,
                    "os_version": hang.os_version,
                    "device_model": hang.device_model,
                    "is_root": False,
                },
                "session": {
                    "fatal": hang.kind == "watchdog_termination",
                    "started_at": int(hang.timestamp),
                    "in_foreground": hang.in_foreground,
                },
                "exception": {
                    "type": hang.kind.upper(),
                    "reason": (
                        f"main thread blocked for {int(hang.duration_ms)}ms"
                    ),
                    "frames": _split_stack(hang.main_thread_stack),
                },
                "severity": hang.severity,
                "fingerprint": hang.fingerprint,
            },
            "sdk_version": self._sdk_version,
            "environment": self._environment,
            "release": hang.app_version or self._release,
        }

    def _build_render_envelope(self, metric: RenderMetric) -> dict[str, Any]:
        # Performance Monitoring v1 events:batchCreate body shape.
        return {
            "appInstance": {
                "googleAppId": self._google_app_id(metric.platform),
                "sessionId": metric.session_id,
            },
            "perfMetrics": [{
                "applicationProcessState": "FOREGROUND_BACKGROUND",
                "applicationInfo": {
                    "appVersion": metric.app_version or self._release or "",
                    "deviceModel": metric.device_model,
                },
                "screenTrace": {
                    "name": f"_st_{metric.screen}",
                    "duration_us": int(metric.value * 1000),
                    "counters": {
                        "frozen_frames" if metric.rating == "poor" else "slow_frames": 1,
                    },
                    "attributes": {
                        "metric_name": metric.name,
                        "rating": metric.rating,
                    },
                },
            }],
        }

    # ── Public API ──

    async def send_crash(self, crash: MobileCrash) -> None:
        body = self._build_crash_envelope(crash)
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            resp = await c.post(
                self._crash_url(),
                headers={**self._auth_header(),
                         "Content-Type": "application/json"},
                content=json.dumps(body),
            )
        _raise_for_firebase(resp, provider=self.provider)

    async def send_hang(self, hang: HangEvent) -> None:
        if hang.severity != "critical" and not self._should_sample():
            return
        body = self._build_hang_envelope(hang)
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            resp = await c.post(
                self._crash_url(),
                headers={**self._auth_header(),
                         "Content-Type": "application/json"},
                content=json.dumps(body),
            )
        _raise_for_firebase(resp, provider=self.provider)

    async def send_render(self, metric: RenderMetric) -> None:
        if not self._should_sample():
            return
        body = self._build_render_envelope(metric)
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            resp = await c.post(
                self._perf_url(),
                headers={**self._auth_header(),
                         "Content-Type": "application/json"},
                content=json.dumps(body),
            )
        _raise_for_firebase(resp, provider=self.provider)

    # ── Native init snippets ──

    def native_snippet(self, platform: str) -> str:
        platform = (platform or "").lower()
        if platform == "android":
            return self._android_snippet()
        if platform == "ios":
            return self._ios_snippet()
        if platform == "flutter":
            return self._flutter_snippet()
        if platform == "react-native":
            return self._react_native_snippet()
        raise ValueError(
            f"unknown platform {platform!r}; "
            f"expected android / ios / flutter / react-native"
        )

    def _android_snippet(self) -> str:
        return (
            "// Firebase Crashlytics + Performance Monitoring (P10 #295).\n"
            "// Drop into Application.onCreate() — the SDK auto-collects\n"
            "// crashes, ANRs (Android 11+), and slow-frame counts.\n"
            "import com.google.firebase.crashlytics.FirebaseCrashlytics\n"
            "import com.google.firebase.perf.FirebasePerformance\n"
            "FirebaseCrashlytics.getInstance().apply {\n"
            "    setCrashlyticsCollectionEnabled(true)\n"
            "    setCustomKey(\"environment\", \"" + self._environment + "\")\n"
            "}\n"
            "FirebasePerformance.getInstance().isPerformanceCollectionEnabled = true\n"
        )

    def _ios_snippet(self) -> str:
        return (
            "// Firebase Crashlytics + Performance Monitoring (P10 #295).\n"
            "// Drop into AppDelegate application(_:didFinishLaunchingWithOptions:)\n"
            "// — the SDK auto-collects crashes, watchdog terminations\n"
            "// (via MetricKit), and frame-drop counts.\n"
            "import FirebaseCore\n"
            "import FirebaseCrashlytics\n"
            "import FirebasePerformance\n"
            "FirebaseApp.configure()\n"
            "Crashlytics.crashlytics().setCrashlyticsCollectionEnabled(true)\n"
            "Crashlytics.crashlytics().setCustomValue(\""
            + self._environment + "\", forKey: \"environment\")\n"
            "Performance.sharedInstance().isInstrumentationEnabled = true\n"
            "Performance.sharedInstance().isDataCollectionEnabled = true\n"
        )

    def _flutter_snippet(self) -> str:
        return (
            "// Firebase Crashlytics + Performance Monitoring (P10 #295).\n"
            "// Drop into main() before runApp() — the SDK auto-routes\n"
            "// uncaught Flutter / native errors to Crashlytics.\n"
            "import 'package:firebase_core/firebase_core.dart';\n"
            "import 'package:firebase_crashlytics/firebase_crashlytics.dart';\n"
            "import 'package:firebase_performance/firebase_performance.dart';\n"
            "await Firebase.initializeApp();\n"
            "FlutterError.onError = FirebaseCrashlytics.instance.recordFlutterFatalError;\n"
            "PlatformDispatcher.instance.onError = (error, stack) {\n"
            "  FirebaseCrashlytics.instance.recordError(error, stack, fatal: true);\n"
            "  return true;\n"
            "};\n"
            "await FirebaseCrashlytics.instance.setCrashlyticsCollectionEnabled(true);\n"
            "await FirebasePerformance.instance.setPerformanceCollectionEnabled(true);\n"
        )

    def _react_native_snippet(self) -> str:
        return (
            "// Firebase Crashlytics + Performance Monitoring (P10 #295).\n"
            "// Drop into index.js before AppRegistry.registerComponent.\n"
            "import crashlytics from '@react-native-firebase/crashlytics';\n"
            "import perf from '@react-native-firebase/perf';\n"
            "crashlytics().setCrashlyticsCollectionEnabled(true);\n"
            "crashlytics().setAttribute('environment', '" + self._environment + "');\n"
            "perf().setPerformanceCollectionEnabled(true);\n"
        )


def _split_stack(stack: str) -> list[dict[str, Any]]:
    """Split a multi-line stacktrace into Crashlytics frames.

    The vendor frame shape is forgiving — when the line can't be
    parsed, we ship the whole line as ``symbol`` so the UI shows it
    without dropping the frame.
    """
    if not stack:
        return []
    frames: list[dict[str, Any]] = []
    for line in stack.splitlines():
        line = line.strip()
        if not line:
            continue
        frames.append({
            "symbol": line,
            "file": "",
            "line": 0,
            "in_app": True,
        })
    return frames


__all__ = ["FirebaseCrashlyticsAdapter", "CRASHLYTICS_INGEST", "PERF_MONITORING_INGEST"]
