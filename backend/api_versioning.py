"""URL-versioned API surface for OP-774.

Sprint D D13. Splits the API into ``/api/v1/...`` (the legacy contract a
frontend bundle could already be calling) and ``/api/v2/...`` (the
forward path), keeps both mounted simultaneously, and emits RFC 8594
``Sunset`` + RFC 9745 ``Deprecation`` headers on the v1 surface so
clients learn about the cutoff date without manual coordination.

Public entry points:

- :func:`api_relative_path` — strip whichever supported version prefix
  a request URL carries so backend middleware gates (rate limit, API key
  scope, password change, bootstrap exempt) treat ``/api/v1/foo`` and
  ``/api/v2/foo`` identically.
- :func:`register_versioned_api(app, routers)` — declarative mount of a
  tuple of shared :class:`APIRouter`-s under both version prefixes. The
  caller passes routers in one place; this module owns the duplication.
- :func:`install_deprecation_headers_middleware(app)` — registers the
  HTTP middleware that adds ``Deprecation`` / ``Sunset`` to v1 responses.
- :func:`install_version_metadata_endpoint(app)` — mounts ``/api/version``
  at the server root (intentionally version-agnostic) so frontend
  bootstrap can negotiate compatibility.
- :data:`api_v1_router` / :data:`api_v2_router` — exported for
  contract-shape pinning tests (see
  ``backend/tests/test_api_versioning_op774.py``).

The split is by URL prefix on purpose. Header-based versioning would
hide the contract from caches, intermediaries, and existing FastAPI
tooling (OpenAPI) that key off the path.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, FastAPI


API_V1_PREFIX = "/api/v1"
API_V2_PREFIX = "/api/v2"
SUPPORTED_API_VERSIONS = ("v1", "v2")
DEFAULT_API_VERSION = "v1"
MIN_FRONTEND_API_VERSION = "v1"
V1_SUNSET_HEADER = "Tue, 30 Jun 2026 23:59:59 GMT"
DEPRECATED_API_VERSIONS = {
    "v1": {"sunset": V1_SUNSET_HEADER},
}

# OP-885 AC#4 — per-endpoint deprecation registry. Keys are the
# version-relative path (i.e. what comes after ``/api/v1``), so a single
# entry marks the endpoint deprecated on every mounted version prefix.
# Each value is the RFC 8594 ``Sunset`` date (HTTP-date format string).
# Use :func:`register_endpoint_deprecation` to populate.
_DEPRECATED_ENDPOINTS: dict[tuple[str, str], str] = {}

# Default deprecation window per OP-885 AC#4. Clients get 90 days of
# overlap between "header started warning me" and "endpoint goes away".
DEPRECATION_WINDOW_DAYS = 90


def _http_date(when: datetime) -> str:
    """Format a datetime as an RFC 7231 IMF-fixdate (HTTP-date)."""
    return when.astimezone(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")


def register_endpoint_deprecation(
    path: str,
    *,
    method: str = "*",
    sunset_date: datetime | None = None,
    window_days: int = DEPRECATION_WINDOW_DAYS,
) -> str:
    """Mark an endpoint as deprecated with a per-endpoint Sunset date.

    ``path`` is the version-relative route (e.g. ``/agents/{id}``) — the
    middleware matches it against any of the mounted API version
    prefixes (v1 today, v1+v2 tomorrow). ``method`` is uppercase HTTP
    verb, or ``"*"`` for all. ``sunset_date`` defaults to "now +
    window_days" so callers only need to remember "deprecated today,
    gone in 90 days." Returns the Sunset header string actually
    registered (for logging / test assertions).
    """
    if sunset_date is None:
        sunset_date = datetime.now(timezone.utc) + timedelta(days=window_days)
    header = _http_date(sunset_date)
    _DEPRECATED_ENDPOINTS[(path, method.upper())] = header
    return header


def clear_endpoint_deprecations() -> None:
    """Reset the per-endpoint deprecation registry (test helper)."""
    _DEPRECATED_ENDPOINTS.clear()


def _endpoint_sunset(path: str, method: str) -> str | None:
    """Return the Sunset header for ``path`` / ``method``, or None."""
    rel = api_relative_path(path, "/api")
    upper = method.upper()
    return (
        _DEPRECATED_ENDPOINTS.get((rel, upper))
        or _DEPRECATED_ENDPOINTS.get((rel, "*"))
    )


# Shared aggregate routers — exported for tests that pin the per-version
# contract shape via _schema_for(...) without spinning up the full app.
api_v1_router = APIRouter()
api_v2_router = APIRouter()


def api_relative_path(path: str, fallback_prefix: str) -> str:
    """Return the route path after stripping any supported API version prefix.

    Pre-OP-774 the middleware gates compared ``request.url.path`` against
    ``settings.api_prefix`` only. With v1/v2 prefixes co-existing, that
    compare would miss ``/api/v2/...`` entirely and the gates would
    silently stop applying. This helper is the single source of truth
    used by every gate.
    """
    for prefix in (API_V1_PREFIX, API_V2_PREFIX, fallback_prefix):
        if path == prefix:
            return "/"
        if path.startswith(prefix + "/"):
            return path.removeprefix(prefix)
    return path


def include_versioned_router(router: APIRouter) -> None:
    """Add ``router`` to both v1 and v2 aggregate surfaces.

    Used internally by :func:`register_versioned_api`; exposed for the
    rare caller (tests) that wants to add a router without going through
    the bulk-register path.
    """
    api_v1_router.include_router(router)
    api_v2_router.include_router(router)


def register_versioned_api(app: FastAPI, routers: Iterable[APIRouter]) -> None:
    """Register a tuple of shared routers under ``/api/v1`` + ``/api/v2``.

    Caller passes the routers exactly once; this function owns the
    per-version duplication and the final ``app.include_router`` calls.
    v2 is intentionally hidden from the OpenAPI schema by default — the
    v1 surface is the source of truth so the openapi-contract CI gate
    keeps a single canonical document.
    """
    for router in routers:
        include_versioned_router(router)
    app.include_router(api_v1_router, prefix=API_V1_PREFIX)
    app.include_router(api_v2_router, prefix=API_V2_PREFIX, include_in_schema=False)


def install_deprecation_headers_middleware(app: FastAPI) -> None:
    """Add ``Deprecation`` + ``Sunset`` response middleware.

    Two layers, in this order:

    1. Per-endpoint registry (OP-885 AC#4): if the request path is
       registered via :func:`register_endpoint_deprecation`, emit a
       Sunset 90 days out regardless of which version prefix served it.
       This lets us deprecate an individual route while v1 as a whole
       lives on.
    2. v1 blanket: every ``/api/v1/...`` response also picks up
       ``Sunset: V1_SUNSET_HEADER`` if a per-endpoint header wasn't
       already set. RFC 8594 forbids stacking conflicting Sunset
       headers, so the per-endpoint header takes precedence.
    """

    @app.middleware("http")
    async def _api_deprecation_headers(request, call_next):
        response = await call_next(request)
        path = request.url.path
        sunset = _endpoint_sunset(path, request.method)
        if sunset is not None:
            response.headers["Deprecation"] = "true"
            response.headers["Sunset"] = sunset
            return response
        if path == API_V1_PREFIX or path.startswith(API_V1_PREFIX + "/"):
            response.headers.setdefault("Deprecation", "true")
            response.headers.setdefault("Sunset", V1_SUNSET_HEADER)
        return response


def install_version_metadata_endpoint(app: FastAPI) -> None:
    """Mount the version-agnostic ``/api/version`` metadata endpoint."""

    @app.get("/api/version", tags=["api-version"], include_in_schema=False)
    async def _api_version() -> dict:
        return {
            "supported_versions": list(SUPPORTED_API_VERSIONS),
            "default_version": DEFAULT_API_VERSION,
            "min_frontend_api_version": MIN_FRONTEND_API_VERSION,
            "deprecated_versions": DEPRECATED_API_VERSIONS,
        }
