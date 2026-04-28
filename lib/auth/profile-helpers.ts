/**
 * AS.7.7 — Profile / Account settings page helpers.
 *
 * Pure browser-safe helpers (no React, no DOM, no Next imports) the
 * AS.7.7 `/settings/account` page composes. The page is a single
 * scaffold around 7 distinct settings sections that share common
 * structural concerns:
 *
 *   1. **Connected OAuth accounts** — orbital-satellite layout where
 *      each linked / available provider becomes a satellite revolving
 *      around the central avatar. The page reads
 *      `OAUTH_PROVIDER_CATALOG` (AS.7.1) + a backend "linked
 *      identities" list and partitions them into `linked` / `available`
 *      via `oauthOrbitState()`.
 *   2. **Auth methods** — flat list of `password / oauth / passkey`
 *      that the user has enabled. Reuses the AS.7.1 OAuth catalog +
 *      AS.7.4 MFA status to compute `authMethodsSummary()`.
 *   3. **MFA setup** — TOTP enroll / disable + WebAuthn add / remove +
 *      Backup codes regenerate. The page composes the AS.7.4
 *      `<MfaMethodTabs>` for the TOTP-vs-WebAuthn pivot but the
 *      submit-gate predicates live here so the failure surface is
 *      consistent with the other rows.
 *   4. **Sessions list** — read-only table backed by
 *      `GET /auth/sessions` (already wired in `lib/api.ts::listSessions`).
 *      The `sessionsRowFingerprint()` helper produces a stable
 *      sort key so React keys stay stable across re-fetches.
 *   5. **Password change** — reuses the AS.7.2 password generator +
 *      AS.7.3 reset-password classifier (the backend
 *      `POST /auth/change-password` endpoint shares the same
 *      strength / history validation surface as the reset endpoint).
 *   6. **API keys** — list / create / rotate / revoke. The endpoints
 *      live behind `require_admin`; non-admin sessions render a
 *      disabled-state card with the canonical "ask your administrator"
 *      hint via `apiKeysVisibility()`.
 *   7. **Export data / delete account (GDPR)** — terminal-confirmation
 *      forms. Both endpoints are not yet wired in the backend; the
 *      page surfaces a canonical `service_unavailable` banner until
 *      the endpoints exist. This mirrors the AS.7.2 / AS.7.3 / AS.7.5
 *      "ship visual layer independently" pattern.
 *
 * Module-global state audit (per implement_phase_step.md SOP §1):
 *   - All exports are `as const` frozen object literals or pure
 *     functions. Zero module-level mutable container.
 *   - Cross-worker / cross-tab derivation is trivially identical
 *     (Answer #1 of the SOP §1 audit) — pure helpers.
 *   - Orbit-layout math reads no module state; it dispatches on
 *     the input args only (provider count + index + radius + tier).
 *
 * Read-after-write timing audit: N/A — pure helpers, no async DB
 * calls, no parallelisation change vs. existing auth-context.
 */

import {
  OAUTH_PROVIDER_CATALOG,
  type OAuthProviderId,
  type OAuthProviderInfo,
} from "./oauth-providers"

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Section vocabulary
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Canonical 7-section vocabulary the dedicated page renders.
 *  Frozen `as const` so adding a new section requires updating
 *  the test in lockstep. */
export const PROFILE_SECTION_KIND = {
  connectedAccounts: "connected_accounts",
  authMethods: "auth_methods",
  mfaSetup: "mfa_setup",
  sessions: "sessions",
  passwordChange: "password_change",
  apiKeys: "api_keys",
  dataPrivacy: "data_privacy",
} as const

export type ProfileSectionKind =
  (typeof PROFILE_SECTION_KIND)[keyof typeof PROFILE_SECTION_KIND]

/** Drift guard: every section the page renders. Pinned by the test
 *  so adding a new section without updating the test is a CI red. */
export const PROFILE_SECTIONS_ORDERED = [
  "connected_accounts",
  "auth_methods",
  "mfa_setup",
  "sessions",
  "password_change",
  "api_keys",
  "data_privacy",
] as const

/** Per-section UI copy. The page reads `title` for the section
 *  H2 + `summary` for the explanation paragraph. Pinned by the
 *  test; do not edit without updating the test. */
