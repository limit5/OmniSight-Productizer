"""Security primitives — auth-adjacent helpers, lazy-loaded.

R20 Phase 0 (chat-layer):
  - prompt_hardening.INJECTION_GUARD_PRELUDE — system-prompt prelude
  - prompt_hardening.looks_like_injection(text) — heuristic detector
  - prompt_hardening.harden_user_message(text) — wrap suspicious input
  - secret_filter.redact(text) — output redaction (returns text + labels)

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
from . import bot_challenge  # noqa: F401
from . import oauth_audit  # noqa: F401
from . import oauth_client  # noqa: F401
from . import oauth_refresh_hook  # noqa: F401
from . import oauth_revoke  # noqa: F401
from . import oauth_vendors  # noqa: F401
from . import password_generator  # noqa: F401
from . import token_vault  # noqa: F401

__all__ = [
    "INJECTION_GUARD_PRELUDE",
    "bot_challenge",
    "harden_user_message",
    "looks_like_injection",
    "oauth_audit",
    "oauth_client",
    "oauth_refresh_hook",
    "oauth_revoke",
    "oauth_vendors",
    "password_generator",
    "redact",
    "token_vault",
]
