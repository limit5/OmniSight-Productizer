"""Phase 52 — Prometheus metrics registry.

Exposes process-wide Counter / Histogram / Gauge instances that the
rest of the codebase imports + bumps. The companion `/metrics`
endpoint in `routers/system.py` (well, a new `metrics_router`) renders
them in Prometheus exposition format.

Naming follows the `omnisight_<domain>_<name>_<unit>` convention so
they sort cleanly in Grafana.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from prometheus_client import (
        Counter,
        Gauge,
        Histogram,
        CollectorRegistry,
        generate_latest,
        CONTENT_TYPE_LATEST,
    )
    _AVAILABLE = True
except ImportError:  # pragma: no cover
    _AVAILABLE = False
    Counter = Gauge = Histogram = CollectorRegistry = None  # type: ignore
    CONTENT_TYPE_LATEST = "text/plain; charset=utf-8"

    def generate_latest(*_a, **_kw) -> bytes:  # type: ignore
        return b"# prometheus_client not installed\n"


def is_available() -> bool:
    return _AVAILABLE


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Registry + metrics
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

if _AVAILABLE:
    REGISTRY = CollectorRegistry()

    # Decisions ─────────────────────────────────────────────────
    decision_total = Counter(
        "omnisight_decision_total",
        "Decisions registered by kind / severity / status",
        labelnames=("kind", "severity", "status"),
        registry=REGISTRY,
    )
    decision_resolve_seconds = Histogram(
        "omnisight_decision_resolve_seconds",
        "Wall-clock seconds from propose to resolve / auto-execute",
        labelnames=("kind", "severity", "resolver"),
        buckets=(0.1, 0.5, 1, 5, 10, 30, 60, 300, 600, 1800),
        registry=REGISTRY,
    )

    # Pipeline ──────────────────────────────────────────────────
    pipeline_step_seconds = Histogram(
        "omnisight_pipeline_step_seconds",
        "Pipeline step wall-clock duration",
        labelnames=("phase", "step", "outcome"),
        buckets=(1, 5, 30, 60, 300, 900, 1800, 3600, 7200),
        registry=REGISTRY,
    )

    # Provider ──────────────────────────────────────────────────
    provider_failure_total = Counter(
        "omnisight_provider_failure_total",
        "LLM provider failures by reason",
        labelnames=("provider", "reason"),
        registry=REGISTRY,
    )
    provider_latency_seconds = Histogram(
        "omnisight_provider_latency_seconds",
        "LLM provider request latency",
        labelnames=("provider", "model"),
        buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60, 120),
        registry=REGISTRY,
    )

    # SSE ───────────────────────────────────────────────────────
    sse_subscribers = Gauge(
        "omnisight_sse_subscribers",
        "Number of currently connected SSE subscribers",
        registry=REGISTRY,
    )
    sse_dropped_total = Counter(
        "omnisight_sse_dropped_total",
        "Subscribers dropped due to backpressure (queue full)",
        registry=REGISTRY,
    )

    # Workflow (Phase 56) ───────────────────────────────────────
    workflow_step_total = Counter(
        "omnisight_workflow_step_total",
        "Durable workflow steps recorded by outcome",
        labelnames=("kind", "outcome"),
        registry=REGISTRY,
    )

    # Auth (Phase 54) ───────────────────────────────────────────
    auth_login_total = Counter(
        "omnisight_auth_login_total",
        "Login attempts by outcome",
        labelnames=("outcome",),
        registry=REGISTRY,
    )

    # Fix-B B6: non-fatal persistence / dispatch failures that used to
    # be `except Exception: pass`. Now logged + incremented so Grafana
    # can alert when a normally-silent write starts failing repeatedly.
    persist_failure_total = Counter(
        "omnisight_persist_failure_total",
        "Non-fatal persistence/dispatch failures that were swallowed",
        labelnames=("module",),
        registry=REGISTRY,
    )

    # Fix-A S6: orphaned CI subprocess tracker ─────────────────
    subprocess_orphan_total = Counter(
        "omnisight_subprocess_orphan_total",
        "CI subprocess kill() failed after timeout — likely zombie",
        labelnames=("target",),
        registry=REGISTRY,
    )

    # Process up-time
    process_start_time = Gauge(
        "omnisight_process_start_time_seconds",
        "Unix timestamp when this process started",
        registry=REGISTRY,
    )
    process_start_time.set(time.time())

else:
    # No-op stubs so callers don't have to guard every increment.
    class _NoOp:
        def labels(self, *_a, **_kw): return self
        def inc(self, *_a, **_kw): pass
        def dec(self, *_a, **_kw): pass
        def set(self, *_a, **_kw): pass
        def observe(self, *_a, **_kw): pass

    decision_total = decision_resolve_seconds = _NoOp()  # type: ignore
    pipeline_step_seconds = _NoOp()  # type: ignore
    provider_failure_total = provider_latency_seconds = _NoOp()  # type: ignore
    sse_subscribers = sse_dropped_total = _NoOp()  # type: ignore
    workflow_step_total = _NoOp()  # type: ignore
    auth_login_total = _NoOp()  # type: ignore
    persist_failure_total = _NoOp()  # type: ignore
    subprocess_orphan_total = _NoOp()  # type: ignore
    process_start_time = _NoOp()  # type: ignore
    REGISTRY = None  # type: ignore


def render_exposition() -> tuple[bytes, str]:
    """Return (body, content_type) for the /metrics endpoint."""
    if not _AVAILABLE:
        return (b"# prometheus_client not installed\n", "text/plain; charset=utf-8")
    return (generate_latest(REGISTRY), CONTENT_TYPE_LATEST)


def reset_for_tests() -> None:
    """Re-create REGISTRY so tests start from clean counters."""
    if not _AVAILABLE:
        return
    global REGISTRY, decision_total, decision_resolve_seconds
    global pipeline_step_seconds, provider_failure_total, provider_latency_seconds
    global sse_subscribers, sse_dropped_total, workflow_step_total
    global auth_login_total, subprocess_orphan_total, persist_failure_total, process_start_time
    REGISTRY = CollectorRegistry()
    decision_total = Counter(
        "omnisight_decision_total", "Decisions registered",
        labelnames=("kind", "severity", "status"), registry=REGISTRY,
    )
    decision_resolve_seconds = Histogram(
        "omnisight_decision_resolve_seconds", "Resolve duration",
        labelnames=("kind", "severity", "resolver"),
        buckets=(0.1, 0.5, 1, 5, 10, 30, 60, 300, 600, 1800),
        registry=REGISTRY,
    )
    pipeline_step_seconds = Histogram(
        "omnisight_pipeline_step_seconds", "Pipeline step duration",
        labelnames=("phase", "step", "outcome"),
        buckets=(1, 5, 30, 60, 300, 900, 1800, 3600, 7200),
        registry=REGISTRY,
    )
    provider_failure_total = Counter(
        "omnisight_provider_failure_total", "Provider failures",
        labelnames=("provider", "reason"), registry=REGISTRY,
    )
    provider_latency_seconds = Histogram(
        "omnisight_provider_latency_seconds", "Provider latency",
        labelnames=("provider", "model"),
        buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60, 120),
        registry=REGISTRY,
    )
    sse_subscribers = Gauge(
        "omnisight_sse_subscribers", "SSE subscribers", registry=REGISTRY,
    )
    sse_dropped_total = Counter(
        "omnisight_sse_dropped_total", "SSE drops", registry=REGISTRY,
    )
    workflow_step_total = Counter(
        "omnisight_workflow_step_total", "Workflow steps",
        labelnames=("kind", "outcome"), registry=REGISTRY,
    )
    auth_login_total = Counter(
        "omnisight_auth_login_total", "Login attempts",
        labelnames=("outcome",), registry=REGISTRY,
    )
    subprocess_orphan_total = Counter(
        "omnisight_subprocess_orphan_total", "CI subprocess kill failed",
        labelnames=("target",), registry=REGISTRY,
    )
    persist_failure_total = Counter(
        "omnisight_persist_failure_total", "Swallowed persistence failures",
        labelnames=("module",), registry=REGISTRY,
    )
    process_start_time = Gauge(
        "omnisight_process_start_time_seconds", "Process start time",
        registry=REGISTRY,
    )
    process_start_time.set(time.time())
