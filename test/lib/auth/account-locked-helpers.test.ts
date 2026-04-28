/**
 * AS.7.6 — `lib/auth/account-locked-helpers.ts` contract tests.
 *
 * Pins the lockout-kind vocabulary, copy table, capability table,
 * reason-hint normaliser dispatch matrix, retry-after parser shape
 * matrix, the precedence cascade in `lockoutEffectiveState`, the
 * countdown formatter rounding rules, the retry-sign-in submit-gate
 * predicate truth table, and the contact-admin mailto builder.
 */

import { describe, expect, it } from "vitest"

import {
  DEFAULT_ADMIN_CONTACT_EMAIL,
  LOCKOUT_KIND_CAPABILITIES,
  LOCKOUT_REASON_COPY,
  LOCKOUT_REASON_KIND,
  LOCKOUT_REASON_KINDS_ORDERED,
  RETRY_SIGN_IN_BLOCKED_REASONS,
  buildContactAdminMailto,
  formatRemainingTime,
  lockoutEffectiveState,
  normaliseLockoutReasonHint,
  parseRetryAfterParam,
  retrySignInBlockedReason,
} from "@/lib/auth/account-locked-helpers"

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Constants drift guard
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

describe("AS.7.6 account-locked-helpers — constants drift guard", () => {
  it("LOCKOUT_REASON_KIND covers the 3 canonical kinds", () => {
    expect(Object.values(LOCKOUT_REASON_KIND)).toEqual([
      "temporary_lockout",
      "admin_suspended",
      "security_hold",
    ])
  })

  it("LOCKOUT_REASON_KINDS_ORDERED matches the kind tuple", () => {
    expect([...LOCKOUT_REASON_KINDS_ORDERED]).toEqual([
      "temporary_lockout",
      "admin_suspended",
      "security_hold",
    ])
  })

  it("LOCKOUT_REASON_COPY pins the title for every kind", () => {
    expect(LOCKOUT_REASON_COPY.temporary_lockout.title).toBe(
      "Account temporarily locked",
    )
    expect(LOCKOUT_REASON_COPY.admin_suspended.title).toBe(
      "Account suspended",
    )
    expect(LOCKOUT_REASON_COPY.security_hold.title).toBe(
      "Account on hold",
    )
  })

  it("LOCKOUT_REASON_COPY pins the summary for every kind", () => {
    expect(LOCKOUT_REASON_COPY.temporary_lockout.summary).toMatch(
      /Too many failed sign-in attempts/,
    )
    expect(LOCKOUT_REASON_COPY.admin_suspended.summary).toMatch(
      /An administrator paused this account/,
    )
    expect(LOCKOUT_REASON_COPY.security_hold.summary).toMatch(
      /security event/i,
    )
  })

  it("LOCKOUT_REASON_COPY pins the recoveryHint for every kind", () => {
    expect(LOCKOUT_REASON_COPY.temporary_lockout.recoveryHint).toMatch(
      /forgotten your password/i,
    )
    expect(LOCKOUT_REASON_COPY.admin_suspended.recoveryHint).toMatch(
      /administrator/i,
    )
    expect(LOCKOUT_REASON_COPY.security_hold.recoveryHint).toMatch(
      /Reset your password/i,
    )
  })

  it("LOCKOUT_KIND_CAPABILITIES matches the documented matrix", () => {
    expect(LOCKOUT_KIND_CAPABILITIES.temporary_lockout).toEqual({
      countdown: true,
      retrySignIn: true,
      resetPassword: true,
      contactAdmin: true,
    })
    expect(LOCKOUT_KIND_CAPABILITIES.admin_suspended).toEqual({
      countdown: false,
      retrySignIn: false,
      resetPassword: false,
      contactAdmin: true,
    })
    expect(LOCKOUT_KIND_CAPABILITIES.security_hold).toEqual({
      countdown: false,
      retrySignIn: false,
      resetPassword: true,
      contactAdmin: true,
    })
  })

  it("RETRY_SIGN_IN_BLOCKED_REASONS pins the 2-tuple drift guard", () => {
    expect([...RETRY_SIGN_IN_BLOCKED_REASONS]).toEqual([
      "kind_unsupported",
      "countdown_active",
    ])
  })

  it("DEFAULT_ADMIN_CONTACT_EMAIL matches the backend bootstrap default", () => {
    expect(DEFAULT_ADMIN_CONTACT_EMAIL).toBe("admin@omnisight.local")
  })

  it("LOCKOUT_REASON_COPY top-level object is frozen", () => {
    expect(Object.isFrozen(LOCKOUT_REASON_COPY)).toBe(true)
  })

  it("LOCKOUT_KIND_CAPABILITIES top-level object is frozen", () => {
    expect(Object.isFrozen(LOCKOUT_KIND_CAPABILITIES)).toBe(true)
  })
})

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Reason-hint normaliser
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

