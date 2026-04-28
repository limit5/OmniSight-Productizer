/**
 * AS.7.5 — Email-verification form helpers.
 *
 * Pure browser-safe helpers (no React, no DOM, no Next imports) the
 * AS.7.5 `/email-verify` page composes. Mirrors the AS.7.3
 * `password-reset-helpers.ts` shape one-to-one so the two
 * token-based flows share the same vocabulary:
 *
 *   1. **Verify-token classifier** — the magic link the user clicks
 *      lands here as `?token=...`. The helper turns the backend
 *      response on `POST /api/v1/auth/verify-email` into the
 *      canonical `EmailVerifyErrorOutcome` the page UI keys on
 *      (invalid_token / expired_token / already_verified / rate
 *      limited / bot challenge / service unavailable).
 *
 *   2. **Resend classifier** — the page also offers a "Re-send
 *      verification email" form that hits
 *      `POST /api/v1/auth/verify-email/resend` with the email the
 *      user typed (or pre-filled from `?email=`). Same precedence
 *      table shape as the AS.7.3 request-reset classifier.
 *
 *   3. **Submit-gate predicate** for the resend form — returns the
 *      first failing reason (busy / email_invalid) as a stable
 *      string, or `null` when every gate has cleared.
 *
 * Why no honeypot — unlike AS.7.2 / AS.7.3 the verify endpoint is
 * authenticated by the magic-link token bound to the URL (analogous
 * to AS.7.4 MFA challenge whose `mfa_token` is the authenticator).
 * The resend endpoint is bot-defended via the backend rate-limit
 * bucket per AS.6.x; adding a per-form honeypot prefix here would
 * require coupling the backend `_FORM_PREFIXES` table (currently 4
 * entries: login / signup / password-reset / contact) which is out
 * of scope for the AS.7.5 row. The page surfaces the canonical
 * `service_unavailable` copy if the resend endpoint isn't live yet
 * — same fail-closed pattern as AS.7.2 / AS.7.3.
 *
 * Module-global state audit (per implement_phase_step.md SOP §1):
 *   - All exports are `as const` frozen object literals or pure
 *     functions. Zero module-level mutable container.
 *   - The error classifiers read no module state; they dispatch on
 *     the input args only.
 *   - Cross-worker / cross-tab derivation is trivially identical
 *     (Answer #1 of the SOP §1 audit) — pure helpers.
 *
 * Read-after-write timing audit: N/A — pure helpers, no async DB
 * calls, no parallelisation change vs. existing auth-context.
 */

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Form path constants
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Backend canonical email-verify form path (token submission stage).
 *  Pinned by the test below; the AS.7.5 page composes against this
 *  exact string so a future backend route move surfaces as CI red
 *  rather than a silent 404. */
export const FORM_PATH_VERIFY_EMAIL: string = "/api/v1/auth/verify-email"

/** Backend canonical resend form path. */
export const FORM_PATH_RESEND_VERIFY_EMAIL: string =
  "/api/v1/auth/verify-email/resend"

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Verify-token stage — error vocabulary
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Canonical error vocabulary the verify-token page branches on.
 *  Frozen `as const` so a new branch needs to update this table +
 *  the test in lockstep. */
export const EMAIL_VERIFY_ERROR_KIND = {
  invalidToken: "invalid_token",
  expiredToken: "expired_token",
  alreadyVerified: "already_verified",
  rateLimited: "rate_limited",
  botChallenge: "bot_challenge_failed",
  serviceUnavailable: "service_unavailable",
} as const

export type EmailVerifyErrorKind =
  (typeof EMAIL_VERIFY_ERROR_KIND)[keyof typeof EMAIL_VERIFY_ERROR_KIND]

export interface EmailVerifyErrorOutcome {
  readonly kind: EmailVerifyErrorKind
  readonly message: string
  readonly retryAfterSeconds: number | null
}

/** Canonical UI copy. Pinned by the test; do not edit without
 *  updating the test. */
