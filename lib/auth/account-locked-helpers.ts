/**
 * AS.7.6 — Account-locked / suspended page helpers.
 *
 * Pure browser-safe helpers (no React, no DOM, no Next imports) the
 * AS.7.6 `/account-locked` page composes. The dedicated page replaces
 * the AS.7.1 inline `<AccountLockedOverlay>` for the "your account is
 * temporarily locked / suspended" branch — the overlay still ships
 * inline on the login form for backwards-compat with the AS.7.1 test
 * suite, but the AS.7.4-pattern redirect (`useEffect` → `router.push`)
 * funnels every fresh 423 lockout to the dedicated page after one
 * frame so users land on a full-screen explanation with a clear
 * recovery path rather than a partially-occluded glass card.
 *
 * Three lockout kinds the page branches on:
 *
 *   1. **temporary_lockout** — repeated failed login attempts hit the
 *      `failed_login_lockout` rate-limit bucket. Shows the canonical
 *      "wait a few minutes" copy, a countdown driven by the
 *      `Retry-After` header / `?retry_after=N` query, and the "Try
 *      signing in again" CTA that becomes enabled when the countdown
 *      hits zero.
 *
 *   2. **admin_suspended** — an administrator manually disabled the
 *      account (the existing `account_disabled` security event from
 *      `app/login/page.tsx::SESSION_REVOCATION_TRIGGER_COPY`). No
 *      countdown — only a "contact your administrator" recovery path
 *      and an optional "sign out" CTA when a session is still alive.
 *
 *   3. **security_hold** — defensive default for any 423 / 451 that
 *      didn't match a known reason hint. Surfaces a forgot-password
 *      CTA + contact-admin path; treats the lock as indefinite and
 *      hides the countdown.
 *
 * Module-global state audit (per implement_phase_step.md SOP §1):
 *   - All exports are `as const` frozen object literals or pure
 *     functions. Zero module-level mutable container.
 *   - Cross-worker / cross-tab derivation is trivially identical
 *     (Answer #1 of the SOP §1 audit) — pure helpers.
 *   - The lockout classifier reads no module state; it dispatches on
 *     the input args only (reason hint / status / accountLocked flag).
 *
 * Read-after-write timing audit: N/A — pure helpers, no async DB
 * calls, no parallelisation change vs. existing auth-context.
 */

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Lockout kind vocabulary
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Canonical lockout-kind vocabulary the dedicated page branches on.
 *  Frozen `as const` so a new branch needs to update this table + the
 *  test in lockstep. */
export const LOCKOUT_REASON_KIND = {
  temporaryLockout: "temporary_lockout",
  adminSuspended: "admin_suspended",
  securityHold: "security_hold",
} as const

export type LockoutReasonKind =
  (typeof LOCKOUT_REASON_KIND)[keyof typeof LOCKOUT_REASON_KIND]

/** Drift guard: every kind string the page may surface. Pinned by the
 *  test so adding a new kind without updating the test is a CI red. */
export const LOCKOUT_REASON_KINDS_ORDERED = [
  "temporary_lockout",
  "admin_suspended",
  "security_hold",
] as const

/** Per-kind UI copy. The page reads `title` for the H1 + `summary`
 *  for the explanation paragraph + `recoveryHint` for the CTA-row
 *  caption. Pinned by the test; do not edit without updating the test. */
export interface LockoutCopy {
  readonly title: string
  readonly summary: string
  readonly recoveryHint: string
}

export const LOCKOUT_REASON_COPY: Readonly<
  Record<LockoutReasonKind, LockoutCopy>
> = Object.freeze({
  temporary_lockout: Object.freeze({
    title: "Account temporarily locked",
    summary:
      "Too many failed sign-in attempts. For your security we paused this account briefly. The countdown below will release the hold automatically — no action needed.",
    recoveryHint:
      "If you've forgotten your password, request a fresh link instead of waiting.",
  }),
  admin_suspended: Object.freeze({
    title: "Account suspended",
    summary:
      "An administrator paused this account. Contact your administrator to restore access — they'll need to lift the suspension before you can sign in again.",
    recoveryHint:
      "If you believe this is a mistake, reach out to your administrator with the email above.",
  }),
  security_hold: Object.freeze({
    title: "Account on hold",
    summary:
      "We placed a temporary hold on this account after a security event. Resetting your password is the fastest way to clear the hold; if you need help, contact your administrator.",
    recoveryHint:
      "Reset your password to clear the hold, or contact your administrator if you can't sign in.",
  }),
})

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Reason hint normaliser
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Map a free-form reason hint (from `?reason=` query / backend
 *  error-code field / login-trigger string) to a canonical lockout
 *  kind. Returns `null` when the hint is empty / unrecognised so the
 *  classifier can fall through to its precedence cascade. */
