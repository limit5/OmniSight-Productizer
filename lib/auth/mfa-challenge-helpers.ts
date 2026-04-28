/**
 * AS.7.4 — MFA challenge form helpers.
 *
 * Pure browser-safe helpers (no React, no DOM, no Next imports) the
 * AS.7.4 dedicated `/mfa-challenge` page composes. Mirrors the
 * AS.7.1 `login-form-helpers.ts` + AS.7.2 `signup-form-helpers.ts` +
 * AS.7.3 `password-reset-helpers.ts` shape one-to-one so the four
 * flows share the same vocabulary:
 *
 *   1. **Method vocabulary** — three UI-method kinds (`totp`,
 *      `webauthn`, `backup_code`) with canonical label / hint copy
 *      and a `selectableMethods()` helper that converts the backend
 *      `LoginResponse.mfa_methods` array into the ordered tuple the
 *      tabs render. Backup-code is always offered alongside TOTP per
 *      the backend AS.6.5 contract: any TOTP-enrolled user has
 *      backup codes generated at enrolment.
 *
 *   2. **Code-format predicates** — `looksLikeTotpCode` (6 digits)
 *      and `looksLikeBackupCode` (`xxxx-xxxx`, byte-equal to the
 *      backend `routers/mfa.py::mfa_challenge` is-backup heuristic
 *      `"-" in code and len(code) == 9`).
 *
 *   3. **Unified error copy** — collapses every backend 4xx/5xx
 *      response into the six canonical strings the AS.7.4 design
 *      pinned. Matches the AS.7.1/AS.7.2/AS.7.3 enum-resist contract:
 *      never expose whether the *code* was wrong vs the *challenge*
 *      was already consumed (both surface as the same `invalid_code`
 *      copy unless the backend explicitly emits an
 *      `mfa_challenge_expired` error code).
 *
 *   4. **Submit-gate predicate** — the page renders the submit
 *      button enabled/disabled based on `mfaChallengeSubmitBlockedReason`
 *      so a malformed code can never round-trip to the backend.
 *
 *   5. **Pulse-bump key** — monotonically-increasing key the page
 *      bumps on every fresh digit so the per-cell pulse animation
 *      replays via React `key={pulseKey}`.
 *
 * Module-global state audit (per implement_phase_step.md SOP §1):
 *   - All exports are `as const` frozen object literals or pure
 *     functions. Zero module-level mutable container.
 *   - The error classifier reads no module state; it dispatches on
 *     the input args only.
 *   - Determinism: every helper is deterministic-by-construction
 *     across workers / tabs (Answer #1 of the SOP §1 audit).
 *
 * Read-after-write timing audit: N/A — pure helpers, no async DB
 * calls, no parallelisation change vs. existing auth-context.
 */

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Method vocabulary
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Canonical UI-method vocabulary. The backend's `mfa_methods`
 *  array uses the bare strings `"totp"` and `"webauthn"`; the UI
 *  surfaces a third explicit `backup_code` kind so the tab picker
 *  can break out the backup-code path from TOTP. */
export const MFA_METHOD_KIND = {
  totp: "totp",
  webauthn: "webauthn",
  backupCode: "backup_code",
} as const

export type MfaMethodKind = (typeof MFA_METHOD_KIND)[keyof typeof MFA_METHOD_KIND]

/** Human-readable label + hint copy keyed by method kind. Pinned by
 *  the test; do not edit without updating the test. */
export const MFA_METHOD_COPY: Readonly<
  Record<
    MfaMethodKind,
    Readonly<{ label: string; hint: string; placeholder: string }>
  >
> = Object.freeze({
  totp: Object.freeze({
    label: "Authenticator",
    hint: "Open your authenticator app and enter the 6-digit code.",
    placeholder: "000000",
  }),
  webauthn: Object.freeze({
    label: "Security key",
    hint: "Use your registered security key or platform biometric.",
    placeholder: "",
  }),
  backup_code: Object.freeze({
    label: "Backup code",
    hint: "Enter one of your one-time backup codes (xxxx-xxxx).",
    placeholder: "xxxx-xxxx",
  }),
})

