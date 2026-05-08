"""FX2.D5.1 — OpenTelemetry distributed tracing wiring.

Module-global state audit: ``_INSTALLED`` is process-local idempotency
state. Each uvicorn worker owns its tracer provider and exporter queue.
"""

from __future__ import annotations

import logging
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from backend.config import settings

logger = logging.getLogger(__name__)

_INSTALLED = False


class TraceContextMiddleware:
    """ASGI middleware that extracts W3C trace context and creates spans."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        from opentelemetry import propagate, trace
        from opentelemetry.trace import SpanKind, Status, StatusCode

        carrier = _headers_to_carrier(scope.get("headers") or [])
        parent_context = propagate.extract(carrier)
        tracer = trace.get_tracer(__name__)
        method = str(scope.get("method") or "GET")
        path = str(scope.get("path") or "/")
        span_name = f"{method} {path}"

        with tracer.start_as_current_span(
            span_name,
            context=parent_context,
            kind=SpanKind.SERVER,
        ) as span:
            span.set_attribute("http.method", method)
            span.set_attribute("http.route", path)
            span.set_attribute("http.target", str(scope.get("raw_path") or path))
            span.set_attribute("http.scheme", str(scope.get("scheme") or "http"))
            span.set_attribute("net.peer.ip", _client_host(scope))

            async def send_with_trace(message: Message) -> None:
                if message["type"] == "http.response.start":
                    status_code = int(message.get("status") or 0)
                    span.set_attribute("http.status_code", status_code)
                    if status_code >= 500:
                        span.set_status(Status(StatusCode.ERROR))
                    _inject_traceparent(message)
                await send(message)

            try:
                await self.app(scope, receive, send_with_trace)
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR))
                raise


def install(app: Any) -> bool:
    """Install tracing middleware and exporters if enabled."""

    global _INSTALLED
    if _INSTALLED or not settings.tracing_enabled:
        return False

    exporter_name = settings.tracing_exporter.strip().lower()
    if exporter_name not in {"jaeger", "datadog"}:
        logger.warning(
            "tracing: disabled because OMNISIGHT_TRACING_EXPORTER=%r "
            "is not one of: jaeger, datadog",
            settings.tracing_exporter,
        )
        return False

    try:
        _configure_provider(exporter_name)
        app.add_middleware(TraceContextMiddleware)
    except ImportError as exc:
        logger.warning("tracing: disabled because OpenTelemetry import failed: %s", exc)
        return False

    _INSTALLED = True
    logger.info("tracing: enabled exporter=%s", exporter_name)
    return True


def _configure_provider(exporter_name: str) -> None:
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.trace.sampling import TraceIdRatioBased

    provider = TracerProvider(
        resource=Resource.create({"service.name": settings.tracing_service_name}),
        sampler=TraceIdRatioBased(_sample_rate()),
    )
    provider.add_span_processor(BatchSpanProcessor(_build_exporter(exporter_name)))
    trace.set_tracer_provider(provider)


def _build_exporter(exporter_name: str) -> Any:
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    if exporter_name == "jaeger":
        return OTLPSpanExporter(endpoint=settings.tracing_jaeger_otlp_endpoint)

    headers = {}
    api_key = settings.tracing_datadog_api_key.strip()
    if api_key:
        headers["DD-API-KEY"] = api_key
    return OTLPSpanExporter(
        endpoint=settings.tracing_datadog_otlp_endpoint,
        headers=headers or None,
    )


def _headers_to_carrier(headers: list[tuple[bytes, bytes]]) -> dict[str, str]:
    carrier: dict[str, str] = {}
    for raw_name, raw_value in headers:
        try:
            name = raw_name.decode("latin-1").lower()
            value = raw_value.decode("latin-1")
        except UnicodeDecodeError:
            continue
        carrier[name] = value
    return carrier


def _inject_traceparent(message: Message) -> None:
    from opentelemetry import propagate

    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    if not carrier:
        return
    headers = list(message.get("headers") or [])
    for name, value in carrier.items():
        headers.append((name.lower().encode("latin-1"), value.encode("latin-1")))
    message["headers"] = headers


def _client_host(scope: Scope) -> str:
    client = scope.get("client")
    if not client:
        return ""
    return str(client[0])


def _sample_rate() -> float:
    return min(1.0, max(0.0, float(settings.tracing_sample_rate)))


__all__ = ["TraceContextMiddleware", "install"]
