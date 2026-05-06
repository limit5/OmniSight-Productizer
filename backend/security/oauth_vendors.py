"""AS.1.3 — OAuth vendor catalog (Python side).

Per-vendor configuration for the 11 OAuth providers OmniSight ships
out-of-the-box: GitHub, Google, Microsoft, Apple, GitLab, Bitbucket,
Slack, Notion, Salesforce, HubSpot, Discord. Each entry is a frozen
:class:`VendorConfig` carrying the protocol-level knobs the AS.1.1
core lib needs to drive an authorization-code flow against that
vendor:

    * authorize / token / userinfo / revocation endpoints
    * default scopes (caller may override)
    * OIDC flag (controls nonce + ``id_token`` expectation)
    * vendor-specific authorize-URL knobs (e.g. Google
      ``access_type=offline``, Apple ``response_mode=form_post``)
    * refresh-token support flag (informational; the parser still
      reads the actual response)
    * PKCE support flag (informational + drives the
      :func:`build_authorize_url_for_vendor` short-circuit)

The catalog is **inline data**, not a YAML file — same pattern as the
AS.0.10 ``DICEWARE_WORDLIST`` and the AS.1.2 numeric defaults, and
emitted into the TS twin (:file:`templates/_shared/oauth-client/vendors.ts`)
byte-for-byte via the AS.1.5 drift-guard SHA-256 oracle.

Why not YAML
────────────
The BS embedded-catalog (``configs/embedded_catalog/*.yaml``) is
operator-edited at runtime — a new CMake toolchain or NDK version
ships as a yaml diff reviewed by humans, **not** a code change. OAuth
vendor endpoints, by contrast, change at vendor-deprecation cadence
(years) and require code-side parsing + auth-flow integration changes
to land at the same time. Two-file split (yaml + adapter) gains
nothing here and adds a third drift-axis (yaml ↔ Python ↔ TS); inline
constants give us one source of truth + one drift guard against the
TS twin.

Path note (per AS.0.10 / AS.1.1 path-deviation precedent)
─────────────────────────────────────────────────────────
Located at ``backend/security/oauth_vendors.py`` to align with the
already-shipped ``backend/security/{oauth_client,password_generator,
honeypot}.py`` siblings; design-doc canonical path is
``backend/auth/oauth_vendors.py`` but ``backend/auth.py`` (legacy
session/RBAC) shadows that namespace and a wholesale package
migration is an independent refactor row outside this scope.

Module-global state audit (per implement_phase_step.md SOP §1)
──────────────────────────────────────────────────────────────
* All :class:`VendorConfig` instances are frozen dataclasses; their
  collections (`default_scopes`, `extra_authorize_params`) are tuples
  of immutable strings so the structure is hashable + deterministic
  across uvicorn workers (answer #1 of SOP §1 audit).
* The catalog index ``VENDORS`` is a :class:`types.MappingProxyType`
  view — read-only, not a ``dict`` (so the AS.1.1 SOP §1 module-dict
  scan stays clean for sibling modules that copy the test).
* No DB, no network IO, no env reads at module import time.
* Per-vendor :func:`build_authorize_url_for_vendor` and
  :func:`begin_authorization_for_vendor` thread the catalog into the
  AS.1.1 core lib; the catalog is data, the lib is behaviour, and
  this module is the seam.

AS.0.8 single-knob behaviour
────────────────────────────
The catalog itself does NOT consult :func:`oauth_client.is_enabled`
— same invariant as the lib's pure helpers (PKCE / state / parsing).
Turning the AS knob off must not break a backfill script that needs
``GITHUB.token_endpoint`` to revoke a previously-stored token. The
``/api/v1/auth/oauth/{provider}/login`` endpoint surface (AS.6.1) is
where the 503 ``as_disabled`` short-circuit lives — not here.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Optional, Sequence

from backend.security.oauth_client import (
    FlowSession,
    begin_authorization,
    build_authorize_url,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Exceptions
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class VendorNotFoundError(KeyError):
    """Lookup of an unknown ``provider_id`` against the catalog."""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  VendorConfig — frozen dataclass
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass(frozen=True)
class VendorConfig:
    """Protocol-level configuration for a single OAuth provider.

    All fields are immutable; the dataclass is hashable so call sites
    can use :class:`VendorConfig` instances as dict keys / set members
    where convenient.

    Attributes
    ----------
    provider_id
        Stable kebab-case slug used as catalog key, audit subject, and
        URL path segment for ``/api/v1/auth/oauth/{provider}/...``
        endpoints. MUST be lowercase letters / digits only — Apple's
        ``apple-id`` style hyphenated slugs are deliberately not used
        here so that the slug doubles as a Python identifier (e.g.
        ``GITHUB`` constant) and a clean URL path. The 11 shipped
        slugs are word-only; future entries are encouraged to follow.
    display_name
        Human-readable name shown in the operator UI / signup
        provider buttons. Free-form (vendor branding flows through);
        not used for routing.
    authorize_endpoint
        RFC 6749 §3.1 authorization endpoint — the URL the
        user-agent is redirected to.
    token_endpoint
        RFC 6749 §3.2 token endpoint — server-side POST target for
        the ``authorization_code`` and ``refresh_token`` grants.
    userinfo_endpoint
        Optional URL the caller queries with the issued access_token
        to retrieve the user's identity (email, display name).
        OIDC: ``/userinfo``. Non-OIDC: vendor-specific
        (``api.github.com/user``, ``discord.com/api/users/@me``, …).
        ``None`` means the vendor
        does not expose a single canonical endpoint and the caller
        must wire something vendor-specific.
    revocation_endpoint
        Optional RFC 7009 token-revocation endpoint. Used by the
        DSAR / GDPR right-to-erasure flow (AS.2.5). ``None`` means
        the vendor does not expose token revocation (or it's not a
        plain ``POST {token, token_type_hint}`` shape — Discord and
        HubSpot fit this).
    default_scopes
        Default ``scope`` set requested at authorize time. Caller
        may pass an explicit override; this is the "out of the box"
        list that lights up first-name/email/avatar for a new
        signup. Empty tuple = vendor does not use scopes
        (Notion: workspaces are the unit of permission, not scopes).
    is_oidc
        Whether the vendor speaks OpenID Connect over OAuth 2.0.
        Drives:
          * mint a ``nonce`` at authorize-time and append to URL;
          * expect ``id_token`` in token response;
          * caller may verify ``id_token`` JWS signature (AS.1.4).
        Vendors with both modes (Slack, Salesforce) — the catalog
        picks the integration shape used by OmniSight self-login.
    extra_authorize_params
        Vendor-specific query params appended to the authorize URL
        (RFC 6749 §3.1 says servers MAY accept additional params —
        Google ``access_type=offline``, Apple
        ``response_mode=form_post``, Microsoft offline_access via
        scope, …). Tuple-of-tuples (immutable, ordered, hashable).
    supports_refresh_token
        Vendor issues a refresh_token along with the access_token
        (when scope or extra_authorize_params requests it).
        ``False`` for Notion (long-lived access_token, no expiry,
        no refresh) and Slack Sign in with Slack (user token, no
        refresh grant). The flag is informational — the parser
        still reads the actual response, this just lets the caller
        short-circuit "schedule a refresh job" wiring.
    supports_pkce
        Vendor accepts the RFC 7636 PKCE ``code_challenge`` /
        ``code_verifier`` flow. ``True`` for the modern majority
        (GitHub, Google, MS, GitLab, Bitbucket, Slack, Salesforce,
        Discord) and a few vendors that quietly accept it without
        documenting (Apple, HubSpot — flagged conservatively here).
        ``False`` for Notion (no PKCE in their docs as of 2026-04).
    """

    provider_id: str
    display_name: str
    authorize_endpoint: str
    token_endpoint: str
    userinfo_endpoint: Optional[str]
    revocation_endpoint: Optional[str]
    default_scopes: tuple[str, ...]
    is_oidc: bool
    extra_authorize_params: tuple[tuple[str, str], ...]
    supports_refresh_token: bool
    supports_pkce: bool

    @property
    def extra_params_mapping(self) -> Mapping[str, str]:
        """Materialize ``extra_authorize_params`` as a read-only mapping
        for direct passthrough into :func:`oauth_client.build_authorize_url`.

        Returns a fresh dict per call — callers are free to mutate
        their copy (e.g. add ``prompt=login`` for forced re-consent)
        without bleeding back into the catalog entry.
        """
        return dict(self.extra_authorize_params)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  The 11 shipped vendors
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Each entry's docstring-line cites the vendor's developer doc as of
# the AS.1.3 row landing date (2026-04-28). Endpoints are stable
# across vendor doc revisions; drift detected by a future scan-the-
# vendor-doc job is filed as a follow-up row, not a hot-fix here.


GITHUB = VendorConfig(
    # GitHub OAuth Apps + GitHub Apps share the authorize/token URLs.
    # Refresh-token support requires opt-in (GitHub Apps with
    # "expiring user-to-server tokens" enabled); classic OAuth Apps
    # issue non-expiring access_tokens. We flag refresh as supported
    # because the modern path (GitHub App) is the recommended
    # integration shape.
    provider_id="github",
    display_name="GitHub",
    authorize_endpoint="https://github.com/login/oauth/authorize",
    token_endpoint="https://github.com/login/oauth/access_token",
    userinfo_endpoint="https://api.github.com/user",
    revocation_endpoint=None,  # uses application/{client_id}/token DELETE — non-RFC-7009 shape
    default_scopes=("read:user", "user:email"),
    is_oidc=False,
    extra_authorize_params=(("allow_signup", "true"),),
    supports_refresh_token=True,
    supports_pkce=True,
)


GOOGLE = VendorConfig(
    # Google OIDC. ``access_type=offline`` + ``prompt=consent`` are
    # required to receive a refresh_token; without them the user gets
    # only an access_token (silent re-consent on subsequent flows).
    # PKCE is now mandatory for new clients (was optional pre-2022).
    provider_id="google",
    display_name="Google",
    authorize_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
    token_endpoint="https://oauth2.googleapis.com/token",
    userinfo_endpoint="https://openidconnect.googleapis.com/v1/userinfo",
    revocation_endpoint="https://oauth2.googleapis.com/revoke",
    default_scopes=("openid", "email", "profile"),
    is_oidc=True,
    extra_authorize_params=(
        ("access_type", "offline"),
        ("prompt", "consent"),
    ),
    supports_refresh_token=True,
    supports_pkce=True,
)


MICROSOFT = VendorConfig(
    # Microsoft Identity Platform / Entra ID v2.0 endpoint. The
    # "common" tenant accepts both work/school + personal accounts;
    # tenant-restricted callers override authorize_endpoint at use
    # site (replace ``common`` with the tenant GUID). ``offline_access``
    # **scope** drives refresh_token issuance (different idiom from
    # Google's ``access_type=offline`` query param).
    provider_id="microsoft",
    display_name="Microsoft",
    authorize_endpoint="https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
    token_endpoint="https://login.microsoftonline.com/common/oauth2/v2.0/token",
    userinfo_endpoint="https://graph.microsoft.com/oidc/userinfo",
    revocation_endpoint=None,  # MS does not expose a public RFC-7009 endpoint; revocation = sign-out URL
    default_scopes=("openid", "email", "profile", "offline_access"),
    is_oidc=True,
    extra_authorize_params=(),
    supports_refresh_token=True,
    supports_pkce=True,
)


APPLE = VendorConfig(
    # Sign in with Apple. ``response_mode=form_post`` is **required**
    # whenever the ``name`` scope is requested (Apple posts the user's
    # name back via form body, not query string — the only chance to
    # capture the name is on the very first auth, never again). The
    # caller's callback handler MUST accept POST + parse the form body.
    # Refresh-token issued only on first auth; subsequent refreshes
    # don't rotate it (vendor-quirk handled by AS.1.1 apply_rotation
    # ``rotated=False`` path when fresh refresh_token is missing).
    provider_id="apple",
    display_name="Apple",
    authorize_endpoint="https://appleid.apple.com/auth/authorize",
    token_endpoint="https://appleid.apple.com/auth/token",
    userinfo_endpoint=None,  # No userinfo endpoint — id_token claims are the source of truth
    revocation_endpoint="https://appleid.apple.com/auth/revoke",
    default_scopes=("name", "email"),
    is_oidc=True,
    extra_authorize_params=(("response_mode", "form_post"),),
    supports_refresh_token=True,
    supports_pkce=True,
)


GITLAB = VendorConfig(
    # GitLab.com SaaS. Self-hosted GitLab instances override the
    # endpoints (``https://gitlab.example.com/oauth/...``). OIDC is
    # supported when ``openid`` is in the scope set. FX2.D9.7.6
    # uses GitLab's OIDC /oauth/userinfo flow, so the default scope
    # tuple includes read_user + the standard OIDC profile claims.
    provider_id="gitlab",
    display_name="GitLab",
    authorize_endpoint="https://gitlab.com/oauth/authorize",
    token_endpoint="https://gitlab.com/oauth/token",
    userinfo_endpoint="https://gitlab.com/oauth/userinfo",
    revocation_endpoint="https://gitlab.com/oauth/revoke",
    default_scopes=("read_user", "openid", "email", "profile"),
    is_oidc=True,
    extra_authorize_params=(),
    supports_refresh_token=True,
    supports_pkce=True,
)


BITBUCKET = VendorConfig(
    # Bitbucket Cloud. Self-hosted Bitbucket Server / Data Center
    # has different endpoints (``/rest/oauth2/...``); operators override
    # at use site. Non-OIDC; ``account`` + ``email`` are the
    # display-name + email scopes.
    provider_id="bitbucket",
    display_name="Bitbucket",
    authorize_endpoint="https://bitbucket.org/site/oauth2/authorize",
    token_endpoint="https://bitbucket.org/site/oauth2/access_token",
    userinfo_endpoint="https://api.bitbucket.org/2.0/user",
    revocation_endpoint=None,  # Bitbucket Cloud has no public revocation endpoint
    default_scopes=("account", "email"),
    is_oidc=False,
    extra_authorize_params=(),
    supports_refresh_token=True,
    supports_pkce=True,
)


SLACK = VendorConfig(
    # Sign in with Slack OpenID Connect. Slack's OIDC flow uses the
    # ``/openid/connect/authorize`` + ``openid.connect.token`` pair,
    # and its userInfo method is the vendor-shaped
    # ``openid.connect.userInfo`` endpoint rather than a standard
    # nested-profile path.
    provider_id="slack",
    display_name="Slack",
    authorize_endpoint="https://slack.com/openid/connect/authorize",
    token_endpoint="https://slack.com/api/openid.connect.token",
    userinfo_endpoint="https://slack.com/api/openid.connect.userInfo",
    revocation_endpoint="https://slack.com/api/auth.revoke",
    default_scopes=("openid", "email", "profile"),
    is_oidc=True,
    extra_authorize_params=(),
    supports_refresh_token=False,
    supports_pkce=True,
)


NOTION = VendorConfig(
    # Notion's OAuth is workspace-scoped not user-scoped — the access
    # token grants access to a specific workspace's content. Tokens
    # don't expire and there is no refresh grant; revocation is via
    # the user's connected-apps UI (no programmatic endpoint). PKCE
    # is not documented in Notion's API reference as of the row's
    # landing date — flagged false; vendors silently-supporting PKCE
    # is the kind of drift AS.1.5 catches.
    provider_id="notion",
    display_name="Notion",
    authorize_endpoint="https://api.notion.com/v1/oauth/authorize",
    token_endpoint="https://api.notion.com/v1/oauth/token",
    userinfo_endpoint=None,  # User owner identity rides in token response
    revocation_endpoint=None,
    default_scopes=(),  # Notion does not use scopes — workspaces are the permission unit
    is_oidc=False,
    extra_authorize_params=(("owner", "user"),),
    supports_refresh_token=False,
    supports_pkce=False,
)


SALESFORCE = VendorConfig(
    # Salesforce Identity (login.salesforce.com is the production
    # multi-tenant host; sandboxes use ``test.salesforce.com``).
    # The ``refresh_token`` scope is **required** to receive a
    # refresh_token (Salesforce-specific — most vendors derive it
    # from prompt/access_type). Caller must add ``refresh_token`` to
    # the scope tuple if they want long-lived sessions.
    provider_id="salesforce",
    display_name="Salesforce",
    authorize_endpoint="https://login.salesforce.com/services/oauth2/authorize",
    token_endpoint="https://login.salesforce.com/services/oauth2/token",
    userinfo_endpoint="https://login.salesforce.com/services/oauth2/userinfo",
    revocation_endpoint="https://login.salesforce.com/services/oauth2/revoke",
    default_scopes=("id", "email", "profile", "openid"),
    is_oidc=True,
    extra_authorize_params=(),
    supports_refresh_token=True,
    supports_pkce=True,
)


HUBSPOT = VendorConfig(
    # HubSpot OAuth 2.0. Authorize endpoint is on app.hubspot.com,
    # token + integrations identity endpoints are on api.hubapi.com —
    # the asymmetry is vendor-canonical. PKCE is not documented;
    # flagged false conservatively. Revocation is ``DELETE
    # /oauth/v1/refresh-tokens/{token}`` — non-RFC-7009 shape, so
    # revocation_endpoint=None and AS.2.5 wires HubSpot's quirk
    # separately.
    provider_id="hubspot",
    display_name="HubSpot",
    authorize_endpoint="https://app.hubspot.com/oauth/authorize",
    token_endpoint="https://api.hubapi.com/oauth/v1/token",
    userinfo_endpoint="https://api.hubapi.com/integrations/v1/me",
    revocation_endpoint=None,  # DELETE /oauth/v1/refresh-tokens/{token} — non-RFC shape, handle in AS.2.5
    default_scopes=("oauth", "crm.objects.contacts.read"),
    is_oidc=False,
    extra_authorize_params=(),
    supports_refresh_token=True,
    supports_pkce=False,
)


DISCORD = VendorConfig(
    # Discord OAuth 2.0. ``identify`` returns user id + username +
    # avatar, ``email`` adds verified email. Token revocation
    # endpoint takes ``token`` + ``token_type_hint`` per RFC 7009.
    provider_id="discord",
    display_name="Discord",
    authorize_endpoint="https://discord.com/oauth2/authorize",
    token_endpoint="https://discord.com/api/oauth2/token",
    userinfo_endpoint="https://discord.com/api/users/@me",
    revocation_endpoint="https://discord.com/api/oauth2/token/revoke",
    default_scopes=("identify", "email"),
    is_oidc=False,
    extra_authorize_params=(),
    supports_refresh_token=True,
    supports_pkce=True,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Catalog — ordered tuple + read-only mapping
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Order is the canonical declaration order from the AS.1.3 row text
# ("GitHub / Google / Microsoft / Apple / GitLab / Bitbucket / Slack /
# Notion / Salesforce / HubSpot / Discord 共 11 個"). The TS twin must
# preserve the same order — AS.1.5 SHA-256 drift guard hashes the
# joined ``provider_id`` sequence and asserts byte-identity.
#
# ALL_VENDORS: tuple of VendorConfig instances (immutable, ordered).
# VENDORS: read-only mapping from provider_id → VendorConfig (no `dict`
# at module scope — the SOP §1 module-state audit pattern).


ALL_VENDORS: tuple[VendorConfig, ...] = (
    GITHUB,
    GOOGLE,
    MICROSOFT,
    APPLE,
    GITLAB,
    BITBUCKET,
    SLACK,
    NOTION,
    SALESFORCE,
    HUBSPOT,
    DISCORD,
)


ALL_VENDOR_IDS: tuple[str, ...] = tuple(v.provider_id for v in ALL_VENDORS)


# MappingProxyType-wrapped lookup table. The underlying dict is built
# inside the constructor call so it is consumed (not bound to a module
# name) — the SOP §1 module-state audit forbids module-level
# ``list / dict / set / bytearray`` containers.
VENDORS: Mapping[str, VendorConfig] = MappingProxyType(
    {v.provider_id: v for v in ALL_VENDORS}
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Lookup + integration helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def get_vendor(provider_id: str) -> VendorConfig:
    """Return the :class:`VendorConfig` for *provider_id*.

    Raises :class:`VendorNotFoundError` (a ``KeyError`` subclass) if
    the slug is not in the catalog. Caller-provided slugs from URL
    paths / form posts MUST be validated against this lookup before
    being used to drive an authorize redirect — otherwise an
    attacker-controlled slug could route the flow at an unintended
    vendor (open-redirect family).
    """
    try:
        return VENDORS[provider_id]
    except KeyError:
        raise VendorNotFoundError(
            f"unknown OAuth provider {provider_id!r}; "
            f"known: {', '.join(ALL_VENDOR_IDS)}"
        ) from None


def build_authorize_url_for_vendor(
    vendor: VendorConfig,
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    code_challenge: str,
    scope: Optional[Sequence[str]] = None,
    nonce: Optional[str] = None,
    extra_params: Optional[Mapping[str, str]] = None,
) -> str:
    """Pre-fill :func:`oauth_client.build_authorize_url` from the
    catalog entry — ``authorize_endpoint`` and the vendor's
    ``extra_authorize_params`` are sourced from *vendor*, the caller
    only supplies what's flow-specific.

    *scope* defaults to ``vendor.default_scopes`` if omitted.

    *extra_params* is **merged** onto ``vendor.extra_authorize_params``
    — caller-supplied keys override the catalog's. Collisions with
    OAuth core keys (``response_type``, ``client_id``, …) are still
    rejected by the underlying core lib, no double-check here.

    *nonce* is forwarded as-is — caller decides whether the flow is
    OIDC. The catalog's ``is_oidc`` flag is **informational**; we
    don't auto-mint a nonce here because the function operates on a
    pre-minted (state, code_challenge, nonce) triple that may have
    been cached by :func:`oauth_client.begin_authorization`.
    """
    merged_params = dict(vendor.extra_authorize_params)
    if extra_params:
        merged_params.update(extra_params)
    return build_authorize_url(
        authorize_endpoint=vendor.authorize_endpoint,
        client_id=client_id,
        redirect_uri=redirect_uri,
        scope=scope if scope is not None else vendor.default_scopes,
        state=state,
        code_challenge=code_challenge,
        nonce=nonce,
        extra_params=merged_params or None,
    )


def begin_authorization_for_vendor(
    vendor: VendorConfig,
    *,
    client_id: str,
    redirect_uri: str,
    scope: Optional[Sequence[str]] = None,
    extra: Optional[Mapping[str, str]] = None,
    extra_authorize_params: Optional[Mapping[str, str]] = None,
    state_ttl_seconds: Optional[int] = None,
    now: Optional[float] = None,
) -> tuple[str, FlowSession]:
    """Catalog-aware :func:`oauth_client.begin_authorization` shim.

    Pulls ``authorize_endpoint``, ``default_scopes``, ``is_oidc``
    (drives ``use_oidc_nonce``), and the vendor's
    ``extra_authorize_params`` from *vendor* — caller only specifies
    client_id + redirect_uri (and optional overrides).

    Returns the same ``(authorize_url, FlowSession)`` shape as the
    underlying lib; caller persists the FlowSession + redirects to
    the URL exactly as before.

    *extra_authorize_params* — caller overrides on top of the
    catalog's static params. Useful for runtime knobs the catalog
    can't pre-bake (per-tenant ``hd=tenant.example`` for Google
    workspace restriction, ``login_hint`` for pre-filled email).

    *state_ttl_seconds* — defaults to
    :data:`oauth_client.DEFAULT_STATE_TTL_SECONDS` (10 min) when
    omitted; passing an explicit value lets the caller widen for
    dev-loop convenience or shrink for security-sensitive flows.
    """
    from backend.security.oauth_client import DEFAULT_STATE_TTL_SECONDS

    merged_extra_params: dict[str, str] = dict(vendor.extra_authorize_params)
    if extra_authorize_params:
        merged_extra_params.update(extra_authorize_params)

    return begin_authorization(
        provider=vendor.provider_id,
        authorize_endpoint=vendor.authorize_endpoint,
        client_id=client_id,
        redirect_uri=redirect_uri,
        scope=scope if scope is not None else vendor.default_scopes,
        use_oidc_nonce=vendor.is_oidc,
        state_ttl_seconds=(
            state_ttl_seconds
            if state_ttl_seconds is not None
            else DEFAULT_STATE_TTL_SECONDS
        ),
        extra_authorize_params=merged_extra_params or None,
        extra=extra,
        now=now,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Public surface
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


__all__ = [
    "ALL_VENDORS",
    "ALL_VENDOR_IDS",
    "APPLE",
    "BITBUCKET",
    "DISCORD",
    "GITHUB",
    "GITLAB",
    "GOOGLE",
    "HUBSPOT",
    "MICROSOFT",
    "NOTION",
    "SALESFORCE",
    "SLACK",
    "VENDORS",
    "VendorConfig",
    "VendorNotFoundError",
    "begin_authorization_for_vendor",
    "build_authorize_url_for_vendor",
    "get_vendor",
]
