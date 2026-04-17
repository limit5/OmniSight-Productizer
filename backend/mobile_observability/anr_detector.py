"""P10 #295 — Android ANR detector + iOS watchdog-termination classifier.

Three responsibilities:

    1. **Android Kotlin snippet** — generate the ``ANRWatchDog`` /
       SDK-init code to drop into ``Application.onCreate()``. The
       snippet wires the chosen vendor (Crashlytics / Sentry-Mobile)
       so any main-thread block longer than ``threshold_ms`` becomes
       a ``HangEvent`` reported to the backend.

    2. **iOS Swift snippet** — generate the ``MetricKit`` subscription
       and watchdog-termination payload-handling glue. Watchdog kills
       are surfaced via ``MXMetricPayload.applicationLaunchMetrics``
       and ``MXDiagnosticPayload.crashDiagnostics`` (signal 0x9 /
       SIGKILL with reason ``Watchdog``).

    3. **Server-side classifier** — given a raw block-duration sample
       (from a custom telemetry pipe, not the vendor SDK), decide
       whether the sample crosses the ANR / watchdog severity threshold
       and produce the ``HangEvent`` for the upstream router.

Why a server-side classifier?

The vendor SDK already classifies on-device, but the backend needs to
re-classify when:
  * Operators shrink the threshold below the vendor default (Sentry's
    ``appHangTimeoutInterval`` defaults to 2s; some teams want 1s).
  * A custom in-house telemetry pipe (proprietary radio firmware,
    field-trial debug dumps) emits raw frame-budget samples.
  * A Flutter / RN app reports a JS-thread block that is NOT an ANR
    (the ANR threshold applies to the platform main thread, not the
    JS isolate); we re-bucket those as warnings.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from backend.mobile_observability.base import HangEvent

logger = logging.getLogger(__name__)


# ── Default thresholds ──────────────────────────────────────────
#
# Numbers come from each platform's published guidance:
#   * Android ANR floor       — 5000 ms (system AppErrors broadcast)
#   * Android Play Vitals     — 2000 ms (warning)
#   * iOS hang warning        — 250 ms
#   * iOS hang fatal          — 1000 ms
#   * iOS launch-watchdog     — 20 s (system kills the process)

DEFAULT_ANR_WARNING_MS = 5_000.0
DEFAULT_ANR_CRITICAL_MS = 10_000.0

DEFAULT_HANG_WARNING_MS = 250.0
DEFAULT_HANG_CRITICAL_MS = 1_000.0


@dataclass
class ANRDetectorConfig:
    """Operator-tunable detector settings.

    ``warning_ms`` and ``critical_ms`` map onto the same severity
    strings the ``HangEvent.severity`` property returns, so the
    classifier and the model agree.
    """

    platform: str = "android"
    warning_ms: float = DEFAULT_ANR_WARNING_MS
    critical_ms: float = DEFAULT_ANR_CRITICAL_MS

    def __post_init__(self) -> None:
        self.platform = (self.platform or "android").lower()
        if self.platform not in ("android", "ios", "flutter", "react-native"):
            raise ValueError(f"unknown platform {self.platform!r}")
        if self.warning_ms < 0 or self.critical_ms < 0:
            raise ValueError("warning/critical must be >= 0")
        if self.warning_ms > self.critical_ms:
            raise ValueError(
                "warning_ms must be <= critical_ms; got "
                f"warning={self.warning_ms}, critical={self.critical_ms}"
            )


class ANRDetector:
    """Server-side classifier for ANR / hang / watchdog samples.

    Stateless — instances are cheap; create one per detector config.
    """

    def __init__(self, config: ANRDetectorConfig) -> None:
        self.config = config

    def classify(
        self,
        *,
        duration_ms: float,
        in_foreground: bool = True,
    ) -> str:
        """Return ``ignored`` / ``info`` / ``warning`` / ``critical``."""
        if duration_ms < 0:
            return "ignored"
        if not in_foreground:
            # Background ANR is suppressed by Play Vitals (per Google guidance).
            return "ignored"
        if duration_ms >= self.config.critical_ms:
            return "critical"
        if duration_ms >= self.config.warning_ms:
            return "warning"
        return "info"

    def to_event(
        self,
        *,
        duration_ms: float,
        kind: Optional[str] = None,
        main_thread_stack: str = "",
        in_foreground: bool = True,
        app_version: str = "",
        os_version: str = "",
        device_model: str = "",
        session_id: str = "",
    ) -> Optional[HangEvent]:
        """Build a ``HangEvent`` if the sample crosses ``warning_ms``.

        Returns ``None`` for samples below the warning threshold (info
        or ignored). Samples below threshold are dropped by design so
        operators don't pay the upstream vendor's per-event quota for
        sub-threshold noise.
        """
        verdict = self.classify(duration_ms=duration_ms, in_foreground=in_foreground)
        if verdict in ("ignored", "info"):
            return None
        # Default ``kind`` flips on platform: Android → anr, iOS → watchdog.
        if kind is None:
            kind = "anr" if self.config.platform == "android" else "watchdog_termination"
        return HangEvent(
            duration_ms=duration_ms,
            platform=self.config.platform,
            kind=kind,
            main_thread_stack=main_thread_stack,
            app_version=app_version,
            os_version=os_version,
            device_model=device_model,
            session_id=session_id,
            in_foreground=in_foreground,
        )


# ── Native init snippets ────────────────────────────────────────


def android_anr_snippet(*, threshold_ms: int = 5_000) -> str:
    """Kotlin snippet for ``Application.onCreate()`` — installs an
    ``ANRWatchDog`` instance (https://github.com/SalomonBrys/ANR-WatchDog)
    that detects main-thread blocks and reports them via the vendor
    bridge wired by the chosen observability adapter.

    The snippet is intentionally vendor-neutral — the report side is
    delegated to the adapter's ``native_snippet("android")`` so
    operators can swap providers without rewriting the detector.
    """
    return (
        "// ANR detector (P10 #295) — drop into Application.onCreate().\n"
        "// Pair with the vendor adapter snippet (Sentry-Android already\n"
        "// has its own ANR; only install this if you need a custom\n"
        "// threshold or a non-Sentry vendor).\n"
        "import com.github.anrwatchdog.ANRWatchDog\n"
        f"ANRWatchDog({threshold_ms})\n"
        "    .setReportMainThreadOnly()\n"
        "    .setIgnoreDebugger(true)\n"
        "    .setANRListener { error ->\n"
        "        // Forward to your installed adapter — Crashlytics:\n"
        "        //   FirebaseCrashlytics.getInstance().recordException(error)\n"
        "        // Sentry:\n"
        "        //   Sentry.captureException(error)\n"
        "    }\n"
        "    .start()\n"
    )


def ios_watchdog_snippet() -> str:
    """Swift snippet for ``AppDelegate`` — subscribes to ``MetricKit``
    so iOS watchdog terminations and app-hang diagnostics are forwarded
    to the installed adapter.

    iOS does not expose a generic on-device ANR API; the canonical
    feed is ``MXMetricManager`` (iOS 13+) which delivers daily payloads
    of hang / watchdog / disk / cellular metrics.
    """
    return (
        "// iOS watchdog termination + app-hang detector (P10 #295).\n"
        "// Drop into AppDelegate; conform to MXMetricManagerSubscriber.\n"
        "import MetricKit\n"
        "func application(_: UIApplication, didFinishLaunchingWithOptions: [UIApplication.LaunchOptionsKey: Any]?) -> Bool {\n"
        "    MXMetricManager.shared.add(self)\n"
        "    return true\n"
        "}\n"
        "// MARK: - MXMetricManagerSubscriber\n"
        "extension AppDelegate: MXMetricManagerSubscriber {\n"
        "    func didReceive(_ payloads: [MXMetricPayload]) {\n"
        "        for payload in payloads {\n"
        "            // Hang metrics — payload.applicationResponsivenessMetrics.\n"
        "            // Forward to your installed adapter.\n"
        "        }\n"
        "    }\n"
        "    func didReceive(_ payloads: [MXDiagnosticPayload]) {\n"
        "        for payload in payloads {\n"
        "            for diag in payload.crashDiagnostics ?? [] {\n"
        "                // signal 0x9 (SIGKILL) + reason \"Watchdog\" → watchdog termination.\n"
        "                // Forward to your installed adapter as a HangEvent.\n"
        "            }\n"
        "        }\n"
        "    }\n"
        "}\n"
    )


__all__ = [
    "ANRDetector",
    "ANRDetectorConfig",
    "DEFAULT_ANR_CRITICAL_MS",
    "DEFAULT_ANR_WARNING_MS",
    "DEFAULT_HANG_CRITICAL_MS",
    "DEFAULT_HANG_WARNING_MS",
    "android_anr_snippet",
    "ios_watchdog_snippet",
]
