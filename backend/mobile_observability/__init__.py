"""P10 #295 — Mobile observability & monitoring package.

Mobile-vertical sibling of the W10 ``backend.observability`` package.
Three responsibilities:

    1. **Crashlytics / Sentry-Mobile adapter** — vendor-agnostic
       ``MobileObservabilityAdapter`` interface with Firebase
       Crashlytics + Sentry-Mobile implementations so the SKILL-IOS /
       SKILL-ANDROID / SKILL-FLUTTER / SKILL-RN scaffolds can ship a
       one-knob mobile-observability story.

    2. **ANR detector / iOS watchdog terminator** —
       ``backend.mobile_observability.anr_detector`` provides a
       server-side classifier and snippet generators for the
       Android ``ANRWatchDog`` and iOS ``MetricKit`` paths.

    3. **Online UI metric aggregator** — in-process
       ``RenderMetricAggregator`` keeps the last ``window_seconds`` of
       render samples bucketed by ``(metric, platform, screen)`` with
       P50 / P75 / P95 + slow / frozen frame counts; mirrors the
       layout of ``backend.observability.vitals`` so an operator
       reading both dashboards sees the same shape.

Example wiring:

    from backend.mobile_observability import (
        get_mobile_adapter, MobileCrash, RenderMetric,
    )

    cls = get_mobile_adapter("sentry-mobile")
    adapter = cls.from_encrypted_dsn(
        dsn_ciphertext, environment="prod", release="1.42.0",
    )
    await adapter.send_crash(
        MobileCrash(message="NPE", platform="android", app_version="1.42.0"),
    )
"""

from __future__ import annotations

from backend.mobile_observability.anr_detector import (
    ANRDetector,
    ANRDetectorConfig,
    DEFAULT_ANR_CRITICAL_MS,
    DEFAULT_ANR_WARNING_MS,
    DEFAULT_HANG_CRITICAL_MS,
    DEFAULT_HANG_WARNING_MS,
    android_anr_snippet,
    ios_watchdog_snippet,
)
from backend.mobile_observability.base import (
    GOOD_RENDER_MS,
    HANG_KINDS,
    HangEvent,
    InvalidMobileTokenError,
    KNOWN_PLATFORMS,
    KNOWN_RENDER_METRICS,
    MissingMobileScopeError,
    MobileCrash,
    MobileObservabilityAdapter,
    MobileObservabilityError,
    MobilePayloadError,
    MobileRateLimitError,
    POOR_RENDER_MS,
    RenderMetric,
    classify_render,
    derive_fingerprint,
    dsn_fingerprint,
)
from backend.mobile_observability.ui_metrics import (
    RenderDashboardSnapshot,
    RenderMetricAggregator,
    RenderStats,
    get_default_aggregator,
    reset_default_aggregator,
)


def list_providers() -> list[str]:
    """Canonical id for every shipped mobile adapter."""
    return ["firebase-crashlytics", "sentry-mobile"]


def get_mobile_adapter(provider: str) -> type[MobileObservabilityAdapter]:
    """Look up a mobile adapter class by canonical provider string.

    Lazy-imports so a missing optional dep in one adapter does not
    cascade to the other.
    """
    key = provider.strip().lower().replace("_", "-")
    if key in (
        "firebase-crashlytics", "crashlytics", "firebase",
        "google-firebase-crashlytics",
    ):
        from backend.mobile_observability.firebase_crashlytics import (
            FirebaseCrashlyticsAdapter,
        )
        return FirebaseCrashlyticsAdapter
    if key in ("sentry-mobile", "sentry", "sentry.io-mobile"):
        from backend.mobile_observability.sentry_mobile import (
            SentryMobileAdapter,
        )
        return SentryMobileAdapter
    raise ValueError(
        f"Unknown mobile observability provider '{provider}'. "
        f"Expected one of: {', '.join(list_providers())}"
    )


__all__ = [
    "ANRDetector",
    "ANRDetectorConfig",
    "DEFAULT_ANR_CRITICAL_MS",
    "DEFAULT_ANR_WARNING_MS",
    "DEFAULT_HANG_CRITICAL_MS",
    "DEFAULT_HANG_WARNING_MS",
    "GOOD_RENDER_MS",
    "HANG_KINDS",
    "HangEvent",
    "InvalidMobileTokenError",
    "KNOWN_PLATFORMS",
    "KNOWN_RENDER_METRICS",
    "MissingMobileScopeError",
    "MobileCrash",
    "MobileObservabilityAdapter",
    "MobileObservabilityError",
    "MobilePayloadError",
    "MobileRateLimitError",
    "POOR_RENDER_MS",
    "RenderDashboardSnapshot",
    "RenderMetric",
    "RenderMetricAggregator",
    "RenderStats",
    "android_anr_snippet",
    "classify_render",
    "derive_fingerprint",
    "dsn_fingerprint",
    "get_default_aggregator",
    "get_mobile_adapter",
    "ios_watchdog_snippet",
    "list_providers",
    "reset_default_aggregator",
]
