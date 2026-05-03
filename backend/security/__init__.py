"""Security primitives — auth-adjacent helpers, lazy-loaded.

R20 Phase 0 (chat-layer):
  - prompt_hardening.INJECTION_GUARD_PRELUDE — system-prompt prelude
  - prompt_hardening.looks_like_injection(text) — heuristic detector
  - prompt_hardening.harden_user_message(text) — wrap suspicious input
  - secret_filter.redact(text) — output redaction (returns text + labels)

SC.7.1 (OWASP mitigation shared lib):
  - input_validation — pure allowlist-first scalar validators for
    generated apps (bounded text, slug, identifier, email, enum, integer
    range).  Framework agnostic; callers map
    InputValidationError.issue to HTTP / form / job errors.

SC.7.2 (OWASP mitigation shared lib):
  - output_encoding — pure context-specific encoders for generated apps
    (HTML text, quoted HTML attribute, JavaScript string literal,
    JSON-in-script body, URL component). Framework agnostic; callers
    choose the encoder matching the sink and keep template autoescape
    enabled.

SC.7.3 (OWASP mitigation shared lib):
  - query_templates — pure PostgreSQL / asyncpg-style parameterized
    CRUD query templates for generated apps.  Framework agnostic;
    callers execute QueryTemplate.sql with QueryTemplate.params and
    keep arbitrary SQL expressions outside this helper.

AS.0.10 (auth shared lib):
  - password_generator — pure-functional auto-gen password core lib
    (Random / Diceware / Pronounceable). Importable submodule, no
    runtime side effects. TS twin lives at
    `templates/_shared/password-generator/`.

AS.1.1 (auth shared lib):
  - oauth_client — protocol primitives (PKCE / state / nonce /
    refresh rotation / auto-refresh middleware). Provider-agnostic;
    vendor catalogs land in AS.1.3. TS twin lives at
    `templates/_shared/oauth-client/index.ts` (AS.1.2).

AS.1.3 (auth shared lib):
  - oauth_vendors — frozen `VendorConfig` catalog for the 11 shipped
    OAuth providers (GitHub / Google / Microsoft / Apple / GitLab /
    Bitbucket / Slack / Notion / Salesforce / HubSpot / Discord) +
    `begin_authorization_for_vendor` / `build_authorize_url_for_vendor`
    catalog-aware shims onto AS.1.1. TS twin lives at
    `templates/_shared/oauth-client/vendors.ts`.

AS.1.4 (auth shared lib):
  - oauth_audit — canonical OAuth audit-event emit layer. Five
    `emit_oauth_*` async helpers (login_init / login_callback /
    refresh / unlink / token_rotated) plus typed context dataclasses
    fixing the `before` / `after` JSON shape. Routes into
    `backend.audit.log`; honours the AS.0.8 single knob (knob-false ⇒
    silent skip per AS.0.8 §5 truth-table). TS twin lives at
    `templates/_shared/oauth-client/audit.ts`.

AS.2.1 (auth shared lib):
  - token_vault — per-user / per-provider OAuth credential at-rest
    encryption. Reuses `backend.secret_store._fernet` (single master
    Fernet key invariant per AS.0.4 §3). Wraps plaintext in a binding
    envelope so a DB-level row swap is caught by `decrypt_for_user`.
    `key_version` column is reserved for the future KMS rotation hook
    (defaults to 1 in this release). Provider whitelist mirrors
    `account_linking._AS1_OAUTH_PROVIDERS` (drift-guarded). TS twin
    lives at `templates/_shared/token-vault/` (AS.2.3).

AS.2.4 (auth shared lib):
  - oauth_refresh_hook — stateless orchestrator that auto-refreshes
    a stored OAuth `oauth_tokens` row's access_token within
    `skew_seconds` (default 60 s) of expiry. Decrypts via the AS.2.1
    vault, calls a caller-provided `refresh_fn` against the IdP's
    token endpoint, applies RFC 6749 §10.4 / OAuth 2.1 BCP §4.13
    rotation via `oauth_client.apply_rotation`, re-encrypts, and
    emits the AS.1.4 `oauth.refresh` + (if rotated) `oauth.token_rotated`
    audit rows. Persistence is the caller's job — the hook returns a
    `RefreshOutcome` carrying a fresh `TokenVaultRecord` whose
    `version` has been bumped by one for the AS.2.2 optimistic-lock
    `UPDATE ... WHERE version = old_version`.

AS.2.5 (auth shared lib):
  - oauth_revoke — stateless orchestrator that revokes a stored
    OAuth credential at the IdP via RFC 7009 then surfaces a
    `RevokeOutcome` the caller acts on (typically followed by
    `DELETE FROM oauth_tokens`).  Powers both the user-initiated
    unlink path (AS.6.1 `POST /api/v1/auth/oauth/{provider}/unlink`)
    and the regulatory DSAR / GDPR right-to-erasure runbook.  IdP
    revocation is best-effort (some vendors expose no endpoint —
    Microsoft / Bitbucket / Notion / HubSpot / GitHub per the AS.1.3
    catalog); local deletion is mandatory regardless of the IdP
    result so DSAR compliance survives an unreachable provider.
    Honours the AS.0.4 §6.2 "DSAR keeps working knob-off" invariant
    via the audit layer's silent-skip.

AS.6.5 (OmniSight self-integration — audit log bridge to AS.5 event format):
  - auth_audit_bridge — thin best-effort wrapper around the AS.5.1
    :mod:`auth_event` emitters that wires the existing OmniSight
    self-handlers (``/auth/login``, ``/auth/mfa/*``,
    ``oauth_refresh_hook``) into the canonical ``auth.*`` rollup-event
    family so the AS.5.2 per-tenant dashboard, the suspicious-pattern
    detector, and any generated-app self-audit sink can count real
    OmniSight login activity.  Dual-emit additive contract — the
    legacy ``login_ok`` / ``auth.login.fail`` / ``mfa.challenge.passed``
    audit rows stay where they are; this bridge fans one additional
    AS.5.1 rollup row alongside (the same shape AS.6.1 already
    adopted in the OAuth callback for ``oauth.login_callback`` +
    ``auth.login_success``).  Provides FastAPI ``Request``-aware
    IP / user-agent extraction (Cloudflare ``cf-connecting-ip``
    precedence matches AS.6.1's ``_client_key``), an
    :func:`mfa_method_to_auth_method` dispatch table mapping the 3
    OmniSight MFA challenge labels onto the AS.5.1
    :data:`auth_event.AUTH_METHODS` vocabulary, and 4 async +
    2 fire-and-forget emit helpers — each best-effort, swallow on
    failure, never raises.

AS.6.4 (OmniSight self-integration — per-form honeypot wiring):
  - honeypot_form_verifier — universal helper that wraps AS.4.1
    ``honeypot.validate_honeypot`` + ``should_reject`` + the
    forensic ``bot_challenge.honeypot_*`` audit emit into one entry
    point so the four OmniSight self-forms (login / signup /
    password-reset / contact) share identical wiring per AS.0.5
    §6.1 acceptance criteria — sibling to AS.6.3
    :mod:`turnstile_form_verifier`. Provides 4 form-action constants
    + 4 form-path constants byte-equal
    :data:`backend.security.honeypot._FORM_PREFIXES` keys (cross-
    module drift-guarded), an :data:`ANONYMOUS_TENANT_ID` sentinel
    for pre-auth forms (login / signup / password-reset before
    user identification), an :func:`extract_bypass_kind_from_request`
    helper that walks the AS.0.6 §4 axes (api_key / test_token /
    ip_allowlist) and routes the result onto the
    :data:`honeypot.ALL_BYPASS_KINDS` vocabulary so bypass-flagged
    callers short-circuit the field check, and two async
    orchestrators — :func:`verify_form_honeypot` (returns the
    result for fail-open knob-off / bypass / pass paths) and
    :func:`verify_form_honeypot_or_reject` (raises
    :class:`honeypot.HoneypotRejected` on field-filled or form-drift
    so the HTTP layer can map to the canonical 429
    ``bot_challenge_failed`` response — same surface as the AS.6.3
    captcha 429 so the front-end UI keys on a single error code
    regardless of which layer caught the bot). The router-side
    wiring lives in ``backend/routers/auth.py`` for the two
    existing forms (``/auth/login`` + ``/auth/change-password``),
    placed BEFORE the AS.6.3 captcha verify so a confirmed bot
    can't even consume the upstream siteverify round-trip budget;
    the helper itself is the SoT every future caller (signup /
    contact / generated-app forms via the AS.7.x React widgets)
    reuses.

AS.6.3 (OmniSight self-integration — Turnstile backend verify wiring):
  - turnstile_form_verifier — universal helper that wraps AS.3.1
    ``bot_challenge.verify_with_fallback`` + AS.3.4
    ``should_reject`` + AS.5.1 ``auth.bot_challenge_pass`` /
    ``auth.bot_challenge_fail`` rollup emit into one entry point so
    the four OmniSight self-forms (login / signup / password-reset
    / contact) share identical wiring per AS.0.5 §6.1 acceptance
    criteria. Provides 4 canonical form-action constants
    (``login`` / ``signup`` / ``pwreset`` / ``contact``) + 4 form-path
    constants byte-equal :data:`backend.security.honeypot._FORM_PREFIXES`
    keys (cross-module drift-guarded), reads phase from
    ``OMNISIGHT_BOT_CHALLENGE_PHASE`` env var (default 1 fail-open
    per AS.0.5 §2.2), reads provider site secrets via
    :func:`bot_challenge.secret_env_for`, and provides two async
    orchestrators — :func:`verify_form_token` (returns the result for
    fail-open Phase 1/2) and :func:`verify_form_token_or_reject`
    (raises :class:`bot_challenge.BotChallengeRejected` on Phase 3
    confirmed reject so the HTTP layer can map to the canonical 429
    ``bot_challenge_failed`` response). The router-side wiring lives
    in ``backend/routers/auth.py`` for the two existing forms
    (``/auth/login`` + ``/auth/change-password``); the helper itself
    is the SoT every future caller (signup / contact / generated-app
    forms via the AS.7.x React widgets) reuses.

AS.6.2 (OmniSight self-integration — credential vault expand phase):
  - credential_vault — generalised binding-envelope vault that
    extends the AS.2.1 token_vault pattern to git_accounts and
    llm_credentials at-rest secrets. Pure expand-only per AS.0.4
    §2 Track C / §6.1: no caller wired (``git_credentials.py`` /
    ``llm_credentials.py`` still call ``secret_store.encrypt``
    directly), no schema migration. The migrate-phase row will
    dual-write through ``encrypt_git_secret`` /
    ``encrypt_llm_credential``; a forward-reservation
    ``migrate_legacy_secret_store_ciphertext`` helper rewraps a
    legacy plain Fernet ciphertext into the binding envelope so
    the migrate row has a single seam to call. Reuses
    ``secret_store._fernet`` (single master Fernet key invariant
    per AS.0.4 §3.1 — drift-guarded).

AS.6.1 (OmniSight self-integration):
  - oauth_login_handler — backend handler that wires the AS.1 OAuth
    shared library to OmniSight's own login flow. Implements the
    four ``Sign in with Google / GitHub / Microsoft / Apple`` SSO
    buttons via two HTTP endpoints registered in
    ``backend/routers/auth.py`` (``GET /api/v1/auth/oauth/{vendor}/
    {authorize,callback}``). FlowSession round-trips through a
    HMAC-SHA256-signed HttpOnly cookie keyed by
    ``oauth_flow_signing_key`` (or ``decision_bearer`` fallback).
    User identity extraction handles per-vendor field-name quirks
    (Google/Microsoft OIDC ``sub``, GitHub numeric ``id``, Apple
    id_token claims since Apple has no userinfo endpoint).
    Account-link flow honours AS.0.3 takeover prevention — refuses
    silent link when the matched email already carries a
    ``"password"`` method. Ships login-only; AS.6.2 will route the
    issued tokens through the AS.2 vault.

AS.5.2 (auth shared lib):
  - auth_dashboard — per-tenant rollup + suspicious-pattern detection.
    Pure-functional read-side companion to AS.5.1 ``auth_event``:
    `summarise(rows, tenant_id=...)` reduces audit rows into a frozen
    `DashboardSummary` (challenge pass/fail rate, auth-method
    distribution, per-event counts + per-vocabulary breakdowns);
    `detect_suspicious_patterns(rows, tenant_id=...)` runs six rules
    (login_fail_burst / bot_challenge_fail_spike / token_refresh_storm
    / honeypot_triggered / oauth_revoke_relink_loop /
    distributed_login_fail) over the same rows and returns frozen
    `SuspiciousPatternAlert` objects ready for the AS.7.x notification
    surface. Async `compute_dashboard(tenant_id, since=..., until=...)`
    orchestrator handles the AS.0.8 knob-off banner contract +
    `audit_log` PG fetch + summarise + detect. TS twin lives at
    `templates/_shared/auth-dashboard/index.ts`.

AS.5.1 (auth shared lib):
  - auth_event — canonical AS.5 dashboard-rollup event family. Eight
    `EVENT_AUTH_*` constants (login_success / login_fail / oauth_connect
    / oauth_revoke / bot_challenge_pass / bot_challenge_fail /
    token_refresh / token_rotated) + per-event frozen Context
    dataclasses + pure `build_*_payload` builders + async `emit_*`
    helpers gated on the AS.0.8 single knob. Sibling to the forensic
    `oauth_audit` family (oauth.login_init/callback/refresh/unlink/
    token_rotated): forensic captures every step of a flow, AS.5.1
    captures one outcome row per attempt for the AS.5.2 dashboard
    rollups. PII (ip / user-agent / attempted-username / refresh
    tokens) is fingerprinted via 12-char SHA-256 — raw values never
    land in the chain. TS twin lives at
    `templates/_shared/auth-event/index.ts`.

AS.3.1 (auth shared lib):
  - bot_challenge — unified bot-challenge interface across Turnstile,
    reCAPTCHA v2, reCAPTCHA v3, and hCaptcha.  Provides:
      * :class:`Provider` enum + :func:`verify_provider` per-provider
        siteverify HTTP wrapper with score normalisation (Turnstile /
        reCAPTCHA v3 → vendor float; reCAPTCHA v2 / hCaptcha → 1.0/0.0
        binary).
      * :class:`VerifyContext` + :func:`verify` end-to-end orchestrator
        (knob → bypass → provider verify → phase-aware classify).
      * :class:`BypassContext` + :func:`evaluate_bypass` walking the
        AS.0.6 §4 axis-internal precedence (api_key → test_token →
        ip_allowlist → path).
      * 19 ``EVENT_BOT_CHALLENGE_*`` constants matching AS.0.5 §3 +
        AS.0.6 §3 byte-for-byte (8 verify + 7 bypass + 4 phase).
      * AS.0.5 §2 phase-aware classify_outcome (Phase 1/2 fail-open,
        Phase 3 fail-closed only on confirmed low-score).
      * AS.0.8 single-knob :func:`is_enabled` short-circuit via
        :func:`passthrough`.
    TS twin lives at `templates/_shared/bot-challenge/` (AS.3.2).
    Provider-selection heuristic (AS.3.3), score-threshold rejection
    wiring (AS.3.4), and fallback chain (AS.3.5) are follow-up rows
    that build on this surface.
"""

