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
import os
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


def _bundle_image_digest(bundle: dict, image_name: str) -> str | None:
    """Return one baked image digest from ``bundle.json`` if present."""
    images = bundle.get("images")
    if not isinstance(images, dict):
        return None
    image = images.get(image_name)
    if not isinstance(image, dict):
        return None
    digest = image.get("digest")
    return digest if isinstance(digest, str) and digest else None


# ─────────────────────────────────────────────────────────────────────
#  OP-1582 (RT-08) — deployment overlay env lock
# ─────────────────────────────────────────────────────────────────────
# The deploy/promote step writes a lock file (env-file format) carrying the
# running deployment's identity: the build git sha/ref, the promoted image
# tag, the backend+frontend digest PAIR (RT-21), and the promotion audit id.
# Per ADR-0040 RT-08-pre the container reads this lock ONCE AT STARTUP; the
# cached snapshot is served on ``/api/version`` and gates ``/readyz``. Read-
# before-start is deliberate: deploys always rolling-restart, so "needs a
# restart to update" is not a limitation, and it avoids per-request disk I/O
# and cache-invalidation failure modes. Fail-closed — a deployed runtime that
# starts without (or with an incomplete) lock must not advertise readiness, so
# the deploy gate catches it (see ``backend.routers.health._check_deploy_overlay``).
DEPLOY_OVERLAY_LOCK_PATH = Path(
    os.environ.get("OMNISIGHT_DEPLOY_OVERLAY_LOCK", "/etc/omnisight/deploy-overlay.lock")
)

# Env-lock variable name → ``/api/version`` overlay field name. The deploy
# writer emits these as ``KEY=value`` lines (the same file the prod/staging
# compose ``env_file:`` sources, hence "env lock"). Every field is required:
# a partial lock means the writer is broken and we must not advertise a
# half-known identity, so a missing/empty value fails the overlay closed.
_OVERLAY_LOCK_FIELDS: dict[str, str] = {
    "OMNISIGHT_BUILD_GIT_SHA": "build_git_sha",
    "OMNISIGHT_BUILD_GIT_REF": "build_git_ref",
    "OMNISIGHT_DEPLOYED_TAG": "deployed_tag",
    "OMNISIGHT_DEPLOYED_DIGEST_BACKEND": "deployed_digest_backend",
    "OMNISIGHT_DEPLOYED_DIGEST_FRONTEND": "deployed_digest_frontend",
    "OMNISIGHT_PROMOTION_AUDIT_ID": "promotion_audit_id",
}

#: The overlay payload fields, in serve order. Always present on
#: ``/api/version`` (as ``None`` when no valid lock was read) so the contract
#: shape is stable across dev and deployed images.
OVERLAY_FIELDS: tuple[str, ...] = tuple(_OVERLAY_LOCK_FIELDS.values())

# Sentinel for "not yet loaded" so the cache can distinguish that from
# "loaded, but no valid lock present" (``None``).
_OVERLAY_UNSET = object()
_deploy_overlay_cache: object = _OVERLAY_UNSET


def _parse_env_lock(text: str) -> dict[str, str]:
    """Parse an env-file (``KEY=value``) lock into a plain dict.

    Tolerant of blank lines, ``#`` comments, an optional ``export`` prefix,
    and single/double-quoted values — the shapes a shell-written deploy lock
    or a compose ``env_file:`` can take. Unknown keys are kept; the caller
    selects the ones it cares about.
    """
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key:
            out[key] = value
    return out


def load_deploy_overlay(path: Path | None = None) -> dict | None:
    """Read the deploy-written env lock and return the overlay, or ``None``.

    Returns ``None`` (the fail-closed marker) when the lock is absent,
    unreadable, or incomplete — i.e. any of the six required identity fields
    is missing or empty. Otherwise returns a dict keyed by the
    :data:`OVERLAY_FIELDS` names. Does NOT cache — :func:`init_deploy_overlay`
    owns the read-once-at-startup cache.
    """
    if path is None:
        path = DEPLOY_OVERLAY_LOCK_PATH
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        _log.warning("Failed to read deploy overlay lock at %s", path)
        return None

    env = _parse_env_lock(text)
    overlay: dict = {}
    for env_key, field in _OVERLAY_LOCK_FIELDS.items():
        value = env.get(env_key, "").strip()
        overlay[field] = value or None

    if any(overlay.get(field) is None for field in OVERLAY_FIELDS):
        missing = [f for f in OVERLAY_FIELDS if overlay.get(f) is None]
        _log.warning(
            "deploy overlay lock at %s is incomplete; missing %s",
            path,
            ",".join(missing),
        )
        return None
    return overlay