describe("AS.7.6 normaliseLockoutReasonHint — dispatch matrix", () => {
  it("returns null for null / undefined / empty / whitespace", () => {
    expect(normaliseLockoutReasonHint(null)).toBeNull()
    expect(normaliseLockoutReasonHint(undefined)).toBeNull()
    expect(normaliseLockoutReasonHint("")).toBeNull()
    expect(normaliseLockoutReasonHint("   ")).toBeNull()
  })

  it("maps temporary_lockout synonyms", () => {
    for (const hint of [
      "temporary_lockout",
      "TEMPORARY_LOCKOUT",
      "temporarily_locked",
      "rate_limited",
      "failed_login_lockout",
    ]) {
      expect(normaliseLockoutReasonHint(hint)).toBe("temporary_lockout")
    }
  })

  it("maps admin_suspended synonyms", () => {
    for (const hint of [
      "admin_suspended",
      "account_disabled",
      "account_suspended",
      "suspended",
      "disabled",
    ]) {
      expect(normaliseLockoutReasonHint(hint)).toBe("admin_suspended")
    }
  })

  it("maps security_hold synonyms", () => {
    for (const hint of [
      "security_hold",
      "security_event",
      "user_security_event",
      "not_me_cascade",
    ]) {
      expect(normaliseLockoutReasonHint(hint)).toBe("security_hold")
    }
  })

  it("returns null for unknown hints", () => {
    expect(normaliseLockoutReasonHint("foo_bar")).toBeNull()
    expect(normaliseLockoutReasonHint("404")).toBeNull()
    expect(normaliseLockoutReasonHint("locked-out-permanently")).toBeNull()
  })

  it("trims whitespace before matching", () => {
    expect(normaliseLockoutReasonHint("  suspended  ")).toBe(
      "admin_suspended",
    )
  })
})

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  parseRetryAfterParam
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

describe("AS.7.6 parseRetryAfterParam — shape matrix", () => {
  it("returns null for null / undefined / empty / whitespace", () => {
    expect(parseRetryAfterParam(null)).toBeNull()
    expect(parseRetryAfterParam(undefined)).toBeNull()
    expect(parseRetryAfterParam("")).toBeNull()
    expect(parseRetryAfterParam("   ")).toBeNull()
  })

  it("parses positive delta-seconds", () => {
    expect(parseRetryAfterParam("0")).toBe(0)
    expect(parseRetryAfterParam("30")).toBe(30)
    expect(parseRetryAfterParam("3600")).toBe(3600)
  })

  it("rejects negative integers", () => {
    expect(parseRetryAfterParam("-1")).toBeNull()
    expect(parseRetryAfterParam("-30")).toBeNull()
  })

  it("rejects garbage strings", () => {
    expect(parseRetryAfterParam("abc")).toBeNull()
    expect(parseRetryAfterParam("30s")).toBeNull()
    expect(parseRetryAfterParam("nan")).toBeNull()
  })

  it("parses HTTP-date forms", () => {
    const future = new Date(Date.now() + 30_000).toUTCString()
    const got = parseRetryAfterParam(future)
    expect(got).not.toBeNull()
    expect(got).toBeGreaterThanOrEqual(28)
    expect(got).toBeLessThanOrEqual(31)
  })

  it("clamps past HTTP-dates to 0", () => {
    const past = new Date(Date.now() - 60_000).toUTCString()
    expect(parseRetryAfterParam(past)).toBe(0)
  })
})

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  lockoutEffectiveState — precedence cascade
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

