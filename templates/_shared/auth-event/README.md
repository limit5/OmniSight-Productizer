# `@omnisight/auth-event` — AS.5.1 Auth event format (TS twin)

> Behaviourally identical mirror of `backend/security/auth_event.py`.
> The OmniSight backend ships the Python lib; this directory ships the
> generated-app TS lib.  Every generated-app self-audit sink emits rows
> via this module so the OmniSight AS.5.2 dashboard sees one unified
> event stream regardless of which app produced the row.

## What this is

Eight canonical **dashboard-rollup** event names + the per-event JSON
payload shape:

| Event                          | When it fires                                     |
| ------------------------------ | ------------------------------------------------- |
| `auth.login_success`           | After successful authentication                   |
| `auth.login_fail`              | After failed authentication                       |
| `auth.oauth_connect`           | After an OAuth provider was linked to a user      |
| `auth.oauth_revoke`            | After an OAuth provider was unlinked              |
| `auth.bot_challenge_pass`      | Captcha verified or bypassed via AS.0.6 axis      |
| `auth.bot_challenge_fail`      | Captcha rejected (lowscore / honeypot / jsfail)   |
| `auth.token_refresh`           | After an OAuth refresh attempt (any outcome)      |
| `auth.token_rotated`           | When the provider issued a NEW refresh token      |

These are **rollup** events.  The forensic trail
(`oauth.login_init` / `oauth.login_callback` / `oauth.refresh` /
`oauth.unlink` / `oauth.token_rotated`, AS.1.4) coexists.  A successful
OAuth login emits TWO rows:

  1. `oauth.login_callback` (full state_fp + scope + oidc) — forensic
  2. `auth.login_success` (compact: actor + auth_method + provider +
     mfa_satisfied) — dashboard rollup

Both row families have a place; AS.5.2 dashboard widgets count the
rollup family for rate / ratio queries.

## Cross-twin contract (10 invariants)

Pinned by `backend/tests/test_auth_event_shape_drift.py`:

  1. **8 event names** — byte-equal across the two twins.
  2. **3 `entity_kind` constants** — `auth_session` /
     `oauth_connection` / `oauth_token`.
  3. **6-value `AUTH_METHODS`** — password / oauth / passkey / mfa_totp
     / mfa_webauthn / magic_link.
  4. **10-value `LOGIN_FAIL_REASONS`** — bad_password / unknown_user /
     account_locked / account_disabled / mfa_required / mfa_failed /
     rate_limited / bot_challenge_failed / oauth_state_invalid /
     oauth_provider_error.
  5. **4-value `BOT_CHALLENGE_PASS_KINDS`** — verified /
     bypass_apikey / bypass_ip_allowlist / bypass_test_token.
  6. **5-value `BOT_CHALLENGE_FAIL_REASONS`** — lowscore / unverified /
     honeypot / jsfail / server_error.
  7. **3-value `TOKEN_REFRESH_OUTCOMES`** — success / no_refresh_token
     / provider_error.
  8. **2-value `TOKEN_ROTATION_TRIGGERS`** — auto_refresh /
     explicit_refresh.
  9. **2-value `OAUTH_CONNECT_OUTCOMES`** — connected / relinked.
 10. **3-value `OAUTH_REVOKE_INITIATORS`** — user / admin / dsar.

Plus per-event field shape (the `after` keys) and the SHA-256 fingerprint
algorithm (12-char prefix, byte-equal with AS.1.4).

## Quick start (Node-side audit sink)

```ts
import {
  emitLoginSuccess,
  type LoginSuccessContext,
  type AuthAuditPayload,
  type AuthAuditSink,
} from "@omnisight/auth-event"

// 1. Implement a sink for your app's transport.
const fetchSink: AuthAuditSink = async (payload: AuthAuditPayload) => {
  await fetch("https://omnisight.example/api/v1/audit", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "authorization": `Bearer ${process.env.OMNISIGHT_AUDIT_TOKEN}`,
    },
    body: JSON.stringify(payload),
  })
}

// 2. Call the typed emit helper after a successful auth.
const ctx: LoginSuccessContext = {
  userId: "u-1234",
  authMethod: "oauth",
  provider: "github",
  mfaSatisfied: true,
  ip: req.ip,
  userAgent: req.headers["user-agent"],
}
await emitLoginSuccess(ctx, fetchSink)
```

