"""W14.4 — Cloudflare Access SSO lock for web-preview ingress URLs.

W14.3 lands a publicly-reachable ``https://preview-{sandbox_id}.
{tunnel_host}`` URL routed through the operator's CF Tunnel; without
W14.4, anybody on the internet who guesses the sandbox_id can hit the
sidecar's Vite dev server. W14.4 closes that gap: every dynamic preview
hostname is registered as a *Cloudflare Access self-hosted application*,
so unauthenticated visits get bounced to the operator's CF Access IdP
(Google / GitHub / OIDC / etc.) and the resulting JWT must list a
permitted email before CF Access lets the request through to the
sidecar.

"OIDC token 對齊 OmniSight session" — the alignment angle
========================================================

The W14.4 row line item asks the CF Access OIDC token to "line up"
with the OmniSight session. Two practical levers:

  1. **Policy email allowlist** — at launch the manager creates an
     access application whose ``policies.include[*].email`` carries the
     launching operator's OmniSight ``User.email`` plus the operator-
     wide admin allowlist (``cf_access_default_emails``). That means
     the only identity CF Access will issue an OIDC token *to* is one
     OmniSight already trusts: the OIDC ``email`` claim is therefore
     a 1-to-1 reflection of the OmniSight session that requested the
     preview.
  2. **Downstream JWT verification** — :func:`extract_jwt_claims` +
     :func:`jwt_claims_align_with_session` decode the
     ``Cf-Access-Jwt-Assertion`` header CF stamps on every authorised
     request and assert ``claims["email"] == omnisight_user.email``.
     This is best-effort (no signature verification — we trust CF's
     edge to have done that already; the JWT just rides through to
     give the backend a consistent identity). Future W14.6 frontend
     code can call this when proxying API calls from inside the
     iframe.

Both levers ship in this row; the second is exposed as a public helper
so future rows (W14.6 frontend handler, W14.7 HMR proxy) can reuse it
without re-implementing JWT decode.

Why a separate module (sibling to :mod:`backend.cf_ingress`)
============================================================

* :mod:`backend.cf_ingress` (W14.3) owns the *tunnel ingress
  configuration* — the routing layer that maps ``preview-X.tunnel`` →
  ``http://127.0.0.1:Y``. CF Access is a *separate* product on the same
  CF account: an "Application" object that gates a hostname behind
  identity policies. The two products have distinct API surfaces
  (``/cfd_tunnel/{id}/configurations`` vs ``/access/apps``) and very
  different lifecycle semantics (ingress is one big PUT with the full
  rule list; Access is one POST per app and one DELETE per app).
* Splitting keeps each module's surface small (one CFAPI surface per
  module), keeps the tests independent (an ingress-only deployment can
  still ship even without Access enabled), and lets the W14.5 idle
  reaper teardown call ``cf_access.delete_application`` and
  ``cf_ingress.delete_rule`` independently with separate retry
  budgets.

Row boundary
============

W14.4 owns:

  1. :class:`CFAccessManager` — thread-safe registry of live access
     applications keyed on ``sandbox_id``, with idempotent
     ``create_application`` / ``delete_application`` methods.
  2. :class:`CFAccessClient` Protocol + :class:`HttpxCFAccessClient`
     production impl + the sync httpx CRUD wrappers.
  3. Pure helpers (:func:`build_application_name`,
     :func:`build_application_domain`, :func:`build_application_payload`,
     :func:`build_policy_payload`, :func:`extract_jwt_claims`,
     :func:`jwt_claims_align_with_session`) — composable, deterministic.
  4. :class:`CFAccessConfig` — immutable settings snapshot read from
     :func:`backend.config.get_settings` at construction time.
  5. Typed errors (:class:`CFAccessError` base +
     :class:`CFAccessAPIError` / :class:`CFAccessNotFound` /
     :class:`CFAccessMisconfigured` subclasses).

W14.4 explicitly does NOT own:

  - CF Tunnel provisioning / token rotation (B12).
  - CF Tunnel ingress rules (W14.3 — sibling).
  - Idle-kill reaper that triggers ``stop()`` / Access app deletion
    (W14.5 — calls ``WebSandboxManager.stop`` which in turn calls
    ``cf_access.delete_application``).
  - Persistent CF Access app audit log (W14.10 alembic 0059).
  - Frontend iframe wiring or in-iframe API call signing (W14.6/W14.7).
  - Operator-facing UI for editing the email allowlist post-launch
    (potential follow-up — today the launch sets it once and stop
    deletes the app).

Module-global state audit (SOP §1)
==================================

:class:`CFAccessManager` holds a per-uvicorn-worker
``_apps: dict[str, CFAccessApplicationRecord]`` cache guarded by an
``RLock``. The cache is an *optimisation* — every public method
fetches the live list of access apps from the CF API before mutating
it. Cross-worker consistency answer = SOP §1 type **#2 (PG/Redis/CF
coordination)**: the canonical state is the CF Access application
catalog itself. Two workers concurrently launching distinct sandboxes
both fetch fresh, then POST their own application — CF assigns a
unique ``id`` per application so there is no PUT-overwrite race like
W14.3 has (each app is its own resource, not a member of one big
config blob). The only race window is two workers launching the
*same* sandbox_id concurrently: both POST, CF returns 409 / dupe app
on the loser, the loser falls back to a GET-by-name lookup and rebuilds
its cache record. Mitigation: future W14.10 row will replace the
in-memory cache with a PG-serialised mutation via
``pg_advisory_xact_lock(crc('cf_access_mutation'))``.

Read-after-write timing audit (SOP §2)
======================================

Fresh module — no compat→pool migration. The only race surface is two
workers launching the same sandbox_id concurrently; idempotent
look-up-by-name on the loser bounds it.

Compat fingerprint grep (SOP §3): N/A — fresh module, zero compat
artefacts.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
import threading
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Protocol

import httpx

logger = logging.getLogger(__name__)


__all__ = [
    "CF_ACCESS_SCHEMA_VERSION",
    "CF_API_BASE",
    "DEFAULT_HTTP_TIMEOUT_S",
    "DEFAULT_SESSION_DURATION",
    "PREVIEW_APP_NAME_PREFIX",
    "JWT_HEADER_NAME",
    "DEFAULT_APP_TYPE",
    "CFAccessError",
    "CFAccessAPIError",
    "CFAccessNotFound",
    "CFAccessMisconfigured",
    "CFAccessConfig",
    "CFAccessClient",
    "HttpxCFAccessClient",
    "CFAccessManager",
    "CFAccessApplicationRecord",
    "build_application_name",
    "build_application_domain",
    "build_application_payload",
    "build_policy_payload",
    "compute_effective_emails",
    "validate_email",
    "validate_session_duration",
    "validate_team_domain",
    "extract_jwt_claims",
    "jwt_claims_align_with_session",
    "token_fingerprint",
]


#: Bump when :class:`CFAccessConfig` / :class:`CFAccessApplicationRecord`
#: shape changes — the W14.10 alembic 0059 audit row keys persisted
#: entries on this version so a forward-compat read of an older row keeps
#: parsing.
CF_ACCESS_SCHEMA_VERSION = "1.0.0"

#: The CF API v4 base — pinned in this module rather than re-imported
#: from :mod:`backend.cloudflare_client` so the W14.4 module is
#: self-contained even if a future refactor moves the B12 client. Drift
#: guard: ``test_cf_api_base_pinned`` keeps this aligned with the
#: B12 / W14.3 sibling constants.
CF_API_BASE = "https://api.cloudflare.com/client/v4"

#: Default sync HTTP timeout for CF Access API calls.
DEFAULT_HTTP_TIMEOUT_S = 30.0

#: CF Access application session duration — how long a successful
#: login lasts before the user has to re-authenticate. Default 30m
#: keeps a stale browser tab from holding access overnight; operators
#: needing longer sessions for live development can override via the
#: ``cf_access_session_duration`` setting.
DEFAULT_SESSION_DURATION = "30m"

#: Application name prefix every dynamic Access app starts with — the
#: W14 epic header pins this naming scheme so an operator can
#: ``access list-apps`` and immediately see which apps are
#: web-preview ones.
PREVIEW_APP_NAME_PREFIX = "omnisight-preview-"

#: HTTP header CF Access stamps on every authorised request, carrying
#: a JWT whose ``email`` claim is the SSO identity that passed the
#: gate. Pinned here so downstream code (W14.6 frontend, W14.7 HMR
#: proxy) doesn't have to re-derive the header name.
JWT_HEADER_NAME = "Cf-Access-Jwt-Assertion"

#: CF Access "self-hosted" applications correspond to a hostname behind
#: the customer's tunnel. The other type ("saas") routes to a CF-hosted
#: identity proxy and is unrelated to W14.
DEFAULT_APP_TYPE = "self_hosted"


# ───────────────────────────────────────────────────────────────────
#  Errors
# ───────────────────────────────────────────────────────────────────


class CFAccessError(RuntimeError):
    """Base for all W14.4 access-manager errors."""


class CFAccessAPIError(CFAccessError):
    """Raised when the CF Access API returns a non-2xx response.

    Carries the upstream status code (when known) so callers can map
    to HTTP responses without re-parsing the message string.
    """

    def __init__(self, message: str, *, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


class CFAccessNotFound(CFAccessError):
    """Raised when :meth:`CFAccessManager.delete_application` is called
    for a sandbox_id that has no recorded application.

    Typically a programmer error or stale-state race; callers ought to
    catch and treat as no-op (the app is already gone, which is the
    desired terminal state).
    """


class CFAccessMisconfigured(CFAccessError):
    """Raised at :class:`CFAccessManager` construction time when one
    of the required Settings fields is empty / malformed.

    The router catches this in :func:`get_manager` and falls back to
    a manager without CF Access wiring — equivalent to the W14.3 dev
    path — so the *absence* of W14.4 config never fails the launch
    endpoint itself.
    """


# ───────────────────────────────────────────────────────────────────
#  Validation helpers (pure)
# ───────────────────────────────────────────────────────────────────


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_TEAM_DOMAIN_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?\.cloudflareaccess\.com")
_SESSION_DURATION_RE = re.compile(r"\d+(?:s|m|h|d)")
_SANDBOX_ID_RE = re.compile(r"ws-[0-9a-f]{6,32}")
_TUNNEL_HOST_RE = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?){1,}"
)
_APP_UUID_RE = re.compile(r"[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def validate_email(value: str) -> None:
    """Reject empty / non-string / RFC-illegal email addresses.

    Relaxed check — we don't want to be a full RFC 5321 parser. The
    goal is to reject obvious garbage (no ``@``, whitespace, leading
    dot, missing TLD) before we splice the value into a CF Access
    policy that an operator can't easily fix without re-launching.
    """

    if not isinstance(value, str) or not value.strip():
        raise CFAccessError("email must be a non-empty string")
    if value != value.strip():
        raise CFAccessError(
            f"email must not have leading/trailing whitespace: {value!r}"
        )
    if "\n" in value or "\r" in value:
        raise CFAccessError(f"email must not contain newlines: {value!r}")
    if not _EMAIL_RE.fullmatch(value):
        raise CFAccessError(f"email is not a valid address: {value!r}")


def validate_team_domain(value: str) -> None:
    """Reject empty / non-cloudflareaccess.com team domains.

    Each CF Access tenant has a dedicated team domain like
    ``acme.cloudflareaccess.com``. This is the OIDC issuer URL CF
    Access uses to sign JWTs (the ``iss`` claim). We validate the
    suffix so a typo can't silently make every JWT verification fail.
    """

    if not isinstance(value, str) or not value.strip():
        raise CFAccessMisconfigured("team_domain must be a non-empty string")
    if value != value.strip():
        raise CFAccessMisconfigured(
            f"team_domain must not have leading/trailing whitespace: {value!r}"
        )
    if "://" in value or "/" in value:
        raise CFAccessMisconfigured(
            f"team_domain must be a bare hostname, not a URL: {value!r}"
        )
    if not value.endswith(".cloudflareaccess.com"):
        raise CFAccessMisconfigured(
            f"team_domain must end with '.cloudflareaccess.com': {value!r}"
        )
    if not _TEAM_DOMAIN_RE.fullmatch(value):
        raise CFAccessMisconfigured(
            f"team_domain has invalid DNS chars: {value!r}"
        )


def validate_session_duration(value: str) -> None:
    """Reject session durations that aren't shaped like CF expects.

    CF Access accepts ``Ns / Nm / Nh / Nd`` (where N is a positive
    integer). We re-validate to catch a typo (``"30 minutes"``) that
    CF would otherwise reject only at app-create time.
    """

    if not isinstance(value, str) or not value.strip():
        raise CFAccessMisconfigured("session_duration must be a non-empty string")
    if not _SESSION_DURATION_RE.fullmatch(value):
        raise CFAccessMisconfigured(
            "session_duration must match Ns/Nm/Nh/Nd (e.g. '30m', '24h'): "
            f"{value!r}"
        )


def validate_sandbox_id(value: str) -> None:
    """Reject sandbox_ids that aren't shaped like the W14.2 emitter.

    Mirrors :func:`backend.cf_ingress.validate_sandbox_id` so the
    W14.3 + W14.4 sibling modules accept the same set of identifiers.
    Defence in depth so a caller-supplied ``sandbox_id`` can't smuggle
    a ``..`` traversal or shell metachar into the CF Access app name.
    """

    if not isinstance(value, str) or not value.strip():
        raise CFAccessError("sandbox_id must be a non-empty string")
    if not _SANDBOX_ID_RE.fullmatch(value):
        raise CFAccessError(
            f"sandbox_id must match 'ws-' + 6-32 lowercase hex chars: {value!r}"
        )


def validate_tunnel_host(value: str) -> None:
    """Reject empty / whitespace / RFC-illegal tunnel hosts.

    Sibling of :func:`backend.cf_ingress.validate_tunnel_host`; both
    modules accept the same hostname shape so a single env knob
    (``OMNISIGHT_TUNNEL_HOST``) drives both.
    """

    if not isinstance(value, str) or not value.strip():
        raise CFAccessMisconfigured("tunnel_host must be a non-empty string")
    if value != value.strip():
        raise CFAccessMisconfigured(
            f"tunnel_host must not have leading/trailing whitespace: {value!r}"
        )
    if "://" in value or "/" in value:
        raise CFAccessMisconfigured(
            f"tunnel_host must be a bare hostname, not a URL: {value!r}"
        )
    if value.startswith(".") or value.endswith("."):
        raise CFAccessMisconfigured(
            f"tunnel_host must not start/end with '.': {value!r}"
        )
    if "." not in value:
        raise CFAccessMisconfigured(
            f"tunnel_host must have at least 2 labels: {value!r}"
        )
    if not _TUNNEL_HOST_RE.fullmatch(value):
        raise CFAccessMisconfigured(
            f"tunnel_host has invalid DNS chars: {value!r}"
        )


def validate_account_id(value: str) -> None:
    """Reject empty / non-32-char-hex CF account UUIDs."""

    if not isinstance(value, str) or not value.strip():
        raise CFAccessMisconfigured("cf_account_id must be a non-empty string")
    cleaned = value.strip()
    if not re.fullmatch(r"[0-9a-f]{32}", cleaned):
        raise CFAccessMisconfigured(
            f"cf_account_id must be a 32-char hex UUID: {value!r}"
        )


def token_fingerprint(token: str) -> str:
    """Return the last-4 fingerprint of a token for logging.

    Mirrors :func:`backend.cloudflare_client.token_fingerprint` and
    :func:`backend.cf_ingress.token_fingerprint` so the log style is
    consistent across the B12 + W14.3 + W14.4 surfaces.
    """

    if not isinstance(token, str) or len(token) <= 8:
        return "****"
    return f"…{token[-4:]}"


# ───────────────────────────────────────────────────────────────────
#  Pure access-policy helpers
# ───────────────────────────────────────────────────────────────────


def build_application_name(sandbox_id: str) -> str:
    """Return the CF Access application name for a sandbox.

    Layout: ``omnisight-preview-{sandbox_id}``. Pure function.
    """

    validate_sandbox_id(sandbox_id)
    return f"{PREVIEW_APP_NAME_PREFIX}{sandbox_id}"


def build_application_domain(sandbox_id: str, tunnel_host: str) -> str:
    """Return the public domain CF Access gates.

    Layout: ``preview-{sandbox_id}.{tunnel_host}`` — must match
    :func:`backend.cf_ingress.build_ingress_hostname` byte-for-byte
    so an Access app and an ingress rule for the same sandbox are
    glued to the same public hostname. Drift between the two would
    leave the Access app gating a hostname that has no tunnel route,
    or a tunnel route with no Access gate — equally bad.

    Pure function; we don't import the cf_ingress helper to avoid a
    cross-module dependency cycle (cf_ingress doesn't depend on
    cf_access either; both are leaf modules).
    """

    validate_sandbox_id(sandbox_id)
    validate_tunnel_host(tunnel_host)
    return f"preview-{sandbox_id}.{tunnel_host}"


def compute_effective_emails(
    requested: Iterable[str],
    *,
    defaults: Iterable[str] = (),
) -> tuple[str, ...]:
    """Return the deduplicated, validated, sorted union of two email
    iterables.

    The launch-time effective email allowlist is
    ``requested ∪ defaults`` — the launching operator's email plus
    the operator-wide admin allowlist (``cf_access_default_emails``).
    Sorted output makes the CF policy payload deterministic so two
    workers launching the same sandbox post identical bytes (and CF's
    409 dedup catches the loser cleanly).

    Pure function; raises :class:`CFAccessError` on any invalid email.
    """

    if isinstance(requested, str) or isinstance(defaults, str):
        # Common bug: pass the CSV string instead of the parsed list.
        raise CFAccessError(
            "compute_effective_emails takes iterables of strings, not a "
            "comma-separated string"
        )
    seen: dict[str, None] = {}
    for source in (requested, defaults):
        for raw in source:
            if not isinstance(raw, str):
                raise CFAccessError(f"email entry must be a string: {raw!r}")
            cleaned = raw.strip()
            if not cleaned:
                continue
            validate_email(cleaned)
            seen[cleaned.lower()] = None
    return tuple(sorted(seen))


def build_policy_payload(
    *,
    name: str,
    emails: Iterable[str],
    decision: str = "allow",
    precedence: int = 1,
) -> dict[str, Any]:
    """Return the JSON body for ``POST /access/apps/{id}/policies``.

    The CF Access API expresses "allow these emails" as
    ``include: [{ email: { email: "addr@host" } }, ...]``. We always
    set ``decision=allow`` (W14.4 never builds deny policies; if
    nobody is in the include list, CF Access denies by default).
    Pure function.
    """

    if not isinstance(name, str) or not name.strip():
        raise CFAccessError("policy name must be non-empty")
    if decision not in {"allow", "deny", "non_identity", "bypass"}:
        raise CFAccessError(
            f"decision must be one of allow/deny/non_identity/bypass: {decision!r}"
        )
    if not isinstance(precedence, int) or precedence < 1:
        raise CFAccessError("precedence must be a positive int")
    emails_t = tuple(emails)
    if not emails_t:
        raise CFAccessError(
            "policy must include at least one email — empty allowlist would "
            "lock everyone out and is almost certainly a misconfiguration"
        )
    include: list[dict[str, Any]] = []
    for addr in emails_t:
        validate_email(addr)
        include.append({"email": {"email": addr}})
    return {
        "name": name,
        "decision": decision,
        "precedence": precedence,
        "include": include,
    }


def build_application_payload(
    *,
    name: str,
    domain: str,
    emails: Iterable[str],
    session_duration: str = DEFAULT_SESSION_DURATION,
    app_type: str = DEFAULT_APP_TYPE,
    auto_redirect_to_identity: bool = True,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the JSON body for ``POST /accounts/{id}/access/apps``.

    Includes a single inline policy (``operators-allow``) carrying
    the email allowlist. CF Access lets us POST the policies inline
    so we don't need a follow-up request for each app create — one
    network round-trip per launch is enough.

    Pure function — no I/O, deterministic shape for a given input.
    """

    if not isinstance(name, str) or not name.strip():
        raise CFAccessError("name must be non-empty")
    if not isinstance(domain, str) or not domain.strip():
        raise CFAccessError("domain must be non-empty")
    validate_session_duration(session_duration)
    if app_type not in {"self_hosted", "saas", "ssh", "vnc"}:
        raise CFAccessError(
            f"app_type must be one of self_hosted/saas/ssh/vnc: {app_type!r}"
        )
    policy = build_policy_payload(name="operators-allow", emails=emails)
    payload: dict[str, Any] = {
        "name": name,
        "domain": domain,
        "type": app_type,
        "session_duration": session_duration,
        "auto_redirect_to_identity": bool(auto_redirect_to_identity),
        "app_launcher_visible": False,
        "policies": [policy],
    }
    if extra:
        for key, value in dict(extra).items():
            if key in payload:
                # Caller-supplied extras may not silently overwrite the
                # row's pinned shape — surface the conflict early.
                raise CFAccessError(
                    f"extra key {key!r} conflicts with default payload key"
                )
            payload[key] = value
    return payload