export const EMAIL_VERIFY_ERROR_COPY: Readonly<
  Record<EmailVerifyErrorKind, string>
> = Object.freeze({
  invalid_token:
    "This verification link is no longer valid. Request a fresh link below to continue.",
  expired_token:
    "This verification link has expired. Request a fresh link below — we'll send a new one to your email.",
  already_verified:
    "This email is already verified. You can sign in now.",
  rate_limited:
    "Too many attempts. Please wait a few minutes and retry.",
  bot_challenge_failed:
    "Verification failed. Please refresh the page and try again.",
  service_unavailable:
    "Email verification is temporarily unavailable. Please try again in a moment.",
})

interface EmailVerifyErrorInput {
  readonly status: number | null
  readonly message?: string | null
  readonly retryAfter?: string | null
  readonly errorCode?: string | null
}

/** Parse a `Retry-After` HTTP header value into integer seconds.
 *  Both delta-seconds and HTTP-date forms are supported (RFC 9110
 *  §10.2.3). Re-implemented locally to keep the helper free of
 *  cross-module dependency; behaviour is byte-equal to AS.7.1's
 *  `parseRetryAfter` and AS.7.3's `parsePasswordResetRetryAfter`. */
export function parseEmailVerifyRetryAfter(
  header: string | null | undefined,
): number | null {
  if (!header) return null
  const trimmed = header.trim()
  if (!trimmed) return null
  if (/^-?\d+$/.test(trimmed)) {
    const asInt = Number(trimmed)
    if (Number.isFinite(asInt) && asInt >= 0) return asInt
    return null
  }
  const asDate = Date.parse(trimmed)
  if (Number.isFinite(asDate)) {
    const diff = Math.ceil((asDate - Date.now()) / 1000)
    return diff > 0 ? diff : 0
  }
  return null
}

/** Map any backend response from `POST /auth/verify-email`
 *  into the canonical outcome the verify-token page UI keys on.
 *
 *  Precedence (most specific → least specific):
 *    1. 410 → expired_token (RFC convention for "Gone")
 *    2. 400 + errorCode = "expired_token" → expired_token
 *    3. 400 + errorCode = "invalid_token" → invalid_token
 *    4. 409 + errorCode = "already_verified" → already_verified
 *    5. 401 / 404 → invalid_token (backend may surface a 401 on a
 *       tampered signature or a 404 on an unknown token id)
 *    6. 429 + errorCode = "bot_challenge_failed" → bot reject
 *    7. 429 → rate limited
 *    8. ≥ 500 or status === null → service unavailable
 *    9. anything else → service_unavailable (defensive default)
 */
