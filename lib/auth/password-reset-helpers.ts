/**
 * AS.7.3 — Password reset form helpers.
 *
 * Pure browser-safe helpers (no React, no DOM, no Next imports) the
 * AS.7.3 forgot-password + reset-password pages compose. Mirrors the
 * AS.7.1 `login-form-helpers.ts` + AS.7.2 `signup-form-helpers.ts`
 * shape one-to-one so the three flows share the same vocabulary:
 *
 *   1. **Honeypot field-name resolver** pinned to the
 *      `FORM_PATH_PASSWORD_RESET = "/api/v1/auth/password-reset"`
 *      form path. Reuses every primitive from AS.7.1's resolver
 *      (Web Crypto SHA-256 + 30-day rotation epoch + anonymous
 *      tenant sentinel — the user is not yet identified when they
 *      request a reset). The backend `validate_honeypot()` accepts
 *      the same signed seed regardless of which page rendered it;
 *      the only difference is the prefix (`pr_` instead of `lg_` /
 *      `sg_`) which is fully driven by the FORM_PREFIXES table.
 *
 *   2. **Unified error copy** — collapses every backend 4xx response
 *      into the canonical strings the AS.7.3 design pinned. Matches
 *      AS.7.1 / AS.7.2's pattern: never expose whether an email is
 *      registered (the request-link page always shows the same
 *      "if your account exists, we've sent a link" terminal copy
 *      regardless of whether the email matched a known user — the
 *      enumeration-resistance contract per AS.0.7 §3.4).
 *
 *   3. **Two-form vocabulary**: `RequestResetGate` for the email-
 *      submission page and `ResetPasswordGate` for the new-password
 *      page (the user lands here from the magic link). Each has its
 *      own classifier + submit-gate predicate so the page-level JSX
 *      stays declarative.
 *
 * Module-global state audit (per implement_phase_step.md SOP §1):
 *   - All exports are `as const` frozen object literals or pure
 *     functions. Zero module-level mutable container.
 *   - The error classifiers read no module state; they dispatch on
 *     the input args only.
 *   - Determinism: `passwordResetHoneypotFieldName(epoch)` is a thin
 *     wrapper around `honeypotFieldName(FORM_PATH_PASSWORD_RESET,
 *     _anonymous, epoch)` so cross-worker / cross-tab derivation is
 *     trivially identical (Answer #1 of the SOP §1 audit).
 *
 * Read-after-write timing audit: N/A — pure helpers, no async DB
 * calls, no parallelisation change vs. existing auth-context.
 */

import {
  ANONYMOUS_TENANT_ID,
  FORM_PREFIXES,
  currentEpoch,
  honeypotFieldName,
} from "@/lib/auth/login-form-helpers"
import { looksLikeEmail } from "@/lib/auth/signup-form-helpers"

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Form path constant
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Backend canonical password-reset form path. Must byte-match
 *  `backend/security/honeypot_form_verifier.py
 *  ::FORM_PATH_PASSWORD_RESET`. `FORM_PREFIXES["/api/v1/auth/password-
 *  reset"] === "pr_"` (drift guard pinned in
 *  `password-reset-helpers.test.ts`). */
export const FORM_PATH_PASSWORD_RESET: string = "/api/v1/auth/password-reset"

/** Re-export so the test pins one place against the FORM_PREFIXES
 *  table (drift-guard symmetry with AS.7.2). */
export const PASSWORD_RESET_FORM_PREFIX: string =
  FORM_PREFIXES[FORM_PATH_PASSWORD_RESET] ?? ""

/** Convenience wrapper resolving the honeypot field name the
 *  AS.7.3 forgot-password / reset-password forms should render.
 *  Pins the form_path + tenant_id to the password-reset-anonymous
 *  tuple so callers don't have to thread every layer through. */