export interface ProfileSectionCopy {
  readonly title: string
  readonly summary: string
}

export const PROFILE_SECTION_COPY: Readonly<
  Record<ProfileSectionKind, ProfileSectionCopy>
> = Object.freeze({
  connected_accounts: Object.freeze({
    title: "Connected accounts",
    summary:
      "OAuth identities you've linked to this account. Disconnecting a provider revokes its sign-in privileges immediately.",
  }),
  auth_methods: Object.freeze({
    title: "Authentication methods",
    summary:
      "Every credential type that can sign you in to this account. Keep at least one fallback method alive at all times.",
  }),
  mfa_setup: Object.freeze({
    title: "Multi-factor authentication",
    summary:
      "Add a second factor to defend the account if your password leaks. TOTP is the standard; passkeys (WebAuthn) are stronger when your hardware supports them.",
  }),
  sessions: Object.freeze({
    title: "Active sessions",
    summary:
      "Every device currently signed in to this account. Revoke a session to sign that device out immediately.",
  }),
  password_change: Object.freeze({
    title: "Change password",
    summary:
      "Rotate your password without losing the session on this device. Every other signed-in device will be signed out automatically.",
  }),
  api_keys: Object.freeze({
    title: "API keys",
    summary:
      "Bearer tokens used by automation that doesn't sign in interactively. Rotate or revoke keys whenever the operator that needs them changes.",
  }),
  data_privacy: Object.freeze({
    title: "Data & privacy",
    summary:
      "Export a portable copy of every artefact tied to your account, or request permanent deletion in line with GDPR Article 17.",
  }),
})

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  OAuth orbit satellite layout
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Orbit ring tiers. The connected providers live on the inner ring
 *  (radius 110px, smaller circle); the available providers live on
 *  the outer ring (radius 165px, easier to scan). */
export const ORBIT_RADIUS_BY_TIER = Object.freeze({
  inner: 110,
  outer: 165,
})

export type OrbitRing = keyof typeof ORBIT_RADIUS_BY_TIER

/** One satellite slot in the orbit layout. Frozen on emit so mutation
 *  at runtime is a TS error. */
export interface OrbitSatellitePosition {
  readonly id: OAuthProviderId
  readonly ring: OrbitRing
  /** Angle in radians, measured clockwise from 12 o'clock. */
  readonly angleRad: number
  /** Resolved x offset from center, in pixels. */
  readonly xPx: number
  /** Resolved y offset from center, in pixels. */
  readonly yPx: number
  /** True when the satellite represents a linked provider, false
   *  for an "available to connect" placeholder. */
  readonly isLinked: boolean
  /** Reference back to the catalog entry so the page can resolve
   *  brand colour + display name without a second lookup. */
  readonly provider: OAuthProviderInfo
}

/** Compute an evenly-spaced ring of satellite positions. Pure —
 *  same inputs always emit byte-identical output (Answer #1 of
 *  the SOP §1 audit, deterministic-by-construction). */
