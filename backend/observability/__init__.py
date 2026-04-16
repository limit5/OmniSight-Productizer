"""W10 #284 — Web observability & monitoring package.

Three responsibilities:

    1. **RUM adapter** — vendor-agnostic ``RUMAdapter`` interface with
       Sentry + Datadog implementations so the W6/W7/W8 web-vertical
       scaffolds can ship a one-knob observability story.

    2. **Core Web Vitals dashboard** — in-process aggregator
       (`CoreWebVitalsAggregator`) keeps the last 10 minutes of vitals
       bucketed by ``(metric, page)`` with P50/P75/P95 + good / poor
       counts; the FastAPI router under ``backend.routers.web_observability``
       exposes it.

    3. **Error → JIRA bridge** — ``ErrorToIntentRouter`` converts a
       browser ``ErrorEvent`` into an O5 ``IntentSource`` subtask so a
       prod JS exception lands in JIRA / GitHub Issues / GitLab Issues
       without a vendor lookup.

Example wiring:

    from backend.observability import get_rum_adapter, WebVital

    cls = get_rum_adapter("sentry")
    rum = cls.from_encrypted_dsn(
        dsn_ciphertext, environment="prod", release="1.42.0",
    )
    await rum.send_vital(WebVital(name="LCP", value=2200, page="/"))
"""

from __future__ import annotations

from backend.observability.base import (
    ErrorEvent,
    GOOD_THRESHOLDS,
    InvalidRUMTokenError,
    KNOWN_VITALS,
    MissingRUMScopeError,
    POOR_THRESHOLDS,
    RUMAdapter,
    RUMError,
    RUMPayloadError,
    RUMRateLimitError,
    WebVital,
    classify_vital,
    derive_fingerprint,
    dsn_fingerprint,
)
from backend.observability.error_router import (
    DedupRecord,
    ErrorToIntentRouter,
    RouterMetrics,
    build_subtask_payload,
    get_default_router,
    reset_default_router,
)
from backend.observability.vitals import (
    CoreWebVitalsAggregator,
    DashboardSnapshot,
    MetricStats,
    get_default_aggregator,
    reset_default_aggregator,
)


def list_providers() -> list[str]:
    """Canonical id for every shipped RUM adapter."""
    return ["sentry", "datadog"]


def get_rum_adapter(provider: str) -> type[RUMAdapter]:
    """Look up a RUM adapter class by canonical provider string.

    Lazy-imports so a missing optional dep in one adapter (Datadog SDK
    pin, etc.) does not cascade.
    """
    key = provider.strip().lower().replace("_", "-")
    if key in ("sentry", "sentry.io"):
        from backend.observability.sentry import SentryRUMAdapter
        return SentryRUMAdapter
    if key in ("datadog", "dd", "datadog-rum"):
        from backend.observability.datadog import DatadogRUMAdapter
        return DatadogRUMAdapter
    raise ValueError(
        f"Unknown RUM provider '{provider}'. "
        f"Expected one of: {', '.join(list_providers())}"
    )


__all__ = [
    "CoreWebVitalsAggregator",
    "DashboardSnapshot",
    "DedupRecord",
    "ErrorEvent",
    "ErrorToIntentRouter",
    "GOOD_THRESHOLDS",
    "InvalidRUMTokenError",
    "KNOWN_VITALS",
    "MetricStats",
    "MissingRUMScopeError",
    "POOR_THRESHOLDS",
    "RUMAdapter",
    "RUMError",
    "RUMPayloadError",
    "RUMRateLimitError",
    "RouterMetrics",
    "WebVital",
    "build_subtask_payload",
    "classify_vital",
    "derive_fingerprint",
    "dsn_fingerprint",
    "get_default_aggregator",
    "get_default_router",
    "get_rum_adapter",
    "list_providers",
    "reset_default_aggregator",
    "reset_default_router",
]
