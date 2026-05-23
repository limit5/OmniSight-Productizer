"""OP-1633 HTTP RED metrics middleware.

The SLO monitors query stock Prometheus HTTP series names directly:
``http_requests_total`` and ``http_request_duration_seconds_bucket``.
This module records those series into ``backend.metrics.REGISTRY`` with
the exact labels the monitors expect.
"""

from __future__ import annotations

import time

from backend import metrics as _metrics


UNKNOWN_ROUTE = "__unknown__"


def _route_template(request) -> str:
    """Return the matched Starlette route template, never a raw URL path."""
    route = request.scope.get("route")
    template = getattr(route, "path", None)
    if isinstance(template, str) and template:
        return template
    return UNKNOWN_ROUTE


def record_http_request(route: str, status_code: int, duration_seconds: float) -> None:
    """Publish one HTTP RED sample."""
    status = str(int(status_code))
    _metrics.http_requests_total.labels(route=route, status=status).inc()
    _metrics.http_request_duration_seconds.labels(
        route=route,
        status=status,
    ).observe(max(0.0, duration_seconds))


def register_middleware(app) -> None:
    """Attach middleware that records request count and duration."""

    @app.middleware("http")
    async def _http_red_metrics_middleware(request, call_next):
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            elapsed = time.perf_counter() - started
            record_http_request(_route_template(request), 500, elapsed)
            raise
        elapsed = time.perf_counter() - started
        record_http_request(_route_template(request), response.status_code, elapsed)
        return response