describe("AS.7.6 lockoutEffectiveState — precedence cascade", () => {
  it("query reason hint wins over live error kind", () => {
    const state = lockoutEffectiveState({
      reasonHint: "admin_suspended",
      retryAfterRaw: null,
      emailHint: null,
      liveLoginError: { accountLocked: true, retryAfterSeconds: 30 },
      liveUserEmail: null,
    })
    expect(state.kind).toBe("admin_suspended")
    expect(state.supportsRetrySignIn).toBe(false)
  })

  it("live in-session 423 promotes to temporary_lockout when no hint", () => {
    const state = lockoutEffectiveState({
      reasonHint: null,
      retryAfterRaw: null,
      emailHint: null,
      liveLoginError: { accountLocked: true, retryAfterSeconds: 30 },
      liveUserEmail: null,
    })
    expect(state.kind).toBe("temporary_lockout")
    expect(state.retryAfterSeconds).toBe(30)
    expect(state.supportsCountdown).toBe(true)
  })

  it("live retryAfterSeconds wins over query retry_after", () => {
    const state = lockoutEffectiveState({
      reasonHint: "temporary_lockout",
      retryAfterRaw: "120",
      emailHint: null,
      liveLoginError: { accountLocked: true, retryAfterSeconds: 30 },
      liveUserEmail: null,
    })
    expect(state.retryAfterSeconds).toBe(30)
  })

  it("falls back to query retry_after when live source missing", () => {
    const state = lockoutEffectiveState({
      reasonHint: "temporary_lockout",
      retryAfterRaw: "120",
      emailHint: null,
      liveLoginError: null,
      liveUserEmail: null,
    })
    expect(state.retryAfterSeconds).toBe(120)
  })

  it("emailHint wins over liveUserEmail", () => {
    const state = lockoutEffectiveState({
      reasonHint: "temporary_lockout",
      retryAfterRaw: null,
      emailHint: "url@example.com",
      liveLoginError: null,
      liveUserEmail: "live@example.com",
    })
    expect(state.email).toBe("url@example.com")
  })

  it("falls back to liveUserEmail when emailHint missing", () => {
    const state = lockoutEffectiveState({
      reasonHint: null,
      retryAfterRaw: null,
      emailHint: null,
      liveLoginError: null,
      liveUserEmail: "live@example.com",
    })
    expect(state.email).toBe("live@example.com")
  })

  it("defensive default is temporary_lockout when nothing supplied", () => {
    const state = lockoutEffectiveState({
      reasonHint: null,
      retryAfterRaw: null,
      emailHint: null,
      liveLoginError: null,
      liveUserEmail: null,
    })
    expect(state.kind).toBe("temporary_lockout")
    expect(state.retryAfterSeconds).toBeNull()
    expect(state.email).toBeNull()
    expect(state.supportsCountdown).toBe(true)
    expect(state.supportsRetrySignIn).toBe(true)
    expect(state.supportsResetPassword).toBe(true)
    expect(state.supportsContactAdmin).toBe(true)
  })

  it("admin_suspended kind has the right capability set", () => {
    const state = lockoutEffectiveState({
      reasonHint: "admin_suspended",
      retryAfterRaw: "30",
      emailHint: null,
      liveLoginError: null,
      liveUserEmail: null,
    })
    expect(state.supportsCountdown).toBe(false)
    expect(state.supportsRetrySignIn).toBe(false)
    expect(state.supportsResetPassword).toBe(false)
    expect(state.supportsContactAdmin).toBe(true)
  })

  it("security_hold kind has the right capability set", () => {
    const state = lockoutEffectiveState({
      reasonHint: "security_hold",
      retryAfterRaw: null,
      emailHint: null,
      liveLoginError: null,
      liveUserEmail: null,
    })
    expect(state.supportsCountdown).toBe(false)
    expect(state.supportsRetrySignIn).toBe(false)
    expect(state.supportsResetPassword).toBe(true)
    expect(state.supportsContactAdmin).toBe(true)
  })

  it("returned state is frozen", () => {
    const state = lockoutEffectiveState({
      reasonHint: null,
      retryAfterRaw: null,
      emailHint: null,
      liveLoginError: null,
      liveUserEmail: null,
    })
    expect(Object.isFrozen(state)).toBe(true)
  })
})

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  formatRemainingTime
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

describe("AS.7.6 formatRemainingTime — rounding rules", () => {
  it("returns 0s for non-positive / NaN inputs", () => {
    expect(formatRemainingTime(0)).toBe("0s")
    expect(formatRemainingTime(-5)).toBe("0s")
    expect(formatRemainingTime(NaN)).toBe("0s")
    expect(formatRemainingTime(Infinity)).toBe("0s")
  })

  it("renders sub-minute as Ns", () => {
    expect(formatRemainingTime(1)).toBe("1s")
    expect(formatRemainingTime(30)).toBe("30s")
    expect(formatRemainingTime(59)).toBe("59s")
  })

  it("renders >= 60s as M:SS with zero-padding", () => {
    expect(formatRemainingTime(60)).toBe("1:00")
    expect(formatRemainingTime(65)).toBe("1:05")
    expect(formatRemainingTime(125)).toBe("2:05")
    expect(formatRemainingTime(3600)).toBe("60:00")
  })

  it("floors fractional seconds", () => {
    expect(formatRemainingTime(30.7)).toBe("30s")
    expect(formatRemainingTime(60.9)).toBe("1:00")
  })
})

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  retrySignInBlockedReason
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