from .prompt_hardening import (
    INJECTION_GUARD_PRELUDE,
    harden_user_message,
    looks_like_injection,
)
from .secret_filter import redact

# Re-export pure submodules by name (cheap — constants + functions, no IO).
from . import auth_audit_bridge  # noqa: F401
from . import auth_dashboard  # noqa: F401
from . import auth_event  # noqa: F401
from . import bot_challenge  # noqa: F401
from . import credential_vault  # noqa: F401
from . import honeypot  # noqa: F401
from . import honeypot_form_verifier  # noqa: F401
from . import input_validation  # noqa: F401
from . import oauth_audit  # noqa: F401
from . import oauth_client  # noqa: F401
from . import oauth_login_handler  # noqa: F401
from . import oauth_refresh_hook  # noqa: F401
from . import oauth_revoke  # noqa: F401
from . import oauth_vendors  # noqa: F401
from . import password_generator  # noqa: F401
from . import token_vault  # noqa: F401
from . import turnstile_form_verifier  # noqa: F401

__all__ = [
    "INJECTION_GUARD_PRELUDE",
    "auth_audit_bridge",
    "auth_dashboard",
    "auth_event",
    "bot_challenge",
    "credential_vault",
    "harden_user_message",
    "honeypot",
    "honeypot_form_verifier",
    "input_validation",
    "looks_like_injection",
    "oauth_audit",
    "oauth_client",
    "oauth_login_handler",
    "oauth_refresh_hook",
    "oauth_revoke",
    "oauth_vendors",
    "password_generator",
    "redact",
    "token_vault",
    "turnstile_form_verifier",
]