/** Ordered tuple of every method kind the UI knows about. Exposed
 *  for the test so a new method addition is a CI red unless the
 *  test is updated in lockstep. */
export const MFA_METHOD_KINDS_ORDERED: readonly MfaMethodKind[] = Object.freeze([
  MFA_METHOD_KIND.totp,
  MFA_METHOD_KIND.webauthn,
  MFA_METHOD_KIND.backupCode,
])

/** Resolve the ordered list of UI-method tabs the page should
 *  render given the backend's `mfa_methods` array. Rules:
 *    - If `totp` ∈ backendMethods → include `totp` AND `backup_code`
 *      (backup code always available alongside TOTP per the backend
 *      AS.6.5 contract — TOTP enrolment always generates 10 backup
 *      codes; the user can fall through to one if they lost their
 *      authenticator).
 *    - If `webauthn` ∈ backendMethods → include `webauthn`.
 *    - Order: totp → webauthn → backup_code.
 *    - Empty array (defensive — backend should never return zero
 *      methods when `mfa_required=true`) → returns the full 3-tuple
 *      so the user has *something* to try.
 */
export function selectableMethods(
  backendMethods: readonly string[],
): readonly MfaMethodKind[] {
  const has = new Set(backendMethods.map((s) => s.toLowerCase()))
  if (has.size === 0) {
    return MFA_METHOD_KINDS_ORDERED
  }
  const out: MfaMethodKind[] = []
  if (has.has("totp")) out.push(MFA_METHOD_KIND.totp)
  if (has.has("webauthn")) out.push(MFA_METHOD_KIND.webauthn)
  if (has.has("totp")) out.push(MFA_METHOD_KIND.backupCode)
  if (out.length === 0) {
    // Unknown method label — defensive: render every tab so the
    // user can still attempt one. Backend is authoritative for
    // success/failure.
    return MFA_METHOD_KINDS_ORDERED
  }
  return Object.freeze(out)
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Code format predicates
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** TOTP code length pinned to 6 digits per RFC 6238 + AS.6.5. */
export const TOTP_CODE_LENGTH: number = 6

/** Backup-code length pinned to 9 chars (xxxx-xxxx) byte-equal to
 *  the backend `routers/mfa.py::mfa_challenge` is-backup heuristic. */
export const BACKUP_CODE_LENGTH: number = 9

/** Test whether a string is a well-formed 6-digit TOTP code. Empty
 *  / partial inputs return false so the submit gate stays closed
 *  until the user has typed all 6 digits. */
export function looksLikeTotpCode(value: string): boolean {
  if (typeof value !== "string") return false
  if (value.length !== TOTP_CODE_LENGTH) return false
  for (let i = 0; i < value.length; i += 1) {
    const c = value.charCodeAt(i)
    if (c < 48 || c > 57) return false
  }
  return true
}

/** Test whether a string is a well-formed `xxxx-xxxx` backup code.
 *  Matches the backend's `"-" in code and len(code) == 9` check
 *  exactly so the frontend predicate doesn't get out of sync. */
export function looksLikeBackupCode(value: string): boolean {
  if (typeof value !== "string") return false
  if (value.length !== BACKUP_CODE_LENGTH) return false
  if (value[4] !== "-") return false
  for (let i = 0; i < value.length; i += 1) {
    if (i === 4) continue
    const c = value.charCodeAt(i)
    const isDigit = c >= 48 && c <= 57
    const isLowerAZ = c >= 97 && c <= 122
    const isUpperAZ = c >= 65 && c <= 90
    if (!isDigit && !isLowerAZ && !isUpperAZ) return false
  }
  return true
}

/** Strip whitespace and normalise the typed input for the active
 *  method. For TOTP we strip non-digits; for backup code we
 *  preserve case + the single `-` separator; for WebAuthn the input
 *  field is unused (the value is ignored). */
export function normaliseMfaInput(
  kind: MfaMethodKind,
  raw: string,
): string {
  if (kind === MFA_METHOD_KIND.totp) {
    let out = ""
    for (let i = 0; i < raw.length && out.length < TOTP_CODE_LENGTH; i += 1) {
      const c = raw.charCodeAt(i)
      if (c >= 48 && c <= 57) out += raw[i]
    }
    return out
  }
  if (kind === MFA_METHOD_KIND.backupCode) {
    return raw.trim().slice(0, BACKUP_CODE_LENGTH).toLowerCase()
  }
  return ""
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Unified error vocabulary
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Canonical AS.7.4 error vocabulary. Frozen `as const` so a new
 *  branch needs to update this table + the test in lockstep. */
export const MFA_CHALLENGE_ERROR_KIND = {
  invalidCode: "invalid_code",
  expiredChallenge: "expired_challenge",
  rateLimited: "rate_limited",
  botChallenge: "bot_challenge_failed",
  webauthnFailed: "webauthn_failed",
  serviceUnavailable: "service_unavailable",
} as const

export type MfaChallengeErrorKind =
  (typeof MFA_CHALLENGE_ERROR_KIND)[keyof typeof MFA_CHALLENGE_ERROR_KIND]

export interface MfaChallengeErrorOutcome {
  readonly kind: MfaChallengeErrorKind
  readonly message: string
  readonly retryAfterSeconds: number | null
}

/** Canonical UI copy. Pinned by the test; do not edit without
 *  updating the test. */
export const MFA_CHALLENGE_ERROR_COPY: Readonly<
  Record<MfaChallengeErrorKind, string>
> = Object.freeze({
  invalid_code:
    "That code is not valid. Double-check the digits and try again.",
  expired_challenge:
    "This challenge has expired. Please sign in again from the start.",
  rate_limited:
    "Too many attempts. Please wait a few minutes and retry.",
  bot_challenge_failed:
    "Verification failed. Please refresh the page and try again.",
  webauthn_failed:
    "Security-key verification did not complete. Please try again or pick another method.",
  service_unavailable:
    "Two-factor verification is temporarily unavailable. Please try again in a moment.",
})

interface MfaChallengeErrorInput {
  readonly status: number | null
  readonly message?: string | null
  readonly retryAfter?: string | null
  readonly errorCode?: string | null
}

/** Parse a `Retry-After` HTTP header value into integer seconds.
 *  Behaviour byte-equal to AS.7.1's `parseRetryAfter`. */
export function parseMfaRetryAfter(
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

/** Map any backend response from the MFA-challenge endpoints
 *  (`/auth/mfa/challenge` and `/auth/mfa/webauthn/challenge/complete`)
 *  into the canonical outcome the page UI keys on.
 *
 *  Precedence (most specific → least specific):
 *    1. errorCode = "webauthn_failed" → webauthn_failed (caller-set
 *       on a navigator.credentials.get() abort / NotAllowedError)
 *    2. 410 → expired_challenge (HTTP "Gone")
 *    3. 401 + errorCode = "mfa_challenge_expired" → expired_challenge
 *    4. 401 → invalid_code (the unified-error contract: caller can't
 *       distinguish "wrong digits" from "challenge consumed")
 *    5. 422 → invalid_code (Pydantic format rejection)
 *    6. 429 + errorCode = "bot_challenge_failed" → bot_challenge
 *    7. 429 → rate_limited
 *    8. ≥ 500 or status === null → service_unavailable
 *    9. anything else → invalid_code (defensive default — the
 *       enum-resist contract means we never expose unknown 4xx
 *       copy to the user)
 */
export function classifyMfaChallengeError(
  input: MfaChallengeErrorInput,
): MfaChallengeErrorOutcome {
  const status = input.status
  const retryAfterSeconds = parseMfaRetryAfter(input.retryAfter)

  if (input.errorCode === "webauthn_failed") {
    return Object.freeze({
      kind: MFA_CHALLENGE_ERROR_KIND.webauthnFailed,
      message: MFA_CHALLENGE_ERROR_COPY.webauthn_failed,
      retryAfterSeconds: null,
    })
  }
  if (status === 410) {
    return Object.freeze({
      kind: MFA_CHALLENGE_ERROR_KIND.expiredChallenge,
      message: MFA_CHALLENGE_ERROR_COPY.expired_challenge,
      retryAfterSeconds: null,
    })
  }
  if (status === 401 && input.errorCode === "mfa_challenge_expired") {
    return Object.freeze({
      kind: MFA_CHALLENGE_ERROR_KIND.expiredChallenge,
      message: MFA_CHALLENGE_ERROR_COPY.expired_challenge,
      retryAfterSeconds: null,
    })
  }
  if (status === 401) {
    return Object.freeze({
      kind: MFA_CHALLENGE_ERROR_KIND.invalidCode,
      message: MFA_CHALLENGE_ERROR_COPY.invalid_code,
      retryAfterSeconds: null,
    })
  }
  if (status === 422) {
    return Object.freeze({
      kind: MFA_CHALLENGE_ERROR_KIND.invalidCode,
      message: MFA_CHALLENGE_ERROR_COPY.invalid_code,
      retryAfterSeconds: null,
    })
  }
  if (status === 429 && input.errorCode === "bot_challenge_failed") {
    return Object.freeze({
      kind: MFA_CHALLENGE_ERROR_KIND.botChallenge,
      message: MFA_CHALLENGE_ERROR_COPY.bot_challenge_failed,
      retryAfterSeconds,
    })
  }
  if (status === 429) {
    return Object.freeze({
      kind: MFA_CHALLENGE_ERROR_KIND.rateLimited,
      message: MFA_CHALLENGE_ERROR_COPY.rate_limited,
      retryAfterSeconds,
    })
  }
  if (status === null || status >= 500) {
    return Object.freeze({
      kind: MFA_CHALLENGE_ERROR_KIND.serviceUnavailable,
      message: MFA_CHALLENGE_ERROR_COPY.service_unavailable,
      retryAfterSeconds: null,
    })
  }
  return Object.freeze({
    kind: MFA_CHALLENGE_ERROR_KIND.invalidCode,
    message: MFA_CHALLENGE_ERROR_COPY.invalid_code,
    retryAfterSeconds: null,
  })
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Submit-gate predicate
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export interface MfaChallengeGateInput {
  readonly kind: MfaMethodKind
  readonly value: string
  readonly busy: boolean
}

/** Canonical predicate for "the MFA-challenge submit can fire".
 *  Returns the first failure reason as a stable string, or `null`
 *  when every gate has cleared. WebAuthn doesn't use the text
 *  field — the form-submit handler short-circuits to the
 *  navigator.credentials.get() flow — so the predicate returns
 *  `null` for that kind regardless of the value. */
export function mfaChallengeSubmitBlockedReason(
  input: MfaChallengeGateInput,
): string | null {
  if (input.busy) return "busy"
  if (input.kind === MFA_METHOD_KIND.webauthn) return null
  if (input.kind === MFA_METHOD_KIND.totp) {
    if (!looksLikeTotpCode(input.value)) return "code_invalid"
    return null
  }
  if (input.kind === MFA_METHOD_KIND.backupCode) {
    if (!looksLikeBackupCode(input.value)) return "code_invalid"
    return null
  }
  return "code_invalid"
}

/** Drift guard: every reason string the MFA-gate may emit. Pinned
 *  by the test so adding a new reason without updating the test is
 *  a CI red. */
export const MFA_CHALLENGE_BLOCKED_REASONS = [
  "busy",
  "code_invalid",
] as const

export type MfaChallengeBlockedReason =
  (typeof MFA_CHALLENGE_BLOCKED_REASONS)[number]

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Misc — pulse-bump key
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Compute the next "pulse bump" key from the previous one. The
 *  AS.7.4 6-digit pulse animation re-mounts via `key={pulseKey}`
 *  on every fresh digit so the pulse keyframe replays. */
export function bumpPulseKey(prev: number): number {
  return prev + 1
}
