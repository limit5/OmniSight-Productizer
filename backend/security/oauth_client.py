"""AS.1.1 — OAuth 2.0 / OIDC core client library (Python side).

Protocol-primitives layer of the AS OAuth shared library. Provides:

    * PKCE (RFC 7636 §4) — ``code_verifier`` + ``code_challenge``
      ``S256`` derivation.
    * ``state`` parameter (RFC 6749 §10.12) — opaque CSRF binder.
    * ``nonce`` parameter (OIDC Core §3.1.2.1) — id_token replay binder
      for OIDC providers.
    * Authorization-URL builder (RFC 6749 §4.1.1) — assembles the
      query-string for the user-agent redirect.
    * Token-response parser (RFC 6749 §5.1) — turns the provider JSON
      into a typed :class:`TokenSet` with absolute ``expires_at``.
    * Refresh-token rotation helper (RFC 6749 §6 + §10.4 + OAuth 2.1
      Security BCP §4.13) — wraps the rotated-token contract.
    * Auto-refresh middleware (:class:`AutoRefreshAuth`) — an
      ``httpx.Auth`` subclass that transparently refreshes the access
      token before each request when the current one is within
      ``skew_seconds`` of expiry.

Vendor-specific clients (GitHub / Google / Microsoft / Apple / GitLab /
Bitbucket / Slack / Notion / Salesforce / HubSpot / Discord) ship in
AS.1.3. Token persistence (``oauth_tokens`` table) ships in AS.2.x. This
module is **provider- and storage-agnostic** — callers wire their own
provider config + their own persistence callback.

Path note (per AS.0.10's path-deviation precedent)
──────────────────────────────────────────────────
The AS.0.8 §3 / design-doc §2 canonical path was
``backend/auth/oauth_client.py``, but the legacy ``backend/auth.py``
session/RBAC module already occupies that namespace; promoting it to a
package would shadow ~140 ``from backend.auth import …`` call sites,
which is a refactor outside this row's scope. Located to
``backend/security/`` instead, parallel to AS.0.7 honeypot
(``backend/security/honeypot.py`` per design freeze §3 / §15
cross-ref) and AS.0.10 password generator
(``backend/security/password_generator.py``). A future row may
consolidate paths once a wider ``backend/auth.py`` → package migration
lands.

Module-global state audit (per implement_phase_step.md SOP §1)
──────────────────────────────────────────────────────────────
* All randomness comes from :mod:`secrets` (kernel CSPRNG). Each
  uvicorn worker derives its own values from the same source — answer
  #1 of SOP §1 audit (deterministic-by-construction across workers).
* No module-level mutable state — only frozen dataclasses, immutable
  tuples / strings, and ``frozenset``s.
* No DB writes, no network IO, no cache. Audit / persistence are the
  caller's responsibility (see :class:`AutoRefreshAuth`'s
  ``on_rotated`` hook).
* Importing the module is free of side effects (no env reads, no file
  IO at module top level — only at function-body call time, gated on
  the AS knob via :func:`is_enabled`).

AS.0.8 single-knob behaviour
────────────────────────────
* :func:`is_enabled` reads ``settings.as_enabled`` via ``getattr``
  fallback (``True`` if the field hasn't been declared yet — AS.3.1
  will land the field per AS.0.9 §7.2.6 forward-promotion guard).
* :func:`is_enabled` is the **only** caller-visible noop hook in this
  module. The lib's pure helpers (``generate_pkce``, ``generate_state``,
  ``parse_token_response``, …) work regardless — they have no IO and
  no caller-visible side effect to gate.
* The eventual ``/api/v1/auth/oauth/login/{provider}`` endpoint
  (AS.6.1) is responsible for the 503 ``as_disabled`` response;
  this lib does NOT raise on knob-off because callers may legitimately
  want to parse a stored token (e.g. a backfill script) even with the
  feature flag disabled.

TS twin
───────
``templates/_shared/oauth-client/index.ts`` (AS.1.2) will mirror the
public API. AS.1.5 drift-guard test will hash the canonical event
strings + provider-config defaults and assert SHA-256 equality, the
same pattern AS.0.10 ``test_wordlist_parity_python_ts`` established.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
import urllib.parse
from dataclasses import dataclass
from typing import (
    Any,
    Awaitable,
    Callable,
    Mapping,
    Optional,
    Sequence,
)

import httpx


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Constants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# RFC 7636 §4.1: code_verifier MUST be 43-128 unreserved characters.
PKCE_VERIFIER_MIN_LENGTH = 43
PKCE_VERIFIER_MAX_LENGTH = 128
# We pick a fixed length comfortably above the floor; 64 raw bytes →
# 86 base64url chars (no padding). 86 is between 43 and 128.
_PKCE_VERIFIER_RAW_BYTES = 64

# Random state / nonce length (raw bytes → base64url). 32 raw bytes →
# 43 base64url chars (no padding) → 256 bits of entropy. Per OAuth 2.1
# Security BCP §4.7, ≥128 bits is required; 256 doubles the headroom.
_STATE_RAW_BYTES = 32
_NONCE_RAW_BYTES = 32

# Default state TTL — 10 minutes is the OIDC-recommended upper bound for
# in-flight authorization (RFC 6749 §4.1.2 references "short-lived";
# OIDC Core §3.1.2.7 mentions "preventing replay"). Most providers
# expire the authorization-code itself within 10 minutes, so a longer
# state TTL gains nothing.
DEFAULT_STATE_TTL_SECONDS = 600

# Default skew before access-token expiry to trigger a refresh.
# 60 s buys time for one slow refresh round-trip on a constrained link
# without being so generous that we refresh tokens with hours of
# remaining life (which would put unnecessary load on the provider).
DEFAULT_REFRESH_SKEW_SECONDS = 60

# Canonical OAuth audit event strings. AS.5.1 will move these into
# ``backend.audit_events`` as ``EVENT_OAUTH_*`` symbols; for now the
# strings live next to the lib that produces them so callers can wire
# audit emission immediately. The string values are part of the
# AS-roadmap contract (AS.0.8 §5 truth table) and MUST NOT change once
# any caller emits them.
EVENT_OAUTH_LOGIN_INIT = "oauth.login_init"
EVENT_OAUTH_LOGIN_CALLBACK = "oauth.login_callback"
EVENT_OAUTH_REFRESH = "oauth.refresh"
EVENT_OAUTH_UNLINK = "oauth.unlink"
EVENT_OAUTH_TOKEN_ROTATED = "oauth.token_rotated"

ALL_OAUTH_EVENTS: tuple[str, ...] = (
    EVENT_OAUTH_LOGIN_INIT,
    EVENT_OAUTH_LOGIN_CALLBACK,
    EVENT_OAUTH_REFRESH,
    EVENT_OAUTH_UNLINK,
    EVENT_OAUTH_TOKEN_ROTATED,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Exceptions
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class OAuthClientError(Exception):
    """Base class for all errors this module raises. Lets callers
    catch ``except OAuthClientError`` once and not have to enumerate."""


class StateMismatchError(OAuthClientError):
    """Returned ``state`` does not match the stored value (CSRF
    suspicion — RFC 6749 §10.12)."""


class StateExpiredError(OAuthClientError):
    """Stored state TTL has elapsed; user must restart the flow."""


class TokenResponseError(OAuthClientError):
    """Provider returned a malformed or error-shaped token response
    (RFC 6749 §5.2 ``error`` payload, or missing ``access_token``)."""


class TokenRefreshError(OAuthClientError):
    """Refresh attempt failed — either we have no refresh_token, or
    the provider rejected ours (typically because rotation already
    consumed it on a previous call)."""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Frozen dataclasses (public surface)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass(frozen=True)
class PkcePair:
    """RFC 7636 §4.1 / §4.2 verifier + S256 challenge pair.

    ``code_verifier`` MUST stay server-side until the token-exchange POST.
    ``code_challenge`` is what travels with the authorize redirect.
    """

    code_verifier: str
    code_challenge: str
    code_challenge_method: str = "S256"


@dataclass(frozen=True)
class FlowSession:
    """In-flight authorization context.

    Created at ``begin_authorization()`` time and persisted by the caller
    (cookie / Redis / DB row) keyed by something derived from
    ``state`` (we recommend ``state`` itself — it's already
    high-entropy, opaque, and intended for this use). Looked up by
    ``state`` on the callback and validated with
    :func:`verify_state_and_consume`.

    All fields are bound to the redirect — the same instance MUST come
    out as went in, otherwise the flow is suspect and aborted.
    """

    provider: str
    state: str
    code_verifier: str
    nonce: Optional[str]
    redirect_uri: str
    scope: tuple[str, ...]
    created_at: float
    expires_at: float
    # Caller-supplied opaque metadata that round-trips with the flow
    # (e.g. "where to land the user after callback"). Frozen mapping →
    # we store as a tuple of (k, v) pairs to keep the dataclass hashable.
    extra: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class TokenSet:
    """Parsed RFC 6749 §5.1 token response.

    Attributes
    ----------
    access_token
        The bearer token to send with API requests.
    refresh_token
        Optional — present only if the provider issues one and the
        scope included ``offline_access`` / equivalent.
    token_type
        Almost always ``"Bearer"``. Stored verbatim so callers can
        round-trip (some providers normalize to ``"bearer"``).
    expires_at
        **Absolute** unix-timestamp seconds at which the access token
        expires. Computed at parse time from the relative
        ``expires_in`` plus ``now``. ``None`` if the provider didn't
        include ``expires_in`` (rare; means "long-lived, no expiry hint").
    scope
        Tuple of scopes the provider granted. May be a strict subset of
        what was requested (the provider decides). Empty tuple if the
        provider didn't echo a ``scope`` field.
    id_token
        OIDC id_token (JWS) — present only when ``openid`` was in the
        requested scope and the provider is OIDC.
    raw
        The full provider response as a dict (frozen via shallow copy).
        Useful for providers that ship vendor-specific extras the lib
        doesn't model. Treat as read-only.
    """

    access_token: str
    refresh_token: Optional[str]
    token_type: str
    expires_at: Optional[float]
    scope: tuple[str, ...]
    id_token: Optional[str]
    raw: Mapping[str, Any]

    def needs_refresh(
        self, *, skew_seconds: int = DEFAULT_REFRESH_SKEW_SECONDS,
        now: Optional[float] = None,
    ) -> bool:
        """Whether the access token is within ``skew_seconds`` of
        expiry (or has already expired). Returns False if the token
        has no expiry hint — caller must rely on a 401 response from
        the provider in that case."""
        if self.expires_at is None:
            return False
        ts = time.time() if now is None else now
        return ts >= (self.expires_at - skew_seconds)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  is_enabled — AS.0.8 single-knob hook
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def is_enabled() -> bool:
    """Whether the AS feature family is enabled per AS.0.8 §3.1 noop
    matrix.

    Reads ``settings.as_enabled`` via ``getattr`` fallback so the lib
    works before AS.3.1 lands the field on
    :class:`backend.config.Settings` (per AS.0.9 §7.2.6 forward-
    promotion guard). Default is ``True`` — the AS feature family is
    on unless explicitly disabled.

    The hook is intentionally cheap (one attribute lookup) so callers
    can sprinkle ``if not is_enabled(): return passthrough`` at every
    request-handler entry without measurable cost. The module's pure
    helpers (PKCE / state / token parsing) deliberately do NOT call
    this — turning the knob off should not break a backfill script
    that needs to parse an already-stored token.
    """
    try:
        from backend.config import settings  # local import: avoid pulling settings at module import time
    except Exception:
        return True
    return bool(getattr(settings, "as_enabled", True))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Random helpers (urlsafe base64 of secrets)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _b64url_no_pad(raw: bytes) -> str:
    """Base64url-encode *raw* and strip the ``=`` padding (RFC 7636
    §4.1, RFC 4648 §5)."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_token(raw_bytes: int) -> str:
    return _b64url_no_pad(secrets.token_bytes(raw_bytes))


