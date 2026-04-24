"""Secure-by-default auth baseline middleware (S2-9 #354).

The PROBLEM this exists to fix
──────────────────────────────
Before 2026-04-20 every `APIRouter()` in `backend/routers/*.py` was
created bare. Whether an endpoint required a session was the individual
handler's decision, and a whole bunch of them (~237 endpoints across
24 routers) shipped with ZERO auth checks by accident — surfaced when
a `/invoke` rate-limit patch needed `current_user` and revealed the
endpoint had always been anonymous.

Relying on every future handler remembering to `Depends(current_user)`
is not a security posture, it's a bet. This middleware flips the
default so the codebase is secure-by-default: any authenticated-
sensitive path requires a session unless the path prefix is on a
SINGLE central allowlist (below), which is code-reviewed when it
grows.

The allowlist
──────────────
Every prefix here has a written justification. Do not extend this
list without adding the justification in the same commit and getting
a code review — this is the boundary between "trusted internal
surface" and "publicly reachable".

Mode gate — OMNISIGHT_AUTH_BASELINE_MODE
────────────────────────────────────────
    log       (default): middleware LOGs would-be-blocks at WARN
                         level but does NOT reject. Used during
                         rollout to sweep false positives.
    enforce              : middleware returns 401 on any path NOT
                         on the allowlist when no session is
                         present.
    off                  : middleware short-circuits; no behaviour
                         change. Emergency fallback only.

Interaction with per-handler `Depends(...)` checks
──────────────────────────────────────────────────
Orthogonal. This middleware is the floor (must-be-logged-in).
Router-level `Depends(require_role("admin"))` etc. stay in place
as RBAC on top — they do additional authz once auth is proven.
Removing a handler's Depends does NOT expose it, because this
middleware still gates it. Adding this middleware does NOT remove
any existing handler's Depends.

Spec: docs/ops/auth_baseline.md (to be written).
"""
from __future__ import annotations

import logging
import os
from typing import Callable, Final

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp

logger = logging.getLogger(__name__)


# ═════════════════════════════════════════════════════════════════════
# Allowlist — path prefixes that may be reached without a session
# ═════════════════════════════════════════════════════════════════════
# Entries must be the longest deterministic prefix that covers the
# legitimate public surface. Match semantics: `request.url.path
# .startswith(prefix)`. Order does not matter for correctness but
# roughly groups by concern for readability.
#
# Adding a prefix here is a security decision. Each entry has a
# justification comment. If you're tempted to add a prefix "just to
# stop the noise", STOP and either (a) add auth to that handler or
# (b) write the justification honestly.

AUTH_BASELINE_ALLOWLIST: Final[tuple[str, ...]] = (
    # ─── Liveness + readiness probes ──────────────────────────
    # Called by docker healthcheck + Caddy + /metrics/healthz
    # dashboards. Leaking "backend is up" is not a secret worth
    # gating.
    "/livez",
    "/readyz",
    "/healthz",
    "/api/v1/livez",
    "/api/v1/readyz",
    "/api/v1/healthz",
    "/api/v1/health",           # legacy alias

    # ─── Prometheus exposition ────────────────────────────────
    # Secondary gate exists: M7 bearer-token check fires if
    # OMNISIGHT_METRICS_TOKEN is set. Also not externally
    # reachable (Next.js rewrites only proxy /api/v1/*), so the
    # current surface is compose-internal only.
    "/metrics",
    "/api/v1/metrics",

    # ─── Auth entry points (users must be able to log in) ────
    # /auth/login + /auth/bootstrap + /auth/reset are the only
    # pre-session paths. /auth/logout, /auth/change-password etc.
    # are post-session and DO require auth — they are NOT on this
    # allowlist because they live under /api/v1/auth/logout which
    # does not match a /login or /reset prefix.
    "/api/v1/auth/login",
    "/api/v1/auth/bootstrap",
    "/api/v1/auth/reset",
    "/api/v1/auth/forgot",       # password-reset-request flow
    "/api/v1/auth/webauthn/challenge",  # webauthn registration flow start
    "/api/v1/auth/webauthn/login",

    # ─── Bootstrap wizard (pre-setup flow) ───────────────────
    # The /bootstrap/* family is the first-boot setup wizard —
    # by definition runs BEFORE any user has been created. Once
    # bootstrap completes, the bootstrap_required middleware
    # short-circuits these endpoints to 503, which is independent
    # of auth.
    "/api/v1/bootstrap/",

    # ─── External webhook receivers ──────────────────────────
    # GitHub / GitLab / Jira / Gerrit / Stripe fire these with
    # their own authentication (HMAC signatures, bearer tokens)
    # verified inside the handler. Session-based auth doesn't
    # apply to machine-to-machine callbacks.
    "/api/v1/webhooks/",

    # ChatOps webhooks — Discord / Teams / Line inbound. Each
    # handler validates the request's HMAC / signature header
    # using the platform-specific secret (see
    # backend/chatops_verification.py). Sibling endpoints under
    # /api/v1/chatops/ that are NOT /webhook/ (mirror, status)
    # are authenticated and NOT on the allowlist.
    "/api/v1/chatops/webhook/",

    # OIDC callback URL — the browser arrives here after the
    # external IdP redirects, BEFORE a session cookie is set.
    # The handler establishes the session from the authorization
    # code; after this one hit, the regular session cookie
    # carries the auth. Scoped narrowly to /auth/oidc/ so other
    # /auth/* paths (logout, change-password, etc.) stay gated.
    "/api/v1/auth/oidc/",

    # ─── Server-Sent Events (SSE) ────────────────────────────
    # /events uses passive session-cookie auth at handler level
    # (the EventSourceResponse reads Cookie from scope). Browsers
    # send cookies on EventSource connections automatically, so
    # this allowlist entry is actually LESS permissive than it
    # looks — it just means the middleware doesn't reject the
    # initial handshake before the handler can read the cookie.
    # Still safe to restrict further once SSE paths are audited.
    "/api/v1/events/",

    # ─── Static Next.js assets served via backend (if any) ───
    # Currently none; placeholder so future static paths can be
    # added explicitly rather than accidentally via a broader
    # prefix. REMOVE if not used by 2026-06-01.
    # "/static/",

    # ─── OpenAPI / docs ──────────────────────────────────────
    # S2-0 turns these off entirely in production. Allowlisted
    # here so dev/staging Swagger UI still works without logging
    # in to look at the API spec.
    "/docs",
    "/redoc",
    "/openapi.json",
    "/api/v1/docs",
    "/api/v1/redoc",
    "/api/v1/openapi.json",
)