def init_deploy_overlay(path: Path | None = None) -> dict | None:
    """Read the env lock ONCE and cache the snapshot (called at startup).

    Subsequent reads go through :func:`get_deploy_overlay`, which returns the
    cached snapshot rather than re-reading the file. Calling this again
    refreshes the cache (used by tests via :func:`reset_deploy_overlay_cache`
    + this function with an injected ``path``).
    """
    global _deploy_overlay_cache
    _deploy_overlay_cache = load_deploy_overlay(path)
    return _deploy_overlay_cache  # type: ignore[return-value]


def get_deploy_overlay() -> dict | None:
    """Return the cached deploy overlay, loading it on first access.

    ``None`` means no valid lock was found at startup — the fail-closed
    marker the ``/readyz`` overlay gate keys on.
    """
    if _deploy_overlay_cache is _OVERLAY_UNSET:
        return init_deploy_overlay()
    return _deploy_overlay_cache  # type: ignore[return-value]


def reset_deploy_overlay_cache() -> None:
    """Test helper — drop the cached overlay so the next access re-reads."""
    global _deploy_overlay_cache
    _deploy_overlay_cache = _OVERLAY_UNSET


# Env var carrying the digest of the image the container is ACTUALLY running,
# injected by the B1b deploy-wiring step (e.g. resolved from the container
# runtime / image inspect at launch). Deliberately separate from the lock's
# ``deployed_digest_backend`` (what the promote step INTENDED to deploy): the
# /readyz overlay-digest gate compares the two to catch a container running an
# image other than the one the lock claims.
RUNNING_IMAGE_DIGEST_BACKEND_ENV = "OMNISIGHT_RUNNING_IMAGE_DIGEST_BACKEND"


def get_running_image_digest_backend() -> str | None:
    """Return the digest of the backend image actually running, or ``None``.

    Reads the B1b-injected env :data:`RUNNING_IMAGE_DIGEST_BACKEND_ENV`. This
    is intentionally NOT sourced from ``bundle.json`` — the baked bundle
    manifest carries the all-zeros placeholder digest (the #23 finding), so it
    cannot be trusted as "what is running". ``None`` (env absent or empty)
    means the running digest is unknown; the overlay-digest gate treats that
    as "skip the compare" (fail-OPEN) so a not-yet-B1b-wired deploy can't brick
    readiness.
    """
    value = os.environ.get(RUNNING_IMAGE_DIGEST_BACKEND_ENV, "").strip()
    return value or None


def build_version_payload(
    *,
    bundle_path: Path | None = None,
    image_manifest_path: Path | None = None,
    overlay: dict | None | object = _OVERLAY_UNSET,
) -> dict:
    """Assemble the JSON body returned by ``GET /api/version``.

    Split out from the route handler so tests can drive it without
    standing up a FastAPI app, and so callers can inject paths for
    fixture-based testing. Defaults resolve against the module-level
    constants at call time, so tests can monkeypatch ``av.BUNDLE_MANIFEST_PATH``
    / ``av._IMAGE_MANIFEST_PATH`` and the route picks up the change.

    ``overlay`` defaults to the cached deploy overlay snapshot
    (:func:`get_deploy_overlay`); tests pass an explicit dict / ``None`` to
    drive the RT-08 overlay fields without touching the cache.
    """
    if bundle_path is None:
        bundle_path = BUNDLE_MANIFEST_PATH
    if image_manifest_path is None:
        image_manifest_path = _IMAGE_MANIFEST_PATH
    if overlay is _OVERLAY_UNSET:
        overlay = get_deploy_overlay()
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
        "backend_image_digest": _bundle_image_digest(bundle, "backend"),
        "frontend_image_digest": _bundle_image_digest(bundle, "frontend"),
    }

    # OP-1582 (RT-08) — deployment overlay. The six identity fields are
    # always present (None when no valid lock was read at startup) so the
    # contract shape is stable across dev images and promoted deployments.
    # ``deploy_overlay_present`` lets a consumer distinguish "dev image, no
    # lock" from "lock read" without inspecting every field.
    overlay_fields = overlay if isinstance(overlay, dict) else {}
    for field in OVERLAY_FIELDS:
        payload[field] = overlay_fields.get(field)
    payload["deploy_overlay_present"] = isinstance(overlay, dict)

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

    OP-1582 (RT-08) — the deploy overlay env lock is also read here, once,
    at install/startup time (``init_deploy_overlay``). The cached snapshot
    feeds both the ``/api/version`` payload below and the ``/readyz`` overlay
    gate, so both surfaces serve the identical read-before-start identity.
    """
    init_deploy_overlay()
    cached_payload = build_version_payload()

    @app.get("/api/version", tags=["api-version"], include_in_schema=False)
    async def _api_version() -> dict:
        return cached_payload