def generate_state() -> str:
    """Generate a fresh CSRF ``state`` (≥256 bits of entropy)."""
    return _b64url_token(_STATE_RAW_BYTES)


def generate_nonce() -> str:
    """Generate a fresh OIDC ``nonce`` (≥256 bits of entropy)."""
    return _b64url_token(_NONCE_RAW_BYTES)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  PKCE (RFC 7636)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def generate_pkce() -> PkcePair:
    """Generate a fresh PKCE verifier + S256 challenge.

    The verifier is 86 chars (64 random bytes → urlsafe-base64 no-pad),
    well within the RFC 7636 §4.1 ``43..128`` window; the challenge is
    SHA-256 of the verifier ASCII bytes, urlsafe-base64 no-pad.
    """
    verifier = _b64url_token(_PKCE_VERIFIER_RAW_BYTES)
    # Defensive bound check — guards against future tweaks to the raw-
    # byte constant accidentally drifting out of the RFC window.
    assert PKCE_VERIFIER_MIN_LENGTH <= len(verifier) <= PKCE_VERIFIER_MAX_LENGTH, (
        f"verifier length {len(verifier)} out of RFC 7636 §4.1 range "
        f"[{PKCE_VERIFIER_MIN_LENGTH}, {PKCE_VERIFIER_MAX_LENGTH}]"
    )
    challenge = _b64url_no_pad(hashlib.sha256(verifier.encode("ascii")).digest())
    return PkcePair(code_verifier=verifier, code_challenge=challenge)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Authorization-URL builder
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def build_authorize_url(
    *,
    authorize_endpoint: str,
    client_id: str,
    redirect_uri: str,
    scope: Sequence[str],
    state: str,
    code_challenge: str,
    code_challenge_method: str = "S256",
    nonce: Optional[str] = None,
    extra_params: Optional[Mapping[str, str]] = None,
) -> str:
    """Assemble the user-agent redirect URL (RFC 6749 §4.1.1).

    ``extra_params`` lets vendor adapters add provider-specific knobs
    (``access_type=offline`` for Google, ``prompt=consent`` for forced
    refresh-token issuance, ``allow_signup=false`` for GitHub, etc.)
    without bloating the core signature.

    ``nonce`` is appended only for OIDC providers (caller decides).
    ``response_type`` is hard-coded to ``code`` — implicit and hybrid
    flows are out of scope for this lib (and discouraged by OAuth 2.1).
    """

    if not authorize_endpoint:
        raise ValueError("authorize_endpoint is required")
    if not client_id:
        raise ValueError("client_id is required")
    if not redirect_uri:
        raise ValueError("redirect_uri is required")
    if not state:
        raise ValueError("state is required")
    if not code_challenge:
        raise ValueError("code_challenge is required")

    params: list[tuple[str, str]] = [
        ("response_type", "code"),
        ("client_id", client_id),
        ("redirect_uri", redirect_uri),
        ("scope", " ".join(scope)),
        ("state", state),
        ("code_challenge", code_challenge),
        ("code_challenge_method", code_challenge_method),
    ]
    if nonce is not None:
        params.append(("nonce", nonce))
    if extra_params:
        # Reject duplicate keys explicitly — silent overwrite would let
        # a vendor adapter accidentally smuggle a different state/scope
        # past the core builder.
        core_keys = {k for k, _ in params}
        for k, v in extra_params.items():
            if k in core_keys:
                raise ValueError(
                    f"extra_params key {k!r} collides with core OAuth param"
                )
            params.append((k, v))

    sep = "&" if "?" in authorize_endpoint else "?"
    return authorize_endpoint + sep + urllib.parse.urlencode(params)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  FlowSession lifecycle
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def begin_authorization(
    *,
    provider: str,
    authorize_endpoint: str,
    client_id: str,
    redirect_uri: str,
    scope: Sequence[str],
    use_oidc_nonce: bool = False,
    state_ttl_seconds: int = DEFAULT_STATE_TTL_SECONDS,
    extra_authorize_params: Optional[Mapping[str, str]] = None,
    extra: Optional[Mapping[str, str]] = None,
    now: Optional[float] = None,
) -> tuple[str, FlowSession]:
    """Start an authorization-code flow.

    Returns ``(authorize_url, flow_session)``. The caller MUST persist
    ``flow_session`` (typical: write to Redis under key
    ``oauth:flow:{state}`` with TTL = ``state_ttl_seconds``), then
    redirect the user-agent to ``authorize_url``.

    On the callback the caller fetches the persisted ``FlowSession`` by
    the returned ``state`` query param, calls
    :func:`verify_state_and_consume`, then exchanges the code via the
    provider's token endpoint with the same ``code_verifier``.

    Parameters
    ----------
    use_oidc_nonce
        Whether to mint a ``nonce`` and append it to the authorize URL.
        Required for OIDC providers (Google, Microsoft, Apple, OIDC-
        configured Salesforce, …); not used by GitHub / Bitbucket /
        Discord / non-OIDC providers.
    state_ttl_seconds
        How long the persisted FlowSession is valid. After this window
        the callback fails with :class:`StateExpiredError`.
    extra_authorize_params
        Vendor-specific knobs (passed through to
        :func:`build_authorize_url`).
    extra
        Caller round-trip metadata — opaque to this lib, surfaced
        unchanged on the FlowSession returned at callback time.
    """

    ts = time.time() if now is None else now
    state = generate_state()
    nonce = generate_nonce() if use_oidc_nonce else None
    pkce = generate_pkce()

    flow = FlowSession(
        provider=provider,
        state=state,
        code_verifier=pkce.code_verifier,
        nonce=nonce,
        redirect_uri=redirect_uri,
        scope=tuple(scope),
        created_at=ts,
        expires_at=ts + max(0, state_ttl_seconds),
        extra=tuple(sorted((extra or {}).items())),
    )

    url = build_authorize_url(
        authorize_endpoint=authorize_endpoint,
        client_id=client_id,
        redirect_uri=redirect_uri,
        scope=flow.scope,
        state=state,
        code_challenge=pkce.code_challenge,
        nonce=nonce,
        extra_params=extra_authorize_params,
    )
    return url, flow