# ═════════════════════════════════════════════════════════════════════
# Middleware
# ═════════════════════════════════════════════════════════════════════

def auth_baseline_mode() -> str:
    """Read the current mode at request time so an operator can flip
    it without rebooting (env vars are read per-request in dev — prod
    is static, env baked at container start, but re-reading is cheap
    enough).

    Task #90 / Step B.2 (2026-04-21): promoted from private ``_mode``
    to public. Callers who need the baseline mode should import
    ``auth_baseline_mode`` from this module; the function re-reads
    ``OMNISIGHT_AUTH_BASELINE_MODE`` every call so:

      * Per-request mode flips work (existing intent).
      * Per-worker env drift under ``uvicorn --workers N`` resolves
        correctly — each worker's env is independent; each worker's
        middleware picks up its own process's env on each call.

    NOT wired through ``ContextVar`` — that would scope per-request
    inside a single worker, but the existing per-call env read
    already achieves the same per-request freshness without the
    extra plumbing. A ContextVar would be redundant here (unlike
    ``_active_workspace`` in agents/tools.py, which holds data that
    shouldn't persist across requests at the worker level).
    """
    return (os.environ.get("OMNISIGHT_AUTH_BASELINE_MODE") or "log").strip().lower()


# Legacy alias kept because the middleware implementation below
# (and a couple of older tests) still refer to ``_mode``. Cheap to
# keep; renames at call sites can land in Epic 7 cleanup if desired.
_mode = auth_baseline_mode


def _path_allowed(path: str) -> bool:
    """Allowlist match. Kept as a module function so the unit test can
    call it directly without spinning up the Starlette app."""
    for prefix in AUTH_BASELINE_ALLOWLIST:
        if path.startswith(prefix):
            return True
    return False


async def _has_valid_cookie_session(request: Request) -> bool:
    """Return True if the request carries a valid session cookie.

    Under the Path B HA topology (2 backend replicas on one SQLite
    WAL + dashboard loads firing ~20 concurrent XHRs) the initial
    burst can briefly trigger ``sqlite3.OperationalError: database
    is locked`` even with ``PRAGMA busy_timeout=5000``. Treating
    that specific transient failure as "no session" ejects legit
    operators back to the login page; treating it as "session
    unknown, let the handler decide" is equally wrong for a gate
    whose whole job is to refuse when unsure. The compromise: retry
    the lookup once with a tiny asyncio sleep, then fail-closed if
    still locked. This absorbs the initial dashboard-load write
    storm without opening any new window to attackers — they still
    need a valid cookie, and a real locked DB for > few-hundred-ms
    usually means something else is already broken.
    """
    try:
        from backend.auth import SESSION_COOKIE, get_session
    except Exception as exc:
        logger.warning("auth_baseline: import of backend.auth failed: %s", exc)
        return False
    cookie = request.cookies.get(SESSION_COOKIE) or ""
    if not cookie:
        return False

    # First attempt.
    try:
        sess = await get_session(cookie)
        return sess is not None
    except Exception as exc:
        is_lock = "database is locked" in str(exc).lower()
        if not is_lock:
            # Non-lock exception → don't retry, fail closed.
            logger.warning(
                "auth_baseline: session lookup failed (non-transient): %s", exc,
            )
            return False
        logger.info(
            "auth_baseline: session lookup saw transient DB lock, "
            "retrying once (token_tail=%s)",
            cookie[-6:],
        )

    # One retry after a short sleep — enough for the competing writer
    # to drain, not enough to meaningfully delay the request.
    import asyncio
    await asyncio.sleep(0.05)
    try:
        sess = await get_session(cookie)
        return sess is not None
    except Exception as exc:
        logger.warning(
            "auth_baseline: session lookup still failed after retry: %s", exc,
        )
        return False


