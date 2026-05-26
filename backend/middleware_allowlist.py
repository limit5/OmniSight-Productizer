"""Single-source public-path allowlist for middleware bypass decisions.

Path C of the Family ⑦ contract — see
``docs/sprint-s12/2026-05-16-v2-family7-allowlist-contract.md`` §4. This
module is the ONE place that answers *"may this path bypass the
auth / rate-limit / bootstrap-redirect / shutdown-503 / password-change
gates?"*. Every such middleware should consult :func:`is_public` (and,
for the bootstrap static-asset case, :func:`is_static_asset`) instead of
carrying its own private ``*_EXEMPT`` set.

This is ⑦-1bc: the helper module + its unit tests. It has NO live
consumers yet — the five middlewares still read their private lists. The
refactor that wires them to :func:`is_public` is ⑦-2bc (claude class);
do not wire consumers here (contract §9.4).

The drift class this dissolves
───────────────────────────────
N independently-edited path sets that morally should agree on a
public-path policy but mechanically can disagree — and any disagreement
is a security-or-availability bug depending on which side drifts
(contract §1). Hoisting the decision into one named object makes every
consumer grep-able and makes a future 6th-middleware-with-its-own-list
detectable at CI time (⑦-ContractTest, §7).

Match semantics (contract §4.4)
────────────────────────────────
:func:`is_public` is a *strict superset* of the historical union of the
five sibling allowlists. It tests BOTH the raw ``request.url.path`` and
its API-prefix-stripped form (so ``/api/v1/livez`` and ``/livez`` both
resolve True). Membership is split two ways:

  * :data:`PUBLIC_PATH_ALLOWLIST` — exact-match entries. The historical
    ``auth_baseline`` list used blanket ``startswith`` for every entry;
    the seed replaces that with a deliberate per-entry exact-vs-prefix
    decision, eliminating the accidental ``/docs`` → ``/docstore`` match
    (contract §4.4: "the seed eliminates the ambiguity").
  * :data:`PUBLIC_PATH_PREFIXES` — entries that genuinely need prefix
    semantics (family roots like ``/api/v1/webhooks/`` and the wizard's
    ``/cloudflare/``). Encoded separately per §4.4 because at least one
    current entry requires prefix matching.

What this module does NOT do (contract §4.5)
─────────────────────────────────────────────
  * No session / cookie / bearer check — auth is ``auth_baseline.py``'s
    job; this only answers "would this path be exempt from gating?".
  * No I/O — the sets are module-level immutable ``frozenset``s; the
    only call-time read is ``settings.api_prefix`` (a loaded singleton
    attribute, not an env lookup).
  * No logging — each consumer logs its own gate decisions.
  * No regex — membership is constant-time.

Adding a prefix to either set is a SECURITY decision. Each entry MUST
keep its justification comment, and any PR touching these sets REQUIRES
one reviewer from the ``non-ai-reviewer`` group (CLAUDE.md L1 Safety
Rules + Family ⑦ drift-contract policy).
"""

from __future__ import annotations

from typing import Final

from backend.api_versioning import api_relative_path
from backend.config import settings

