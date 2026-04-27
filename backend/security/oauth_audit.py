"""AS.1.4 — OAuth audit event format + unified emit into ``audit_log``.

Canonical emission surface for the five OAuth events AS.1.1 defined as
strings (``oauth.{login_init, login_callback, refresh, unlink,
token_rotated}``). Every OAuth caller (AS.6.1 ``/api/v1/auth/oauth/...``
endpoints, AS.2.x token-vault refresh path, future webhook revocation
handlers) MUST use these helpers — handcrafting an ``audit.log(...)``
call with the same ``action=`` string is forbidden. The drift-guard tests
will fail CI if a caller bypasses the layer.

Why a thin emit layer instead of every caller calling ``audit.log``
──────────────────────────────────────────────────────────────────
* **Field-shape contract**: the audit row's ``before`` / ``after`` JSON
  is read by ``/admin/audit/tenants/{tid}`` query surface, the I8 chain
  verifier, future T-series billing aggregators, and the AS.5.2
  dashboard's per-event count widgets. Hand-rolled callers drift on
  field names ("provider" vs "provider_slug" vs "vendor"); a single
  emit module pins the names once and forever.
* **Token redaction**: OAuth audit rows are visible to admins via
  ``/admin/audit/tenants/{tid}``. Raw access_tokens or refresh_tokens
  in the row body would be a ``credentials in audit log`` finding from
  any external auditor. The emit layer enforces "fingerprint only"
  (first 12 chars of the value, after a SHA-256 over the bytes) so
  forensic correlation works without exposing the secret.
* **Knob-off symmetry**: AS.0.8 §5 audit-behaviour matrix says
  knob-false MUST NOT write any ``oauth.*`` row (because
  ``oauth_client`` returns 503 from ``/api/v1/auth/oauth/...`` per the
  AS.0.8 §3.1 noop matrix, so emitting the row would be lying — there
  was no flow to record). Centralising the gate here means every
  caller automatically inherits the correct knob-off behaviour even
  if a future caller forgets to check ``is_enabled()``.

Cross-twin contract
───────────────────
The TS twin (``templates/_shared/oauth-client/audit.ts``) ships
byte-identical event-name + field-set + outcome-vocabulary shapes so
generated-app audit sinks emit rows the OmniSight backend can unify.
The AS.1.5 drift-guard test pins the SHA-256 of the canonical field
sets across the two sides — same pattern as AS.1.3
``test_vendor_catalog_field_parity_python_ts``.

Module-global state audit (per implement_phase_step.md SOP §1)
──────────────────────────────────────────────────────────────
* No module-level mutable state. The frozen dataclasses
  (``LoginInitContext`` / ``LoginCallbackContext`` / …) are created
  per-call by the caller and discarded after emit.
* No DB connections held at module level — every emit borrows from
  ``backend.audit.log`` which holds the connection only for the
  duration of the chain-append transaction.
* No env reads at module top — the AS knob is read lazily inside each
  emitter via ``oauth_client.is_enabled()`` (which itself goes through
  ``backend.config.settings`` → fresh per worker).
* SHA-256 of the canonical refresh-token / state value comes from
  :mod:`hashlib`, not :mod:`secrets`. We're not generating randomness
  here, just deriving a stable fingerprint from already-secret
  material — using ``hashlib`` directly avoids confusion in the
  ``import secrets`` provenance grep that the AS lib's drift-guard
  tests enforce.

Read-after-write timing audit (per implement_phase_step.md SOP §1)
──────────────────────────────────────────────────────────────────
N/A — fan-out is to ``audit.log`` which serialises chain appends
through ``pg_advisory_xact_lock(hashtext('audit-chain-' || tenant_id))``
inside ``conn.transaction()``. Two concurrent OAuth callers in the
same tenant cannot interleave their chain rows.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any, Optional

from backend import audit
from backend.security import oauth_client

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Constants — canonical entity_kind + outcome vocabularies
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# The audit-row ``entity_kind`` for the two OAuth aggregates.
# ``oauth_flow`` covers in-flight init+callback (lifetime ≤ state TTL,
# default 600 s); ``oauth_token`` covers the per-user-per-provider
# stored credential (lifetime measured in months / years).
ENTITY_KIND_FLOW = "oauth_flow"
ENTITY_KIND_TOKEN = "oauth_token"

# Outcome vocabulary — pinned strings the dashboard widget keys on.
# Adding a new outcome means updating both Python + TS sides AND the
# AS.5.2 dashboard mapping; deleting one is a breaking change for the
# audit-trail consumers (chain verifier, billing aggregator).
OUTCOME_SUCCESS = "success"
OUTCOME_STATE_MISMATCH = "state_mismatch"
OUTCOME_STATE_EXPIRED = "state_expired"
OUTCOME_TOKEN_ERROR = "token_error"
OUTCOME_CALLBACK_ERROR = "callback_error"
OUTCOME_NO_REFRESH_TOKEN = "no_refresh_token"
OUTCOME_PROVIDER_ERROR = "provider_error"
OUTCOME_NOT_LINKED = "not_linked"
OUTCOME_REVOCATION_FAILED = "revocation_failed"
OUTCOME_REVOCATION_SKIPPED = "revocation_skipped"

# Vocabularies grouped per event family. Tests assert each emit only
# accepts an outcome from its allowed set so a typo can't sneak a
# "successs" string into the audit chain.
LOGIN_CALLBACK_OUTCOMES: frozenset[str] = frozenset({
    OUTCOME_SUCCESS,
    OUTCOME_STATE_MISMATCH,
    OUTCOME_STATE_EXPIRED,
    OUTCOME_TOKEN_ERROR,
    OUTCOME_CALLBACK_ERROR,
})
REFRESH_OUTCOMES: frozenset[str] = frozenset({
    OUTCOME_SUCCESS,
    OUTCOME_NO_REFRESH_TOKEN,
    OUTCOME_PROVIDER_ERROR,
})
UNLINK_OUTCOMES: frozenset[str] = frozenset({
    OUTCOME_SUCCESS,
    OUTCOME_NOT_LINKED,
    OUTCOME_REVOCATION_FAILED,
})
ROTATION_TRIGGERS: frozenset[str] = frozenset({
    "auto_refresh",
    "explicit_refresh",
})
REVOCATION_OUTCOMES: frozenset[str] = frozenset({
    OUTCOME_SUCCESS,
    OUTCOME_REVOCATION_FAILED,
    OUTCOME_REVOCATION_SKIPPED,
})

# Fingerprint length — first 12 chars of a SHA-256 hex digest. 48 bits
# of selectivity is plenty for forensic correlation (2^48 / 4G ≈ 65k
# rows before a collision becomes likely) without leaking the underlying
# secret. Mirrors AS.0.6 §5 ``token_fp last-12`` convention.
FINGERPRINT_LENGTH = 12


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Helpers — fingerprinting + actor defaults
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def fingerprint(value: Optional[str]) -> Optional[str]:
    """Return a stable first-12-chars fingerprint of *value*.

    Used for cross-row correlation (login_init↔login_callback by state,
    rotation history by refresh_token) WITHOUT writing the underlying
    secret to the audit chain. Returns ``None`` if value is ``None`` or
    empty so the JSON column round-trips a typed null instead of an
    empty string ("").

    Implementation note: SHA-256 is overkill for this purpose
    (correlation, not security), but it's deterministic, available in
    every runtime, and consistent with the TS twin which reuses
    ``crypto.subtle.digest("SHA-256", ...)``.
    """
    if value is None or value == "":
        return None
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return digest[:FINGERPRINT_LENGTH]


def _normalize_scope(scope: Any) -> list[str]:
    """Normalise scope to a list of strings.

    Accepts ``None`` (→ ``[]``), a single space-separated string
    (→ split), a tuple, or any other iterable of strings. Mirrors
    ``oauth_client.parse_token_response``'s scope handling so the
    audit row's scope shape is stable regardless of which caller
    layer (raw vendor response vs. parsed TokenSet.scope tuple) feeds
    in.
    """
    if scope is None:
        return []
    if isinstance(scope, str):
        return [s for s in scope.replace(",", " ").split() if s]
    return [str(s) for s in scope]


def _entity_id_token(provider: str, user_id: str) -> str:
    """Compose the ``entity_id`` for ``oauth_token``-kind rows.

    ``f"{provider}:{user_id}"`` — survives URL-encoding, sorts naturally
    in the admin audit-pane filter, and is the same key the AS.2.x
    token vault row uses, so the audit row joins cleanly to the vault.
    """
    return f"{provider}:{user_id}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Frozen context dataclasses (caller-built, emit-consumed)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass(frozen=True)
class LoginInitContext:
    """Inputs for ``oauth.login_init``.

    Built by the AS.6.1 ``/api/v1/auth/oauth/{provider}/login`` handler
    after :func:`oauth_client.begin_authorization` returns the FlowSession.
    The actor is whoever initiated — ``"anonymous"`` for the signup /
    pre-auth path; the logged-in user_id for an account-link flow from
    Settings.
    """

    provider: str
    state: str
    scope: tuple[str, ...]
    redirect_uri: str
    use_oidc_nonce: bool
    state_ttl_seconds: int
    actor: str = "anonymous"


@dataclass(frozen=True)
class LoginCallbackContext:
    """Inputs for ``oauth.login_callback``.

    Built by the AS.6.1 ``/api/v1/auth/oauth/{provider}/callback``
    handler after either a successful token exchange or any of the
    failure modes the lib raises (``StateMismatchError`` /
    ``StateExpiredError`` / ``TokenResponseError`` / unhandled exception).
    """

    provider: str
    state: str
    outcome: str
    actor: str = "anonymous"
    granted_scope: tuple[str, ...] = ()
    has_refresh_token: bool = False
    expires_in_seconds: Optional[int] = None
    is_oidc: bool = False
    error: Optional[str] = None


@dataclass(frozen=True)
class RefreshContext:
    """Inputs for ``oauth.refresh``.

    Built by the AS.2.x token-vault refresh path (whether triggered by
    :class:`oauth_client.AutoRefreshAuth` background-refresh or by an
    explicit revocation-recovery call). ``previous_expires_at`` is the
    absolute epoch-seconds the OLD token was due to expire (or ``None``
    if the vault row had no ``expires_at`` recorded — rare, only for
    providers that don't issue ``expires_in``).
    """

    provider: str
    user_id: str
    outcome: str
    previous_expires_at: Optional[float] = None
    new_expires_in_seconds: Optional[int] = None
    granted_scope: tuple[str, ...] = ()
    error: Optional[str] = None
    actor: Optional[str] = None  # defaults to user_id when omitted


@dataclass(frozen=True)
class UnlinkContext:
    """Inputs for ``oauth.unlink``.

    Built by the AS.6.1 ``/api/v1/auth/oauth/{provider}/unlink`` handler
    or by the GDPR / DSAR right-to-erasure flow (AS.2.5). Both paths
    delete the ``oauth_tokens`` row and (best-effort) call the
    provider's RFC 7009 token-revocation endpoint when one is known.
    """

    provider: str
    user_id: str
    outcome: str
    revocation_attempted: bool = False
    revocation_outcome: Optional[str] = None
    actor: Optional[str] = None


@dataclass(frozen=True)
class TokenRotatedContext:
    """Inputs for ``oauth.token_rotated``.

    Fired in addition to ``oauth.refresh`` whenever the provider issued
    a NEW ``refresh_token`` (i.e. RFC 6749 §10.4 / OAuth 2.1 BCP §4.13
    rotation actually happened). Lets ops differentiate "we refreshed
    the access_token" (frequent, low-signal) from "we rotated the
    refresh_token" (rare, security-sensitive — ground truth for
    detecting refresh-token replay attempts).
    """

    provider: str
    user_id: str
    previous_refresh_token: str
    new_refresh_token: str
    triggered_by: str
    actor: Optional[str] = None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Knob gate
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _gate() -> bool:
    """AS.0.8 §5 audit-behaviour matrix: knob-false ⇒ no oauth.* rows.

    Returns False if the AS feature family is globally disabled. Every
    public emitter checks this first and silently no-ops if false —
    callers don't need their own ``if not is_enabled(): ...`` because
    knob-flip changes downstream OAuth behaviour (endpoint returns 503
    per AS.0.8 §3.1) and an audit row in that case would be lying
    (there was no flow to record).
    """
    return bool(oauth_client.is_enabled())


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Public emitters — one per EVENT_OAUTH_* constant
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def emit_login_init(ctx: LoginInitContext) -> Optional[int]:
    """Emit one ``oauth.login_init`` row and return the new audit row id.

    Returns ``None`` if the AS knob is off, or if ``audit.log`` swallowed
    a transient chain-append failure (which it does by policy — see
    ``backend/audit.py`` docstring). Never raises on emit failures;
    callers MUST treat the return value as best-effort.
    """
    if not _gate():
        return None
    after = {
        "provider": ctx.provider,
        "state_fp": fingerprint(ctx.state),
        "scope": list(ctx.scope),
        "redirect_uri": ctx.redirect_uri,
        "use_oidc_nonce": bool(ctx.use_oidc_nonce),
        "state_ttl_seconds": int(ctx.state_ttl_seconds),
    }
    return await audit.log(
        action=oauth_client.EVENT_OAUTH_LOGIN_INIT,
        entity_kind=ENTITY_KIND_FLOW,
        entity_id=ctx.state,
        before=None,
        after=after,
        actor=ctx.actor,
    )


async def emit_login_callback(ctx: LoginCallbackContext) -> Optional[int]:
    """Emit one ``oauth.login_callback`` row.

    Validates ``ctx.outcome`` against :data:`LOGIN_CALLBACK_OUTCOMES`
    so a typo cannot sneak in. ``before`` carries ``state_fp`` only —
    the link to the originating ``oauth.login_init`` row is via
    ``entity_id`` (full state) plus the fingerprint for query
    selectivity in the admin pane.
    """
    if ctx.outcome not in LOGIN_CALLBACK_OUTCOMES:
        raise ValueError(
            f"oauth.login_callback outcome {ctx.outcome!r} not in "
            f"{sorted(LOGIN_CALLBACK_OUTCOMES)}"
        )
    if not _gate():
        return None
    state_fp = fingerprint(ctx.state)
    before = {"provider": ctx.provider, "state_fp": state_fp}
    after: dict[str, Any] = {
        "provider": ctx.provider,
        "state_fp": state_fp,
        "outcome": ctx.outcome,
        "granted_scope": list(ctx.granted_scope),
        "has_refresh_token": bool(ctx.has_refresh_token),
        "expires_in_seconds": (
            int(ctx.expires_in_seconds)
            if ctx.expires_in_seconds is not None
            else None
        ),
        "is_oidc": bool(ctx.is_oidc),
    }
    if ctx.error:
        after["error"] = str(ctx.error)
    return await audit.log(
        action=oauth_client.EVENT_OAUTH_LOGIN_CALLBACK,
        entity_kind=ENTITY_KIND_FLOW,
        entity_id=ctx.state,
        before=before,
        after=after,
        actor=ctx.actor,
    )


async def emit_refresh(ctx: RefreshContext) -> Optional[int]:
    """Emit one ``oauth.refresh`` row.

    The complementary ``oauth.token_rotated`` row (if rotation actually
    happened) is a SEPARATE emit — see :func:`emit_token_rotated`.
    Callers fire both: ``emit_refresh`` always, ``emit_token_rotated``
    only if the provider issued a fresh refresh_token.
    """
    if ctx.outcome not in REFRESH_OUTCOMES:
        raise ValueError(
            f"oauth.refresh outcome {ctx.outcome!r} not in "
            f"{sorted(REFRESH_OUTCOMES)}"
        )
    if not _gate():
        return None
    actor = ctx.actor or ctx.user_id
    before = {
        "provider": ctx.provider,
        "previous_expires_at": (
            float(ctx.previous_expires_at)
            if ctx.previous_expires_at is not None
            else None
        ),
    }
    after: dict[str, Any] = {
        "provider": ctx.provider,
        "outcome": ctx.outcome,
        "new_expires_in_seconds": (
            int(ctx.new_expires_in_seconds)
            if ctx.new_expires_in_seconds is not None
            else None
        ),
        "granted_scope": list(ctx.granted_scope),
    }
    if ctx.error:
        after["error"] = str(ctx.error)
    return await audit.log(
        action=oauth_client.EVENT_OAUTH_REFRESH,
        entity_kind=ENTITY_KIND_TOKEN,
        entity_id=_entity_id_token(ctx.provider, ctx.user_id),
        before=before,
        after=after,
        actor=actor,
    )


async def emit_unlink(ctx: UnlinkContext) -> Optional[int]:
    """Emit one ``oauth.unlink`` row.

    ``revocation_outcome`` is validated against
    :data:`REVOCATION_OUTCOMES` when ``revocation_attempted`` is true;
    when revocation was not attempted the field is forced to ``None``
    so the audit row's shape stays consistent (no half-typed fields).
    """
    if ctx.outcome not in UNLINK_OUTCOMES:
        raise ValueError(
            f"oauth.unlink outcome {ctx.outcome!r} not in "
            f"{sorted(UNLINK_OUTCOMES)}"
        )
    revocation_outcome = ctx.revocation_outcome
    if ctx.revocation_attempted:
        if revocation_outcome is None or revocation_outcome not in REVOCATION_OUTCOMES:
            raise ValueError(
                "oauth.unlink revocation_outcome must be one of "
                f"{sorted(REVOCATION_OUTCOMES)} when revocation_attempted=True"
            )
    else:
        revocation_outcome = None
    if not _gate():
        return None
    actor = ctx.actor or ctx.user_id
    before = {"provider": ctx.provider}
    after = {
        "provider": ctx.provider,
        "outcome": ctx.outcome,
        "revocation_attempted": bool(ctx.revocation_attempted),
        "revocation_outcome": revocation_outcome,
    }
    return await audit.log(
        action=oauth_client.EVENT_OAUTH_UNLINK,
        entity_kind=ENTITY_KIND_TOKEN,
        entity_id=_entity_id_token(ctx.provider, ctx.user_id),
        before=before,
        after=after,
        actor=actor,
    )


async def emit_token_rotated(ctx: TokenRotatedContext) -> Optional[int]:
    """Emit one ``oauth.token_rotated`` row.

    Records that the provider rotated the refresh_token. Both old and
    new refresh_tokens are stored as 12-char SHA-256 fingerprints —
    the raw values would be credentials, never written. The two
    fingerprints together let the chain verifier reconstruct rotation
    history without ever holding plaintext.
    """
    if ctx.triggered_by not in ROTATION_TRIGGERS:
        raise ValueError(
            f"oauth.token_rotated triggered_by {ctx.triggered_by!r} not in "
            f"{sorted(ROTATION_TRIGGERS)}"
        )
    if not _gate():
        return None
    actor = ctx.actor or ctx.user_id
    before = {
        "provider": ctx.provider,
        "prior_refresh_token_fp": fingerprint(ctx.previous_refresh_token),
    }
    after = {
        "provider": ctx.provider,
        "new_refresh_token_fp": fingerprint(ctx.new_refresh_token),
        "triggered_by": ctx.triggered_by,
    }
    return await audit.log(
        action=oauth_client.EVENT_OAUTH_TOKEN_ROTATED,
        entity_kind=ENTITY_KIND_TOKEN,
        entity_id=_entity_id_token(ctx.provider, ctx.user_id),
        before=before,
        after=after,
        actor=actor,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Public surface
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


__all__ = [
    "ENTITY_KIND_FLOW",
    "ENTITY_KIND_TOKEN",
    "FINGERPRINT_LENGTH",
    "LOGIN_CALLBACK_OUTCOMES",
    "LoginCallbackContext",
    "LoginInitContext",
    "OUTCOME_CALLBACK_ERROR",
    "OUTCOME_NOT_LINKED",
    "OUTCOME_NO_REFRESH_TOKEN",
    "OUTCOME_PROVIDER_ERROR",
    "OUTCOME_REVOCATION_FAILED",
    "OUTCOME_REVOCATION_SKIPPED",
    "OUTCOME_STATE_EXPIRED",
    "OUTCOME_STATE_MISMATCH",
    "OUTCOME_SUCCESS",
    "OUTCOME_TOKEN_ERROR",
    "REFRESH_OUTCOMES",
    "REVOCATION_OUTCOMES",
    "ROTATION_TRIGGERS",
    "RefreshContext",
    "TokenRotatedContext",
    "UNLINK_OUTCOMES",
    "UnlinkContext",
    "emit_login_callback",
    "emit_login_init",
    "emit_refresh",
    "emit_token_rotated",
    "emit_unlink",
    "fingerprint",
]