async def _has_valid_bearer_token(request: Request) -> bool:
    """Return True if the request carries a valid ``Authorization:
    Bearer <omni_...>`` API-key token.

    Added 2026-04-24 to close the A2 smoke-test gap: API-key-only
    integrations (operator scripts, CI jobs, CLI tools) previously got
    401'd at the baseline even though per-handler ``current_user``
    would have accepted them — forcing callers to send BOTH a cookie
    AND a Bearer as a workaround. The baseline now delegates bearer
    validation to ``backend.api_keys.validate_bearer`` so Bearer-only
    callers pass the floor.

    On match we cache the validated ``ApiKey`` onto
    ``request.state.api_key`` so the downstream ``current_user``
    dependency skips re-validation (otherwise each request would hit
    ``api_keys.validate_bearer`` twice and UPDATE ``last_used_at``
    twice).

    Multi-worker note (SOP Step 1): ``validate_bearer`` reads and
    writes the shared ``api_keys`` table via the asyncpg pool —
    answer (2) "coordinated through PG", identical across workers.
    """
    authz = request.headers.get("authorization") or ""
    if not authz.startswith("Bearer "):
        return False
    raw = authz[len("Bearer "):].strip()
    if not raw:
        return False

    try:
        from backend import api_keys
    except Exception as exc:
        logger.warning(
            "auth_baseline: import of backend.api_keys failed: %s", exc,
        )
        return False

    try:
        ip = request.client.host if request.client else ""
        key = await api_keys.validate_bearer(raw, ip=ip)
    except Exception as exc:
        logger.warning(
            "auth_baseline: bearer validation raised: %s", exc,
        )
        return False

    if key is not None:
        try:
            request.state.api_key = key
        except Exception:
            pass
        return True

    # Legacy fallback: OMNISIGHT_DECISION_BEARER env (pre-K6 single
    # shared secret). Mirror backend.auth._legacy_bearer_matches so
    # baseline acceptance matches what current_user would accept.
    import secrets as _secrets
    expected = (os.environ.get("OMNISIGHT_DECISION_BEARER") or "").strip()
    if expected and _secrets.compare_digest(raw, expected):
        return True
    return False


async def _has_valid_session(request: Request) -> bool:
    """Return True if the request is authenticated — via either a
    session cookie OR a valid API-key Bearer token. Kept as an
    umbrella so the middleware has one gate to call and tests that
    monkeypatch the name keep working unchanged.

    Cookie is tried first (cheaper; no DB UPDATE on hit). Bearer is
    tried only when the cookie path fails, which covers: (a) no
    cookie at all (Bearer-only scripts), (b) cookie present but
    expired/invalid while Bearer is fresh (operator with stale
    browser session who also set an API token).
    """
    if await _has_valid_cookie_session(request):
        return True
    if await _has_valid_bearer_token(request):
        return True
    return False


def install(app: ASGIApp) -> Callable:
    """Register the middleware. Call from backend.main after the
    existing CORS + routing middlewares are added."""

    from fastapi import FastAPI
    assert isinstance(app, FastAPI), "install() expects a FastAPI app"

    @app.middleware("http")
    async def auth_baseline(request: Request, call_next):
        mode = _mode()
        if mode == "off":
            return await call_next(request)

        # OPTIONS preflight requests never carry credentials and must
        # never be rejected — the CORS middleware above us has already
        # answered the preflight with the appropriate Allow-Origin /
        # Allow-Credentials headers.
        if request.method == "OPTIONS":
            return await call_next(request)

        path = request.url.path

        if _path_allowed(path):
            return await call_next(request)

        # Path NOT in the allowlist → require a session.
        if await _has_valid_session(request):
            return await call_next(request)

        # No session + non-allowlisted path.
        if mode == "log":
            # Advisory mode: LOG, then let it through so whatever
            # handler is behind it can decide what to do. Used during
            # rollout to discover false positives on the allowlist
            # without breaking production.
            logger.warning(
                "auth_baseline[log-only]: would-block %s %s "
                "(no session, not on allowlist)",
                request.method, path,
            )
            return await call_next(request)

        # enforce mode
        logger.info(
            "auth_baseline[enforce]: rejected %s %s (no session)",
            request.method, path,
        )
        return JSONResponse(
            status_code=401,
            content={
                "detail": "authentication required",
                "path": path,
            },
            headers={"WWW-Authenticate": "Cookie"},
        )

    return auth_baseline
