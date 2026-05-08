"""OP-234 — backend Sentry / DataDog error aggregation.

The integration is deliberately SDK-free: deployments opt in with
environment variables, and missing / unreachable upstreams must never
break request handling.  The FastAPI middleware reports uncaught request
exceptions; the logging handler reports explicit ``logger.exception`` /
``logger.error(..., exc_info=True)`` paths.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from types import TracebackType
from typing import Any
from urllib import error as urlerror
from urllib import parse, request

from fastapi import FastAPI

logger = logging.getLogger(__name__)

_TRUTHY = {"1", "true", "yes", "on"}
_FALSEY = {"0", "false", "no", "off"}
_SCRUBBED_HEADERS = {
    "authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "x-csrf-token",
}
_CLIENT_NAME = "omnisight-backend/0.1"


@dataclass(frozen=True)
class ErrorAggregationConfig:
    """Immutable process-local config derived from ``OMNISIGHT_*`` env."""

    enabled: bool
    sentry_dsn: str = ""
    datadog_api_key: str = ""
    datadog_site: str = "datadoghq.com"
    service: str = "omnisight-backend"
    environment: str = ""
    release: str = ""
    timeout_seconds: float = 1.5

    @classmethod
    def from_env(cls) -> "ErrorAggregationConfig":
        raw_enabled = (
            os.environ.get("OMNISIGHT_ERROR_AGGREGATION_ENABLED") or ""
        ).strip().lower()
        sentry_dsn = (os.environ.get("OMNISIGHT_SENTRY_DSN") or "").strip()
        datadog_api_key = (
            os.environ.get("OMNISIGHT_DATADOG_API_KEY") or ""
        ).strip()
        if raw_enabled in _FALSEY:
            enabled = False
        elif raw_enabled in _TRUTHY:
            enabled = True
        else:
            enabled = bool(sentry_dsn or datadog_api_key)

        timeout_raw = (
            os.environ.get("OMNISIGHT_ERROR_AGGREGATION_TIMEOUT_SECONDS")
            or "1.5"
        )
        try:
            timeout_seconds = max(0.1, min(10.0, float(timeout_raw)))
        except ValueError:
            timeout_seconds = 1.5

        return cls(
            enabled=enabled,
            sentry_dsn=sentry_dsn,
            datadog_api_key=datadog_api_key,
            datadog_site=(
                os.environ.get("OMNISIGHT_DATADOG_SITE") or "datadoghq.com"
            ).strip(),
            service=(
                os.environ.get("OMNISIGHT_ERROR_AGGREGATION_SERVICE")
                or "omnisight-backend"
            ).strip(),
            environment=(
                os.environ.get("OMNISIGHT_ENV")
                or os.environ.get("ENV")
                or ""
            ).strip(),
            release=(
                os.environ.get("OMNISIGHT_RELEASE")
                or os.environ.get("RELEASE")
                or ""
            ).strip(),
            timeout_seconds=timeout_seconds,
        )


@dataclass(frozen=True)
class _SentryDsn:
    store_url: str
    public_key: str


def _parse_sentry_dsn(dsn: str) -> _SentryDsn | None:
    parsed = parse.urlparse(dsn)
    project_id = parsed.path.strip("/").split("/")[-1] if parsed.path else ""
    if (
        not parsed.scheme
        or not parsed.netloc
        or not parsed.username
        or not project_id
    ):
        return None
    base_path = "/".join(p for p in parsed.path.strip("/").split("/")[:-1] if p)
    prefix = f"/{base_path}" if base_path else ""
    netloc = parsed.hostname or ""
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    return _SentryDsn(
        store_url=f"{parsed.scheme}://{netloc}{prefix}/api/{project_id}/store/",
        public_key=parse.unquote(parsed.username),
    )


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_headers(headers: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in getattr(headers, "items", lambda: [])():
        key_s = str(key)
        if key_s.lower() in _SCRUBBED_HEADERS:
            out[key_s] = "[Filtered]"
        else:
            out[key_s] = str(value)
    return out


def _stacktrace(tb: TracebackType | None) -> list[dict[str, Any]]:
    if tb is None:
        return []
    frames = []
    for frame in traceback.extract_tb(tb):
        frames.append(
            {
                "filename": frame.filename,
                "function": frame.name,
                "lineno": frame.lineno,
                "context_line": frame.line or "",
            }
        )
    return frames


def _exception_payload(
    exc: BaseException,
    tb: TracebackType | None,
) -> dict[str, Any]:
    return {
        "type": type(exc).__name__,
        "value": str(exc),
        "stacktrace": {"frames": _stacktrace(tb)},
    }


def _request_context(scope: dict[str, Any] | None) -> dict[str, Any]:
    if not scope:
        return {}
    headers = {
        key.decode("latin1"): value.decode("latin1")
        for key, value in scope.get("headers", [])
    }
    safe = _safe_headers(headers)
    scheme = scope.get("scheme") or "http"
    server = scope.get("server") or ("", 0)
    host = safe.get("host") or f"{server[0]}:{server[1]}"
    path = scope.get("path") or ""
    query = scope.get("query_string") or b""
    url = f"{scheme}://{host}{path}"
    if query:
        url = f"{url}?{query.decode('latin1')}"
    return {
        "method": scope.get("method") or "",
        "url": url,
        "headers": safe,
    }


class ErrorAggregationReporter:
    """Best-effort reporter for Sentry store API and DataDog log intake."""

    def __init__(self, config: ErrorAggregationConfig | None = None) -> None:
        self.config = config or ErrorAggregationConfig.from_env()
        self._sentry = _parse_sentry_dsn(self.config.sentry_dsn)
        self._executor = ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="error-aggregation",
        )
        self._closed = False

    @property
    def active(self) -> bool:
        return self.config.enabled and bool(
            self._sentry or self.config.datadog_api_key
        )

    def close(self) -> None:
        self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)

    def capture_exception(
        self,
        exc: BaseException,
        *,
        tb: TracebackType | None = None,
        scope: dict[str, Any] | None = None,
        logger_name: str = "backend",
        message: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        if not self.active or self._closed:
            return
        tb = tb or exc.__traceback__
        event = self._build_event(
            exc,
            tb=tb,
            scope=scope,
            logger_name=logger_name,
            message=message,
            extra=extra,
        )
        self._submit(event)

    def capture_log_record(self, record: logging.LogRecord) -> None:
        if not record.exc_info or not self.active or self._closed:
            return
        exc = record.exc_info[1]
        tb = record.exc_info[2]
        if exc is None:
            return
        self.capture_exception(
            exc,
            tb=tb,
            logger_name=record.name,
            message=record.getMessage(),
            extra={"pathname": record.pathname, "lineno": record.lineno},
        )

    def _build_event(
        self,
        exc: BaseException,
        *,
        tb: TracebackType | None,
        scope: dict[str, Any] | None,
        logger_name: str,
        message: str | None,
        extra: dict[str, Any] | None,
    ) -> dict[str, Any]:
        event: dict[str, Any] = {
            "event_id": uuid.uuid4().hex,
            "timestamp": _utc_timestamp(),
            "platform": "python",
            "logger": logger_name,
            "server_name": socket.gethostname(),
            "level": "error",
            "message": message or f"{type(exc).__name__}: {exc}",
            "exception": {"values": [_exception_payload(exc, tb)]},
            "tags": {"service": self.config.service},
        }
        if self.config.environment:
            event["environment"] = self.config.environment
            event["tags"]["env"] = self.config.environment
        if self.config.release:
            event["release"] = self.config.release
            event["tags"]["version"] = self.config.release
        request_context = _request_context(scope)
        if request_context:
            event["request"] = request_context
        if extra:
            event["extra"] = extra
        return event

    def _submit(self, event: dict[str, Any]) -> None:
        if self._closed:
            return
        self._executor.submit(self._send_all, event)

    def _send_all(self, event: dict[str, Any]) -> None:
        if self._sentry:
            self._send_sentry(event)
        if self.config.datadog_api_key:
            self._send_datadog(event)

    def _send_sentry(self, event: dict[str, Any]) -> None:
        assert self._sentry is not None
        headers = {
            "Content-Type": "application/json",
            "X-Sentry-Auth": (
                "Sentry sentry_version=7, "
                f"sentry_client={_CLIENT_NAME}, "
                f"sentry_key={self._sentry.public_key}"
            ),
        }
        self._post_json(self._sentry.store_url, event, headers)

    def _send_datadog(self, event: dict[str, Any]) -> None:
        site = self.config.datadog_site or "datadoghq.com"
        url = f"https://http-intake.logs.{site}/api/v2/logs"
        tags = [
            f"service:{self.config.service}",
            "source:python",
        ]
        if self.config.environment:
            tags.append(f"env:{self.config.environment}")
        if self.config.release:
            tags.append(f"version:{self.config.release}")
        exc = event["exception"]["values"][0]
        request_ctx = event.get("request") or {}
        payload = [
            {
                "ddsource": "python",
                "service": self.config.service,
                "status": "error",
                "message": event["message"],
                "hostname": event.get("server_name", ""),
                "ddtags": ",".join(tags),
                "error.kind": exc.get("type", ""),
                "error.message": exc.get("value", ""),
                "error.stack": "".join(
                    traceback.format_list(
                        (
                            (
                                f.get("filename", ""),
                                int(f.get("lineno") or 0),
                                f.get("function", ""),
                                f.get("context_line", ""),
                            )
                            for f in exc.get("stacktrace", {}).get("frames", [])
                        )
                    )
                ),
                "http.method": request_ctx.get("method", ""),
                "http.url": request_ctx.get("url", ""),
            }
        ]
        headers = {
            "Content-Type": "application/json",
            "DD-API-KEY": self.config.datadog_api_key,
        }
        self._post_json(url, payload, headers)

    def _post_json(
        self,
        url: str,
        payload: Any,
        headers: dict[str, str],
    ) -> None:
        body = json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8")
        req = request.Request(url, data=body, headers=headers, method="POST")
        try:
            with request.urlopen(req, timeout=self.config.timeout_seconds) as resp:
                resp.read(128)
        except (OSError, urlerror.URLError, urlerror.HTTPError, TimeoutError) as exc:
            logger.debug("error aggregation post failed: %s", exc)


class ErrorAggregationLoggingHandler(logging.Handler):
    """Logging hook that reports ERROR records carrying ``exc_info``."""

    def __init__(self, reporter: ErrorAggregationReporter) -> None:
        super().__init__(level=logging.ERROR)
        self.reporter = reporter

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.reporter.capture_log_record(record)
        except Exception:
            self.handleError(record)


_INSTALL_LOCK = threading.Lock()
_REPORTER: ErrorAggregationReporter | None = None
_HANDLER: ErrorAggregationLoggingHandler | None = None


def install_error_aggregation(app: FastAPI) -> ErrorAggregationReporter:
    """Install FastAPI + logging error aggregation once per process."""

    global _REPORTER, _HANDLER
    with _INSTALL_LOCK:
        if _REPORTER is None:
            _REPORTER = ErrorAggregationReporter()
        reporter = _REPORTER
        if reporter.active and _HANDLER is None:
            _HANDLER = ErrorAggregationLoggingHandler(reporter)
            logging.getLogger().addHandler(_HANDLER)

    app.state.error_aggregation = reporter

    @app.middleware("http")
    async def _error_aggregation_middleware(request, call_next):
        try:
            return await call_next(request)
        except Exception as exc:
            reporter.capture_exception(
                exc,
                scope=request.scope,
                logger_name="backend.main",
                message=f"Unhandled request exception: {type(exc).__name__}",
            )
            raise

    return reporter