# ═════════════════════════════════════════════════════════════════════
# Exact-match allowlist — union of the five sibling allowlists (§4.3)
# ═════════════════════════════════════════════════════════════════════
# Seeded from the UNION of:
#   1. AUTH_BASELINE_ALLOWLIST          (backend/auth_baseline.py)
#   2. _RATE_LIMIT_EXEMPT               (backend/main.py)
#   3. _PASSWORD_CHANGE_EXEMPT          (backend/main.py)
#   4. _GRACEFUL_SHUTDOWN_EXEMPT_RAW    (backend/main.py)
#   5. _BOOTSTRAP_EXEMPT_REL/_RAW (+ special-cases)  (backend/main.py)
# Duplicates eliminated; justification comments migrated. Trailing-slash
# family roots and /cloudflare/ live in PUBLIC_PATH_PREFIXES below;
# static-asset prefixes/suffixes live in is_static_asset (§4.3).
PUBLIC_PATH_ALLOWLIST: Final[frozenset[str]] = frozenset(
    {
        # ─── Liveness + readiness probes ──────────────────────────
        # Called by docker healthcheck + Caddy + /metrics/healthz
        # dashboards. Leaking "backend is up" is not a secret worth
        # gating.
        #
        # Probe policy (OP-1131, 2026-05-16):
        #   /health         — bare alias for /livez (cheap process
        #                     pulse). Convention used by Caddy and
        #                     several legacy external monitors that
        #                     probe /health by default. Same shallow
        #                     semantics as /livez — does NOT touch
        #                     DB / queues.
        #   /livez          — canonical cheap process pulse.
        #   /readyz         — DEEP readiness (DB + queues + deps).
        #                     Slower; reserved for orchestrator gates.
        #   /api/v1/health  — legacy alias for /livez (kept for
        #                     pre-/v1 dashboards still in the wild).
        #
        # NB: /health membership is the union state as of ⑦-1bc;
        # whether /health stays or leaves is the ⑦-FixHealth decision
        # (contract §6). Strict removal is FixHealth territory — this
        # seed keeps every union entry (§4.4 strict-superset).
        "/health",
        "/livez",
        "/readyz",
        "/healthz",
        "/api/v1/livez",
        "/api/v1/readyz",
        "/api/v1/healthz",
        "/api/v1/health",  # legacy alias for /livez
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
        "/api/v1/auth/forgot",  # password-reset-request flow
        "/api/v1/auth/webauthn/challenge",  # webauthn registration flow start
        "/api/v1/auth/webauthn/login",
        # API-relative auth forms contributed by the rate-limit,
        # password-change and bootstrap gates (which match against the
        # API-prefix-stripped path). is_public() also derives these via
        # _api_relative(), but they are pinned explicitly so the union
        # seed is self-documenting and the no-regression assertion can
        # check literal membership.
        #   /auth/login, /auth/logout     — _RATE_LIMIT_EXEMPT (login
        #                                   has its own K2 limiter;
        #                                   logout must work to clear a
        #                                   compromised session).
        #   /auth/change-password         — _PASSWORD_CHANGE_EXEMPT /
        #                                   _BOOTSTRAP_EXEMPT_REL: the
        #                                   user MUST be able to reach
        #                                   the change-password form
        #                                   while the K1 gate is forcing
        #                                   the change.
        #   /auth/whoami                  — _PASSWORD_CHANGE_EXEMPT: the
        #                                   client polls identity to
        #                                   render the change-password
        #                                   screen.
        "/auth/login",
        "/auth/logout",
        "/auth/change-password",
        "/auth/whoami",
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
        # ─── Bootstrap wizard surface (pre-setup flow) ───────────
        # _BOOTSTRAP_EXEMPT_RAW / _REL: served BEFORE any user exists.
        # Once bootstrap finalizes the bootstrap_required gate 503s
        # these, independent of auth.
        #   /              — the SPA shell / landing redirect target.
        #   /version       — build-info probe (API-relative form).
        #   /api/version   — build-info probe (raw form, special-cased
        #                     in _bootstrap_path_is_exempt).
        #   /bootstrap     — the wizard root (the /bootstrap/* subtree
        #                     is PUBLIC_PATH_PREFIXES below).
        #   /favicon.ico,
        #   /robots.txt    — browser auto-requests during the wizard.
        "/",
        "/version",
        "/api/version",
        "/bootstrap",
        "/favicon.ico",
        "/robots.txt",
    }
)


# ═════════════════════════════════════════════════════════════════════
# Prefix-match allowlist — entries that need startswith semantics (§4.4)
# ═════════════════════════════════════════════════════════════════════
# These are the "family root" entries where every path under the prefix
# is public. Encoded as prefixes (not exact entries) because at least one
# current entry requires prefix semantics; §4.4 sanctions the separate
# set in exactly this case.
PUBLIC_PATH_PREFIXES: Final[frozenset[str]] = frozenset(
    {
        # ─── Bootstrap wizard subtree ────────────────────────────
        # /bootstrap/* (raw + API-relative): the first-boot setup
        # wizard, by definition runs BEFORE any user is created.
        "/bootstrap/",
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
        # OAuth callback / authorize URLs — the browser arrives here
        # before a session cookie is set. The OAuth login handler either
        # redirects to the external provider or establishes the session
        # from the authorization code; after that one hit, the regular
        # session cookie carries the auth. Scoped narrowly to /auth/oauth/
        # so other /auth/* paths (logout, change-password, etc.) stay gated.
        "/api/v1/auth/oauth/",
        # ─── Server-Sent Events (SSE) ────────────────────────────
        # /events uses passive session-cookie auth at handler level
        # (the EventSourceResponse reads Cookie from scope). Browsers
        # send cookies on EventSource connections automatically, so
        # this allowlist entry is actually LESS permissive than it
        # looks — it just means the middleware doesn't reject the
        # initial handshake before the handler can read the cookie.
        # Still safe to restrict further once SSE paths are audited.
        "/api/v1/events/",
        # ─── Cloudflare tunnel embed (bootstrap wizard) ──────────
        # _BOOTSTRAP_EXEMPT_REL_PREFIXES: the wizard's Cloudflare
        # tunnel embed (B12 wizard) calls these endpoints before
        # login. The router itself still enforces operator RBAC once
        # bootstrap has finalized; this exemption only waives the
        # redirect during install.
        "/cloudflare/",
    }
)