# ───────────────────────────────────────────────────────────────────
#  CF Access JWT helpers (best-effort decode, no signature verify)
# ───────────────────────────────────────────────────────────────────


def _b64url_decode(segment: str) -> bytes:
    """Pad + decode a base64url-encoded JWT segment to bytes."""

    padded = segment + "=" * (-len(segment) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode("ascii"))
    except (binascii.Error, ValueError) as exc:
        raise CFAccessError(f"jwt segment is not valid base64url: {exc}") from exc


def extract_jwt_claims(token: str) -> dict[str, Any]:
    """Best-effort decode a CF Access JWT's claims block (no signature
    verification).

    CF Access stamps every authorised request with the
    ``Cf-Access-Jwt-Assertion`` header; the value is a standard
    three-segment JWT (``header.payload.signature``). We trust CF's
    edge to have already enforced the signature before forwarding the
    request to our origin (an unauthenticated request never reaches
    this code path), so this helper just decodes the claims so callers
    can read ``email`` / ``sub`` / ``aud`` / ``iss`` without pulling
    in a full JWT library.

    Raises :class:`CFAccessError` when the token is malformed. Returns
    the parsed claims dict on success.

    DO NOT call this from a public-facing endpoint that is itself
    reachable bypassing CF Access — the lack of signature verification
    means a hostile client could forge claims. The W14 epic gates
    every preview behind the CF Access proxy, so by construction every
    request that reaches the sidecar already has a CF-verified JWT.
    Future rows that expose the same JWT outside the iframe (e.g. an
    SSE channel from W14.6) must layer additional defence.
    """

    if not isinstance(token, str) or not token.strip():
        raise CFAccessError("jwt token must be a non-empty string")
    parts = token.strip().split(".")
    if len(parts) != 3:
        raise CFAccessError(
            f"jwt token must have 3 segments, got {len(parts)}"
        )
    payload_b = _b64url_decode(parts[1])
    try:
        claims = json.loads(payload_b.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CFAccessError(f"jwt payload is not valid JSON: {exc}") from exc
    if not isinstance(claims, Mapping):
        raise CFAccessError("jwt payload must be a JSON object")
    return dict(claims)


def jwt_claims_align_with_session(
    claims: Mapping[str, Any],
    *,
    session_email: str,
    expected_aud: str | None = None,
    expected_iss: str | None = None,
) -> bool:
    """Return True iff the JWT claims line up with the OmniSight session.

    Three checks (any failure → False):

      1. ``claims["email"]`` (case-insensitive) equals ``session_email``.
      2. When ``expected_aud`` is provided, the JWT's ``aud`` claim
         contains it (CF Access tokens carry ``aud`` as a list of
         application UUIDs).
      3. When ``expected_iss`` is provided, the JWT's ``iss`` claim
         equals it (CF Access tokens carry the team-domain URL).

    Pure function. The caller is responsible for sourcing
    ``session_email`` from the OmniSight session cookie / bearer.
    """

    if not isinstance(claims, Mapping):
        return False
    if not isinstance(session_email, str) or not session_email.strip():
        return False
    jwt_email = claims.get("email")
    if not isinstance(jwt_email, str) or not jwt_email.strip():
        return False
    if jwt_email.strip().lower() != session_email.strip().lower():
        return False
    if expected_aud is not None:
        aud = claims.get("aud")
        if isinstance(aud, str):
            audiences = (aud,)
        elif isinstance(aud, (list, tuple)):
            audiences = tuple(str(a) for a in aud)
        else:
            return False
        if expected_aud not in audiences:
            return False
    if expected_iss is not None:
        iss = claims.get("iss")
        if not isinstance(iss, str) or iss != expected_iss:
            return False
    return True


# ───────────────────────────────────────────────────────────────────
#  Config snapshot
# ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CFAccessConfig:
    """Immutable snapshot of the W14.4 settings.

    Constructed once at :class:`CFAccessManager` init from
    :func:`backend.config.get_settings` so the manager never re-reads
    env mid-flight. Cross-worker consistency = SOP §1 type-1: every
    worker derives the same config from the same source.

    Required for production wiring:

      - ``tunnel_host`` (also used by W14.3) — the public DNS suffix
        sandbox URLs hang off.
      - ``api_token`` — CF API token with ``Account:Cloudflare Access:
        Edit`` scope (the ingress-edit scope from W14.3 is *not*
        sufficient — Access is a separate API surface).
      - ``account_id`` — CF account UUID.
      - ``team_domain`` — ``<team>.cloudflareaccess.com``; used to
        construct the OIDC issuer URL the JWT ``iss`` claim carries.

    Optional:

      - ``default_emails`` — admin emails always added to every
        per-sandbox policy. Useful so an on-call admin can take over
        a preview without the launching operator's session being live.
      - ``session_duration`` — how long a successful login lasts
        (default 30m).
      - ``aud_tag`` — a fixed CF Access AUD UUID, used by callers that
        want to verify ``claims["aud"]`` matches expected app cohort.
        Empty string ⇒ JWT verifier will not enforce ``aud``.
    """

    tunnel_host: str
    api_token: str
    account_id: str
    team_domain: str
    default_emails: tuple[str, ...] = ()
    session_duration: str = DEFAULT_SESSION_DURATION
    aud_tag: str = ""
    auto_redirect_to_identity: bool = True
    http_timeout_s: float = DEFAULT_HTTP_TIMEOUT_S

    def __post_init__(self) -> None:
        validate_tunnel_host(self.tunnel_host)
        if not isinstance(self.api_token, str) or not self.api_token.strip():
            raise CFAccessMisconfigured("api_token must be a non-empty string")
        validate_account_id(self.account_id)
        validate_team_domain(self.team_domain)
        validate_session_duration(self.session_duration)
        if not isinstance(self.aud_tag, str):
            raise CFAccessMisconfigured("aud_tag must be a string")
        if not isinstance(self.auto_redirect_to_identity, bool):
            raise CFAccessMisconfigured("auto_redirect_to_identity must be a bool")
        if (
            not isinstance(self.http_timeout_s, (int, float))
            or self.http_timeout_s <= 0
        ):
            raise CFAccessMisconfigured("http_timeout_s must be positive")
        # Normalise + validate default_emails.
        if isinstance(self.default_emails, str):
            raise CFAccessMisconfigured(
                "default_emails must be an iterable of strings — caller should "
                "split CSV before constructing CFAccessConfig"
            )
        cleaned: list[str] = []
        for entry in self.default_emails:
            if not isinstance(entry, str):
                raise CFAccessMisconfigured(
                    f"default_emails entry must be a string: {entry!r}"
                )
            stripped = entry.strip()
            if not stripped:
                continue
            validate_email(stripped)
            cleaned.append(stripped)
        # Dedup case-insensitively but preserve first-seen casing.
        seen: dict[str, str] = {}
        for addr in cleaned:
            lower = addr.lower()
            if lower not in seen:
                seen[lower] = addr
        object.__setattr__(self, "default_emails", tuple(seen.values()))

    @property
    def issuer_url(self) -> str:
        """OIDC issuer URL CF Access stamps in the JWT ``iss`` claim."""

        return f"https://{self.team_domain}"

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe dict with the api_token redacted to fingerprint."""

        return {
            "schema_version": CF_ACCESS_SCHEMA_VERSION,
            "tunnel_host": self.tunnel_host,
            "api_token_fingerprint": token_fingerprint(self.api_token),
            "account_id": self.account_id,
            "team_domain": self.team_domain,
            "default_emails": list(self.default_emails),
            "session_duration": self.session_duration,
            "aud_tag": self.aud_tag,
            "auto_redirect_to_identity": self.auto_redirect_to_identity,
            "http_timeout_s": float(self.http_timeout_s),
            "issuer_url": self.issuer_url,
        }

    @classmethod
    def from_settings(cls, settings: Any) -> "CFAccessConfig":
        """Build a :class:`CFAccessConfig` from a
        :class:`backend.config.Settings` instance.

        Raises :class:`CFAccessMisconfigured` when any required field
        is empty / invalid. The router catches this and falls back to
        "no CF Access wiring" — *missing* config is a soft failure,
        *malformed* config is a hard one.
        """

        tunnel_host = (getattr(settings, "tunnel_host", "") or "").strip()
        api_token = (getattr(settings, "cf_api_token", "") or "").strip()
        account_id = (getattr(settings, "cf_account_id", "") or "").strip()
        team_domain = (getattr(settings, "cf_access_team_domain", "") or "").strip()
        if not (tunnel_host and api_token and account_id and team_domain):
            missing = [
                name
                for name, value in (
                    ("OMNISIGHT_TUNNEL_HOST", tunnel_host),
                    ("OMNISIGHT_CF_API_TOKEN", api_token),
                    ("OMNISIGHT_CF_ACCOUNT_ID", account_id),
                    ("OMNISIGHT_CF_ACCESS_TEAM_DOMAIN", team_domain),
                )
                if not value
            ]
            raise CFAccessMisconfigured(
                "W14.4 CF Access SSO requires four env knobs to be set; "
                f"missing: {', '.join(missing)}"
            )
        session_duration = (
            getattr(settings, "cf_access_session_duration", "")
            or DEFAULT_SESSION_DURATION
        ).strip() or DEFAULT_SESSION_DURATION
        aud_tag = (getattr(settings, "cf_access_aud_tag", "") or "").strip()
        defaults_raw = (getattr(settings, "cf_access_default_emails", "") or "").strip()
        defaults: tuple[str, ...]
        if defaults_raw:
            defaults = tuple(
                part.strip() for part in defaults_raw.split(",") if part.strip()
            )
        else:
            defaults = ()
        return cls(
            tunnel_host=tunnel_host,
            api_token=api_token,
            account_id=account_id,
            team_domain=team_domain,
            default_emails=defaults,
            session_duration=session_duration,
            aud_tag=aud_tag,
        )


# ───────────────────────────────────────────────────────────────────
#  HTTP client (Protocol + sync httpx impl)
# ───────────────────────────────────────────────────────────────────


class CFAccessClient(Protocol):
    """Structural Protocol the manager calls into.

    Implementations: :class:`HttpxCFAccessClient` (production) and
    test fakes that capture call sequences without talking to CF.
    """

    def list_applications(self) -> list[dict[str, Any]]:
        """GET ``/accounts/{id}/access/apps``.

        Returns the unwrapped ``result`` list (the CF API wraps it in
        ``{result: [{...}, ...]}``).
        """

    def create_application(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """POST ``/accounts/{id}/access/apps``.

        Returns the unwrapped ``result`` dict (carries the new
        application's ``id`` / ``uid``).
        """

    def delete_application(self, app_id: str) -> dict[str, Any]:
        """DELETE ``/accounts/{id}/access/apps/{app_id}``.

        Returns the unwrapped ``result`` dict (CF returns ``{id: ...}``
        on success).
        """


class HttpxCFAccessClient:
    """Sync :class:`CFAccessClient` impl using ``httpx.Client``.

    Why sync: :meth:`backend.web_sandbox.WebSandboxManager.launch` is
    sync, called from a FastAPI ``async def`` route handler that
    delegates to it via ``Depends`` injection. Running the launch sync
    avoids the impedance mismatch of calling an async client from a
    sync method (would require ``asyncio.run_coroutine_threadsafe``).
    """

    def __init__(self, config: CFAccessConfig) -> None:
        if not isinstance(config, CFAccessConfig):
            raise TypeError("config must be a CFAccessConfig")
        self._config = config

    @property
    def config(self) -> CFAccessConfig:
        return self._config

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._config.api_token}",
            "Content-Type": "application/json",
        }

    def _base_path(self) -> str:
        return f"/accounts/{self._config.account_id}/access/apps"

    def _raise(self, response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        try:
            body = response.json()
        except Exception:
            body = {}
        message = ""
        errors = body.get("errors") if isinstance(body, Mapping) else None
        if isinstance(errors, list) and errors:
            first = errors[0]
            if isinstance(first, Mapping):
                message = str(first.get("message") or "")
        if not message:
            message = response.text or f"HTTP {response.status_code}"
        raise CFAccessAPIError(
            f"CF Access API {response.status_code}: {message}",
            status=response.status_code,
        )

    def _unwrap_result(self, response: httpx.Response) -> Any:
        try:
            payload = response.json()
        except Exception as exc:
            raise CFAccessAPIError(
                f"CF Access API returned non-JSON body: {exc}",
                status=response.status_code,
            ) from exc
        if not isinstance(payload, Mapping):
            raise CFAccessAPIError(
                "CF Access API response must be a JSON object",
                status=response.status_code,
            )
        return payload.get("result")

    def list_applications(self) -> list[dict[str, Any]]:
        url = f"{CF_API_BASE}{self._base_path()}"
        with httpx.Client(timeout=self._config.http_timeout_s) as client:
            response = client.get(
                url,
                headers=self._headers(),
                params={"per_page": "100"},
            )
        self._raise(response)
        result = self._unwrap_result(response)
        if not isinstance(result, list):
            return []
        return [dict(item) for item in result if isinstance(item, Mapping)]

    def create_application(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise TypeError("payload must be a mapping")
        url = f"{CF_API_BASE}{self._base_path()}"
        with httpx.Client(timeout=self._config.http_timeout_s) as client:
            response = client.post(url, headers=self._headers(), json=dict(payload))
        self._raise(response)
        result = self._unwrap_result(response)
        if not isinstance(result, Mapping):
            raise CFAccessAPIError(
                "CF Access create response missing result object",
                status=response.status_code,
            )
        return dict(result)

    def delete_application(self, app_id: str) -> dict[str, Any]:
        if not isinstance(app_id, str) or not app_id.strip():
            raise CFAccessError("app_id must be a non-empty string")
        url = f"{CF_API_BASE}{self._base_path()}/{app_id}"
        with httpx.Client(timeout=self._config.http_timeout_s) as client:
            response = client.delete(url, headers=self._headers())
        self._raise(response)
        result = self._unwrap_result(response)
        if isinstance(result, Mapping):
            return dict(result)
        # CF returns ``{result: {id: ...}}``; if the result is null/empty
        # treat as success-with-empty-payload (idempotent semantics).
        return {}


# ───────────────────────────────────────────────────────────────────
#  Manager
# ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CFAccessApplicationRecord:
    """In-memory record of a live CF Access application.

    Lightweight shadow of the CF-side application — the manager keeps
    these in ``_apps`` indexed by sandbox_id so :meth:`delete_application`
    does not need the caller to remember the app id (the W14.10 row
    will move this into PG with extra audit columns).
    """

    sandbox_id: str
    app_id: str
    name: str
    domain: str
    emails: tuple[str, ...]


class CFAccessManager:
    """Thread-safe lifecycle manager for per-sandbox CF Access apps.

    One manager per uvicorn worker. Each worker maintains its own
    in-memory cache of apps it has created — every public method
    fetches the live apps list from the CF API before mutating it,
    so the cache is an *optimisation* not source of truth (cross-
    worker correctness = CF API is canonical, see SOP §1 audit at
    module top).

    Lifecycle (called from :class:`backend.web_sandbox.WebSandboxManager`
    via the optional ``cf_access_manager`` constructor param):

      1. ``manager.create_application(sandbox_id, emails=...)`` on
         launch → returns the new application's id; the launcher pins
         it onto :attr:`WebSandboxInstance.access_app_id`.
      2. ``manager.delete_application(sandbox_id)`` on stop —
         idempotent.
      3. ``manager.list_applications()`` for triage.
      4. ``manager.cleanup()`` for test teardown / worker shutdown.
    """

    def __init__(
        self,
        *,
        config: CFAccessConfig,
        client: CFAccessClient | None = None,
    ) -> None:
        if not isinstance(config, CFAccessConfig):
            raise TypeError("config must be a CFAccessConfig")
        self._config = config
        self._client: CFAccessClient = client or HttpxCFAccessClient(config)
        self._lock = threading.RLock()
        self._apps: dict[str, CFAccessApplicationRecord] = {}

    @property
    def config(self) -> CFAccessConfig:
        return self._config

    @property
    def client(self) -> CFAccessClient:
        return self._client

    # ─────────────── Public API ───────────────

    def create_application(
        self,
        *,
        sandbox_id: str,
        emails: Iterable[str],
    ) -> CFAccessApplicationRecord:
        """Create (or recover) an Access application for ``sandbox_id``.

        Idempotent: if the CF account already has an application with
        the deterministic name :func:`build_application_name`,
        :meth:`create_application` returns a record pointing at the
        existing app rather than POSTing a duplicate. Useful when a
        worker crashes mid-launch — the next launch picks up the
        already-created app cleanly.

        Raises :class:`CFAccessAPIError` when the CF API rejects
        either the GET or the POST — the launcher catches and folds
        into a per-instance warning so the launch itself doesn't fail
        just because CF is briefly unreachable.
        """

        validate_sandbox_id(sandbox_id)
        effective = compute_effective_emails(emails, defaults=self._config.default_emails)
        if not effective:
            raise CFAccessError(
                "no emails to allow — operator must supply at least one "
                "email or configure cf_access_default_emails"
            )
        name = build_application_name(sandbox_id)
        domain = build_application_domain(sandbox_id, self._config.tunnel_host)
        with self._lock:
            existing_apps = self._client.list_applications()
            existing = _find_app_by_name(existing_apps, name)
            if existing is not None:
                app_id = _extract_app_id(existing)
                record = CFAccessApplicationRecord(
                    sandbox_id=sandbox_id,
                    app_id=app_id,
                    name=name,
                    domain=domain,
                    emails=effective,
                )
                self._apps[sandbox_id] = record
                logger.debug(
                    "cf_access: app already present for sandbox_id=%s name=%s",
                    sandbox_id,
                    name,
                )
                return record
            payload = build_application_payload(
                name=name,
                domain=domain,
                emails=effective,
                session_duration=self._config.session_duration,
                auto_redirect_to_identity=self._config.auto_redirect_to_identity,
            )
            response = self._client.create_application(payload)
            app_id = _extract_app_id(response)
            record = CFAccessApplicationRecord(
                sandbox_id=sandbox_id,
                app_id=app_id,
                name=name,
                domain=domain,
                emails=effective,
            )
            self._apps[sandbox_id] = record
            logger.info(
                "cf_access: app created for sandbox_id=%s name=%s app_id=%s "
                "(token=%s account=%s)",
                sandbox_id,
                name,
                app_id,
                token_fingerprint(self._config.api_token),
                self._config.account_id,
            )
        return record

    def delete_application(self, sandbox_id: str) -> bool:
        """Delete the Access application for ``sandbox_id``. Idempotent.

        Returns ``True`` when an application was actually deleted,
        ``False`` when none was present (already gone). Raises
        :class:`CFAccessAPIError` when the CF API itself rejects the
        operation (other than ``404`` which is folded into ``False``).
        """

        validate_sandbox_id(sandbox_id)
        name = build_application_name(sandbox_id)
        with self._lock:
            cached = self._apps.get(sandbox_id)
            app_id = cached.app_id if cached is not None else None
            if app_id is None:
                # Cache miss — fall back to a live lookup so we don't
                # leave a stale CF app behind after a worker restart.
                existing_apps = self._client.list_applications()
                existing = _find_app_by_name(existing_apps, name)
                if existing is None:
                    self._apps.pop(sandbox_id, None)
                    logger.debug(
                        "cf_access: app already absent for sandbox_id=%s name=%s",
                        sandbox_id,
                        name,
                    )
                    return False
                app_id = _extract_app_id(existing)
            try:
                self._client.delete_application(app_id)
            except CFAccessAPIError as exc:
                if exc.status == 404:
                    self._apps.pop(sandbox_id, None)
                    logger.debug(
                        "cf_access: 404 on delete for sandbox_id=%s name=%s — "
                        "treating as already-gone",
                        sandbox_id,
                        name,
                    )
                    return False
                raise
            self._apps.pop(sandbox_id, None)
            logger.info(
                "cf_access: app deleted for sandbox_id=%s name=%s app_id=%s "
                "(token=%s account=%s)",
                sandbox_id,
                name,
                app_id,
                token_fingerprint(self._config.api_token),
                self._config.account_id,
            )
        return True

    def get_application(self, sandbox_id: str) -> CFAccessApplicationRecord | None:
        """Return the cached record for ``sandbox_id`` (does not GET CF)."""

        with self._lock:
            return self._apps.get(sandbox_id)

    def list_applications(self) -> tuple[CFAccessApplicationRecord, ...]:
        """Return all cached records (does not GET CF)."""

        with self._lock:
            return tuple(self._apps.values())

    def public_url_for(self, sandbox_id: str) -> str:
        """Return the public URL for ``sandbox_id`` without touching CF.

        Mirror of :meth:`backend.cf_ingress.CFIngressManager.public_url_for`
        — same hostname shape so a caller building a URL preview
        ahead of CF round-trip gets a consistent answer from either
        manager.
        """

        validate_sandbox_id(sandbox_id)
        domain = build_application_domain(sandbox_id, self._config.tunnel_host)
        return f"https://{domain}"

    def snapshot(self) -> dict[str, Any]:
        """JSON-safe dict describing the manager's cached state."""

        with self._lock:
            return {
                "schema_version": CF_ACCESS_SCHEMA_VERSION,
                "config": self._config.to_dict(),
                "applications": [
                    {
                        "sandbox_id": r.sandbox_id,
                        "app_id": r.app_id,
                        "name": r.name,
                        "domain": r.domain,
                        "emails": list(r.emails),
                    }
                    for r in self._apps.values()
                ],
                "count": len(self._apps),
            }

    def cleanup(self) -> int:
        """Delete every Access app the manager has cached. Returns count.

        Best-effort: errors deleting individual apps are logged and do
        not stop the loop. Used by tests + worker shutdown when the
        manager wants to leave the CF Access tenant clean.
        """

        with self._lock:
            sandbox_ids = list(self._apps.keys())
        removed = 0
        for sid in sandbox_ids:
            try:
                if self.delete_application(sid):
                    removed += 1
            except CFAccessError as exc:
                logger.warning(
                    "cf_access: cleanup failed for sandbox_id=%s: %s",
                    sid,
                    exc,
                )
        return removed


# ───────────────────────────────────────────────────────────────────
#  Internal helpers
# ───────────────────────────────────────────────────────────────────


def _find_app_by_name(
    apps: Iterable[Mapping[str, Any]],
    name: str,
) -> Mapping[str, Any] | None:
    """Return the first app whose ``name`` matches ``name``, else None."""

    for app in apps:
        if not isinstance(app, Mapping):
            continue
        if app.get("name") == name:
            return app
    return None


def _extract_app_id(app: Mapping[str, Any]) -> str:
    """Extract the application id from a CF API response item.

    CF Access uses ``id`` (and sometimes ``uid``); we accept either,
    preferring ``id`` when both are present. Raises
    :class:`CFAccessAPIError` when neither field is a non-empty
    string.
    """

    if not isinstance(app, Mapping):
        raise CFAccessAPIError("CF Access app payload must be a mapping")
    for key in ("id", "uid"):
        value = app.get(key)
        if isinstance(value, str) and value.strip():
            return value
    raise CFAccessAPIError(
        "CF Access app payload missing 'id' / 'uid' field"
    )