export function normaliseLockoutReasonHint(
  raw: string | null | undefined,
): LockoutReasonKind | null {
  if (!raw) return null
  const lower = raw.trim().toLowerCase()
  if (!lower) return null
  // The full set is small enough that explicit synonym mapping beats
  // a regex — the page receives at most one of these strings on a
  // given navigation.
  if (
    lower === "temporary_lockout" ||
    lower === "temporarily_locked" ||
    lower === "rate_limited" ||
    lower === "failed_login_lockout"
  ) {
    return LOCKOUT_REASON_KIND.temporaryLockout
  }
  if (
    lower === "admin_suspended" ||
    lower === "account_disabled" ||
    lower === "account_suspended" ||
    lower === "suspended" ||
    lower === "disabled"
  ) {
    return LOCKOUT_REASON_KIND.adminSuspended
  }
  if (
    lower === "security_hold" ||
    lower === "security_event" ||
    lower === "user_security_event" ||
    lower === "not_me_cascade"
  ) {
    return LOCKOUT_REASON_KIND.securityHold
  }
  return null
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Retry-after parsing
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Parse the `?retry_after=` query param into integer seconds.
 *  Tolerates both delta-seconds (canonical) and HTTP-date forms
 *  (when forwarded from a `Retry-After` header). Returns `null` for
 *  empty / malformed / negative values so the page knows to hide the
 *  countdown and disable the retry CTA indefinitely. */
export function parseRetryAfterParam(
  raw: string | null | undefined,
): number | null {
  if (raw === null || raw === undefined) return null
  const trimmed = String(raw).trim()
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

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Effective state classifier
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** What the page renders is the merge of three input sources, in
 *  precedence order:
 *
 *    1. Live `auth.lastLoginError` — when the user just hit a 423 on
 *       the previous /login submit. Carries the canonical kind from
 *       AS.7.1's `classifyLoginError` (always `account_locked`) plus
 *       the `retryAfterSeconds` parsed from the backend response.
 *       Promotes to `temporary_lockout` because the rate-limit bucket
 *       is the only path that sets the 423 status today.
 *    2. URL query params — `?reason=<hint>` + `?retry_after=N` +
 *       `?email=<addr>`. Used for direct navigation (admin sends a
 *       link, security pipeline emits a redirect, dashboard catches
 *       a stale 423 and pushes the user here).
 *    3. Defensive default — `temporary_lockout` with no countdown
 *       (the most common 423 source) and no email pre-fill.
 */
export interface LockoutEffectiveStateInput {
  /** `?reason=` query param value (null when absent). */
  readonly reasonHint: string | null
  /** `?retry_after=` query param value (null when absent). */
  readonly retryAfterRaw: string | null
  /** `?email=` query param value (null when absent). */
  readonly emailHint: string | null
  /** Live `auth.lastLoginError` (null when no in-session lockout). */
  readonly liveLoginError: {
    readonly accountLocked: boolean
    readonly retryAfterSeconds: number | null
  } | null
  /** Live `auth.user.email` when a session is active — gives the
   *  forgot-password / contact-admin CTAs a sensible default when
   *  the URL didn't carry an email hint. */
  readonly liveUserEmail: string | null
}

export interface LockoutEffectiveState {
  readonly kind: LockoutReasonKind
  readonly retryAfterSeconds: number | null
  readonly email: string | null
  readonly copy: LockoutCopy
  readonly supportsCountdown: boolean
  readonly supportsRetrySignIn: boolean
  readonly supportsResetPassword: boolean
  readonly supportsContactAdmin: boolean
}

/** Per-kind capability table. Pinned alongside the copy table — a
 *  new lockout kind needs to declare which of the four recovery
 *  surfaces it offers up front so the test can drift-guard the
 *  combinatorics (a `temporary_lockout` with `supportsContactAdmin
 *  = false` would silently strip the recovery hint). */
export const LOCKOUT_KIND_CAPABILITIES: Readonly<
  Record<
    LockoutReasonKind,
    Readonly<{
      countdown: boolean
      retrySignIn: boolean
      resetPassword: boolean
      contactAdmin: boolean
    }>
  >
> = Object.freeze({
  temporary_lockout: Object.freeze({
    countdown: true,
    retrySignIn: true,
    resetPassword: true,
    contactAdmin: true,
  }),
  admin_suspended: Object.freeze({
    countdown: false,
    retrySignIn: false,
    resetPassword: false,
    contactAdmin: true,
  }),
  security_hold: Object.freeze({
    countdown: false,
    retrySignIn: false,
    resetPassword: true,
    contactAdmin: true,
  }),
})

/** Compute the effective state the page renders from the merge of
 *  query params + live auth-context state. Pure — same input always
 *  yields the same output (Answer #1 of the SOP §1 audit). */
export function lockoutEffectiveState(
  input: LockoutEffectiveStateInput,
): LockoutEffectiveState {
  const hintKind = normaliseLockoutReasonHint(input.reasonHint)
  const live = input.liveLoginError
  // Precedence: live in-session 423 wins over `?reason=` because a
  // fresh login attempt is the strongest signal we have. The query
  // param is still consulted for the *kind* so a security pipeline
  // sending the user here with `?reason=admin_suspended` overrides
  // a stale `accountLocked=true` flag from a prior login.
  let kind: LockoutReasonKind
  if (hintKind !== null) {
    kind = hintKind
  } else if (live?.accountLocked) {
    kind = LOCKOUT_REASON_KIND.temporaryLockout
  } else {
    kind = LOCKOUT_REASON_KIND.temporaryLockout
  }

  // Retry-after: prefer the live source (just-now lockout has the
  // freshest timer); fall back to the query param.
  let retryAfterSeconds: number | null = null
  if (live?.retryAfterSeconds !== undefined && live?.retryAfterSeconds !== null) {
    retryAfterSeconds = live.retryAfterSeconds
  } else {
    retryAfterSeconds = parseRetryAfterParam(input.retryAfterRaw)
  }

  // Email: query param (forwarded from /login submit) > live user
  // email (when the page is rendered for an authenticated user
  // whose session was just terminated).
  let email: string | null = null
  if (input.emailHint && input.emailHint.trim()) {
    email = input.emailHint.trim()
  } else if (input.liveUserEmail && input.liveUserEmail.trim()) {
    email = input.liveUserEmail.trim()
  }

  const caps = LOCKOUT_KIND_CAPABILITIES[kind]
  const copy = LOCKOUT_REASON_COPY[kind]

  return Object.freeze({
    kind,
    retryAfterSeconds,
    email,
    copy,
    supportsCountdown: caps.countdown,
    supportsRetrySignIn: caps.retrySignIn,
    supportsResetPassword: caps.resetPassword,
    supportsContactAdmin: caps.contactAdmin,
  })
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Countdown formatter
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Format a remaining-seconds countdown for the "Retry in 0:30" /
 *  "Retry in 45s" copy under the lock icon. Negative or NaN inputs
 *  collapse to "0s" so the UI doesn't flash an inconsistent label
 *  during the final tick. Anything ≥ 60s renders as `M:SS`; otherwise
 *  `Ns`. */
export function formatRemainingTime(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return "0s"
  const total = Math.max(0, Math.floor(seconds))
  if (total < 60) return `${total}s`
  const m = Math.floor(total / 60)
  const s = total % 60
  return `${m}:${s.toString().padStart(2, "0")}`
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Retry-sign-in submit-gate predicate
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export interface RetrySignInGateInput {
  readonly state: LockoutEffectiveState
  /** Live remaining seconds tracked by the page's countdown timer. */
  readonly remainingSeconds: number | null
}

/** Canonical predicate for "the retry-sign-in CTA can fire". Returns
 *  the first failure reason as a stable string, or `null` when every
 *  gate has cleared. */
export function retrySignInBlockedReason(
  input: RetrySignInGateInput,
): string | null {
  if (!input.state.supportsRetrySignIn) return "kind_unsupported"
  if (input.remainingSeconds !== null && input.remainingSeconds > 0) {
    return "countdown_active"
  }
  return null
}

/** Drift guard: every reason string the retry gate may emit. Pinned
 *  by the test so adding a new reason without updating the test is a
 *  CI red. */
export const RETRY_SIGN_IN_BLOCKED_REASONS = [
  "kind_unsupported",
  "countdown_active",
] as const

export type RetrySignInBlockedReason =
  (typeof RETRY_SIGN_IN_BLOCKED_REASONS)[number]

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Contact-admin mailto resolver
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Default contact-admin mailto target. The deployment can override
 *  via `NEXT_PUBLIC_OMNISIGHT_ADMIN_EMAIL` env at build time; the
 *  fallback below mirrors the bootstrap default `OMNISIGHT_ADMIN_EMAIL`
 *  the backend ships with so a fresh deployment still surfaces a
 *  plausible address rather than an empty link. */
export const DEFAULT_ADMIN_CONTACT_EMAIL = "admin@omnisight.local"

/** Build the `mailto:` href the contact-admin CTA points at. The
 *  subject + body are pre-populated with context the user / admin
 *  team finds useful for triage; the user can edit before sending. */
export function buildContactAdminMailto(input: {
  readonly adminEmail: string
  readonly userEmail: string | null
  readonly kind: LockoutReasonKind
}): string {
  const subject =
    input.kind === LOCKOUT_REASON_KIND.adminSuspended
      ? "OmniSight account suspension — restore access"
      : input.kind === LOCKOUT_REASON_KIND.securityHold
      ? "OmniSight account on security hold — assistance"
      : "OmniSight account locked — assistance"
  const userLine = input.userEmail
    ? `\n\nMy account email: ${input.userEmail}`
    : ""
  const body = `Hi,\n\nMy OmniSight account is currently ${input.kind.replace(/_/g, " ")} and I would like to restore access.${userLine}\n\nThanks.`
  return `mailto:${input.adminEmail}?subject=${encodeURIComponent(
    subject,
  )}&body=${encodeURIComponent(body)}`
}