export async function passwordResetHoneypotFieldName(
  nowMs?: number,
): Promise<string> {
  return honeypotFieldName(
    FORM_PATH_PASSWORD_RESET,
    ANONYMOUS_TENANT_ID,
    currentEpoch(nowMs),
  )
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Reset-link request — email submission stage
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Canonical error vocabulary the email-submission page branches
 *  on. Frozen `as const` so a new branch needs to update this table
 *  + the test in lockstep. Matches AS.7.2's enum-resist contract:
 *  the page never exposes whether the email is registered.
 *
 *  Note: `email_oauth_only` is a deliberate kind. When the operator
 *  configured an account that has only OAuth-provider sign-ins (no
 *  password set), backend may surface a 409 with the explicit
 *  `oauth_only` code so the page can show a clear "this account
 *  signs in with Google / GitHub / etc.; password reset doesn't
 *  apply" message. Per the AS.7.3 TODO row this is required UX
 *  ("OAuth-only 帳號明確訊息"). To keep the surface enumeration-
 *  resistant the backend ONLY emits this code when the user has
 *  already proven their identity (e.g. they completed the magic
 *  link first); the public email-submission endpoint always returns
 *  the generic `link_sent` terminal copy. */
export const REQUEST_RESET_ERROR_KIND = {
  invalidInput: "invalid_input",
  rateLimited: "rate_limited",
  botChallenge: "bot_challenge_failed",
  emailOauthOnly: "email_oauth_only",
  serviceUnavailable: "service_unavailable",
} as const

export type RequestResetErrorKind =
  (typeof REQUEST_RESET_ERROR_KIND)[keyof typeof REQUEST_RESET_ERROR_KIND]

export interface RequestResetErrorOutcome {
  readonly kind: RequestResetErrorKind
  readonly message: string
  readonly retryAfterSeconds: number | null
}

/** Canonical UI copy. Pinned by the test; do not edit without
 *  updating the test. */
export const REQUEST_RESET_ERROR_COPY: Readonly<
  Record<RequestResetErrorKind, string>
> = Object.freeze({
  invalid_input:
    "Please double-check the email address and try again.",
  rate_limited:
    "Too many requests. Please wait a few minutes and retry.",
  bot_challenge_failed:
    "Verification failed. Please refresh the page and try again.",
  email_oauth_only:
    "This account signs in with a connected provider (Google, GitHub, etc.). Password reset does not apply — open the sign-in page and click your provider button.",
  service_unavailable:
    "Password reset is temporarily unavailable. Please try again in a moment.",
})

interface RequestResetErrorInput {
  readonly status: number | null
  readonly message?: string | null
  readonly retryAfter?: string | null
  readonly errorCode?: string | null
}

/** Parse a `Retry-After` HTTP header value into integer seconds.
 *  Re-implemented locally to keep the helper free of cross-module
 *  dependency; behaviour is byte-equal to AS.7.1's `parseRetryAfter`. */
export function parsePasswordResetRetryAfter(
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

/** Map any backend response from `POST /auth/password-reset/request`
 *  into the canonical outcome the request-page UI keys on.
 *
 *  Precedence (most specific → least specific):
 *    1. 422 → invalid_input (Pydantic email rejection)
 *    2. 409 + errorCode = "oauth_only" → email_oauth_only (the
 *       backend has already authenticated the requester; this is
 *       the only branch that surfaces the OAuth-only copy)
 *    3. 429 + errorCode = "bot_challenge_failed" → bot reject
 *    4. 429 → rate limited
 *    5. ≥ 500 or status === null → service unavailable
 *    6. anything else → service_unavailable (defensive default —
 *       enumeration-resist: never expose unknown 4xx copy)
 *
 *  Note that this classifier is ONLY consulted on a *failure* path.
 *  The success / 2xx path always lands at the terminal "link sent"
 *  copy regardless of whether the email matched a known user — the
 *  page handles that case directly by branching on the response
 *  body, not on this classifier.
 */
export function classifyRequestResetError(
  input: RequestResetErrorInput,
): RequestResetErrorOutcome {
  const status = input.status
  const retryAfterSeconds = parsePasswordResetRetryAfter(input.retryAfter)

  if (status === 422) {
    return Object.freeze({
      kind: REQUEST_RESET_ERROR_KIND.invalidInput,
      message: REQUEST_RESET_ERROR_COPY.invalid_input,
      retryAfterSeconds: null,
    })
  }
  if (status === 409 && input.errorCode === "oauth_only") {
    return Object.freeze({
      kind: REQUEST_RESET_ERROR_KIND.emailOauthOnly,
      message: REQUEST_RESET_ERROR_COPY.email_oauth_only,
      retryAfterSeconds: null,
    })
  }
  if (status === 429 && input.errorCode === "bot_challenge_failed") {
    return Object.freeze({
      kind: REQUEST_RESET_ERROR_KIND.botChallenge,
      message: REQUEST_RESET_ERROR_COPY.bot_challenge_failed,
      retryAfterSeconds,
    })
  }
  if (status === 429) {
    return Object.freeze({
      kind: REQUEST_RESET_ERROR_KIND.rateLimited,
      message: REQUEST_RESET_ERROR_COPY.rate_limited,
      retryAfterSeconds,
    })
  }
  if (status === null || status >= 500) {
    return Object.freeze({
      kind: REQUEST_RESET_ERROR_KIND.serviceUnavailable,
      message: REQUEST_RESET_ERROR_COPY.service_unavailable,
      retryAfterSeconds: null,
    })
  }
  return Object.freeze({
    kind: REQUEST_RESET_ERROR_KIND.serviceUnavailable,
    message: REQUEST_RESET_ERROR_COPY.service_unavailable,
    retryAfterSeconds: null,
  })
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Reset-link request — submit-gate predicate
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export interface RequestResetGateInput {
  readonly email: string
  readonly busy: boolean
  readonly honeypotResolved: boolean
}

/** Canonical predicate for "the request-reset submit can fire".
 *  Returns the first failure reason as a stable string, or `null`
 *  when every gate has cleared. */
export function requestResetSubmitBlockedReason(
  input: RequestResetGateInput,
): string | null {
  if (input.busy) return "busy"
  if (!input.honeypotResolved) return "honeypot_pending"
  if (!looksLikeEmail(input.email)) return "email_invalid"
  return null
}

/** Drift guard: every reason string the request-reset gate may
 *  emit. Pinned by the test so adding a new reason without updating
 *  the test is a CI red. */
export const REQUEST_RESET_BLOCKED_REASONS = [
  "busy",
  "honeypot_pending",
  "email_invalid",
] as const

export type RequestResetBlockedReason =
  (typeof REQUEST_RESET_BLOCKED_REASONS)[number]

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  New-password submission stage (after token landing)
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Canonical error vocabulary the new-password page branches on.
 *  Includes the token-specific failure modes (`invalid_token`,
 *  `expired_token`) that the request-stage classifier doesn't
 *  surface — those branches drive a "link no longer valid" copy
 *  with a "request a new link" CTA. */
export const RESET_PASSWORD_ERROR_KIND = {
  invalidToken: "invalid_token",
  expiredToken: "expired_token",
  weakPassword: "weak_password",
  rateLimited: "rate_limited",
  botChallenge: "bot_challenge_failed",
  serviceUnavailable: "service_unavailable",
} as const

export type ResetPasswordErrorKind =
  (typeof RESET_PASSWORD_ERROR_KIND)[keyof typeof RESET_PASSWORD_ERROR_KIND]

export interface ResetPasswordErrorOutcome {
  readonly kind: ResetPasswordErrorKind
  readonly message: string
  readonly retryAfterSeconds: number | null
}

/** Canonical UI copy. Pinned by the test; do not edit without
 *  updating the test. */
export const RESET_PASSWORD_ERROR_COPY: Readonly<
  Record<ResetPasswordErrorKind, string>
> = Object.freeze({
  invalid_token:
    "This reset link is no longer valid. Please request a fresh link from the sign-in page.",
  expired_token:
    "This reset link has expired. Please request a fresh link from the sign-in page.",
  weak_password:
    "This password does not meet the strength requirements. Try a longer or more random one.",
  rate_limited:
    "Too many attempts. Please wait a few minutes and retry.",
  bot_challenge_failed:
    "Verification failed. Please refresh the page and try again.",
  service_unavailable:
    "Password reset is temporarily unavailable. Please try again in a moment.",
})

interface ResetPasswordErrorInput {
  readonly status: number | null
  readonly message?: string | null
  readonly retryAfter?: string | null
  readonly errorCode?: string | null
}

/** Map any backend response from `POST /auth/password-reset/confirm`
 *  into the canonical outcome the new-password page UI keys on.
 *
 *  Precedence (most specific → least specific):
 *    1. 410 → expired_token (the spec convention for "Gone")
 *    2. 400 + errorCode = "expired_token" → expired_token
 *    3. 400 + errorCode = "invalid_token" → invalid_token
 *    4. 400 + errorCode = "weak_password" → weak_password
 *    5. 401 / 404 → invalid_token (the backend may surface a 401
 *       on a tampered signature or a 404 on an unknown token id)
 *    6. 429 + errorCode = "bot_challenge_failed" → bot reject
 *    7. 429 → rate limited
 *    8. ≥ 500 or status === null → service unavailable
 *    9. anything else → service_unavailable
 */
export function classifyResetPasswordError(
  input: ResetPasswordErrorInput,
): ResetPasswordErrorOutcome {
  const status = input.status
  const retryAfterSeconds = parsePasswordResetRetryAfter(input.retryAfter)

  if (status === 410) {
    return Object.freeze({
      kind: RESET_PASSWORD_ERROR_KIND.expiredToken,
      message: RESET_PASSWORD_ERROR_COPY.expired_token,
      retryAfterSeconds: null,
    })
  }
  if (status === 400 && input.errorCode === "expired_token") {
    return Object.freeze({
      kind: RESET_PASSWORD_ERROR_KIND.expiredToken,
      message: RESET_PASSWORD_ERROR_COPY.expired_token,
      retryAfterSeconds: null,
    })
  }
  if (status === 400 && input.errorCode === "invalid_token") {
    return Object.freeze({
      kind: RESET_PASSWORD_ERROR_KIND.invalidToken,
      message: RESET_PASSWORD_ERROR_COPY.invalid_token,
      retryAfterSeconds: null,
    })
  }
  if (status === 400 && input.errorCode === "weak_password") {
    return Object.freeze({
      kind: RESET_PASSWORD_ERROR_KIND.weakPassword,
      message: RESET_PASSWORD_ERROR_COPY.weak_password,
      retryAfterSeconds: null,
    })
  }
  if (status === 401 || status === 404) {
    return Object.freeze({
      kind: RESET_PASSWORD_ERROR_KIND.invalidToken,
      message: RESET_PASSWORD_ERROR_COPY.invalid_token,
      retryAfterSeconds: null,
    })
  }
  if (status === 429 && input.errorCode === "bot_challenge_failed") {
    return Object.freeze({
      kind: RESET_PASSWORD_ERROR_KIND.botChallenge,
      message: RESET_PASSWORD_ERROR_COPY.bot_challenge_failed,
      retryAfterSeconds,
    })
  }
  if (status === 429) {
    return Object.freeze({
      kind: RESET_PASSWORD_ERROR_KIND.rateLimited,
      message: RESET_PASSWORD_ERROR_COPY.rate_limited,
      retryAfterSeconds,
    })
  }
  if (status === null || status >= 500) {
    return Object.freeze({
      kind: RESET_PASSWORD_ERROR_KIND.serviceUnavailable,
      message: RESET_PASSWORD_ERROR_COPY.service_unavailable,
      retryAfterSeconds: null,
    })
  }
  return Object.freeze({
    kind: RESET_PASSWORD_ERROR_KIND.serviceUnavailable,
    message: RESET_PASSWORD_ERROR_COPY.service_unavailable,
    retryAfterSeconds: null,
  })
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  New-password submission — submit-gate predicate
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export interface ResetPasswordGateInput {
  readonly token: string
  readonly password: string
  readonly passwordPasses: boolean
  readonly hasSaved: boolean
  readonly busy: boolean
  readonly honeypotResolved: boolean
}

/** Canonical predicate for "the new-password submit can fire".
 *  Returns the first failure reason as a stable string, or `null`
 *  when every gate has cleared. The token-missing branch is checked
 *  first so a tampered URL never falls through to a misleading
 *  "password too weak" message. */
export function resetPasswordSubmitBlockedReason(
  input: ResetPasswordGateInput,
): string | null {
  if (input.busy) return "busy"
  if (!input.token) return "token_missing"
  if (!input.honeypotResolved) return "honeypot_pending"
  if (!input.password) return "password_empty"
  if (!input.passwordPasses) return "password_weak"
  if (!input.hasSaved) return "password_not_saved"
  return null
}

/** Drift guard: every reason string the new-password gate may emit.
 *  Pinned by the test. */
export const RESET_PASSWORD_BLOCKED_REASONS = [
  "busy",
  "token_missing",
  "honeypot_pending",
  "password_empty",
  "password_weak",
  "password_not_saved",
] as const

export type ResetPasswordBlockedReason =
  (typeof RESET_PASSWORD_BLOCKED_REASONS)[number]