def verify_state_and_consume(
    stored: FlowSession,
    returned_state: str,
    *,
    now: Optional[float] = None,
) -> None:
    """Validate the callback's ``state`` against the stored FlowSession.

    Raises :class:`StateMismatchError` on any difference (constant-time
    compare to avoid leaking which character mismatched), or
    :class:`StateExpiredError` if the FlowSession's TTL has elapsed.

    Returns ``None`` on success. The "consume" in the name signals the
    caller's responsibility: after a successful verify, **delete the
    stored FlowSession** so it cannot be replayed. We don't delete it
    ourselves because the storage backend is the caller's choice
    (Redis vs. DB vs. cookie).
    """

    ts = time.time() if now is None else now
    if ts >= stored.expires_at:
        raise StateExpiredError(
            f"oauth flow expired at {stored.expires_at:.0f} (now {ts:.0f})"
        )
    # Constant-time comparison — both sides are urlsafe-b64 strings of
    # the same expected length, so timing differences would otherwise
    # leak the prefix.
    if not hmac.compare_digest(stored.state, returned_state or ""):
        raise StateMismatchError("oauth state mismatch")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Token-response parser (RFC 6749 §5.1 / §5.2)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def parse_token_response(
    payload: Mapping[str, Any],
    *,
    now: Optional[float] = None,
) -> TokenSet:
    """Turn the provider's token-endpoint JSON into a typed :class:`TokenSet`.

    Handles:

    * the success shape (RFC 6749 §5.1): ``access_token``,
      ``token_type``, optional ``expires_in``, ``refresh_token``,
      ``scope``, plus OIDC ``id_token``;
    * the error shape (RFC 6749 §5.2): a JSON object with an
      ``error`` field (and optional ``error_description``,
      ``error_uri``) — raised as :class:`TokenResponseError` with the
      provider message preserved.

    The returned ``expires_at`` is **absolute** (now + expires_in),
    deliberately decoupling the caller from clock-skew bugs that come
    from storing relative durations and re-evaluating later.
    """

    if not isinstance(payload, Mapping):
        raise TokenResponseError(
            f"token payload must be a mapping, got {type(payload).__name__}"
        )

    if "error" in payload:
        # RFC 6749 §5.2 error shape — surface verbatim.
        err = str(payload.get("error") or "unknown_error")
        desc = payload.get("error_description")
        raise TokenResponseError(
            f"token endpoint returned error={err}"
            + (f" description={desc!r}" if desc else "")
        )

    access = payload.get("access_token")
    if not isinstance(access, str) or not access:
        raise TokenResponseError("token response missing access_token")

    token_type = payload.get("token_type") or "Bearer"
    if not isinstance(token_type, str):
        raise TokenResponseError("token_type must be a string")

    refresh = payload.get("refresh_token")
    if refresh is not None and not isinstance(refresh, str):
        raise TokenResponseError("refresh_token must be a string when present")

    expires_in = payload.get("expires_in")
    expires_at: Optional[float]
    if expires_in is None:
        expires_at = None
    else:
        try:
            expires_in_f = float(expires_in)
        except (TypeError, ValueError) as exc:
            raise TokenResponseError(
                f"expires_in not a number: {expires_in!r}"
            ) from exc
        if expires_in_f < 0:
            raise TokenResponseError(f"expires_in negative: {expires_in_f}")
        ts = time.time() if now is None else now
        expires_at = ts + expires_in_f

    raw_scope = payload.get("scope")
    if raw_scope is None:
        scope_tuple: tuple[str, ...] = ()
    elif isinstance(raw_scope, str):
        # RFC 6749 §3.3 — space-separated; some providers use commas.
        # Parse both, dedupe in order.
        seen: set[str] = set()
        out: list[str] = []
        for tok in raw_scope.replace(",", " ").split():
            if tok not in seen:
                seen.add(tok)
                out.append(tok)
        scope_tuple = tuple(out)
    elif isinstance(raw_scope, (list, tuple)):
        scope_tuple = tuple(str(s) for s in raw_scope)
    else:
        raise TokenResponseError(f"scope has unsupported type {type(raw_scope).__name__}")

    id_token = payload.get("id_token")
    if id_token is not None and not isinstance(id_token, str):
        raise TokenResponseError("id_token must be a string when present")

    return TokenSet(
        access_token=access,
        refresh_token=refresh,
        token_type=token_type,
        expires_at=expires_at,
        scope=scope_tuple,
        id_token=id_token,
        # Shallow-copy into a plain dict — the lib promises read-only
        # ``Mapping`` so callers can't mutate provider response by
        # accident, but the original payload reference is not held.
        raw=dict(payload),
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Refresh-token rotation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def apply_rotation(
    previous: TokenSet,
    refreshed_payload: Mapping[str, Any],
    *,
    now: Optional[float] = None,
) -> tuple[TokenSet, bool]:
    """Apply RFC 6749 §6 + §10.4 refresh-token rotation.

    Parses ``refreshed_payload`` (the JSON returned by the provider's
    ``grant_type=refresh_token`` POST) and merges it onto ``previous``:

    * ``access_token`` and ``expires_at`` always replaced by the fresh
      response.
    * ``refresh_token``: per RFC 6749 §10.4 / OAuth 2.1 BCP §4.13,
      providers SHOULD issue a new ``refresh_token`` on every refresh;
      if they do, the old one MUST be considered consumed. If the
      provider omits ``refresh_token`` in the response (some
      explicitly opt out of rotation), we keep the previous one.
    * ``scope`` — replaced if the response includes a fresh value, else
      kept.
    * ``id_token`` — providers may or may not re-issue; replaced if
      present.

    Returns ``(new_token, rotated)`` where ``rotated`` is True iff the
    provider actually rotated the refresh_token (i.e. the new one
    differs from the old). Callers persist the new TokenSet and, if
    ``rotated``, MUST emit an :data:`EVENT_OAUTH_TOKEN_ROTATED` audit
    row + delete the old refresh_token from any cache.
    """

    fresh = parse_token_response(refreshed_payload, now=now)
    new_refresh = fresh.refresh_token if fresh.refresh_token is not None else previous.refresh_token
    rotated = (
        fresh.refresh_token is not None
        and previous.refresh_token is not None
        and fresh.refresh_token != previous.refresh_token
    )
    merged = TokenSet(
        access_token=fresh.access_token,
        refresh_token=new_refresh,
        token_type=fresh.token_type or previous.token_type,
        expires_at=fresh.expires_at,
        scope=fresh.scope or previous.scope,
        id_token=fresh.id_token if fresh.id_token is not None else previous.id_token,
        raw=fresh.raw,
    )
    return merged, rotated


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Auto-refresh middleware
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


# Type aliases for the refresh callback (caller-provided, provider-aware).
RefreshCallable = Callable[[str], Awaitable[Mapping[str, Any]]]
RotationHook = Callable[[TokenSet, TokenSet, bool], Awaitable[None]]


async def auto_refresh(
    current: TokenSet,
    refresh_fn: RefreshCallable,
    *,
    skew_seconds: int = DEFAULT_REFRESH_SKEW_SECONDS,
    on_rotated: Optional[RotationHook] = None,
    now: Optional[float] = None,
) -> TokenSet:
    """Return a TokenSet that is fresh for at least ``skew_seconds`` more.

    If ``current`` has not yet entered the skew window, returns it
    unchanged. Otherwise:

    1. Calls ``refresh_fn(current.refresh_token)`` to obtain the
       provider's new token response (caller-implemented because the
       token endpoint URL + auth + content-type vary by vendor).
    2. Merges via :func:`apply_rotation`.
    3. If the refresh_token rotated, awaits ``on_rotated(old, new, True)``
       so the caller can persist + audit.
    4. Returns the merged :class:`TokenSet`.

    Raises :class:`TokenRefreshError` if the current token has no
    ``refresh_token`` (no way to refresh — user must re-auth).
    """

    if not current.needs_refresh(skew_seconds=skew_seconds, now=now):
        return current

    if not current.refresh_token:
        raise TokenRefreshError(
            "current token has no refresh_token; user must re-authenticate"
        )

    payload = await refresh_fn(current.refresh_token)
    new_token, rotated = apply_rotation(current, payload, now=now)
    if on_rotated is not None:
        # Always notify — even when the refresh_token did NOT rotate,
        # callers usually want to persist the new access_token /
        # expires_at. The boolean flag tells them whether to also
        # invalidate the old refresh_token from any cache.
        await on_rotated(current, new_token, rotated)
    return new_token


class AutoRefreshAuth(httpx.Auth):
    """``httpx.Auth``-compatible middleware that auto-refreshes the
    access token before each request.

    Usage::

        token = TokenSet(...)  # loaded from token vault
        auth = AutoRefreshAuth(
            token,
            refresh_fn=my_provider_refresh,         # async callable
            on_rotated=persist_to_vault,            # async callable
        )
        async with httpx.AsyncClient(auth=auth) as client:
            r = await client.get("https://api.provider.example/me")

    The middleware mutates its own ``token`` attribute so subsequent
    requests within the same client lifetime reuse the freshly-rotated
    value without re-fetching from the vault. Cross-process / cross-
    request reuse is the caller's responsibility (via ``on_rotated``).

    Designed against the ``httpx`` async-auth-flow contract:

    * ``async_auth_flow(request)`` must yield the request once with
      auth applied. We refresh first, set the ``Authorization``
      header, then yield.

    Falls back to a synchronous-auth-flow path that raises (we never
    refresh from sync code — callers must use ``httpx.AsyncClient``).
    """

    # Tells httpx that we need the request body (we don't actually,
    # but staying False keeps streaming bodies streamable).
    requires_request_body: bool = False
    requires_response_body: bool = False

    def __init__(
        self,
        token: TokenSet,
        refresh_fn: RefreshCallable,
        *,
        skew_seconds: int = DEFAULT_REFRESH_SKEW_SECONDS,
        on_rotated: Optional[RotationHook] = None,
    ) -> None:
        if not isinstance(token, TokenSet):
            raise TypeError("token must be a TokenSet")
        self.token = token
        self._refresh_fn = refresh_fn
        self._skew_seconds = skew_seconds
        self._on_rotated = on_rotated

    def sync_auth_flow(self, request):  # pragma: no cover - sync httpx path
        # Sync clients can't await our refresh callback; refuse rather
        # than silently send a stale token.
        raise RuntimeError(
            "AutoRefreshAuth requires httpx.AsyncClient (sync path unsupported)"
        )

    async def async_auth_flow(self, request):
        self.token = await auto_refresh(
            self.token,
            self._refresh_fn,
            skew_seconds=self._skew_seconds,
            on_rotated=self._on_rotated,
        )
        request.headers["Authorization"] = (
            f"{self.token.token_type or 'Bearer'} {self.token.access_token}"
        )
        yield request


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Public surface
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


__all__ = [
    "ALL_OAUTH_EVENTS",
    "AutoRefreshAuth",
    "DEFAULT_REFRESH_SKEW_SECONDS",
    "DEFAULT_STATE_TTL_SECONDS",
    "EVENT_OAUTH_LOGIN_CALLBACK",
    "EVENT_OAUTH_LOGIN_INIT",
    "EVENT_OAUTH_REFRESH",
    "EVENT_OAUTH_TOKEN_ROTATED",
    "EVENT_OAUTH_UNLINK",
    "FlowSession",
    "OAuthClientError",
    "PKCE_VERIFIER_MAX_LENGTH",
    "PKCE_VERIFIER_MIN_LENGTH",
    "PkcePair",
    "RefreshCallable",
    "RotationHook",
    "StateExpiredError",
    "StateMismatchError",
    "TokenRefreshError",
    "TokenResponseError",
    "TokenSet",
    "apply_rotation",
    "auto_refresh",
    "begin_authorization",
    "build_authorize_url",
    "generate_nonce",
    "generate_pkce",
    "generate_state",
    "is_enabled",
    "parse_token_response",
    "verify_state_and_consume",
]
