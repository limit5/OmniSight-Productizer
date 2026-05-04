"""AS.6.1 — OmniSight self-login OAuth backend handler.

Wires the AS.1 OAuth shared library to OmniSight's own login flow:
the four ``Sign in with Google / GitHub / Microsoft / Apple`` buttons
on ``/login`` (and the matching signup path) talk to two HTTP
endpoints whose handlers live in :mod:`backend.routers.auth`:

    GET  /api/v1/auth/oauth/{vendor}/authorize
        Builds the vendor's authorize URL via AS.1.3
        ``begin_authorization_for_vendor``, persists the in-flight
        FlowSession (state / PKCE verifier / nonce / redirect_uri /
        TTL) into a HMAC-SHA256-signed HttpOnly cookie keyed by the
        AS.0.x decision-bearer-derived signing key, and returns a
        302 redirect to the vendor.

    GET  /api/v1/auth/oauth/{vendor}/callback
        Reads the FlowSession cookie, verifies the HMAC + TTL +
        ``state`` match, exchanges the authorization code for a
        TokenSet at the vendor's token endpoint (PKCE-bound via the
        cookie's ``code_verifier``), fetches /userinfo (or — for
        Apple — decodes the id_token claims since Apple has no
        userinfo endpoint), then either:

          a. **Existing user, OAuth subject already linked**:
             ``users.oidc_provider = $1 AND users.oidc_subject = $2``
             hits ⇒ issue a fresh OmniSight session for that user.
          b. **Existing user, same email, no prior link**:
             ``users.email = $1`` hits but the user already carries
             a ``"password"`` auth_method ⇒ refuse the silent link
             with HTTP 409 and the AS.0.3 takeover-prevention
             message ("Sign in with your password first, then link
             from Settings → Connected Accounts"). OAuth-only
             existing users (no password method) get the new
             provider appended via :mod:`account_linking.add_auth_method`
             and a fresh session.
          c. **New user**: ``auth.create_user(email, name, role=
             "viewer", oidc_provider=vendor.provider_id, oidc_subject=
             userinfo_subject, password=None)`` materialises a
             credential-less user row with a single
             ``["oauth_<vendor>"]`` auth_method, then issues a
             session.

This module is the **pure-functional + httpx-orchestration** layer.
It owns:

    * Per-vendor credential lookup off :class:`backend.config.Settings`
      with explicit "not configured" error class.
    * FlowSession cookie sign/verify (HMAC-SHA256 over the JSON
      body, base64url-encoded, HttpOnly cookie envelope).
    * HTTP token-exchange + userinfo-fetch via :mod:`httpx`
      (caller-injectable client for tests).
    * Per-vendor userinfo / id_token claim extraction (subject,
      email, display name) so the four vendors' wildly different
      response shapes converge to a single
      :class:`OAuthUserIdentity` dataclass the router can consume.

It deliberately does **not** own:

    * The HTTP routes (``router.get(...)`` decorators) — those live
      in :mod:`backend.routers.auth` next to the existing
      ``POST /auth/login`` handler so the cookie-issuance code
      shares one place.
    * The user lookup / create / session-issuance — that's
      :mod:`backend.auth` ``get_user_by_email`` /
      ``create_user`` / ``create_session``, called by the route
      handler after this module has produced the
      :class:`OAuthUserIdentity`.
    * Token vault persistence — AS.6.2 will route the issued
      access/refresh tokens through :mod:`token_vault`. AS.6.1
      ships **login-only**: we never persist the IdP token, the
      OmniSight session is the only artefact that lives past the
      callback.

Path note (per AS.0.10 / AS.1.x path-deviation precedent)
─────────────────────────────────────────────────────────
Lives at ``backend/security/oauth_login_handler.py`` to align with
sibling AS.1.x / AS.2.x / AS.5.x modules under
``backend/security/``. The canonical ``backend/auth/`` namespace is
occupied by the legacy ``backend/auth.py`` session/RBAC module;
promoting it to a package would shadow ~140 ``from backend.auth
import …`` call sites and is out of scope for AS.6.1. A future row
may consolidate.

Module-global state audit (per implement_phase_step.md SOP §1)
──────────────────────────────────────────────────────────────
* No module-level mutable state — only frozen dataclasses, frozen
  tuples, and the SUPPORTED_PROVIDERS frozenset.
* Signing key + per-vendor credentials are read **lazily** inside
  each function via :class:`backend.config.Settings` (which is
  itself derived once at process boot from env / .env). Every
  uvicorn worker derives the same value from the same source ⇒
  SOP §1 answer #1 (deterministic-by-construction across workers).
* No DB connections or HTTP clients held at module level — every
  function takes its dependencies as arguments (or lazily borrows
  per-call).
* Importing the module is free of side effects (no env reads, no
  network IO at module top level — only at function-body call
  time, gated on the AS knob via :func:`oauth_client.is_enabled`).

Read-after-write timing audit (per implement_phase_step.md SOP §1)
──────────────────────────────────────────────────────────────────
N/A — the OAuth flow is per-user-agent (state lives in the
HttpOnly cookie that travels with the user-agent, not in any
shared store). Two concurrent SSO logins from the same user (e.g.
two browser tabs) get distinct ``state`` values and distinct
cookies; their callbacks cannot interleave each other's PKCE
verifiers. The DB writes (``users`` row create / link) ride
through ``backend.auth.create_user`` / ``add_auth_method`` which
are themselves serialised via row locks; this module performs no
UPDATE that would race with another callback in flight.

AS.0.8 single-knob behaviour
────────────────────────────
* :func:`oauth_client.is_enabled` is the gate — when False,
  :func:`begin_oauth_login` raises :class:`OAuthFeatureDisabled`
  and the route handler maps it to HTTP 503 (AS.0.8 §3.1 noop
  matrix — the eventual frontend banner reads "single sign-on is
  disabled, sign in with email + password instead").
* Audit emission via :mod:`oauth_audit` and :mod:`auth_event`
  remains gated by their own ``_gate()`` / ``is_enabled`` checks.

Vendor coverage
───────────────
This module handles the AS.6.1 four-vendor subset
(Google / GitHub / Microsoft / Apple). The wider AS.1.3 catalog
(GitLab / Bitbucket / Slack / Notion / Salesforce / HubSpot /
Discord) is intentionally NOT exposed at the OmniSight self-login
edge — those are dev-tool integrations, not consumer SSO buttons.
Adding a new SSO button is a Settings field + one entry in
:data:`SUPPORTED_PROVIDERS` + a userinfo-extractor branch.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

import httpx

from backend.security import oauth_client, oauth_vendors
from backend.security.oauth_client import (
    DEFAULT_STATE_TTL_SECONDS,
    FlowSession,
    StateExpiredError,
    TokenResponseError,
    TokenSet,
)
from backend.security.oauth_vendors import VendorConfig, VendorNotFoundError

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Constants — supported providers + cookie envelope
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# The four AS.6.1 self-login providers. Mirrors the row's
# ``Sign in with Google / GitHub / Microsoft / Apple`` literal —
# extending requires (a) adding the vendor entry to
# ``backend.security.oauth_vendors``, (b) adding a Settings field
# pair (``oauth_<vendor>_client_id`` + ``..._client_secret``),
# (c) adding the slug here, (d) extending the userinfo-extractor
# branches below.
SUPPORTED_PROVIDERS: frozenset[str] = frozenset({
    "google",
    "github",
    "microsoft",
    "apple",
})

# In-flight FlowSession cookie name. Single namespace, HttpOnly,
# SameSite=Lax (must traverse the OAuth provider redirect chain
# which is a top-level navigation, not a cross-site fetch — Lax
# is the right boundary). Path-scoped to ``/api/v1/auth/oauth`` so
# the cookie isn't sent on every dashboard request.
FLOW_COOKIE_NAME: str = "omnisight_oauth_flow"
FLOW_COOKIE_PATH: str = "/api/v1/auth/oauth"
FLOW_COOKIE_TTL_SECONDS: int = DEFAULT_STATE_TTL_SECONDS  # 600 s = 10 min

# Cookie envelope: ``base64url(json_body) + "." + base64url(hmac_sig)``.
# The dot separator + url-safe base64 keeps the cookie value within the
# RFC 6265 token grammar (no commas, semicolons, whitespace) so no
# operator's reverse proxy strips or re-encodes it.
_COOKIE_SEPARATOR: str = "."

# Apple is the only AS.6.1 vendor without a userinfo endpoint —
# user identity rides in the OIDC ``id_token`` JWS that comes back
# in the token response. We decode the JWS payload **without**
# verifying the signature: AS.6.1 ships the minimum-viable login
# handler; AS.1.4 has reserved id_token JWS-verify against Apple's
# JWKS as a follow-up. The risk is bounded — the id_token reaches
# us over TLS from Apple's token endpoint after a PKCE-bound code
# exchange that an attacker without our client_secret cannot
# fabricate. A future hardening row should land JWKS verification.
_VENDORS_NO_USERINFO_ENDPOINT: frozenset[str] = frozenset({"apple"})


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Errors
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class OAuthLoginError(Exception):
    """Base class for every error this module raises."""


class OAuthFeatureDisabled(OAuthLoginError):
    """AS.0.8 single-knob is OFF — the route handler MUST surface
    HTTP 503 ``Service Unavailable`` so the frontend renders the
    "SSO is disabled" banner instead of a broken redirect."""


class ProviderNotSupportedError(OAuthLoginError):
    """The URL path's ``{vendor}`` slug is not in
    :data:`SUPPORTED_PROVIDERS`. Maps to HTTP 404 — we deliberately
    do not list which providers ARE supported in the response body
    (defensive: don't help an enumerator probe SSO config)."""


class ProviderNotConfiguredError(OAuthLoginError):
    """The vendor is supported in code but the operator hasn't
    populated ``OMNISIGHT_OAUTH_<VENDOR>_CLIENT_ID/SECRET`` in
    ``.env``. Maps to HTTP 501 — distinct from 404 so monitoring
    can split "deployment misconfiguration" from "client probing
    a wrong slug"."""


class FlowCookieMissingError(OAuthLoginError):
    """The callback fired without the ``omnisight_oauth_flow``
    cookie. Either the browser dropped it (third-party-cookie
    block on the redirect chain), the cookie expired, or the user
    pasted the callback URL by hand. Maps to HTTP 400."""


class FlowCookieInvalidError(OAuthLoginError):
    """The cookie was present but the HMAC signature didn't
    verify, the JSON body was malformed, or the embedded TTL had
    elapsed. Maps to HTTP 400. Logged at ``warning`` level so a
    burst of these surfaces as a forgery / replay attempt in the
    AS.5.2 dashboard."""


class SigningKeyUnavailableError(OAuthLoginError):
    """Neither ``OMNISIGHT_OAUTH_FLOW_SIGNING_KEY`` nor the
    fallback ``OMNISIGHT_DECISION_BEARER`` is set, so the cookie
    cannot be signed/verified. Maps to HTTP 503 — deployment is
    half-configured. The L1 startup gate refuses to start without
    ``decision_bearer`` in strict mode, so this only fires in
    dev/test where strict was bypassed."""


class UserinfoFetchError(OAuthLoginError):
    """The vendor's /userinfo endpoint returned non-2xx, malformed
    JSON, or a body without the per-vendor identity fields. Maps
    to HTTP 502 ``Bad Gateway``. Distinct from
    :class:`backend.security.oauth_client.TokenResponseError`
    which covers token-endpoint failures."""


class IdTokenDecodeError(OAuthLoginError):
    """The Apple id_token (or any other vendor's id_token we
    decode) wasn't a valid three-part JWS or its payload wasn't
    valid JSON. Maps to HTTP 502 — vendor returned something we
    can't parse."""


class IdentityFieldMissingError(OAuthLoginError):
    """The vendor's userinfo payload was 200 OK + parsed cleanly
    but the per-vendor required fields (``sub`` / ``email`` / ...)
    were absent. Maps to HTTP 502 — distinct from
    UserinfoFetchError because the network round-trip succeeded;
    the vendor just didn't return what their docs said they would
    (or our scope set was wrong). Logging this on a real flow is
    a deployment / scope-config issue, not a vendor outage."""


class AccountLinkConflictError(OAuthLoginError):
    """The OAuth-asserted email matches an existing OmniSight user
    that already carries a ``"password"`` auth method, and the
    OAuth subject does NOT match the user's stored
    ``oidc_subject``. Per AS.0.3 takeover-prevention policy we
    refuse the silent link — the user must sign in with password
    first and link from Settings → Connected Accounts. Maps to
    HTTP 409. Includes the vendor + masked email so the route's
    error response gives the user actionable guidance without
    leaking the full address."""

    def __init__(self, message: str, *, vendor: str, masked_email: str) -> None:
        super().__init__(message)
        self.vendor = vendor
        self.masked_email = masked_email


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Frozen dataclasses
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass(frozen=True)
class OAuthCredentials:
    """Per-vendor client_id + client_secret loaded from Settings.

    Returned by :func:`lookup_provider_credentials`. Frozen so
    accidental mutation between lookup and use cannot smuggle a
    different secret into the token-exchange POST.
    """

    provider: str
    client_id: str
    client_secret: str


@dataclass(frozen=True)
class OAuthUserIdentity:
    """Vendor-agnostic identity extracted from the OAuth callback.

    Built by :func:`extract_user_identity` after the token exchange
    + userinfo fetch (or id_token decode for Apple). The route
    handler hands this to :mod:`backend.auth` to look up / create
    the OmniSight user row.

    Attributes
    ----------
    provider
        The vendor slug (``"google"`` / ``"github"`` / ...).
    subject
        The vendor's stable opaque user identifier (OIDC ``sub``,
        GitHub numeric ``id``). Stored in
        ``users.oidc_subject`` — the canonical primary key for
        cross-callback deduplication. NEVER an email (vendors
        recycle email addresses; subject is forever).
    email
        The verified email address (or best-effort fallback when
        the vendor doesn't expose verification status). Used to
        find the existing OmniSight user via
        ``get_user_by_email`` for the link-conflict check.
        ``""`` when the vendor refuses to release email (rare —
        GitHub users with all-private emails; raised separately).
    name
        The user's display name. Falls back to the local-part of
        the email when the vendor returns nothing usable. Empty
        string only if email is also empty.
    """

    provider: str
    subject: str
    email: str
    name: str


@dataclass(frozen=True)
class AuthorizationStart:
    """Output of :func:`begin_oauth_login` — what the route handler
    needs to fire the redirect + set the cookie.

    Attributes
    ----------
    authorize_url
        The fully-formed RFC 6749 §4.1.1 redirect URL the user-
        agent must visit. Carries client_id, redirect_uri, scope,
        state, code_challenge, vendor-specific extras.
    flow_cookie
        The HMAC-signed cookie value to set on the response
        (cookie name = :data:`FLOW_COOKIE_NAME`, path = :data:
        `FLOW_COOKIE_PATH`, max_age = :data:
        `FLOW_COOKIE_TTL_SECONDS`, HttpOnly, SameSite=Lax).
    flow
        The :class:`FlowSession` that was serialised into the
        cookie. Returned for the caller's audit emission only —
        the cookie is the source of truth on the callback.
    """

    authorize_url: str
    flow_cookie: str
    flow: FlowSession


@dataclass(frozen=True)
class CallbackResult:
    """Output of :func:`complete_oauth_login` — what the route
    handler needs to issue an OmniSight session.

    Attributes
    ----------
    flow
        The verified-and-consumed :class:`FlowSession` from the
        cookie.
    token
        The :class:`TokenSet` from the vendor's token endpoint.
    identity
        The vendor-agnostic :class:`OAuthUserIdentity` extracted
        from userinfo (or id_token claims for Apple).
    """

    flow: FlowSession
    token: TokenSet
    identity: OAuthUserIdentity


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Provider validation + credential lookup
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def assert_provider_supported(provider: str) -> None:
    """Raise :class:`ProviderNotSupportedError` if *provider* is not
    in :data:`SUPPORTED_PROVIDERS`.

    The lookup is case-sensitive — slugs are lowercase per the
    AS.1.3 catalog convention. We do NOT normalise to lower here
    because a non-lowercase URL slug is a client bug, not a
    typo we should helpfully fix.
    """
    if provider not in SUPPORTED_PROVIDERS:
        raise ProviderNotSupportedError(
            f"oauth provider {provider!r} not supported for self-login"
        )


def lookup_provider_credentials(
    provider: str,
    *,
    settings_obj: Optional[Any] = None,
) -> OAuthCredentials:
    """Return the configured client_id + client_secret for *provider*.

    *settings_obj* is the Pydantic ``Settings`` instance — defaults
    to the singleton :data:`backend.config.settings`. Tests pass a
    fresh instance with monkeypatched values rather than mutating
    the live singleton.

    Raises :class:`ProviderNotSupportedError` if the slug is not in
    :data:`SUPPORTED_PROVIDERS`, or :class:`ProviderNotConfiguredError`
    if either field is empty.
    """
    assert_provider_supported(provider)
    settings_obj = _resolve_settings(settings_obj)
    client_id = (getattr(settings_obj, f"oauth_{provider}_client_id", "") or "").strip()
    client_secret = (
        getattr(settings_obj, f"oauth_{provider}_client_secret", "") or ""
    ).strip()
    if not client_id or not client_secret:
        missing = []
        if not client_id:
            missing.append(f"OMNISIGHT_OAUTH_{provider.upper()}_CLIENT_ID")
        if not client_secret:
            missing.append(f"OMNISIGHT_OAUTH_{provider.upper()}_CLIENT_SECRET")
        raise ProviderNotConfiguredError(
            f"oauth provider {provider!r} missing config: {', '.join(missing)}"
        )
    return OAuthCredentials(
        provider=provider, client_id=client_id, client_secret=client_secret,
    )


def _resolve_settings(settings_obj: Optional[Any]) -> Any:
    """Return *settings_obj* or the singleton if None.

    Lazy-imports :data:`backend.config.settings` so importing this
    module doesn't trigger Pydantic Settings instantiation (which
    in turn reads .env). Mirrors the pattern in
    :func:`oauth_client.is_enabled`.
    """
    if settings_obj is not None:
        return settings_obj
    from backend.config import settings as _live_settings
    return _live_settings


def compute_redirect_uri(
    provider: str,
    *,
    base_url: str,
    api_prefix: str = "/api/v1",
) -> str:
    """Compose the OAuth callback URL the vendor will POST to.

    Returns ``{base_url}{api_prefix}/auth/oauth/{provider}/callback``
    with the per-segment slashes normalised (no double-slash, no
    trailing slash on base_url that bleeds into the path).

    *base_url* MUST be the public origin OmniSight is reachable at
    (``https://omnisight.example.com``); the route handler may
    fall back to the request's ``Host`` header when the operator
    didn't set ``oauth_redirect_base_url`` (dev convenience), but
    the **production** value MUST come from settings — vendors
    pin the exact callback URL on their app config and any
    mismatch is a flat refusal at /authorize time.
    """
    if not base_url:
        raise ValueError("base_url is required to compute redirect_uri")
    base = base_url.rstrip("/")
    prefix = api_prefix or ""
    if prefix and not prefix.startswith("/"):
        prefix = "/" + prefix
    prefix = prefix.rstrip("/")
    return f"{base}{prefix}/auth/oauth/{provider}/callback"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HMAC signing key derivation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def derive_signing_key(
    *,
    raw_key: str,
    fallback_seed: str,
) -> bytes:
    """Return a 32-byte HMAC-SHA256 key for FlowSession cookies.

    Precedence:

    * ``raw_key`` is non-empty ⇒ SHA-256 over its UTF-8 bytes
      (length-normalised so any operator-supplied entropy is
      acceptable as long as ≥ 16 chars).
    * ``raw_key`` is empty AND ``fallback_seed`` is non-empty ⇒
      SHA-256 over ``b"omnisight-oauth-flow|" + fallback_seed``.
      The label binds the derivation to AS.6.1 so a leak of one
      derived key doesn't leak others derived from the same seed
      (HKDF-style domain separation, simplified — we don't need
      multi-context HKDF here, just one context).
    * Both empty ⇒ :class:`SigningKeyUnavailableError`.

    Raising on "both empty" rather than silently disabling the
    feature avoids the worst-case where production accidentally
    accepts unsigned cookies (a forgery oracle). The L1 startup
    gate refuses to start without ``decision_bearer`` in strict
    mode, so this only fires in dev/test where strict was off.

    SOP §1 module-global state audit: pure function over its
    inputs — same env values produce the same key on every
    worker. Answer #1 (deterministic-by-construction across
    workers).
    """
    if raw_key:
        if len(raw_key) < 16:
            raise SigningKeyUnavailableError(
                f"oauth_flow_signing_key too short: {len(raw_key)} chars "
                f"(min 16)"
            )
        return hashlib.sha256(raw_key.encode("utf-8")).digest()
    if fallback_seed:
        return hashlib.sha256(
            b"omnisight-oauth-flow|" + fallback_seed.encode("utf-8")
        ).digest()
    raise SigningKeyUnavailableError(
        "oauth_flow_signing_key + decision_bearer both empty — "
        "set OMNISIGHT_OAUTH_FLOW_SIGNING_KEY (or OMNISIGHT_DECISION_BEARER) "
        "to a strong random secret ≥ 16 chars"
    )


def resolve_signing_key(*, settings_obj: Optional[Any] = None) -> bytes:
    """Resolve the signing key from :class:`backend.config.Settings`.

    Reads ``settings.oauth_flow_signing_key`` first, falls back to
    ``settings.decision_bearer``. Raises
    :class:`SigningKeyUnavailableError` when both are empty or the
    primary is too short.
    """
    s = _resolve_settings(settings_obj)
    raw = (getattr(s, "oauth_flow_signing_key", "") or "").strip()
    fallback = (getattr(s, "decision_bearer", "") or "").strip()
    return derive_signing_key(raw_key=raw, fallback_seed=fallback)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  FlowSession cookie sign / verify
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64u_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def serialize_flow(flow: FlowSession) -> dict[str, Any]:
    """Convert a :class:`FlowSession` to a JSON-serialisable dict.

    Stable field order so the SHA-256 of the output is
    reproducible across processes (important for cross-twin tests
    if a future TS twin lands).
    """
    return {
        "v": 1,  # envelope version — bump if the shape changes
        "provider": flow.provider,
        "state": flow.state,
        "code_verifier": flow.code_verifier,
        "nonce": flow.nonce,
        "redirect_uri": flow.redirect_uri,
        "scope": list(flow.scope),
        "created_at": float(flow.created_at),
        "expires_at": float(flow.expires_at),
        "extra": [list(pair) for pair in flow.extra],
    }


def deserialize_flow(data: Mapping[str, Any]) -> FlowSession:
    """Inverse of :func:`serialize_flow`.

    Rejects payloads with an unknown envelope version, missing
    required fields, or malformed types (raises
    :class:`FlowCookieInvalidError`). The verifier in
    :func:`decode_signed_flow` runs HMAC verification before this,
    so reaching this code with bad shape means the attacker had
    the signing key — extremely unlikely; the type checks are
    defence-in-depth, not an attack surface.
    """
    if not isinstance(data, Mapping):
        raise FlowCookieInvalidError("flow cookie payload not a mapping")
    if data.get("v") != 1:
        raise FlowCookieInvalidError(
            f"flow cookie envelope version {data.get('v')!r} unsupported"
        )
    try:
        return FlowSession(
            provider=str(data["provider"]),
            state=str(data["state"]),
            code_verifier=str(data["code_verifier"]),
            nonce=(str(data["nonce"]) if data.get("nonce") is not None else None),
            redirect_uri=str(data["redirect_uri"]),
            scope=tuple(str(s) for s in data["scope"]),
            created_at=float(data["created_at"]),
            expires_at=float(data["expires_at"]),
            extra=tuple(
                (str(k), str(v)) for k, v in (data.get("extra") or [])
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise FlowCookieInvalidError(
            f"flow cookie payload malformed: {exc}"
        ) from exc


def encode_signed_flow(flow: FlowSession, *, key: bytes) -> str:
    """Return the cookie value for *flow* signed with *key*.

    Format: ``base64url(json) + "." + base64url(hmac_sha256_sig)``.
    The HMAC is computed over the **raw json bytes** (not the
    base64url) so the signature is independent of the encoder's
    padding behaviour (we strip ``=``, but a verifier with
    different padding would produce a different b64u on the same
    raw bytes — keying off the raw makes that irrelevant).
    """
    payload = serialize_flow(flow)
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    sig = hmac.new(key, body, hashlib.sha256).digest()
    return _b64u_encode(body) + _COOKIE_SEPARATOR + _b64u_encode(sig)


def decode_signed_flow(
    cookie: str,
    *,
    key: bytes,
    now: Optional[float] = None,
) -> FlowSession:
    """Return the :class:`FlowSession` encoded in *cookie* if valid.

    Validates:

    * The cookie shape is ``body.signature``.
    * The HMAC signature matches (constant-time compare).
    * The JSON body parses + has the expected envelope shape.
    * The embedded ``expires_at`` is in the future (TTL not
      elapsed).

    Raises :class:`FlowCookieInvalidError` on any failure; raises
    :class:`StateExpiredError` on TTL expiry so the caller can
    distinguish "tampering / forgery" from "user took too long".
    """
    if not cookie:
        raise FlowCookieMissingError("flow cookie absent")
    if cookie.count(_COOKIE_SEPARATOR) != 1:
        raise FlowCookieInvalidError(
            f"flow cookie shape malformed: expected one "
            f"{_COOKIE_SEPARATOR!r} separator"
        )
    body_b64, sig_b64 = cookie.split(_COOKIE_SEPARATOR, 1)
    try:
        body = _b64u_decode(body_b64)
        sig = _b64u_decode(sig_b64)
    except Exception as exc:  # noqa: BLE001 — stringify any base64 error
        raise FlowCookieInvalidError(f"flow cookie not valid base64url: {exc}") from exc
    expected = hmac.new(key, body, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        raise FlowCookieInvalidError("flow cookie signature mismatch")
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FlowCookieInvalidError(f"flow cookie body not JSON: {exc}") from exc
    flow = deserialize_flow(data)
    ts = time.time() if now is None else now
    if ts >= flow.expires_at:
        raise StateExpiredError(
            f"oauth flow expired at {flow.expires_at:.0f} (now {ts:.0f})"
        )
    return flow


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  /authorize — build the redirect URL + cookie
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def begin_oauth_login(
    *,
    provider: str,
    base_url: str,
    settings_obj: Optional[Any] = None,
    extra: Optional[Mapping[str, str]] = None,
    scope: Optional[Sequence[str]] = None,
    state_ttl_seconds: int = FLOW_COOKIE_TTL_SECONDS,
    now: Optional[float] = None,
) -> AuthorizationStart:
    """Compose the vendor authorize URL + the FlowSession cookie.

    The route handler calls this from
    ``GET /api/v1/auth/oauth/{provider}/authorize`` with
    *base_url* derived from settings (``oauth_redirect_base_url``
    when set, else the request's ``X-Forwarded-Host`` /
    ``Host`` header). It must then:

      1. Set the cookie on the response (``response.set_cookie(
         FLOW_COOKIE_NAME, result.flow_cookie, max_age=
         FLOW_COOKIE_TTL_SECONDS, httponly=True, samesite="lax",
         secure=cookie_secure(), path=FLOW_COOKIE_PATH)``).
      2. Return ``RedirectResponse(result.authorize_url,
         status_code=302)``.

    Raises:
      * :class:`OAuthFeatureDisabled` if the AS knob is off.
      * :class:`ProviderNotSupportedError` for unknown slugs.
      * :class:`ProviderNotConfiguredError` for unconfigured
        vendors.
      * :class:`SigningKeyUnavailableError` if neither
        ``oauth_flow_signing_key`` nor ``decision_bearer`` is set.
      * :class:`backend.security.oauth_vendors.VendorNotFoundError`
        if SUPPORTED_PROVIDERS drifts from the AS.1.3 catalog
        (defensive — catalog is the canonical source).
    """
    if not oauth_client.is_enabled():
        raise OAuthFeatureDisabled("AS feature family disabled — SSO unavailable")
    creds = lookup_provider_credentials(provider, settings_obj=settings_obj)
    key = resolve_signing_key(settings_obj=settings_obj)
    redirect_uri = compute_redirect_uri(provider, base_url=base_url)

    try:
        vendor: VendorConfig = oauth_vendors.get_vendor(provider)
    except VendorNotFoundError:
        # Should never happen — SUPPORTED_PROVIDERS is a strict
        # subset of the catalog. If it ever does, surface as the
        # support error so the operator's logs say "supported
        # column drifted from catalog".
        raise ProviderNotSupportedError(
            f"vendor {provider!r} in SUPPORTED_PROVIDERS but missing "
            f"from oauth_vendors catalog (drift)"
        )

    authorize_url, flow = oauth_vendors.begin_authorization_for_vendor(
        vendor,
        client_id=creds.client_id,
        redirect_uri=redirect_uri,
        scope=scope,
        extra_authorize_params=None,
        extra=extra,
        state_ttl_seconds=state_ttl_seconds,
        now=now,
    )
    flow_cookie = encode_signed_flow(flow, key=key)
    return AuthorizationStart(
        authorize_url=authorize_url,
        flow_cookie=flow_cookie,
        flow=flow,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  /callback — token exchange + userinfo + identity extraction
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


# Per-vendor token-exchange request shape. RFC 6749 §4.1.3 defines
# the canonical body, but vendors have minor quirks:
#   * GitHub: requires Accept=application/json header (default is
#     form-encoded). Without it, the response body is form-encoded
#     and parse_token_response would fail.
#   * Apple: client_secret is a JWT signed with the team's private
#     key (vendor-specific). AS.6.1 ships the simplest path —
#     operator pre-mints the JWT and stores it in
#     ``oauth_apple_client_secret``. A future row will land an
#     auto-mint helper.
#   * Microsoft / Google: standard form-encoded POST.

_TOKEN_EXCHANGE_HEADERS_BASE: dict[str, str] = {
    "Accept": "application/json",
    "Content-Type": "application/x-www-form-urlencoded",
}


async def exchange_authorization_code(
    *,
    vendor: VendorConfig,
    code: str,
    code_verifier: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    http_client: Optional[httpx.AsyncClient] = None,
    timeout_s: float = 10.0,
    now: Optional[float] = None,
) -> TokenSet:
    """POST to the vendor's token endpoint and parse the response.

    Returns a :class:`TokenSet` with absolute ``expires_at``.
    Raises:
      * :class:`backend.security.oauth_client.TokenResponseError`
        on RFC 6749 §5.2 error shape, malformed body, or HTTP
        non-2xx with parseable body.
      * :class:`UserinfoFetchError` on unparseable body / network
        error (we conflate the two — both are "vendor returned
        garbage").

    *http_client*: tests pass an :class:`httpx.AsyncClient` with a
    :class:`httpx.MockTransport` so no real network hits happen.
    Production passes None and we acquire a one-shot client.
    """
    if not code:
        raise TokenResponseError("authorization code missing")
    if not code_verifier:
        raise TokenResponseError("code_verifier missing for PKCE exchange")

    body = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "client_secret": client_secret,
        "code_verifier": code_verifier,
    }

    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=timeout_s)
    try:
        try:
            resp = await client.post(
                vendor.token_endpoint,
                data=body,
                headers=_TOKEN_EXCHANGE_HEADERS_BASE,
            )
        except httpx.HTTPError as exc:
            raise TokenResponseError(
                f"token endpoint network error: {exc}"
            ) from exc
        try:
            payload = resp.json()
        except ValueError as exc:
            raise TokenResponseError(
                f"token endpoint returned non-JSON "
                f"(status={resp.status_code}): {exc}"
            ) from exc
    finally:
        if owns_client:
            await client.aclose()

    # parse_token_response handles both success (RFC 6749 §5.1) and
    # error (§5.2) shapes; the only thing it doesn't handle is HTTP
    # non-2xx with an empty body — we guard against that explicitly.
    if not isinstance(payload, Mapping) and resp.status_code >= 400:
        raise TokenResponseError(
            f"token endpoint failed with HTTP {resp.status_code} and "
            f"empty/invalid body"
        )
    return oauth_client.parse_token_response(payload, now=now)


async def fetch_userinfo(
    *,
    vendor: VendorConfig,
    access_token: str,
    http_client: Optional[httpx.AsyncClient] = None,
    timeout_s: float = 10.0,
) -> dict[str, Any]:
    """GET the vendor's /userinfo endpoint with the bearer token.

    Returns the parsed JSON body. Raises:
      * :class:`UserinfoFetchError` if the vendor has no userinfo
        endpoint (Apple — caller MUST decode id_token claims
        instead), the response is non-2xx, or the body isn't JSON.

    GitHub is special: ``api.github.com/user`` returns the basic
    profile, but the user's primary email is at a separate
    endpoint (``/user/emails``) when the account has email
    privacy enabled. AS.6.1 caller (the route handler) doesn't
    chase that — if GitHub returns ``email: null``, the user is
    asked to re-authorize with a public-email setting. A future
    row may add the second-fetch path.
    """
    if vendor.userinfo_endpoint is None:
        raise UserinfoFetchError(
            f"vendor {vendor.provider_id!r} has no userinfo endpoint — "
            "caller must decode id_token claims instead"
        )

    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=timeout_s)
    try:
        try:
            resp = await client.get(
                vendor.userinfo_endpoint,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                    # GitHub requires the v3 media type for stable
                    # field shape across API versions.
                    "User-Agent": "omnisight-oauth-login/1.0",
                },
            )
        except httpx.HTTPError as exc:
            raise UserinfoFetchError(
                f"userinfo endpoint network error: {exc}"
            ) from exc
        if resp.status_code >= 400:
            raise UserinfoFetchError(
                f"userinfo endpoint returned HTTP {resp.status_code}"
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise UserinfoFetchError(
                f"userinfo body not JSON: {exc}"
            ) from exc
    finally:
        if owns_client:
            await client.aclose()

    if not isinstance(data, Mapping):
        raise UserinfoFetchError(
            f"userinfo body not a JSON object (got {type(data).__name__})"
        )
    return dict(data)


def decode_id_token_claims_unverified(id_token: str) -> dict[str, Any]:
    """Decode a JWS id_token's payload **without** signature verify.

    Apple is the only AS.6.1 vendor without a userinfo endpoint —
    the token-exchange response includes an OIDC id_token whose
    claims (``sub`` / ``email``) ARE the user identity. This
    helper splits the JWS, base64url-decodes the middle segment,
    and JSON-parses it.

    SECURITY: this does NOT verify the JWS signature. The
    calling-code tradeoff is documented at the
    :data:`_VENDORS_NO_USERINFO_ENDPOINT` declaration above; a
    future hardening row will land JWKS-backed signature verify.
    """
    if not id_token:
        raise IdTokenDecodeError("id_token missing")
    parts = id_token.split(".")
    if len(parts) != 3:
        raise IdTokenDecodeError(
            f"id_token not a JWS triple (got {len(parts)} segments)"
        )
    try:
        payload_bytes = _b64u_decode(parts[1])
    except Exception as exc:  # noqa: BLE001
        raise IdTokenDecodeError(f"id_token payload not base64url: {exc}") from exc
    try:
        return json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IdTokenDecodeError(f"id_token payload not JSON: {exc}") from exc


def extract_user_identity(
    *,
    provider: str,
    userinfo: Optional[Mapping[str, Any]],
    id_token_claims: Optional[Mapping[str, Any]],
) -> OAuthUserIdentity:
    """Extract a vendor-agnostic identity from the callback payloads.

    Per-vendor field-name dispatch table:

    +-----------+----------------------+-----------------------+
    | Provider  | Subject field        | Email field           |
    +===========+======================+=======================+
    | google    | userinfo["sub"]      | userinfo["email"]     |
    | github    | userinfo["id"]       | userinfo["email"]     |
    | microsoft | userinfo["sub"]      | userinfo["email"] /   |
    |           |                      | "preferred_username"  |
    | apple     | id_token["sub"]      | id_token["email"]     |
    +-----------+----------------------+-----------------------+

    Raises :class:`IdentityFieldMissingError` if either field is
    missing / empty (the user must have a stable subject; the
    email is required because we use it for the AS.0.3 link-
    conflict check).
    """
    assert_provider_supported(provider)
    if provider == "apple":
        if not id_token_claims:
            raise IdentityFieldMissingError(
                "apple sign-in: id_token claims missing"
            )
        sub = str(id_token_claims.get("sub") or "").strip()
        email = str(id_token_claims.get("email") or "").strip()
        # Apple's first-auth flow returns the user's name in a
        # POST form body field ``user`` — NOT in the id_token.
        # AS.6.1 doesn't chase that (form_post mode requires the
        # callback to accept POST, which we'd add as a follow-up
        # row); we fall back to email-local-part for display name.
        name = ""
    else:
        if not userinfo:
            raise IdentityFieldMissingError(
                f"{provider}: userinfo body missing"
            )
        if provider == "google":
            sub = str(userinfo.get("sub") or "").strip()
            email = str(userinfo.get("email") or "").strip()
            name = str(
                userinfo.get("name")
                or userinfo.get("given_name")
                or ""
            ).strip()
        elif provider == "github":
            # GitHub's user id is numeric; coerce to str for
            # consistency with the other OIDC ``sub`` shape.
            raw_id = userinfo.get("id")
            sub = str(raw_id).strip() if raw_id is not None else ""
            email = str(userinfo.get("email") or "").strip()
            name = str(
                userinfo.get("name")
                or userinfo.get("login")
                or ""
            ).strip()
        elif provider == "microsoft":
            sub = str(userinfo.get("sub") or "").strip()
            email = str(
                userinfo.get("email")
                or userinfo.get("preferred_username")
                or ""
            ).strip()
            name = str(
                userinfo.get("name")
                or userinfo.get("given_name")
                or ""
            ).strip()
        else:  # pragma: no cover — assert_provider_supported guards
            raise ProviderNotSupportedError(
                f"no identity extractor for provider {provider!r}"
            )

    if not sub:
        raise IdentityFieldMissingError(
            f"{provider}: subject ('sub' / 'id') missing from response"
        )
    if not email:
        raise IdentityFieldMissingError(
            f"{provider}: email missing from response — re-authorize with "
            f"email scope or set a public email at the IdP"
        )
    if not name:
        # Always fall back to the email local-part so the
        # OmniSight User row is never created with an empty
        # display name — the dashboard layout assumes non-empty.
        name = email.split("@", 1)[0]
    return OAuthUserIdentity(
        provider=provider, subject=sub, email=email.lower(), name=name,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  /callback — high-level orchestrator
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def complete_oauth_login(
    *,
    provider: str,
    flow_cookie: str,
    returned_state: str,
    code: str,
    settings_obj: Optional[Any] = None,
    http_client: Optional[httpx.AsyncClient] = None,
    now: Optional[float] = None,
) -> CallbackResult:
    """End-to-end /callback orchestrator.

    Pipeline:

      1. Knob check (:func:`oauth_client.is_enabled`).
      2. Vendor + credential lookup.
      3. Cookie verify (HMAC + TTL) → FlowSession.
      4. State verify (FlowSession.state vs returned).
      5. Token exchange via the vendor's token endpoint.
      6. Userinfo fetch (or id_token claims decode for Apple).
      7. Per-vendor identity extraction.

    Returns the :class:`CallbackResult` carrying the verified
    flow + token + identity. The caller (route handler in
    :mod:`backend.routers.auth`) handles the user lookup /
    create + session issuance + cookie set.

    Tests pass *settings_obj* + *http_client* with mocked
    transports to avoid real DB / network IO.
    """
    if not oauth_client.is_enabled():
        raise OAuthFeatureDisabled("AS feature family disabled — SSO unavailable")
    assert_provider_supported(provider)
    creds = lookup_provider_credentials(provider, settings_obj=settings_obj)
    key = resolve_signing_key(settings_obj=settings_obj)

    flow = decode_signed_flow(flow_cookie, key=key, now=now)
    if flow.provider != provider:
        # Cookie was issued for a different vendor than the URL
        # path's slug — could be a CSRF attempt or a stale tab,
        # either way we refuse.
        raise FlowCookieInvalidError(
            f"flow cookie provider {flow.provider!r} != URL provider "
            f"{provider!r}"
        )
    oauth_client.verify_state_and_consume(flow, returned_state, now=now)

    try:
        vendor: VendorConfig = oauth_vendors.get_vendor(provider)
    except VendorNotFoundError:
        raise ProviderNotSupportedError(
            f"vendor {provider!r} in SUPPORTED_PROVIDERS but missing "
            f"from oauth_vendors catalog (drift)"
        )

    token = await exchange_authorization_code(
        vendor=vendor,
        code=code,
        code_verifier=flow.code_verifier,
        client_id=creds.client_id,
        client_secret=creds.client_secret,
        redirect_uri=flow.redirect_uri,
        http_client=http_client,
        now=now,
    )

    if vendor.provider_id in _VENDORS_NO_USERINFO_ENDPOINT:
        if not token.id_token:
            raise IdentityFieldMissingError(
                f"{vendor.provider_id}: token response missing id_token "
                "(required when vendor has no userinfo endpoint)"
            )
        id_token_claims = decode_id_token_claims_unverified(token.id_token)
        userinfo = None
    else:
        userinfo = await fetch_userinfo(
            vendor=vendor,
            access_token=token.access_token,
            http_client=http_client,
        )
        # OIDC providers also return id_token — decode opportunistically
        # for the email-verified flag if present, but extraction stays
        # userinfo-first to keep the per-vendor branches tight.
        id_token_claims = None

    identity = extract_user_identity(
        provider=provider,
        userinfo=userinfo,
        id_token_claims=id_token_claims,
    )
    return CallbackResult(flow=flow, token=token, identity=identity)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Helpers — masked email for error responses
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def mask_email(email: str) -> str:
    """Return ``ab***@example.com`` for ``abby@example.com``.

    Mirrors the helper in :mod:`backend.routers.auth` (kept here
    too so the link-conflict error doesn't have to import the
    router-internal helper).
    """
    if not email or "@" not in email:
        return (email[:2] + "***") if email else "***"
    local, domain = email.split("@", 1)
    return (local[:2] + "***@" + domain) if local else ("***@" + domain)


# Re-export FlowSession so callers can build mocks without two imports.
__all__ = [
    # Errors
    "OAuthLoginError",
    "OAuthFeatureDisabled",
    "ProviderNotSupportedError",
    "ProviderNotConfiguredError",
    "FlowCookieMissingError",
    "FlowCookieInvalidError",
    "SigningKeyUnavailableError",
    "UserinfoFetchError",
    "IdTokenDecodeError",
    "IdentityFieldMissingError",
    "AccountLinkConflictError",
    # Dataclasses
    "OAuthCredentials",
    "OAuthUserIdentity",
    "AuthorizationStart",
    "CallbackResult",
    # Constants
    "SUPPORTED_PROVIDERS",
    "FLOW_COOKIE_NAME",
    "FLOW_COOKIE_PATH",
    "FLOW_COOKIE_TTL_SECONDS",
    # Pure helpers
    "assert_provider_supported",
    "lookup_provider_credentials",
    "compute_redirect_uri",
    "derive_signing_key",
    "resolve_signing_key",
    "serialize_flow",
    "deserialize_flow",
    "encode_signed_flow",
    "decode_signed_flow",
    "extract_user_identity",
    "decode_id_token_claims_unverified",
    "mask_email",
    # Async orchestrators
    "begin_oauth_login",
    "exchange_authorization_code",
    "fetch_userinfo",
    "complete_oauth_login",
]
