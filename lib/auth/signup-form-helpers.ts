/**
 * AS.7.2 — Signup page form helpers.
 *
 * Pure browser-safe helpers (no React, no DOM, no Next imports) the
 * signup page composes. Mirrors the AS.7.1 `login-form-helpers.ts`
 * shape one-to-one so the two pages share the same vocabulary:
 *
 *   1. **Honeypot field-name resolver** pinned to the
 *      `FORM_PATH_SIGNUP = "/api/v1/auth/signup"` form path. Reuses
 *      every primitive from AS.7.1's resolver (Web Crypto SHA-256 +
 *      30-day rotation epoch + anonymous-tenant sentinel). The
 *      backend `validate_honeypot()` accepts the same signed seed
 *      regardless of which page rendered it; the only difference
 *      is the prefix (`sg_` instead of `lg_`) which is fully
 *      driven by the FORM_PREFIXES table.
 *
 *   2. **Unified signup error** copy — collapses every backend 4xx
 *      response into the canonical strings the AS.7.2 design
 *      pinned. Matches AS.7.1's pattern: never expose whether an
 *      email already exists vs. fails policy (the unified-error
 *      contract is enumeration-resistant).
 *
 *   3. **Save-acknowledgement state** — the "I have saved my
 *      password" checkbox the design requires before submit
 *      enables. Tracked as a small toggle helper so the page-level
 *      logic stays declarative.
 *
 * Module-global state audit (per implement_phase_step.md SOP §1):
 *   - All exports are `as const` frozen object literals or pure
 *     functions. Zero module-level mutable container.
 *   - The error-classifier reads no module state; it dispatches on
 *     the input args only.
 *   - Determinism: `signupHoneypotFieldName(epoch)` is a thin wrapper
 *     around `honeypotFieldName(FORM_PATH_SIGNUP, _anonymous, epoch)`
 *     so cross-worker / cross-tab derivation is trivially identical
 *     (Answer #1 of the SOP §1 audit).
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

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Form path constant
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Backend canonical signup form path. Must byte-match
 *  `backend/security/honeypot_form_verifier.py::FORM_PATH_SIGNUP`.
 *  `FORM_PREFIXES["/api/v1/auth/signup"] === "sg_"` (drift guard
 *  pinned in `signup-form-helpers.test.ts`). */
export const FORM_PATH_SIGNUP: string = "/api/v1/auth/signup"

/** Convenience wrapper resolving the honeypot field name the
 *  AS.7.2 signup form should render. Pins the form_path + tenant_id
 *  to the signup-anonymous tuple so callers don't have to pass
 *  every layer through. */
