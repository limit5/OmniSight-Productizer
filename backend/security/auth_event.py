"""AS.5.1 — Auth event format (dashboard-rollup family).

Canonical event surface for the AS.5 observability family.  Defines the
**eight** dashboard-facing auth-outcome events that the AS.5.2 per-tenant
dashboard, the AS.6.5 OmniSight self-audit-log integration, and any
generated-app self-audit sinks key on:

    auth.login_success
    auth.login_fail
    auth.oauth_connect
    auth.oauth_revoke
    auth.bot_challenge_pass
    auth.bot_challenge_fail
    auth.token_refresh
    auth.token_rotated

Why a separate event family from the existing forensic-trail families
─────────────────────────────────────────────────────────────────────
The existing AS event families serve different purposes:

  * ``oauth_client.EVENT_OAUTH_*`` (5 events: ``oauth.login_init`` /
    ``oauth.login_callback`` / ``oauth.refresh`` / ``oauth.unlink`` /
    ``oauth.token_rotated``) — **forensic / debug trail**, captures
    every step of an OAuth handshake (init + callback as 2 rows, refresh
    + rotation as 2 rows).  Read by the I8 hash-chain verifier and
    ``/admin/audit/tenants/{tid}`` query surface.
  * ``bot_challenge.EVENT_BOT_CHALLENGE_*`` (19 events: 8 verify + 7
    bypass + 4 phase) — **per-vendor / per-phase ground truth** for the
    captcha verify path.
  * ``honeypot.EVENT_BOT_CHALLENGE_HONEYPOT_*`` (3 events) —
    honeypot-trap pass / fail / form-drift signal.

The eight ``auth.*`` events this module ships are the **high-level
outcome rollups** the AS.5.2 dashboard counts and alerts on:

    "How many login_success per tenant per hour?"
    "What's the bot_challenge_pass / fail ratio?"
    "Spike in token_refresh suggests a stuck refresh loop."

Forensic and rollup events coexist by design.  A successful OAuth login
emits two rows (one in each family):

    1. ``oauth.login_callback`` (full state_fp + scope + oidc + ...) →
       forensic chain, queryable in admin audit pane.
    2. ``auth.login_success`` (compact: actor + auth_method=oauth +
       provider + mfa_satisfied) → dashboard rollup, counted by AS.5.2.

Plan / spec source
──────────────────
* TODO row AS.5.1 — eight canonical event names.
* ``docs/design/as-auth-security-shared-library.md`` §3 — twin pattern.
* ``docs/security/as_0_8_single_knob_rollback.md`` §5 — knob-off audit
  matrix (knob-false ⇒ silent skip; same shape as ``oauth_audit._gate``).
* ``backend/security/oauth_audit.py`` — emitter pattern this module
  mirrors (frozen ``*Context`` dataclasses → pure ``build_*_payload``
  → gated ``emit_*`` async helpers).

What this row ships (AS.5.1 scope, strict)
──────────────────────────────────────────
1. **Eight ``EVENT_AUTH_*`` string constants** + :data:`ALL_AUTH_EVENTS`
   tuple.  Names live here as the SoT; the existing ``oauth_client.
   EVENT_OAUTH_*`` constants stay where they are (forensic family).
2. **Vocabularies** — :data:`AUTH_METHOD` (6 values: ``password`` /
   ``oauth`` / ``passkey`` / ``mfa_totp`` / ``mfa_webauthn`` /
   ``magic_link``), :data:`LOGIN_FAIL_REASONS` (10 values),
   :data:`BOT_CHALLENGE_PASS_KINDS` (4: ``verified`` / ``bypass_apikey``
   / ``bypass_ip_allowlist`` / ``bypass_test_token``),
   :data:`BOT_CHALLENGE_FAIL_REASONS` (5 values),
   :data:`TOKEN_REFRESH_OUTCOMES` (3 values, mirrors AS.1.4
   ``REFRESH_OUTCOMES``), :data:`TOKEN_ROTATION_TRIGGERS` (2 values,
   mirrors AS.1.4 ``ROTATION_TRIGGERS``),
   :data:`OAUTH_CONNECT_OUTCOMES` (2: ``connected`` / ``relinked``),
   :data:`OAUTH_REVOKE_INITIATORS` (3: ``user`` / ``admin`` / ``dsar``).
3. **Eight frozen ``*Context`` dataclasses** — one per event, fixing
   the canonical ``before`` / ``after`` JSON shape the dashboard reads.
4. **Eight pure ``build_*_payload`` functions** — return an
   :class:`AuthAuditPayload` with ``action``, ``entity_kind``,
   ``entity_id``, ``before``, ``after``, ``actor``.  No IO.  Validate
   each event's outcome / method / reason against its vocabulary.
5. **Eight async ``emit_*`` helpers** — gate on :func:`is_enabled`
   (AS.0.8 single-knob), then route into ``backend.audit.log``.  Return
   ``Optional[int]`` (audit row id; ``None`` on knob-off / transient
   audit failure, mirroring ``oauth_audit`` semantics).
6. **Fingerprint helper** — :func:`fingerprint` first-12-chars SHA-256,
   byte-identical to :func:`oauth_audit.fingerprint`.  Used to redact
   IP / user-agent / attempted-username in audit row metadata.
7. **AS.0.8 knob hook** — :func:`is_enabled` reads
   ``settings.as_enabled`` lazily via ``getattr`` fallback.

Out of scope (deferred to follow-up rows)
─────────────────────────────────────────
* AS.5.2 — Per-tenant dashboard widgets (challenge pass/fail rate, auth
  method distribution, suspicious pattern alerts) consume these events
  but the dashboard widget code lives in AS.5.2.
* AS.6.5 — Existing OmniSight backend handlers (login / OAuth /
  password-reset / contact form) actually call ``emit_login_success`` /
  etc. instead of bare ``audit.log``.  This row ships the lib; the
  wiring lands in AS.6.5.
* AS.6.3 — bot_challenge + honeypot reject paths actually emit
  ``auth.bot_challenge_fail`` (as additional fan-out alongside the
  forensic ``bot_challenge.blocked_lowscore`` etc.).
* Any SQL view / materialised view backing the AS.5.2 dashboard counts.

Module-global state audit (per implement_phase_step.md SOP §1)
──────────────────────────────────────────────────────────────
* All public symbols are immutable (``frozen=True`` dataclasses,
  ``frozenset`` / ``tuple`` constants, plain strings).  No module-level
  mutable container that two workers could disagree on.
* No DB connections held at module level.  Every emit borrows from
  :func:`backend.audit.log` which holds a connection only for the
  ``pg_advisory_xact_lock`` chain-append transaction (per-tenant
  serialisation).
* No env reads at module top.  :func:`is_enabled` reads
  ``settings.as_enabled`` lazily on every call so each uvicorn worker
  derives the same value from the same source — answer #1 of SOP §1
  (deterministic-by-construction across workers).
* :func:`fingerprint` uses :mod:`hashlib`, not :mod:`secrets` — we're
  not generating randomness, just deriving a stable redaction from
  PII material; same provenance grep AS.1.4 already enforces.
* Module-import is side-effect free — pure constants + dataclasses +
  function defs.

Read-after-write timing audit (per implement_phase_step.md SOP §1)
──────────────────────────────────────────────────────────────────
N/A — fan-out is to ``audit.log`` which serialises chain appends
through ``pg_advisory_xact_lock(hashtext('audit-chain-' || tenant_id))``
inside ``conn.transaction()``.  Two concurrent emitters in the same
tenant cannot interleave their chain rows.  Two emitters in different
tenants run on independent advisory locks (no cross-tenant contention).

AS.0.8 single-knob behaviour
────────────────────────────
* :func:`is_enabled` reads ``settings.as_enabled`` (default ``True``).
* When knob-false, every ``emit_*`` returns ``None`` immediately
  without writing — same matrix as :func:`oauth_audit._gate`.  Builder
  functions deliberately ignore the knob: a script that wants to
  inspect the canonical payload shape (test harness, doc generator)
  must work regardless.

TS twin
───────
``templates/_shared/auth-event/`` ships the byte-equal mirror.  The
AS.5.1 cross-twin drift guard (``backend/tests/test_auth_event_shape_drift.py``)
locks the 8 event names + every vocabulary + per-event ``after``
field-set + entity_kind constants + fingerprint algorithm.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any, Optional

from backend import audit

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Eight canonical event names — AS.5.1 SoT
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

EVENT_AUTH_LOGIN_SUCCESS: str = "auth.login_success"
EVENT_AUTH_LOGIN_FAIL: str = "auth.login_fail"
EVENT_AUTH_OAUTH_CONNECT: str = "auth.oauth_connect"
EVENT_AUTH_OAUTH_REVOKE: str = "auth.oauth_revoke"
EVENT_AUTH_BOT_CHALLENGE_PASS: str = "auth.bot_challenge_pass"
EVENT_AUTH_BOT_CHALLENGE_FAIL: str = "auth.bot_challenge_fail"
EVENT_AUTH_TOKEN_REFRESH: str = "auth.token_refresh"
EVENT_AUTH_TOKEN_ROTATED: str = "auth.token_rotated"


ALL_AUTH_EVENTS: tuple[str, ...] = (
    EVENT_AUTH_LOGIN_SUCCESS,
    EVENT_AUTH_LOGIN_FAIL,
    EVENT_AUTH_OAUTH_CONNECT,
    EVENT_AUTH_OAUTH_REVOKE,
    EVENT_AUTH_BOT_CHALLENGE_PASS,
    EVENT_AUTH_BOT_CHALLENGE_FAIL,
    EVENT_AUTH_TOKEN_REFRESH,
    EVENT_AUTH_TOKEN_ROTATED,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  entity_kind constants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# ``auth_session`` covers per-attempt auth events (login_success /
# login_fail / bot_challenge_pass / bot_challenge_fail).  Lifetime is
# bounded by the request that produced the row.
ENTITY_KIND_AUTH_SESSION: str = "auth_session"

# ``oauth_connection`` covers the per-user-per-provider connection
# (oauth_connect / oauth_revoke).  Sibling to AS.1.4
# ``ENTITY_KIND_TOKEN``; chosen to disambiguate "the user-visible
# connection" (this row) from "the stored token blob" (AS.1.4).  The
# AS.5.2 dashboard joins these on (tenant_id, provider, user_id).
ENTITY_KIND_OAUTH_CONNECTION: str = "oauth_connection"

# ``oauth_token`` covers token-lifecycle events (token_refresh /
# token_rotated).  Same string as AS.1.4 ``oauth_audit.ENTITY_KIND_TOKEN``
# so the AS.5.2 dashboard can correlate refresh activity in the rollup
# family with the forensic ``oauth.refresh`` rows in the trail family.
ENTITY_KIND_OAUTH_TOKEN: str = "oauth_token"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Vocabularies — frozen sets the dashboard widget keys on
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Auth method literals.  Six values cover every login surface AS ships:
#   * ``password`` — classic email + password.
#   * ``oauth`` — third-party IdP login (any of 11 vendors per AS.1.3).
#   * ``passkey`` — WebAuthn passwordless (AS.7.1 / AS.7.7).
#   * ``mfa_totp`` — TOTP second factor (AS.7.4).
#   * ``mfa_webauthn`` — WebAuthn second factor (AS.7.4).
#   * ``magic_link`` — email magic link (AS.7.3 password-reset reuse).
AUTH_METHOD_PASSWORD: str = "password"
AUTH_METHOD_OAUTH: str = "oauth"
AUTH_METHOD_PASSKEY: str = "passkey"
AUTH_METHOD_MFA_TOTP: str = "mfa_totp"
AUTH_METHOD_MFA_WEBAUTHN: str = "mfa_webauthn"
AUTH_METHOD_MAGIC_LINK: str = "magic_link"

AUTH_METHODS: frozenset[str] = frozenset({
    AUTH_METHOD_PASSWORD,
    AUTH_METHOD_OAUTH,
    AUTH_METHOD_PASSKEY,
    AUTH_METHOD_MFA_TOTP,
    AUTH_METHOD_MFA_WEBAUTHN,
    AUTH_METHOD_MAGIC_LINK,
})


# Login fail reason literals.  Ten values cover every observed failure
# class.  Mirrors the AS.7.1 unified-error-message contract: the audit
# row records the *real* reason for forensics, the response body returns
# a generic "invalid credentials" message so an attacker can't enumerate.
LOGIN_FAIL_BAD_PASSWORD: str = "bad_password"
LOGIN_FAIL_UNKNOWN_USER: str = "unknown_user"
LOGIN_FAIL_ACCOUNT_LOCKED: str = "account_locked"
LOGIN_FAIL_ACCOUNT_DISABLED: str = "account_disabled"
LOGIN_FAIL_MFA_REQUIRED: str = "mfa_required"
LOGIN_FAIL_MFA_FAILED: str = "mfa_failed"
LOGIN_FAIL_RATE_LIMITED: str = "rate_limited"
LOGIN_FAIL_BOT_CHALLENGE_FAILED: str = "bot_challenge_failed"
LOGIN_FAIL_OAUTH_STATE_INVALID: str = "oauth_state_invalid"
LOGIN_FAIL_OAUTH_PROVIDER_ERROR: str = "oauth_provider_error"

LOGIN_FAIL_REASONS: frozenset[str] = frozenset({
    LOGIN_FAIL_BAD_PASSWORD,
    LOGIN_FAIL_UNKNOWN_USER,
    LOGIN_FAIL_ACCOUNT_LOCKED,
    LOGIN_FAIL_ACCOUNT_DISABLED,
    LOGIN_FAIL_MFA_REQUIRED,
    LOGIN_FAIL_MFA_FAILED,
    LOGIN_FAIL_RATE_LIMITED,
    LOGIN_FAIL_BOT_CHALLENGE_FAILED,
    LOGIN_FAIL_OAUTH_STATE_INVALID,
    LOGIN_FAIL_OAUTH_PROVIDER_ERROR,
})


# Bot-challenge pass kinds.  Four values let the dashboard split
# "captcha verified" from "bypassed via AS.0.6 axis" so a tenant admin
# can spot if their bypass-list is being abused.
BOT_CHALLENGE_PASS_VERIFIED: str = "verified"
BOT_CHALLENGE_PASS_BYPASS_APIKEY: str = "bypass_apikey"
BOT_CHALLENGE_PASS_BYPASS_IP_ALLOWLIST: str = "bypass_ip_allowlist"
BOT_CHALLENGE_PASS_BYPASS_TEST_TOKEN: str = "bypass_test_token"

BOT_CHALLENGE_PASS_KINDS: frozenset[str] = frozenset({
    BOT_CHALLENGE_PASS_VERIFIED,
    BOT_CHALLENGE_PASS_BYPASS_APIKEY,
    BOT_CHALLENGE_PASS_BYPASS_IP_ALLOWLIST,
    BOT_CHALLENGE_PASS_BYPASS_TEST_TOKEN,
})


# Bot-challenge fail reasons.  Five values cover the AS.3 + AS.4
# fail-class taxonomy (lowscore + unverified + jsfail + honeypot +
# server-error).  ``server_error`` is distinct from ``unverified`` so
# the dashboard can alert on vendor outage vs. real bot traffic.
BOT_CHALLENGE_FAIL_LOWSCORE: str = "lowscore"
BOT_CHALLENGE_FAIL_UNVERIFIED: str = "unverified"
BOT_CHALLENGE_FAIL_HONEYPOT: str = "honeypot"
BOT_CHALLENGE_FAIL_JSFAIL: str = "jsfail"
BOT_CHALLENGE_FAIL_SERVER_ERROR: str = "server_error"

BOT_CHALLENGE_FAIL_REASONS: frozenset[str] = frozenset({
    BOT_CHALLENGE_FAIL_LOWSCORE,
    BOT_CHALLENGE_FAIL_UNVERIFIED,
    BOT_CHALLENGE_FAIL_HONEYPOT,
    BOT_CHALLENGE_FAIL_JSFAIL,
    BOT_CHALLENGE_FAIL_SERVER_ERROR,
})


# Token-refresh outcomes — mirrors AS.1.4 ``REFRESH_OUTCOMES`` so the
# rollup row's outcome string equals the forensic row's outcome string.
TOKEN_REFRESH_SUCCESS: str = "success"
TOKEN_REFRESH_NO_REFRESH_TOKEN: str = "no_refresh_token"
TOKEN_REFRESH_PROVIDER_ERROR: str = "provider_error"

TOKEN_REFRESH_OUTCOMES: frozenset[str] = frozenset({
    TOKEN_REFRESH_SUCCESS,
    TOKEN_REFRESH_NO_REFRESH_TOKEN,
    TOKEN_REFRESH_PROVIDER_ERROR,
})


# Token-rotation triggers — mirrors AS.1.4 ``ROTATION_TRIGGERS``.
TOKEN_ROTATION_TRIGGER_AUTO: str = "auto_refresh"
TOKEN_ROTATION_TRIGGER_EXPLICIT: str = "explicit_refresh"

TOKEN_ROTATION_TRIGGERS: frozenset[str] = frozenset({
    TOKEN_ROTATION_TRIGGER_AUTO,
    TOKEN_ROTATION_TRIGGER_EXPLICIT,
})


# OAuth-connect outcomes.  ``connected`` = first-time link of a provider
# to the user.  ``relinked`` = a previously-revoked (or expired)
# connection re-established — distinct so the dashboard can spot the
# "user keeps revoking + relinking" anti-pattern.
OAUTH_CONNECT_CONNECTED: str = "connected"
OAUTH_CONNECT_RELINKED: str = "relinked"

OAUTH_CONNECT_OUTCOMES: frozenset[str] = frozenset({
    OAUTH_CONNECT_CONNECTED,
    OAUTH_CONNECT_RELINKED,
})


# OAuth-revoke initiators.  ``user`` = self-service via Settings.
# ``admin`` = tenant admin force-unlink.  ``dsar`` = GDPR right-to-
# erasure flow.  Three buckets so the dashboard separates "user
# decision" from "admin action" from "regulatory deletion".
OAUTH_REVOKE_USER: str = "user"
OAUTH_REVOKE_ADMIN: str = "admin"
OAUTH_REVOKE_DSAR: str = "dsar"

OAUTH_REVOKE_INITIATORS: frozenset[str] = frozenset({
    OAUTH_REVOKE_USER,
    OAUTH_REVOKE_ADMIN,
    OAUTH_REVOKE_DSAR,
})


# First N chars of a SHA-256 hex digest.  12 = 48 bits, plenty for
# forensic correlation without leaking the underlying secret.  Mirrors
# AS.1.4 ``oauth_audit.FINGERPRINT_LENGTH`` byte-for-byte.
FINGERPRINT_LENGTH: int = 12


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Helpers — fingerprint, knob gate
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def fingerprint(value: Optional[str]) -> Optional[str]:
    """Return a stable first-12-chars SHA-256 fingerprint of *value*.

    Used to redact PII (IP address, user-agent, attempted-username) in
    audit row metadata.  Forensic correlation works (same input always
    produces the same fingerprint) without writing the underlying
    secret to the audit chain.

    Returns ``None`` for ``None`` / empty so the JSON column round-trips
    a typed null instead of an empty string.  Byte-identical to
    :func:`backend.security.oauth_audit.fingerprint`.
    """
    if value is None or value == "":
        return None
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return digest[:FINGERPRINT_LENGTH]


def is_enabled() -> bool:
    """Whether the AS family is enabled per AS.0.8 §3.1 noop matrix.

    Mirrors :func:`backend.security.bot_challenge.is_enabled` and
    :func:`backend.security.oauth_client.is_enabled`.  Reads
    ``settings.as_enabled`` lazily via ``getattr`` fallback (default
    ``True``) so the module is importable before AS.3.1 lands the field
    on :class:`backend.config.Settings`.
    """
    try:
        from backend.config import settings  # local import: zero import-time side effect
    except Exception:
        return True
    return bool(getattr(settings, "as_enabled", True))


def _gate() -> bool:
    """AS.0.8 §5 audit-behaviour matrix: knob-false ⇒ no auth.* rows.

    Mirrors :func:`backend.security.oauth_audit._gate` so all AS audit
    families share the same gate semantics.
    """
    return is_enabled()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Frozen Context dataclasses — caller-built, builder-consumed
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass(frozen=True)
class LoginSuccessContext:
    """Inputs for :data:`EVENT_AUTH_LOGIN_SUCCESS`.

    Built by the AS.6.1 ``/api/v1/auth/login`` handler (or any caller
    that successfully authenticated a user).  The ``ip`` and
    ``user_agent`` fields are stored as 12-char SHA-256 fingerprints
    (PII redaction); the raw values are never written to the chain.
    """

    user_id: str
    auth_method: str
    provider: Optional[str] = None
    mfa_satisfied: bool = False
    ip: Optional[str] = None
    user_agent: Optional[str] = None
    actor: Optional[str] = None  # defaults to user_id when omitted


@dataclass(frozen=True)
class LoginFailContext:
    """Inputs for :data:`EVENT_AUTH_LOGIN_FAIL`.

    ``attempted_user`` is the email / username the request asserted
    (PII; stored as 12-char SHA-256 fingerprint).  Fingerprinting an
    asserted identifier lets the dashboard detect "10 fail attempts on
    the same fingerprint" without storing every guessed username
    plaintext.
    """

    attempted_user: str
    auth_method: str
    fail_reason: str
    provider: Optional[str] = None
    ip: Optional[str] = None
    user_agent: Optional[str] = None
    actor: str = "anonymous"


@dataclass(frozen=True)
class OAuthConnectContext:
    """Inputs for :data:`EVENT_AUTH_OAUTH_CONNECT`.

    Fired AFTER the OAuth callback successfully exchanged code → token
    AND the token landed in :mod:`token_vault`.  Distinct from
    ``oauth.login_callback`` (the forensic family) which fires
    regardless of vault outcome.
    """

    user_id: str
    provider: str
    outcome: str
    scope: tuple[str, ...] = ()
    is_account_link: bool = False
    actor: Optional[str] = None  # defaults to user_id when omitted


@dataclass(frozen=True)
class OAuthRevokeContext:
    """Inputs for :data:`EVENT_AUTH_OAUTH_REVOKE`.

    ``initiator`` records who triggered the revoke (user vs admin vs
    DSAR / GDPR right-to-erasure).  ``revocation_succeeded`` is the IdP
    revocation outcome (best-effort per AS.2.5; some vendors expose no
    revocation endpoint, the row still fires after the local DELETE).
    """

    user_id: str
    provider: str
    initiator: str
    revocation_succeeded: bool = False
    actor: Optional[str] = None


@dataclass(frozen=True)
class BotChallengePassContext:
    """Inputs for :data:`EVENT_AUTH_BOT_CHALLENGE_PASS`.

    Rollup of the AS.3 ``bot_challenge.pass`` + AS.0.6 ``bot_challenge.
    bypass_*`` family.  ``score`` is the normalised challenge score
    (0.0-1.0) when ``kind="verified"``; for bypass kinds it's ``None``.
    The ``form_path`` correlates with the AS.4.1 honeypot rows so the
    dashboard can split per-form challenge rates.
    """

    form_path: str
    kind: str
    provider: Optional[str] = None
    score: Optional[float] = None
    actor: str = "anonymous"


@dataclass(frozen=True)
class BotChallengeFailContext:
    """Inputs for :data:`EVENT_AUTH_BOT_CHALLENGE_FAIL`.

    Rollup of the AS.3 ``bot_challenge.blocked_lowscore`` /
    ``unverified_*`` + AS.4.1 ``bot_challenge.honeypot_fail`` family.
    ``score`` is the normalised challenge score when ``reason="lowscore"``;
    for ``honeypot`` / ``jsfail`` / ``server_error`` it's ``None``.
    """

    form_path: str
    reason: str
    provider: Optional[str] = None
    score: Optional[float] = None
    actor: str = "anonymous"


@dataclass(frozen=True)
class TokenRefreshContext:
    """Inputs for :data:`EVENT_AUTH_TOKEN_REFRESH`.

    Dashboard rollup sibling of ``oauth.refresh`` (forensic family).
    ``new_expires_in_seconds`` lets the dashboard graph "average token
    lifetime granted per provider" to spot vendors silently shortening
    the lifetime they advertise.
    """

    user_id: str
    provider: str
    outcome: str
    new_expires_in_seconds: Optional[int] = None
    actor: Optional[str] = None  # defaults to user_id


@dataclass(frozen=True)
class TokenRotatedContext:
    """Inputs for :data:`EVENT_AUTH_TOKEN_ROTATED`.

    Dashboard rollup sibling of ``oauth.token_rotated`` (forensic
    family).  Both old and new refresh tokens are stored as 12-char
    SHA-256 fingerprints — raw values would be credentials, never
    written.  ``triggered_by`` records whether rotation was on the
    automatic-refresh path or an explicit force-refresh.
    """

    user_id: str
    provider: str
    previous_refresh_token: str
    new_refresh_token: str
    triggered_by: str
    actor: Optional[str] = None  # defaults to user_id


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  AuthAuditPayload — the canonical row shape the sink receives
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass(frozen=True)
class AuthAuditPayload:
    """Canonical AS.5.1 audit row, ready for ``audit.log``.

    Mirrors the positional + keyword args of
    ``backend.audit.log(action=, entity_kind=, entity_id=, before=,
    after=, actor=)``.  Frozen so accidental mutation between build and
    emit raises ``FrozenInstanceError``.
    """

    action: str
    entity_kind: str
    entity_id: str
    before: Optional[dict[str, Any]]
    after: dict[str, Any]
    actor: str


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pure builders — no IO, no knob check
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _entity_id_oauth_connection(provider: str, user_id: str) -> str:
    """``f"{provider}:{user_id}"`` — same shape AS.1.4 uses for
    ``oauth_token`` rows so the dashboard can natural-join the two
    families on entity_id.
    """
    return f"{provider}:{user_id}"


def build_login_success_payload(ctx: LoginSuccessContext) -> AuthAuditPayload:
    """Build the canonical ``auth.login_success`` payload.

    Validates ``auth_method`` against :data:`AUTH_METHODS`.  Raises
    :class:`ValueError` on an unknown method so a typo cannot sneak
    into the dashboard counts.
    """
    if ctx.auth_method not in AUTH_METHODS:
        raise ValueError(
            f"auth.login_success auth_method {ctx.auth_method!r} not in "
            f"{sorted(AUTH_METHODS)}"
        )
    after: dict[str, Any] = {
        "auth_method": ctx.auth_method,
        "provider": ctx.provider,
        "mfa_satisfied": bool(ctx.mfa_satisfied),
        "ip_fp": fingerprint(ctx.ip),
        "user_agent_fp": fingerprint(ctx.user_agent),
    }
    return AuthAuditPayload(
        action=EVENT_AUTH_LOGIN_SUCCESS,
        entity_kind=ENTITY_KIND_AUTH_SESSION,
        entity_id=ctx.user_id,
        before=None,
        after=after,
        actor=ctx.actor or ctx.user_id,
    )


def build_login_fail_payload(ctx: LoginFailContext) -> AuthAuditPayload:
    """Build the canonical ``auth.login_fail`` payload.

    Validates ``auth_method`` and ``fail_reason`` against their
    vocabularies.  ``attempted_user`` is fingerprinted (PII redaction);
    the raw value never lands in the chain.  ``entity_id`` uses the
    fingerprint so two failed attempts against the same asserted
    identifier share an entity_id (dashboard rollup-by-entity works).
    """
    if ctx.auth_method not in AUTH_METHODS:
        raise ValueError(
            f"auth.login_fail auth_method {ctx.auth_method!r} not in "
            f"{sorted(AUTH_METHODS)}"
        )
    if ctx.fail_reason not in LOGIN_FAIL_REASONS:
        raise ValueError(
            f"auth.login_fail fail_reason {ctx.fail_reason!r} not in "
            f"{sorted(LOGIN_FAIL_REASONS)}"
        )
    attempted_fp = fingerprint(ctx.attempted_user)
    after: dict[str, Any] = {
        "auth_method": ctx.auth_method,
        "fail_reason": ctx.fail_reason,
        "provider": ctx.provider,
        "attempted_user_fp": attempted_fp,
        "ip_fp": fingerprint(ctx.ip),
        "user_agent_fp": fingerprint(ctx.user_agent),
    }
    return AuthAuditPayload(
        action=EVENT_AUTH_LOGIN_FAIL,
        entity_kind=ENTITY_KIND_AUTH_SESSION,
        entity_id=attempted_fp or "anonymous",
        before=None,
        after=after,
        actor=ctx.actor,
    )


def build_oauth_connect_payload(ctx: OAuthConnectContext) -> AuthAuditPayload:
    """Build the canonical ``auth.oauth_connect`` payload.

    Validates ``outcome`` against :data:`OAUTH_CONNECT_OUTCOMES`.
    """
    if ctx.outcome not in OAUTH_CONNECT_OUTCOMES:
        raise ValueError(
            f"auth.oauth_connect outcome {ctx.outcome!r} not in "
            f"{sorted(OAUTH_CONNECT_OUTCOMES)}"
        )
    after: dict[str, Any] = {
        "provider": ctx.provider,
        "outcome": ctx.outcome,
        "scope": list(ctx.scope),
        "is_account_link": bool(ctx.is_account_link),
    }
    return AuthAuditPayload(
        action=EVENT_AUTH_OAUTH_CONNECT,
        entity_kind=ENTITY_KIND_OAUTH_CONNECTION,
        entity_id=_entity_id_oauth_connection(ctx.provider, ctx.user_id),
        before=None,
        after=after,
        actor=ctx.actor or ctx.user_id,
    )


def build_oauth_revoke_payload(ctx: OAuthRevokeContext) -> AuthAuditPayload:
    """Build the canonical ``auth.oauth_revoke`` payload.

    Validates ``initiator`` against :data:`OAUTH_REVOKE_INITIATORS`.
    """
    if ctx.initiator not in OAUTH_REVOKE_INITIATORS:
        raise ValueError(
            f"auth.oauth_revoke initiator {ctx.initiator!r} not in "
            f"{sorted(OAUTH_REVOKE_INITIATORS)}"
        )
    after: dict[str, Any] = {
        "provider": ctx.provider,
        "initiator": ctx.initiator,
        "revocation_succeeded": bool(ctx.revocation_succeeded),
    }
    return AuthAuditPayload(
        action=EVENT_AUTH_OAUTH_REVOKE,
        entity_kind=ENTITY_KIND_OAUTH_CONNECTION,
        entity_id=_entity_id_oauth_connection(ctx.provider, ctx.user_id),
        before=None,
        after=after,
        actor=ctx.actor or ctx.user_id,
    )


def build_bot_challenge_pass_payload(
    ctx: BotChallengePassContext,
) -> AuthAuditPayload:
    """Build the canonical ``auth.bot_challenge_pass`` payload.

    Validates ``kind`` against :data:`BOT_CHALLENGE_PASS_KINDS`.
    ``score`` is required when ``kind="verified"``, must be ``None``
    for bypass kinds (a verified-pass without a score is a bug; a
    bypass with a score is misleading because no challenge ran).
    """
    if ctx.kind not in BOT_CHALLENGE_PASS_KINDS:
        raise ValueError(
            f"auth.bot_challenge_pass kind {ctx.kind!r} not in "
            f"{sorted(BOT_CHALLENGE_PASS_KINDS)}"
        )
    if ctx.kind == BOT_CHALLENGE_PASS_VERIFIED and ctx.score is None:
        raise ValueError(
            "auth.bot_challenge_pass kind='verified' requires score"
        )
    if ctx.kind != BOT_CHALLENGE_PASS_VERIFIED and ctx.score is not None:
        raise ValueError(
            f"auth.bot_challenge_pass kind={ctx.kind!r} must have score=None "
            "(no challenge ran)"
        )
    after: dict[str, Any] = {
        "form_path": ctx.form_path,
        "kind": ctx.kind,
        "provider": ctx.provider,
        "score": float(ctx.score) if ctx.score is not None else None,
    }
    return AuthAuditPayload(
        action=EVENT_AUTH_BOT_CHALLENGE_PASS,
        entity_kind=ENTITY_KIND_AUTH_SESSION,
        entity_id=ctx.form_path,
        before=None,
        after=after,
        actor=ctx.actor,
    )


def build_bot_challenge_fail_payload(
    ctx: BotChallengeFailContext,
) -> AuthAuditPayload:
    """Build the canonical ``auth.bot_challenge_fail`` payload.

    Validates ``reason`` against :data:`BOT_CHALLENGE_FAIL_REASONS`.
    """
    if ctx.reason not in BOT_CHALLENGE_FAIL_REASONS:
        raise ValueError(
            f"auth.bot_challenge_fail reason {ctx.reason!r} not in "
            f"{sorted(BOT_CHALLENGE_FAIL_REASONS)}"
        )
    after: dict[str, Any] = {
        "form_path": ctx.form_path,
        "reason": ctx.reason,
        "provider": ctx.provider,
        "score": float(ctx.score) if ctx.score is not None else None,
    }
    return AuthAuditPayload(
        action=EVENT_AUTH_BOT_CHALLENGE_FAIL,
        entity_kind=ENTITY_KIND_AUTH_SESSION,
        entity_id=ctx.form_path,
        before=None,
        after=after,
        actor=ctx.actor,
    )


def build_token_refresh_payload(ctx: TokenRefreshContext) -> AuthAuditPayload:
    """Build the canonical ``auth.token_refresh`` payload.

    Validates ``outcome`` against :data:`TOKEN_REFRESH_OUTCOMES`.
    """
    if ctx.outcome not in TOKEN_REFRESH_OUTCOMES:
        raise ValueError(
            f"auth.token_refresh outcome {ctx.outcome!r} not in "
            f"{sorted(TOKEN_REFRESH_OUTCOMES)}"
        )
    after: dict[str, Any] = {
        "provider": ctx.provider,
        "outcome": ctx.outcome,
        "new_expires_in_seconds": (
            int(ctx.new_expires_in_seconds)
            if ctx.new_expires_in_seconds is not None
            else None
        ),
    }
    return AuthAuditPayload(
        action=EVENT_AUTH_TOKEN_REFRESH,
        entity_kind=ENTITY_KIND_OAUTH_TOKEN,
        entity_id=_entity_id_oauth_connection(ctx.provider, ctx.user_id),
        before=None,
        after=after,
        actor=ctx.actor or ctx.user_id,
    )


def build_token_rotated_payload(ctx: TokenRotatedContext) -> AuthAuditPayload:
    """Build the canonical ``auth.token_rotated`` payload.

    Validates ``triggered_by`` against :data:`TOKEN_ROTATION_TRIGGERS`.
    Stores both refresh tokens as 12-char SHA-256 fingerprints — raw
    values would be credentials, never written.
    """
    if ctx.triggered_by not in TOKEN_ROTATION_TRIGGERS:
        raise ValueError(
            f"auth.token_rotated triggered_by {ctx.triggered_by!r} not in "
            f"{sorted(TOKEN_ROTATION_TRIGGERS)}"
        )
    before = {
        "provider": ctx.provider,
        "prior_refresh_token_fp": fingerprint(ctx.previous_refresh_token),
    }
    after: dict[str, Any] = {
        "provider": ctx.provider,
        "new_refresh_token_fp": fingerprint(ctx.new_refresh_token),
        "triggered_by": ctx.triggered_by,
    }
    return AuthAuditPayload(
        action=EVENT_AUTH_TOKEN_ROTATED,
        entity_kind=ENTITY_KIND_OAUTH_TOKEN,
        entity_id=_entity_id_oauth_connection(ctx.provider, ctx.user_id),
        before=before,
        after=after,
        actor=ctx.actor or ctx.user_id,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Async emitters — gate on AS.0.8 knob, then route to audit.log
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def _route_to_audit_log(payload: AuthAuditPayload) -> Optional[int]:
    """Forward a payload into ``backend.audit.log``.

    Returns the new audit row id, or ``None`` if the chain-append
    swallowed a transient failure (which it does by policy — see
    ``backend/audit.py``).  Never raises on emit failures; callers MUST
    treat the return value as best-effort.
    """
    return await audit.log(
        action=payload.action,
        entity_kind=payload.entity_kind,
        entity_id=payload.entity_id,
        before=payload.before,
        after=payload.after,
        actor=payload.actor,
    )


async def emit_login_success(ctx: LoginSuccessContext) -> Optional[int]:
    """Emit one ``auth.login_success`` row.  Returns the audit row id
    or ``None`` on knob-off / transient audit failure."""
    if not _gate():
        return None
    return await _route_to_audit_log(build_login_success_payload(ctx))


async def emit_login_fail(ctx: LoginFailContext) -> Optional[int]:
    """Emit one ``auth.login_fail`` row."""
    if not _gate():
        return None
    return await _route_to_audit_log(build_login_fail_payload(ctx))


async def emit_oauth_connect(ctx: OAuthConnectContext) -> Optional[int]:
    """Emit one ``auth.oauth_connect`` row."""
    if not _gate():
        return None
    return await _route_to_audit_log(build_oauth_connect_payload(ctx))


async def emit_oauth_revoke(ctx: OAuthRevokeContext) -> Optional[int]:
    """Emit one ``auth.oauth_revoke`` row."""
    if not _gate():
        return None
    return await _route_to_audit_log(build_oauth_revoke_payload(ctx))


async def emit_bot_challenge_pass(
    ctx: BotChallengePassContext,
) -> Optional[int]:
    """Emit one ``auth.bot_challenge_pass`` row."""
    if not _gate():
        return None
    return await _route_to_audit_log(build_bot_challenge_pass_payload(ctx))


async def emit_bot_challenge_fail(
    ctx: BotChallengeFailContext,
) -> Optional[int]:
    """Emit one ``auth.bot_challenge_fail`` row."""
    if not _gate():
        return None
    return await _route_to_audit_log(build_bot_challenge_fail_payload(ctx))


async def emit_token_refresh(ctx: TokenRefreshContext) -> Optional[int]:
    """Emit one ``auth.token_refresh`` row."""
    if not _gate():
        return None
    return await _route_to_audit_log(build_token_refresh_payload(ctx))


async def emit_token_rotated(ctx: TokenRotatedContext) -> Optional[int]:
    """Emit one ``auth.token_rotated`` row."""
    if not _gate():
        return None
    return await _route_to_audit_log(build_token_rotated_payload(ctx))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Public surface — stable export list
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


__all__ = [
    # Eight canonical event names
    "EVENT_AUTH_LOGIN_SUCCESS",
    "EVENT_AUTH_LOGIN_FAIL",
    "EVENT_AUTH_OAUTH_CONNECT",
    "EVENT_AUTH_OAUTH_REVOKE",
    "EVENT_AUTH_BOT_CHALLENGE_PASS",
    "EVENT_AUTH_BOT_CHALLENGE_FAIL",
    "EVENT_AUTH_TOKEN_REFRESH",
    "EVENT_AUTH_TOKEN_ROTATED",
    "ALL_AUTH_EVENTS",
    # entity_kind constants
    "ENTITY_KIND_AUTH_SESSION",
    "ENTITY_KIND_OAUTH_CONNECTION",
    "ENTITY_KIND_OAUTH_TOKEN",
    # Vocabularies
    "AUTH_METHOD_PASSWORD",
    "AUTH_METHOD_OAUTH",
    "AUTH_METHOD_PASSKEY",
    "AUTH_METHOD_MFA_TOTP",
    "AUTH_METHOD_MFA_WEBAUTHN",
    "AUTH_METHOD_MAGIC_LINK",
    "AUTH_METHODS",
    "LOGIN_FAIL_BAD_PASSWORD",
    "LOGIN_FAIL_UNKNOWN_USER",
    "LOGIN_FAIL_ACCOUNT_LOCKED",
    "LOGIN_FAIL_ACCOUNT_DISABLED",
    "LOGIN_FAIL_MFA_REQUIRED",
    "LOGIN_FAIL_MFA_FAILED",
    "LOGIN_FAIL_RATE_LIMITED",
    "LOGIN_FAIL_BOT_CHALLENGE_FAILED",
    "LOGIN_FAIL_OAUTH_STATE_INVALID",
    "LOGIN_FAIL_OAUTH_PROVIDER_ERROR",
    "LOGIN_FAIL_REASONS",
    "BOT_CHALLENGE_PASS_VERIFIED",
    "BOT_CHALLENGE_PASS_BYPASS_APIKEY",
    "BOT_CHALLENGE_PASS_BYPASS_IP_ALLOWLIST",
    "BOT_CHALLENGE_PASS_BYPASS_TEST_TOKEN",
    "BOT_CHALLENGE_PASS_KINDS",
    "BOT_CHALLENGE_FAIL_LOWSCORE",
    "BOT_CHALLENGE_FAIL_UNVERIFIED",
    "BOT_CHALLENGE_FAIL_HONEYPOT",
    "BOT_CHALLENGE_FAIL_JSFAIL",
    "BOT_CHALLENGE_FAIL_SERVER_ERROR",
    "BOT_CHALLENGE_FAIL_REASONS",
    "TOKEN_REFRESH_SUCCESS",
    "TOKEN_REFRESH_NO_REFRESH_TOKEN",
    "TOKEN_REFRESH_PROVIDER_ERROR",
    "TOKEN_REFRESH_OUTCOMES",
    "TOKEN_ROTATION_TRIGGER_AUTO",
    "TOKEN_ROTATION_TRIGGER_EXPLICIT",
    "TOKEN_ROTATION_TRIGGERS",
    "OAUTH_CONNECT_CONNECTED",
    "OAUTH_CONNECT_RELINKED",
    "OAUTH_CONNECT_OUTCOMES",
    "OAUTH_REVOKE_USER",
    "OAUTH_REVOKE_ADMIN",
    "OAUTH_REVOKE_DSAR",
    "OAUTH_REVOKE_INITIATORS",
    "FINGERPRINT_LENGTH",
    # Helpers
    "fingerprint",
    "is_enabled",
    # Context dataclasses
    "LoginSuccessContext",
    "LoginFailContext",
    "OAuthConnectContext",
    "OAuthRevokeContext",
    "BotChallengePassContext",
    "BotChallengeFailContext",
    "TokenRefreshContext",
    "TokenRotatedContext",
    # Payload + builders
    "AuthAuditPayload",
    "build_login_success_payload",
    "build_login_fail_payload",
    "build_oauth_connect_payload",
    "build_oauth_revoke_payload",
    "build_bot_challenge_pass_payload",
    "build_bot_challenge_fail_payload",
    "build_token_refresh_payload",
    "build_token_rotated_payload",
    # Async emitters
    "emit_login_success",
    "emit_login_fail",
    "emit_oauth_connect",
    "emit_oauth_revoke",
    "emit_bot_challenge_pass",
    "emit_bot_challenge_fail",
    "emit_token_refresh",
    "emit_token_rotated",
]