# ═════════════════════════════════════════════════════════════════════
# Static-asset matchers (§4.3) — bootstrap-specific, NOT in the allowlist
# ═════════════════════════════════════════════════════════════════════
# Static assets are shaped differently from the auth allowlist: they are
# prefix- and suffix-based bootstrap exemptions, not "API endpoints that
# bypass auth". Keeping them in is_static_asset() keeps the allowlist
# focused on API surface (contract §4.3, Q11.2).
#
# Migrated from _BOOTSTRAP_EXEMPT_RAW_PREFIXES + _BOOTSTRAP_STATIC_SUFFIXES
# (backend/main.py).
_STATIC_ASSET_PREFIXES: Final[tuple[str, ...]] = (
    "/_next/",  # Next.js build output
    "/static/",  # backend-served static mount
    "/assets/",  # SPA bundled assets
    "/public/",  # public/ passthrough
)
_STATIC_ASSET_SUFFIXES: Final[tuple[str, ...]] = (
    ".css",
    ".js",
    ".map",
    ".ico",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".webp",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
)


def _api_relative(path: str) -> str:
    """Return ``path`` with any supported API version prefix stripped.

    Delegates to the single source of truth
    (:func:`backend.api_versioning.api_relative_path`) so this helper
    composes against the same normalization every gate uses (contract
    Q11.1). The fallback prefix is pinned to ``settings.api_prefix`` —
    an attribute read on the loaded config singleton, not an env lookup
    (§4.5 "no I/O at call time").
    """
    return api_relative_path(path, settings.api_prefix)


def is_public(path: str) -> bool:
    """Return True iff ``path`` may bypass the path-gating middlewares.

    Tests BOTH the raw ``path`` AND its API-prefix-stripped form against
    :data:`PUBLIC_PATH_ALLOWLIST` (exact) and :data:`PUBLIC_PATH_PREFIXES`
    (startswith), so ``/api/v1/livez`` and ``/livez`` both return True
    when ``/livez`` is in the set (contract §4.2 / §4.4).

    This is the ONLY function a middleware should call to ask "is this
    path public?" — no private allowlists, no inline
    ``path.startswith(...)`` checks. The drift contract (§7) enforces
    this at CI time. It does NOT check session/cookie/bearer, perform
    I/O, or log (§4.5).
    """
    rel = _api_relative(path)
    if path in PUBLIC_PATH_ALLOWLIST or rel in PUBLIC_PATH_ALLOWLIST:
        return True
    for prefix in PUBLIC_PATH_PREFIXES:
        if path.startswith(prefix) or rel.startswith(prefix):
            return True
    return False


def is_static_asset(path: str) -> bool:
    """Return True iff ``path`` is a static asset that bypasses the
    bootstrap-redirect gate.

    Sibling to :func:`is_public` per contract §4.3: static-asset
    matching is prefix/suffix-based (``/static/...``, ``*.css``) and is
    kept out of :data:`PUBLIC_PATH_ALLOWLIST` so the allowlist stays
    focused on API endpoints. Like :func:`is_public`, both the raw and
    API-prefix-stripped forms are checked.
    """
    rel = _api_relative(path)
    for prefix in _STATIC_ASSET_PREFIXES:
        if path.startswith(prefix) or rel.startswith(prefix):
            return True
    return path.endswith(_STATIC_ASSET_SUFFIXES) or rel.endswith(
        _STATIC_ASSET_SUFFIXES
    )