The emit helper:

  * Resolves `OMNISIGHT_AS_FRONTEND_ENABLED` (default `true`).  When
    `false`, the helper returns `null` without invoking the sink —
    matches the Python lib's AS.0.8 single-knob behaviour.
  * Validates `authMethod` against the `AUTH_METHODS` set — throws
    `Error` on a typo so a bad string can't silently land in the
    dashboard.
  * Builds the canonical payload with PII redaction (IP, user-agent,
    attempted-username, refresh tokens are SHA-256 fingerprinted; raw
    values never leave the helper).

## PII redaction policy

| Field                   | How it's stored                       |
| ----------------------- | ------------------------------------- |
| `ip` → `ip_fp`          | First 12 chars of `SHA-256(ip)`       |
| `userAgent` → `user_agent_fp` | First 12 chars of `SHA-256(userAgent)` |
| `attemptedUser` → `attempted_user_fp` | First 12 chars of `SHA-256(attemptedUser)` |
| `previousRefreshToken` → `prior_refresh_token_fp` | First 12 chars of `SHA-256(previousRefreshToken)` |
| `newRefreshToken` → `new_refresh_token_fp` | First 12 chars of `SHA-256(newRefreshToken)` |

12 chars × 4 bits/char = 48 bits of selectivity.  Plenty for forensic
correlation ("here's a fingerprint that fired 50 login_fail rows in a
minute"); not enough to enumerate the underlying secret space.  Same
algorithm + length as AS.1.4 `oauth-client/audit.ts`, so a single
secret correlates across the OAuth forensic family and the AS.5.1
rollup family.

## AS.0.8 single-knob behaviour

`isEnabled()` reads `OMNISIGHT_AS_FRONTEND_ENABLED` lazily on every
call.  When `"false"` or `"0"` (case-insensitive), the eight `emit*`
helpers short-circuit to `null` without invoking the sink.  The pure
`build*Payload` builders deliberately ignore the knob — a doc-generator
or test harness needs to inspect the canonical payload shape regardless.

## Public API

```ts
// Eight event-name constants
EVENT_AUTH_LOGIN_SUCCESS, EVENT_AUTH_LOGIN_FAIL,
EVENT_AUTH_OAUTH_CONNECT, EVENT_AUTH_OAUTH_REVOKE,
EVENT_AUTH_BOT_CHALLENGE_PASS, EVENT_AUTH_BOT_CHALLENGE_FAIL,
EVENT_AUTH_TOKEN_REFRESH, EVENT_AUTH_TOKEN_ROTATED,
ALL_AUTH_EVENTS

// Three entity_kind constants
ENTITY_KIND_AUTH_SESSION, ENTITY_KIND_OAUTH_CONNECTION,
ENTITY_KIND_OAUTH_TOKEN

// Vocabularies
AUTH_METHODS, LOGIN_FAIL_REASONS,
BOT_CHALLENGE_PASS_KINDS, BOT_CHALLENGE_FAIL_REASONS,
TOKEN_REFRESH_OUTCOMES, TOKEN_ROTATION_TRIGGERS,
OAUTH_CONNECT_OUTCOMES, OAUTH_REVOKE_INITIATORS

// Helpers
fingerprint, isEnabled, FINGERPRINT_LENGTH

// 8 typed Context interfaces
LoginSuccessContext, LoginFailContext,
OAuthConnectContext, OAuthRevokeContext,
BotChallengePassContext, BotChallengeFailContext,
TokenRefreshContext, TokenRotatedContext

// Payload + sink types
AuthAuditPayload, AuthAuditSink, noopSink

// 8 pure builders
buildLoginSuccessPayload, buildLoginFailPayload,
buildOAuthConnectPayload, buildOAuthRevokePayload,
buildBotChallengePassPayload, buildBotChallengeFailPayload,
buildTokenRefreshPayload, buildTokenRotatedPayload

// 8 emit helpers (gated on isEnabled)
emitLoginSuccess, emitLoginFail,
emitOAuthConnect, emitOAuthRevoke,
emitBotChallengePass, emitBotChallengeFail,
emitTokenRefresh, emitTokenRotated
```