describe("AS.7.6 retrySignInBlockedReason — gate predicate", () => {
  it("returns kind_unsupported when state disallows retry", () => {
    const state = lockoutEffectiveState({
      reasonHint: "admin_suspended",
      retryAfterRaw: null,
      emailHint: null,
      liveLoginError: null,
      liveUserEmail: null,
    })
    expect(retrySignInBlockedReason({ state, remainingSeconds: null })).toBe(
      "kind_unsupported",
    )
  })

  it("returns countdown_active while remaining > 0", () => {
    const state = lockoutEffectiveState({
      reasonHint: "temporary_lockout",
      retryAfterRaw: "30",
      emailHint: null,
      liveLoginError: null,
      liveUserEmail: null,
    })
    expect(retrySignInBlockedReason({ state, remainingSeconds: 30 })).toBe(
      "countdown_active",
    )
    expect(retrySignInBlockedReason({ state, remainingSeconds: 1 })).toBe(
      "countdown_active",
    )
  })

  it("returns null when retry is supported and timer is zero / null", () => {
    const state = lockoutEffectiveState({
      reasonHint: "temporary_lockout",
      retryAfterRaw: null,
      emailHint: null,
      liveLoginError: null,
      liveUserEmail: null,
    })
    expect(
      retrySignInBlockedReason({ state, remainingSeconds: 0 }),
    ).toBeNull()
    expect(
      retrySignInBlockedReason({ state, remainingSeconds: null }),
    ).toBeNull()
  })

  it("kind_unsupported wins over countdown_active when both apply", () => {
    const state = lockoutEffectiveState({
      reasonHint: "admin_suspended",
      retryAfterRaw: null,
      emailHint: null,
      liveLoginError: null,
      liveUserEmail: null,
    })
    expect(retrySignInBlockedReason({ state, remainingSeconds: 30 })).toBe(
      "kind_unsupported",
    )
  })
})

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  buildContactAdminMailto
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

describe("AS.7.6 buildContactAdminMailto — href shape", () => {
  it("starts with mailto: prefix and admin email", () => {
    const href = buildContactAdminMailto({
      adminEmail: "ops@example.com",
      userEmail: null,
      kind: "temporary_lockout",
    })
    expect(href.startsWith("mailto:ops@example.com?")).toBe(true)
  })

  it("uses the temporary_lockout subject for that kind", () => {
    const href = buildContactAdminMailto({
      adminEmail: "ops@example.com",
      userEmail: null,
      kind: "temporary_lockout",
    })
    expect(decodeURIComponent(href)).toMatch(
      /OmniSight account locked — assistance/,
    )
  })

  it("uses the suspension subject on admin_suspended", () => {
    const href = buildContactAdminMailto({
      adminEmail: "ops@example.com",
      userEmail: null,
      kind: "admin_suspended",
    })
    expect(decodeURIComponent(href)).toMatch(
      /OmniSight account suspension — restore access/,
    )
  })

  it("uses the security-hold subject on security_hold", () => {
    const href = buildContactAdminMailto({
      adminEmail: "ops@example.com",
      userEmail: null,
      kind: "security_hold",
    })
    expect(decodeURIComponent(href)).toMatch(
      /OmniSight account on security hold — assistance/,
    )
  })

  it("includes the user email in the body when supplied", () => {
    const href = buildContactAdminMailto({
      adminEmail: "ops@example.com",
      userEmail: "user@example.com",
      kind: "temporary_lockout",
    })
    expect(decodeURIComponent(href)).toMatch(
      /My account email: user@example.com/,
    )
  })

  it("omits the user-email line when null", () => {
    const href = buildContactAdminMailto({
      adminEmail: "ops@example.com",
      userEmail: null,
      kind: "temporary_lockout",
    })
    expect(decodeURIComponent(href)).not.toMatch(/My account email/)
  })
})