export async function signupHoneypotFieldName(
  nowMs?: number,
): Promise<string> {
  return honeypotFieldName(FORM_PATH_SIGNUP, ANONYMOUS_TENANT_ID, currentEpoch(nowMs))
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Unified signup-error normaliser
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Canonical AS.7.2 error copy — six kinds the signup page UI
 *  branches on. Frozen `as const` so a new branch needs to update
 *  this table + the test in lockstep. Note that
 *  `emailAlreadyTaken` is intentionally NOT in this list — the
 *  unified-error contract pins the response to a generic
 *  "registration failed" so the surface can't be used to enumerate
 *  existing accounts. The backend collapses the 409 into the same
 *  canonical message before it ever reaches the frontend. */
export const SIGNUP_ERROR_KIND = {
  invalidInput: "invalid_input",
  weakPassword: "weak_password",
  rateLimited: "rate_limited",
  botChallenge: "bot_challenge_failed",
  registrationFailed: "registration_failed",
  serviceUnavailable: "service_unavailable",
} as const

export type SignupErrorKind =
  (typeof SIGNUP_ERROR_KIND)[keyof typeof SIGNUP_ERROR_KIND]

export interface SignupErrorOutcome {
  readonly kind: SignupErrorKind
  readonly message: string
  /** Set on a 429 with Retry-After so the page can render a
   *  countdown next to the message. May be `null` if the header
   *  was absent or unparseable. */
  readonly retryAfterSeconds: number | null
}

/** Canonical UI copy. Pinned by `signup-form-helpers.test.ts`; do
 *  not edit without updating the test. */
export const SIGNUP_ERROR_COPY: Readonly<Record<SignupErrorKind, string>> =
  Object.freeze({
    invalid_input:
      "Please double-check the email address and try again.",
    weak_password:
      "This password does not meet the strength requirements. Try a longer or more random one.",
    rate_limited:
      "Too many signup attempts. Please wait a few minutes and retry.",
    bot_challenge_failed:
      "Verification failed. Please refresh the page and try again.",
    registration_failed:
      "Sign-up could not be completed. Please try again.",
    service_unavailable:
      "Sign-up is temporarily unavailable. Please try again in a moment.",
  })

interface SignupErrorInput {
  readonly status: number | null
  readonly message?: string | null
  readonly retryAfter?: string | null
  readonly errorCode?: string | null
}

/** Parse a `Retry-After` HTTP header value into integer seconds.
 *  Re-implemented locally so the helper has no cross-module
 *  dependency; behaviour is byte-equal to AS.7.1's
 *  `parseRetryAfter`. Returns `null` for empty / malformed inputs. */
export function parseSignupRetryAfter(
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

/** Map any backend response into the canonical `SignupErrorOutcome`
 *  the UI keys on. Status precedence (most specific → least specific):
 *
 *    1. 422 → invalid input (Pydantic validation rejection — email
 *             format etc.)
 *    2. 400 with body code = "weak_password" → weak password
 *    3. 429 with body code = "bot_challenge_failed" → bot reject
 *    4. 429 → rate limited
 *    5. 409 → registration failed (collapsed enum-resist copy)
 *    6. ≥ 500 or status === null → service unavailable
 *    7. anything else → registration failed (defensive default —
 *       same enumeration-resist contract, never expose unknown 4xx
 *       copy to the user)
 */
export function classifySignupError(
  input: SignupErrorInput,
): SignupErrorOutcome {
  const status = input.status
  const retryAfterSeconds = parseSignupRetryAfter(input.retryAfter)

  if (status === 422) {
    return Object.freeze({
      kind: SIGNUP_ERROR_KIND.invalidInput,
      message: SIGNUP_ERROR_COPY.invalid_input,
      retryAfterSeconds: null,
    })
  }
  if (status === 400 && input.errorCode === "weak_password") {
    return Object.freeze({
      kind: SIGNUP_ERROR_KIND.weakPassword,
      message: SIGNUP_ERROR_COPY.weak_password,
      retryAfterSeconds: null,
    })
  }
  if (status === 429 && input.errorCode === "bot_challenge_failed") {
    return Object.freeze({
      kind: SIGNUP_ERROR_KIND.botChallenge,
      message: SIGNUP_ERROR_COPY.bot_challenge_failed,
      retryAfterSeconds,
    })
  }
  if (status === 429) {
    return Object.freeze({
      kind: SIGNUP_ERROR_KIND.rateLimited,
      message: SIGNUP_ERROR_COPY.rate_limited,
      retryAfterSeconds,
    })
  }
  if (status === 409) {
    return Object.freeze({
      kind: SIGNUP_ERROR_KIND.registrationFailed,
      message: SIGNUP_ERROR_COPY.registration_failed,
      retryAfterSeconds: null,
    })
  }
  if (status === null || status >= 500) {
    return Object.freeze({
      kind: SIGNUP_ERROR_KIND.serviceUnavailable,
      message: SIGNUP_ERROR_COPY.service_unavailable,
      retryAfterSeconds: null,
    })
  }
  return Object.freeze({
    kind: SIGNUP_ERROR_KIND.registrationFailed,
    message: SIGNUP_ERROR_COPY.registration_failed,
    retryAfterSeconds: null,
  })
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Email format quick-check
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** A loose RFC-5322-ish email regex sufficient for client-side
 *  sanity check before submit. The backend Pydantic `EmailStr`
 *  remains authoritative — this is purely a UX gate so the submit
 *  button doesn't enable on an obvious typo. Pinned by tests. */
export const SIGNUP_EMAIL_REGEX = /^[^\s@]+@[^\s@]+\.[^\s@]+$/

export function looksLikeEmail(s: string): boolean {
  if (!s) return false
  if (s.length > 254) return false  // RFC 5321 §4.5.3.1.1 envelope limit
  return SIGNUP_EMAIL_REGEX.test(s)
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Submit-gate composition helper
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export interface SignupGateInput {
  readonly email: string
  readonly password: string
  readonly passwordPasses: boolean
  readonly hasSaved: boolean
  readonly hasAcceptedTos: boolean
  readonly busy: boolean
  readonly honeypotResolved: boolean
}

/** Canonical predicate for "the submit button can fire". Returns
 *  the first failure reason as a stable string, or `null` when
 *  every gate has cleared. Centralising this keeps the page-level
 *  JSX free of nested boolean cascades. */
export function signupSubmitBlockedReason(
  input: SignupGateInput,
): string | null {
  if (input.busy) return "busy"
  if (!input.honeypotResolved) return "honeypot_pending"
  if (!looksLikeEmail(input.email)) return "email_invalid"
  if (!input.password) return "password_empty"
  if (!input.passwordPasses) return "password_weak"
  if (!input.hasSaved) return "password_not_saved"
  if (!input.hasAcceptedTos) return "tos_not_accepted"
  return null
}

/** Drift guard: every reason string the gate may emit. Pinned by
 *  `signup-form-helpers.test.ts` so adding a new reason without
 *  updating the test is a CI red. */
export const SIGNUP_BLOCKED_REASONS = [
  "busy",
  "honeypot_pending",
  "email_invalid",
  "password_empty",
  "password_weak",
  "password_not_saved",
  "tos_not_accepted",
] as const

export type SignupBlockedReason = (typeof SIGNUP_BLOCKED_REASONS)[number]

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Drift-guard re-export (so the test file pins one place)
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Re-export so the test can assert the prefix wiring in lockstep
 *  with `lib/auth/login-form-helpers.ts::FORM_PREFIXES`. */
export const SIGNUP_FORM_PREFIX: string =
  FORM_PREFIXES[FORM_PATH_SIGNUP] ?? ""