export function classifyEmailVerifyError(
  input: EmailVerifyErrorInput,
): EmailVerifyErrorOutcome {
  const status = input.status
  const retryAfterSeconds = parseEmailVerifyRetryAfter(input.retryAfter)

  if (status === 410) {
    return Object.freeze({
      kind: EMAIL_VERIFY_ERROR_KIND.expiredToken,
      message: EMAIL_VERIFY_ERROR_COPY.expired_token,
      retryAfterSeconds: null,
    })
  }
  if (status === 400 && input.errorCode === "expired_token") {
    return Object.freeze({
      kind: EMAIL_VERIFY_ERROR_KIND.expiredToken,
      message: EMAIL_VERIFY_ERROR_COPY.expired_token,
      retryAfterSeconds: null,
    })
  }
  if (status === 400 && input.errorCode === "invalid_token") {
    return Object.freeze({
      kind: EMAIL_VERIFY_ERROR_KIND.invalidToken,
      message: EMAIL_VERIFY_ERROR_COPY.invalid_token,
      retryAfterSeconds: null,
    })
  }
  if (status === 409 && input.errorCode === "already_verified") {
    return Object.freeze({
      kind: EMAIL_VERIFY_ERROR_KIND.alreadyVerified,
      message: EMAIL_VERIFY_ERROR_COPY.already_verified,
      retryAfterSeconds: null,
    })
  }
  if (status === 401 || status === 404) {
    return Object.freeze({
      kind: EMAIL_VERIFY_ERROR_KIND.invalidToken,
      message: EMAIL_VERIFY_ERROR_COPY.invalid_token,
      retryAfterSeconds: null,
    })
  }
  if (status === 429 && input.errorCode === "bot_challenge_failed") {
    return Object.freeze({
      kind: EMAIL_VERIFY_ERROR_KIND.botChallenge,
      message: EMAIL_VERIFY_ERROR_COPY.bot_challenge_failed,
      retryAfterSeconds,
    })
  }
  if (status === 429) {
    return Object.freeze({
      kind: EMAIL_VERIFY_ERROR_KIND.rateLimited,
      message: EMAIL_VERIFY_ERROR_COPY.rate_limited,
      retryAfterSeconds,
    })
  }
  if (status === null || status >= 500) {
    return Object.freeze({
      kind: EMAIL_VERIFY_ERROR_KIND.serviceUnavailable,
      message: EMAIL_VERIFY_ERROR_COPY.service_unavailable,
      retryAfterSeconds: null,
    })
  }
  return Object.freeze({
    kind: EMAIL_VERIFY_ERROR_KIND.serviceUnavailable,
    message: EMAIL_VERIFY_ERROR_COPY.service_unavailable,
    retryAfterSeconds: null,
  })
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Resend stage — error vocabulary
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Canonical error vocabulary the resend form branches on. */
export const RESEND_VERIFY_EMAIL_ERROR_KIND = {
  invalidInput: "invalid_input",
  alreadyVerified: "already_verified",
  rateLimited: "rate_limited",
  botChallenge: "bot_challenge_failed",
  serviceUnavailable: "service_unavailable",
} as const

export type ResendVerifyEmailErrorKind =
  (typeof RESEND_VERIFY_EMAIL_ERROR_KIND)[keyof typeof RESEND_VERIFY_EMAIL_ERROR_KIND]

export interface ResendVerifyEmailErrorOutcome {
  readonly kind: ResendVerifyEmailErrorKind
  readonly message: string
  readonly retryAfterSeconds: number | null
}

/** Canonical UI copy. Pinned by the test; do not edit without
 *  updating the test. */
export const RESEND_VERIFY_EMAIL_ERROR_COPY: Readonly<
  Record<ResendVerifyEmailErrorKind, string>
> = Object.freeze({
  invalid_input:
    "Please double-check the email address and try again.",
  already_verified:
    "This email is already verified. You can sign in now.",
  rate_limited:
    "Too many requests. Please wait a few minutes and retry.",
  bot_challenge_failed:
    "Verification failed. Please refresh the page and try again.",
  service_unavailable:
    "Email verification is temporarily unavailable. Please try again in a moment.",
})

interface ResendVerifyEmailErrorInput {
  readonly status: number | null
  readonly message?: string | null
  readonly retryAfter?: string | null
  readonly errorCode?: string | null
}

/** Map any backend response from `POST /auth/verify-email/resend`
 *  into the canonical outcome the resend form UI keys on.
 *
 *  Precedence (most specific → least specific):
 *    1. 422 → invalid_input (Pydantic email rejection)
 *    2. 409 + errorCode = "already_verified" → already_verified
 *       (the backend has already verified the requester; the page
 *       surfaces this as "you can sign in now" rather than the
 *       generic terminal "check your inbox" — friendlier UX since
 *       this branch is *not* enumeration-resistant: the user already
 *       proved control by clicking a previous magic link)
 *    3. 429 + errorCode = "bot_challenge_failed" → bot reject
 *    4. 429 → rate limited
 *    5. ≥ 500 or status === null → service unavailable
 *    6. anything else → service_unavailable (defensive default —
 *       enumeration-resist: never expose unknown 4xx copy)
 *
 *  Note that the success / 2xx path always lands at the terminal
 *  "we sent another link" copy regardless of whether the email
 *  matched a known unverified user — the page handles that case
 *  directly by branching on the response body, not on this
 *  classifier.
 */
export function classifyResendVerifyEmailError(
  input: ResendVerifyEmailErrorInput,
): ResendVerifyEmailErrorOutcome {
  const status = input.status
  const retryAfterSeconds = parseEmailVerifyRetryAfter(input.retryAfter)

  if (status === 422) {
    return Object.freeze({
      kind: RESEND_VERIFY_EMAIL_ERROR_KIND.invalidInput,
      message: RESEND_VERIFY_EMAIL_ERROR_COPY.invalid_input,
      retryAfterSeconds: null,
    })
  }
  if (status === 409 && input.errorCode === "already_verified") {
    return Object.freeze({
      kind: RESEND_VERIFY_EMAIL_ERROR_KIND.alreadyVerified,
      message: RESEND_VERIFY_EMAIL_ERROR_COPY.already_verified,
      retryAfterSeconds: null,
    })
  }
  if (status === 429 && input.errorCode === "bot_challenge_failed") {
    return Object.freeze({
      kind: RESEND_VERIFY_EMAIL_ERROR_KIND.botChallenge,
      message: RESEND_VERIFY_EMAIL_ERROR_COPY.bot_challenge_failed,
      retryAfterSeconds,
    })
  }
  if (status === 429) {
    return Object.freeze({
      kind: RESEND_VERIFY_EMAIL_ERROR_KIND.rateLimited,
      message: RESEND_VERIFY_EMAIL_ERROR_COPY.rate_limited,
      retryAfterSeconds,
    })
  }
  if (status === null || status >= 500) {
    return Object.freeze({
      kind: RESEND_VERIFY_EMAIL_ERROR_KIND.serviceUnavailable,
      message: RESEND_VERIFY_EMAIL_ERROR_COPY.service_unavailable,
      retryAfterSeconds: null,
    })
  }
  return Object.freeze({
    kind: RESEND_VERIFY_EMAIL_ERROR_KIND.serviceUnavailable,
    message: RESEND_VERIFY_EMAIL_ERROR_COPY.service_unavailable,
    retryAfterSeconds: null,
  })
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Resend stage — submit-gate predicate
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export interface ResendVerifyEmailGateInput {
  readonly email: string
  readonly busy: boolean
}

/** Lightweight RFC 5322-shaped sanity check copied from AS.7.2 to
 *  avoid pulling in the signup module. The backend Pydantic email
 *  validator is the authoritative gate; this helper just catches
 *  the obvious "haven't typed an `@` yet" / "no domain" cases so the
 *  submit button stays disabled until the input shape is plausible. */
function _looksLikeEmail(s: string): boolean {
  if (!s || s.length > 254) return false
  const at = s.indexOf("@")
  if (at <= 0 || at !== s.lastIndexOf("@")) return false
  const local = s.slice(0, at)
  const domain = s.slice(at + 1)
  if (!local || !domain) return false
  if (!domain.includes(".")) return false
  if (s.includes(" ")) return false
  return true
}

/** Canonical predicate for "the resend submit can fire". Returns
 *  the first failure reason as a stable string, or `null` when every
 *  gate has cleared. */
export function resendVerifyEmailSubmitBlockedReason(
  input: ResendVerifyEmailGateInput,
): string | null {
  if (input.busy) return "busy"
  if (!_looksLikeEmail(input.email)) return "email_invalid"
  return null
}

/** Drift guard: every reason string the resend gate may emit.
 *  Pinned by the test so adding a new reason without updating the
 *  test is a CI red. */
export const RESEND_VERIFY_EMAIL_BLOCKED_REASONS = [
  "busy",
  "email_invalid",
] as const

export type ResendVerifyEmailBlockedReason =
  (typeof RESEND_VERIFY_EMAIL_BLOCKED_REASONS)[number]
