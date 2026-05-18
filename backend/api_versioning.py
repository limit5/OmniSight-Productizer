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

import json
import logging
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, FastAPI

_log = logging.getLogger(__name__)


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


# OP-1479 — bundle manifest baked into the runtime image at /app/bundle.json
# by Dockerfile.backend. Read once at import time so /api/version is a
# pure dict-return path (the endpoint is on the bootstrap-exempt
# fast-path; we don't want to do disk I/O per request).
BUNDLE_MANIFEST_PATH = Path("/app/bundle.json")
_IMAGE_MANIFEST_PATH = Path("/app/MANIFEST.json")

# OP-1491 — dev fallback when /app/bundle.json is absent or unreadable.
# Surfaced as bundle_id + a "warning" field on /api/version so operators
# can distinguish "real release running an uninstrumented image" from
# "bake step ran but produced a different id". Picked over None so
# downstream consumers (api-compat CI gate, client SDKs) can rely on
# bundle_id being a non-null string.
DEV_FALLBACK_BUNDLE_ID = "dev+unknown"
_MISSING_BUNDLE_WARNING = (
    "bundle manifest at /app/bundle.json is missing or unreadable; "
    "served bundle_id is a dev placeholder"
)
_NO_BUNDLE_ID_WARNING = (
    "bundle manifest is present but bundle_id is missing or invalid; "
    "served bundle_id is a dev placeholder"
)


def _load_bundle_manifest(path: Path = BUNDLE_MANIFEST_PATH) -> dict | None:
    """Read the bundle manifest baked into the image, or ``None`` if absent.

    The runtime image bakes this file from the bundle artifact emitted
    by ``scripts/build_image_bundle.py``. Local dev builds and tests
    will not have it; missing or malformed manifests resolve to
    ``None`` so the caller can distinguish "no bundle, serve dev
    fallback" from "bundle present but field missing" (OP-1491).
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        _log.warning("Failed to read bundle manifest at %s", path)
        return None
    if not isinstance(raw, dict):
        return None
    return raw


def _load_image_manifest_db_head(path: Path = _IMAGE_MANIFEST_PATH) -> str | None:
    """Return the alembic head baked into the backend image, or None.

    ``MANIFEST.json`` is the v2-⑤-1a artifact already produced by
    ``scripts/bake-image-manifest.sh``. The bundle manifest also carries
    ``contracts.db_migration_head``; we fall back to MANIFEST.json so
    that ``/api/version`` reports the field even on older images that
    pre-date the bundle manifest.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    value = raw.get("alembic_head_in_image")
    return value if isinstance(value, str) else None


def build_version_payload(
    *,
    bundle_path: Path | None = None,
    image_manifest_path: Path | None = None,
) -> dict:
    """Assemble the JSON body returned by ``GET /api/version``.

    Split out from the route handler so tests can drive it without
    standing up a FastAPI app, and so callers can inject paths for
    fixture-based testing. Defaults resolve against the module-level
    constants at call time, so tests can monkeypatch ``av.BUNDLE_MANIFEST_PATH``
    / ``av._IMAGE_MANIFEST_PATH`` and the route picks up the change.
    """
    if bundle_path is None:
        bundle_path = BUNDLE_MANIFEST_PATH
    if image_manifest_path is None:
        image_manifest_path = _IMAGE_MANIFEST_PATH
    bundle = _load_bundle_manifest(bundle_path)
    warning: str | None = None
    if bundle is None:
        bundle = {}
        warning = _MISSING_BUNDLE_WARNING
    contracts_raw = bundle.get("contracts")
    contracts = contracts_raw if isinstance(contracts_raw, dict) else {}

    api_supported = contracts.get("api_supported")
    if not isinstance(api_supported, list) or not api_supported:
        api_supported = list(SUPPORTED_API_VERSIONS)

    api_required = contracts.get("api_required")
    if not isinstance(api_required, str) or not api_required:
        api_required = MIN_FRONTEND_API_VERSION

    openapi_hash = contracts.get("openapi_hash")
    if not isinstance(openapi_hash, str):
        openapi_hash = None

    db_migration_head = contracts.get("db_migration_head")
    if not isinstance(db_migration_head, str) or not db_migration_head:
        db_migration_head = _load_image_manifest_db_head(image_manifest_path)

    frontend_built_against_api = contracts.get("frontend_built_against_api")
    if not isinstance(frontend_built_against_api, str) or not frontend_built_against_api:
        frontend_built_against_api = None

    raw_bundle_id = bundle.get("bundle_id")
    if isinstance(raw_bundle_id, str) and raw_bundle_id:
        bundle_id = raw_bundle_id
    else:
        bundle_id = DEV_FALLBACK_BUNDLE_ID
        if warning is None:
            warning = _NO_BUNDLE_ID_WARNING

    payload: dict = {
        # Legacy fields preserved for backwards compatibility with the
        # existing v1 frontend bootstrap check.
        "supported_versions": list(SUPPORTED_API_VERSIONS),
        "default_version": DEFAULT_API_VERSION,
        "min_frontend_api_version": MIN_FRONTEND_API_VERSION,
        "deprecated_versions": DEPRECATED_API_VERSIONS,
        # OP-1479 / OP-1491 bundle-aware additions. Keys match the
        # contract shape documented in omnisight-bundle.schema.json so
        # operators can diff /api/version against the bundle artifact
        # directly.
        "bundle_id": bundle_id,
        "api_required": api_required,
        "api_supported": list(api_supported),
        "openapi_hash": openapi_hash,
        "db_migration_head": db_migration_head,
        "frontend_built_against_api": frontend_built_against_api,
    }
    if warning is not None:
        payload["warning"] = warning
    return payload


def install_version_metadata_endpoint(app: FastAPI) -> None:
    """Mount the version-agnostic ``/api/version`` metadata endpoint.

    OP-1491 — the bundle manifest is read once here at install/startup
    time and cached in a closure (the "module-level state" the ticket
    asks for: a single payload built once, served on every request).
    /api/version sits on the bootstrap-exempt fast path, so we want to
    avoid disk I/O per request. ``build_version_payload`` swallows
    missing/malformed bundle files and resolves to the dev fallback so
    startup never crashes on an image that wasn't built with V1c bake.
    """
    cached_payload = build_version_payload()

    @app.get("/api/version", tags=["api-version"], include_in_schema=False)
    async def _api_version() -> dict:
        return cached_payload