export function orbitPositionsForRing(
  providers: readonly OAuthProviderInfo[],
  ring: OrbitRing,
  isLinked: boolean,
  angleOffsetRad: number = 0,
): readonly OrbitSatellitePosition[] {
  const radius = ORBIT_RADIUS_BY_TIER[ring]
  const count = providers.length
  if (count === 0) return Object.freeze([])
  const step = (2 * Math.PI) / count
  const positions = providers.map((provider, i) => {
    const angleRad = angleOffsetRad + step * i
    // Round to 3 decimals so the data-attribute strings are stable
    // for the tests across browsers / vitest runs.
    const xPx = Math.round(Math.sin(angleRad) * radius * 1000) / 1000
    const yPx = Math.round(-Math.cos(angleRad) * radius * 1000) / 1000
    return Object.freeze({
      id: provider.id,
      ring,
      angleRad,
      xPx,
      yPx,
      isLinked,
      provider,
    })
  })
  return Object.freeze(positions)
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Connected-accounts state classifier
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Backend payload shape for one linked OAuth identity. The endpoint
 *  is not yet wired in the backend (see file-level docstring); we
 *  shape the helper so when the endpoint lands the wiring is
 *  obvious. */
export interface LinkedOAuthIdentity {
  readonly provider: string
  /** Backend-supplied display name (e.g. user@gmail.com). May be
   *  empty when the IdP doesn't expose it. */
  readonly displayName?: string | null
  /** ISO-8601 timestamp of the link. Optional for backend
   *  compatibility. */
  readonly linkedAt?: string | null
}

export interface OAuthOrbitStateInput {
  /** Linked identities returned by the backend (may be empty). */
  readonly linked: readonly LinkedOAuthIdentity[]
}

export interface OAuthOrbitState {
  readonly innerRing: readonly OrbitSatellitePosition[]
  readonly outerRing: readonly OrbitSatellitePosition[]
  readonly linkedCount: number
  readonly availableCount: number
}

/** Compute the partitioned orbit state from the backend's linked-
 *  identities list. Unknown provider IDs from the backend are
 *  ignored (defensive — the backend may add a vendor before the
 *  frontend catalog is updated). */
export function oauthOrbitState(
  input: OAuthOrbitStateInput,
): OAuthOrbitState {
  const linkedSet = new Set(
    input.linked
      .map((row) => (row.provider || "").toLowerCase().trim())
      .filter((id): id is string => id.length > 0),
  )
  const linkedProviders: OAuthProviderInfo[] = []
  const availableProviders: OAuthProviderInfo[] = []
  for (const provider of OAUTH_PROVIDER_CATALOG) {
    if (linkedSet.has(provider.id)) {
      linkedProviders.push(provider)
    } else {
      availableProviders.push(provider)
    }
  }
  const innerRing = orbitPositionsForRing(linkedProviders, "inner", true)
  const outerRing = orbitPositionsForRing(
    availableProviders,
    "outer",
    false,
    // Offset the outer ring by half a step so satellites on different
    // rings don't visually align at 12 o'clock (the user reads them
    // as a single radial column otherwise).
    availableProviders.length > 0
      ? Math.PI / Math.max(1, availableProviders.length)
      : 0,
  )
  return Object.freeze({
    innerRing,
    outerRing,
    linkedCount: linkedProviders.length,
    availableCount: availableProviders.length,
  })
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Auth methods summary
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export const AUTH_METHOD_KIND = {
  password: "password",
  oauth: "oauth",
  passkey: "passkey",
  totp: "totp",
  backupCode: "backup_code",
} as const

export type AuthMethodKind =
  (typeof AUTH_METHOD_KIND)[keyof typeof AUTH_METHOD_KIND]

export const AUTH_METHOD_KINDS_ORDERED = [
  "password",
  "oauth",
  "passkey",
  "totp",
  "backup_code",
] as const

export interface AuthMethodSummaryInput {
  /** True when the user has a local password set. The backend's
   *  whoami payload exposes this via `auth_mode === "password"` or
   *  `oauth_only === false`; the helper accepts the resolved
   *  boolean directly so the page is the only thing parsing the
   *  whoami shape. */
  readonly hasPassword: boolean
  readonly linkedOAuth: readonly LinkedOAuthIdentity[]
  /** True when the user has at least one verified TOTP. */
  readonly hasTotp: boolean
  /** True when the user has at least one verified WebAuthn /
   *  passkey credential. */
  readonly hasPasskey: boolean
  /** Remaining backup-code count (0 when the bundle has been used
   *  up; null when the page hasn't fetched the status yet). */
  readonly backupCodesRemaining: number | null
}

export interface AuthMethodRow {
  readonly kind: AuthMethodKind
  readonly enabled: boolean
  readonly label: string
  readonly hint: string
}

const AUTH_METHOD_LABEL: Readonly<Record<AuthMethodKind, string>> =
  Object.freeze({
    password: "Password",
    oauth: "OAuth identity",
    passkey: "Passkey (WebAuthn)",
    totp: "Authenticator app (TOTP)",
    backup_code: "Backup codes",
  })

/** Compute the auth-methods summary table the section renders.
 *  Pure — same input always yields the same output. */
export function authMethodsSummary(
  input: AuthMethodSummaryInput,
): readonly AuthMethodRow[] {
  const oauthCount = input.linkedOAuth.length
  return Object.freeze([
    Object.freeze({
      kind: AUTH_METHOD_KIND.password,
      enabled: input.hasPassword,
      label: AUTH_METHOD_LABEL.password,
      hint: input.hasPassword
        ? "Active — you can sign in with email + password."
        : "Inactive — set a password to enable email sign-in.",
    }),
    Object.freeze({
      kind: AUTH_METHOD_KIND.oauth,
      enabled: oauthCount > 0,
      label: AUTH_METHOD_LABEL.oauth,
      hint:
        oauthCount > 0
          ? `Active — ${oauthCount} provider${oauthCount === 1 ? "" : "s"} linked.`
          : "Inactive — link an OAuth provider to enable single sign-on.",
    }),
    Object.freeze({
      kind: AUTH_METHOD_KIND.passkey,
      enabled: input.hasPasskey,
      label: AUTH_METHOD_LABEL.passkey,
      hint: input.hasPasskey
        ? "Active — your passkey can replace your password during sign-in."
        : "Inactive — register a passkey for hardware-backed sign-in.",
    }),
    Object.freeze({
      kind: AUTH_METHOD_KIND.totp,
      enabled: input.hasTotp,
      label: AUTH_METHOD_LABEL.totp,
      hint: input.hasTotp
        ? "Active — your authenticator app is required to complete sign-in."
        : "Inactive — enrol an authenticator app for second-factor protection.",
    }),
    Object.freeze({
      kind: AUTH_METHOD_KIND.backupCode,
      enabled: (input.backupCodesRemaining ?? 0) > 0,
      label: AUTH_METHOD_LABEL.backup_code,
      hint:
        input.backupCodesRemaining === null
          ? "Loading current backup-code status…"
          : input.backupCodesRemaining > 0
          ? `Active — ${input.backupCodesRemaining} backup code${input.backupCodesRemaining === 1 ? "" : "s"} remaining.`
          : "Inactive — regenerate backup codes after enrolling TOTP.",
    }),
  ])
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Password-change submit gate
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export interface PasswordChangeGateInput {
  readonly busy: boolean
  readonly currentPassword: string
  readonly newPassword: string
  readonly newPasswordSaved: boolean
}

/** Canonical predicate for "the change-password CTA can fire". Returns
 *  the first failure reason as a stable string, or `null` when every
 *  gate has cleared. */
export function passwordChangeBlockedReason(
  input: PasswordChangeGateInput,
): string | null {
  if (input.busy) return "busy"
  if (!input.currentPassword.trim()) return "current_password_missing"
  if (input.newPassword.length < 12) return "new_password_too_short"
  if (input.newPassword === input.currentPassword) {
    return "new_password_same_as_current"
  }
  if (!input.newPasswordSaved) return "password_not_saved"
  return null
}

/** Drift guard: every reason string the change-password gate may
 *  emit. Pinned by the test so adding a new reason without updating
 *  the test is a CI red. */
export const PASSWORD_CHANGE_BLOCKED_REASONS = [
  "busy",
  "current_password_missing",
  "new_password_too_short",
  "new_password_same_as_current",
  "password_not_saved",
] as const

export type PasswordChangeBlockedReason =
  (typeof PASSWORD_CHANGE_BLOCKED_REASONS)[number]

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Password-change error classifier
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Canonical error vocabulary surfaced in the change-password
 *  banner. The backend's responses are documented in
 *  `lib/api.ts::changePassword`'s docstring. */
export const PASSWORD_CHANGE_ERROR_KIND = {
  invalidCurrentPassword: "invalid_current_password",
  weakPassword: "weak_password",
  rateLimited: "rate_limited",
  serviceUnavailable: "service_unavailable",
} as const

export type PasswordChangeErrorKind =
  (typeof PASSWORD_CHANGE_ERROR_KIND)[keyof typeof PASSWORD_CHANGE_ERROR_KIND]

export const PASSWORD_CHANGE_ERROR_COPY: Readonly<
  Record<PasswordChangeErrorKind, string>
> = Object.freeze({
  invalid_current_password:
    "Your current password didn't match — double-check it and try again.",
  weak_password:
    "That new password is too weak or has been used recently. Pick something stronger.",
  rate_limited:
    "Too many password-change attempts. Wait a bit before trying again.",
  service_unavailable:
    "We couldn't change your password right now. Try again in a moment.",
})

export interface PasswordChangeErrorInput {
  readonly status: number | null
  readonly errorCode?: string | null
  readonly retryAfter?: string | null
  readonly message?: string | null
}

export interface PasswordChangeErrorOutcome {
  readonly kind: PasswordChangeErrorKind
  readonly message: string
  readonly retryAfterSeconds: number | null
}

export function classifyPasswordChangeError(
  input: PasswordChangeErrorInput,
): PasswordChangeErrorOutcome {
  const retry = input.retryAfter
    ? Number.isFinite(Number(input.retryAfter))
      ? Number(input.retryAfter)
      : null
    : null
  const status = input.status
  if (status === 401) {
    return Object.freeze({
      kind: PASSWORD_CHANGE_ERROR_KIND.invalidCurrentPassword,
      message: PASSWORD_CHANGE_ERROR_COPY.invalid_current_password,
      retryAfterSeconds: null,
    })
  }
  if (status === 422) {
    return Object.freeze({
      kind: PASSWORD_CHANGE_ERROR_KIND.weakPassword,
      message: PASSWORD_CHANGE_ERROR_COPY.weak_password,
      retryAfterSeconds: null,
    })
  }
  if (status === 429) {
    return Object.freeze({
      kind: PASSWORD_CHANGE_ERROR_KIND.rateLimited,
      message: PASSWORD_CHANGE_ERROR_COPY.rate_limited,
      retryAfterSeconds: retry,
    })
  }
  if (status === null || (status >= 500 && status < 600)) {
    return Object.freeze({
      kind: PASSWORD_CHANGE_ERROR_KIND.serviceUnavailable,
      message: PASSWORD_CHANGE_ERROR_COPY.service_unavailable,
      retryAfterSeconds: retry,
    })
  }
  return Object.freeze({
    kind: PASSWORD_CHANGE_ERROR_KIND.serviceUnavailable,
    message: PASSWORD_CHANGE_ERROR_COPY.service_unavailable,
    retryAfterSeconds: retry,
  })
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Session row formatter
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export interface SessionRowInput {
  readonly tokenHint: string
  readonly createdAt: number
  readonly lastSeenAt: number
  readonly ip: string
  readonly userAgent: string
  readonly isCurrent: boolean
}

/** Stable sort key suitable for React `key` — the token-hint is
 *  globally unique within an account so this is collision-free. */
export function sessionsRowFingerprint(row: SessionRowInput): string {
  return row.tokenHint
}

/** "5 min ago" / "2 h ago" / "3 d ago" / "just now" formatter for
 *  the last-seen-at column. Pure — clock is passed as an argument
 *  for test determinism. */
export function formatRelativeTime(
  pastSeconds: number,
  nowSeconds: number,
): string {
  const delta = Math.max(0, Math.floor(nowSeconds - pastSeconds))
  if (delta < 60) return "just now"
  if (delta < 3600) return `${Math.floor(delta / 60)} min ago`
  if (delta < 86400) return `${Math.floor(delta / 3600)} h ago`
  return `${Math.floor(delta / 86400)} d ago`
}

/** Truncate a User-Agent string to a single readable line. The
 *  table is space-constrained so we drop the version-fluff after the
 *  product token. Pure. */
export function shortenUserAgent(raw: string): string {
  if (!raw) return "Unknown device"
  const trimmed = raw.trim()
  if (trimmed.length <= 60) return trimmed
  return `${trimmed.slice(0, 57)}…`
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  API keys visibility gate
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export interface ApiKeysVisibilityInput {
  readonly userRole: string | null
}

export interface ApiKeysVisibility {
  readonly visible: boolean
  readonly reason: "ok" | "not_admin" | "no_session"
}

/** API-key endpoints are admin-only on the backend. Non-admin
 *  sessions render a disabled-state card with the canonical "ask
 *  your administrator" hint. Pure. */
export function apiKeysVisibility(
  input: ApiKeysVisibilityInput,
): ApiKeysVisibility {
  const role = (input.userRole || "").toLowerCase().trim()
  if (!role) return Object.freeze({ visible: false, reason: "no_session" })
  if (role === "admin" || role === "owner" || role === "super_admin") {
    return Object.freeze({ visible: true, reason: "ok" })
  }
  return Object.freeze({ visible: false, reason: "not_admin" })
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Account-deletion confirmation gate
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** The literal string the user must type to arm the delete-account
 *  CTA. Same convention as GitHub / Vercel ("type DELETE to confirm"). */
export const DELETE_ACCOUNT_CONFIRM_PHRASE = "DELETE"

export interface DeleteAccountGateInput {
  readonly busy: boolean
  readonly typedConfirmation: string
  readonly acknowledgedIrreversible: boolean
}

export function deleteAccountBlockedReason(
  input: DeleteAccountGateInput,
): string | null {
  if (input.busy) return "busy"
  if (input.typedConfirmation.trim() !== DELETE_ACCOUNT_CONFIRM_PHRASE) {
    return "confirmation_mismatch"
  }
  if (!input.acknowledgedIrreversible) {
    return "irreversible_unacknowledged"
  }
  return null
}

export const DELETE_ACCOUNT_BLOCKED_REASONS = [
  "busy",
  "confirmation_mismatch",
  "irreversible_unacknowledged",
] as const

export type DeleteAccountBlockedReason =
  (typeof DELETE_ACCOUNT_BLOCKED_REASONS)[number]

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  GDPR endpoint outcome shapes
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Backend payload shape for `POST /auth/account/export`. The
 *  endpoint is not yet wired; the helper still pins the shape so
 *  the page can branch deterministically once the backend lands. */
export interface AccountExportResponse {
  readonly status: string
  readonly downloadUrl?: string | null
  readonly expiresAt?: string | null
}

/** Backend payload shape for `POST /auth/account/delete`. */
export interface AccountDeleteResponse {
  readonly status: string
  /** ISO-8601 timestamp of the scheduled deletion (some backends
   *  enforce a 30-day grace period). */
  readonly scheduledFor?: string | null
}

export const GDPR_ERROR_KIND = {
  notImplemented: "not_implemented",
  rateLimited: "rate_limited",
  serviceUnavailable: "service_unavailable",
  unauthorised: "unauthorised",
} as const

export type GdprErrorKind =
  (typeof GDPR_ERROR_KIND)[keyof typeof GDPR_ERROR_KIND]

export const GDPR_ERROR_COPY: Readonly<Record<GdprErrorKind, string>> =
  Object.freeze({
    not_implemented:
      "This action isn't available yet. Reach out to your administrator while we ship the endpoint.",
    rate_limited:
      "Too many requests. Wait a moment before trying again.",
    service_unavailable:
      "We couldn't reach the server. Try again in a few moments.",
    unauthorised:
      "Your session expired. Sign in again to continue.",
  })

export interface GdprErrorInput {
  readonly status: number | null
  readonly retryAfter?: string | null
}

export interface GdprErrorOutcome {
  readonly kind: GdprErrorKind
  readonly message: string
  readonly retryAfterSeconds: number | null
}

export function classifyGdprError(
  input: GdprErrorInput,
): GdprErrorOutcome {
  const retry = input.retryAfter
    ? Number.isFinite(Number(input.retryAfter))
      ? Number(input.retryAfter)
      : null
    : null
  const status = input.status
  if (status === 401 || status === 403) {
    return Object.freeze({
      kind: GDPR_ERROR_KIND.unauthorised,
      message: GDPR_ERROR_COPY.unauthorised,
      retryAfterSeconds: null,
    })
  }
  if (status === 404 || status === 405 || status === 501) {
    return Object.freeze({
      kind: GDPR_ERROR_KIND.notImplemented,
      message: GDPR_ERROR_COPY.not_implemented,
      retryAfterSeconds: null,
    })
  }
  if (status === 429) {
    return Object.freeze({
      kind: GDPR_ERROR_KIND.rateLimited,
      message: GDPR_ERROR_COPY.rate_limited,
      retryAfterSeconds: retry,
    })
  }
  return Object.freeze({
    kind: GDPR_ERROR_KIND.serviceUnavailable,
    message: GDPR_ERROR_COPY.service_unavailable,
    retryAfterSeconds: retry,
  })
}
