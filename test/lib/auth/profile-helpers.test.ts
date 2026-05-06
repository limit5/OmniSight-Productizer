/**
 * AS.7.7 — `lib/auth/profile-helpers.ts` contract tests.
 *
 * Pins the section vocabulary, copy table, orbit-layout math,
 * connected-accounts state classifier, auth-methods summary table,
 * password-change submit-gate predicate, password-change error
 * classifier, session row formatter, API-keys visibility gate,
 * delete-account confirmation gate, and the GDPR error classifier.
 */

import { describe, expect, it } from "vitest"

import {
  apiKeysVisibility,
  authMethodsSummary,
  AUTH_METHOD_KIND,
  AUTH_METHOD_KINDS_ORDERED,
  classifyGdprError,
  classifyPasswordChangeError,
  DELETE_ACCOUNT_BLOCKED_REASONS,
  DELETE_ACCOUNT_CONFIRM_PHRASE,
  deleteAccountBlockedReason,
  formatRelativeTime,
  GDPR_ERROR_COPY,
  GDPR_ERROR_KIND,
  oauthOrbitState,
  ORBIT_RADIUS_BY_TIER,
  orbitPositionsForRing,
  PASSWORD_CHANGE_BLOCKED_REASONS,
  PASSWORD_CHANGE_ERROR_COPY,
  PASSWORD_CHANGE_ERROR_KIND,
  passwordChangeBlockedReason,
  PROFILE_SECTION_COPY,
  PROFILE_SECTION_KIND,
  PROFILE_SECTIONS_ORDERED,
  sessionsRowFingerprint,
  shortenUserAgent,
} from "@/lib/auth/profile-helpers"
import { OAUTH_PROVIDER_CATALOG } from "@/lib/auth/oauth-providers"

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Section vocabulary drift guard
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

describe("AS.7.7 profile-helpers — section vocabulary", () => {
  it("PROFILE_SECTION_KIND covers exactly 8 sections", () => {
    expect(Object.values(PROFILE_SECTION_KIND)).toEqual([
      "connected_accounts",
      "auth_methods",
      "auth_providers",
      "mfa_setup",
      "sessions",
      "password_change",
      "api_keys",
      "data_privacy",
    ])
  })

  it("PROFILE_SECTIONS_ORDERED matches the kind tuple", () => {
    expect([...PROFILE_SECTIONS_ORDERED]).toEqual([
      "connected_accounts",
      "auth_methods",
      "auth_providers",
      "mfa_setup",
      "sessions",
      "password_change",
      "api_keys",
      "data_privacy",
    ])
  })

  it("PROFILE_SECTION_COPY pins title + summary for every section", () => {
    for (const kind of PROFILE_SECTIONS_ORDERED) {
      expect(PROFILE_SECTION_COPY[kind].title.length).toBeGreaterThan(0)
      expect(PROFILE_SECTION_COPY[kind].summary.length).toBeGreaterThan(0)
    }
    expect(PROFILE_SECTION_COPY.connected_accounts.title).toBe(
      "Connected accounts",
    )
    expect(PROFILE_SECTION_COPY.auth_providers.title).toBe("Auth providers")
    expect(PROFILE_SECTION_COPY.password_change.title).toBe("Change password")
    expect(PROFILE_SECTION_COPY.data_privacy.title).toBe("Data & privacy")
  })

  it("PROFILE_SECTION_COPY entries are frozen", () => {
    expect(Object.isFrozen(PROFILE_SECTION_COPY)).toBe(true)
    expect(Object.isFrozen(PROFILE_SECTION_COPY.connected_accounts)).toBe(true)
  })
})

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Orbit layout math
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

describe("AS.7.7 profile-helpers — orbit layout", () => {
  it("ORBIT_RADIUS_BY_TIER pins canonical inner / outer radii", () => {
    expect(ORBIT_RADIUS_BY_TIER.inner).toBe(110)
    expect(ORBIT_RADIUS_BY_TIER.outer).toBe(165)
  })

  it("orbitPositionsForRing returns empty list for empty providers", () => {
    expect(orbitPositionsForRing([], "inner", true)).toEqual([])
  })

  it("orbitPositionsForRing places one provider at 12 o'clock", () => {
    const [google] = OAUTH_PROVIDER_CATALOG
    const positions = orbitPositionsForRing([google], "inner", true)
    expect(positions).toHaveLength(1)
    expect(positions[0].xPx).toBeCloseTo(0, 5)
    expect(positions[0].yPx).toBeCloseTo(-110, 5)
    expect(positions[0].ring).toBe("inner")
    expect(positions[0].isLinked).toBe(true)
  })

  it("orbitPositionsForRing evenly spaces 4 providers in 90° steps", () => {
    const four = OAUTH_PROVIDER_CATALOG.slice(0, 4)
    const positions = orbitPositionsForRing(four, "outer", false)
    expect(positions).toHaveLength(4)
    // 12 o'clock
    expect(positions[0].yPx).toBeCloseTo(-165, 5)
    // 3 o'clock
    expect(positions[1].xPx).toBeCloseTo(165, 5)
    // 6 o'clock
    expect(positions[2].yPx).toBeCloseTo(165, 5)
    // 9 o'clock
    expect(positions[3].xPx).toBeCloseTo(-165, 5)
  })

  it("orbitPositionsForRing applies an angle offset", () => {
    const two = OAUTH_PROVIDER_CATALOG.slice(0, 2)
    const positions = orbitPositionsForRing(
      two,
      "inner",
      true,
      Math.PI / 2,
    )
    // First satellite shifted to 3 o'clock by the offset.
    expect(positions[0].xPx).toBeCloseTo(110, 5)
    expect(positions[0].yPx).toBeCloseTo(0, 5)
  })

  it("oauthOrbitState partitions linked vs available", () => {
    const state = oauthOrbitState({
      linked: [{ provider: "google" }, { provider: "github" }],
    })
    expect(state.linkedCount).toBe(2)
    expect(state.availableCount).toBe(OAUTH_PROVIDER_CATALOG.length - 2)
    expect(state.innerRing.map((p) => p.id)).toEqual(["google", "github"])
    expect(state.outerRing.map((p) => p.id)).not.toContain("google")
    expect(state.outerRing.map((p) => p.id)).not.toContain("github")
  })

  it("oauthOrbitState ignores unknown provider ids from the backend", () => {
    const state = oauthOrbitState({
      linked: [{ provider: "google" }, { provider: "myspace" }],
    })
    // Only google counts as linked (myspace isn't in the catalog).
    expect(state.linkedCount).toBe(1)
    expect(state.availableCount).toBe(OAUTH_PROVIDER_CATALOG.length - 1)
  })

  it("oauthOrbitState treats empty linked list as all-available", () => {
    const state = oauthOrbitState({ linked: [] })
    expect(state.linkedCount).toBe(0)
    expect(state.availableCount).toBe(OAUTH_PROVIDER_CATALOG.length)
    expect(state.innerRing).toHaveLength(0)
    expect(state.outerRing).toHaveLength(OAUTH_PROVIDER_CATALOG.length)
  })

  it("oauthOrbitState lower-cases backend provider strings", () => {
    const state = oauthOrbitState({
      linked: [{ provider: "GOOGLE" }, { provider: "  github  " }],
    })
    expect(state.linkedCount).toBe(2)
  })
})

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Auth methods summary
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

describe("AS.7.7 profile-helpers — authMethodsSummary", () => {
  it("AUTH_METHOD_KIND covers 5 method kinds", () => {
    expect(Object.values(AUTH_METHOD_KIND)).toEqual([
      "password",
      "oauth",
      "passkey",
      "totp",
      "backup_code",
    ])
  })

  it("AUTH_METHOD_KINDS_ORDERED matches AUTH_METHOD_KIND", () => {
    expect([...AUTH_METHOD_KINDS_ORDERED]).toEqual([
      "password",
      "oauth",
      "passkey",
      "totp",
      "backup_code",
    ])
  })

  it("returns 5 rows with correct enabled flags for the active path", () => {
    const rows = authMethodsSummary({
      hasPassword: true,
      linkedOAuth: [{ provider: "google" }],
      hasTotp: true,
      hasPasskey: false,
      backupCodesRemaining: 8,
    })
    expect(rows).toHaveLength(5)
    expect(rows[0]).toMatchObject({ kind: "password", enabled: true })
    expect(rows[1]).toMatchObject({ kind: "oauth", enabled: true })
    expect(rows[2]).toMatchObject({ kind: "passkey", enabled: false })
    expect(rows[3]).toMatchObject({ kind: "totp", enabled: true })
    expect(rows[4]).toMatchObject({ kind: "backup_code", enabled: true })
  })

  it("disables every row in the empty-state path", () => {
    const rows = authMethodsSummary({
      hasPassword: false,
      linkedOAuth: [],
      hasTotp: false,
      hasPasskey: false,
      backupCodesRemaining: 0,
    })
    expect(rows.every((r) => !r.enabled)).toBe(true)
  })

  it("renders backup-code loading hint when remaining is null", () => {
    const rows = authMethodsSummary({
      hasPassword: true,
      linkedOAuth: [],
      hasTotp: true,
      hasPasskey: false,
      backupCodesRemaining: null,
    })
    expect(rows[4].hint).toMatch(/Loading/i)
  })

  it("formats the OAuth count line for plural / singular", () => {
    const single = authMethodsSummary({
      hasPassword: true,
      linkedOAuth: [{ provider: "google" }],
      hasTotp: false,
      hasPasskey: false,
      backupCodesRemaining: 0,
    })
    expect(single[1].hint).toMatch(/1 provider linked/)
    const plural = authMethodsSummary({
      hasPassword: true,
      linkedOAuth: [{ provider: "google" }, { provider: "github" }],
      hasTotp: false,
      hasPasskey: false,
      backupCodesRemaining: 0,
    })
    expect(plural[1].hint).toMatch(/2 providers linked/)
  })
})

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Password-change submit gate
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

describe("AS.7.7 profile-helpers — passwordChangeBlockedReason", () => {
  const ok = {
    busy: false,
    currentPassword: "old-password",
    newPassword: "new-password-12chars",
    newPasswordSaved: true,
  }

  it("returns null when every gate has cleared", () => {
    expect(passwordChangeBlockedReason(ok)).toBeNull()
  })

  it("flags busy first", () => {
    expect(passwordChangeBlockedReason({ ...ok, busy: true })).toBe("busy")
  })

  it("flags missing current password", () => {
    expect(
      passwordChangeBlockedReason({ ...ok, currentPassword: " " }),
    ).toBe("current_password_missing")
  })

  it("flags too-short new password", () => {
    expect(
      passwordChangeBlockedReason({ ...ok, newPassword: "tooshort" }),
    ).toBe("new_password_too_short")
  })

  it("flags reuse of current password (when both pass length gate)", () => {
    expect(
      passwordChangeBlockedReason({
        ...ok,
        newPassword: "same-very-long-password",
        currentPassword: "same-very-long-password",
      }),
    ).toBe("new_password_same_as_current")
  })

  it("flags unsaved acknowledgement", () => {
    expect(
      passwordChangeBlockedReason({ ...ok, newPasswordSaved: false }),
    ).toBe("password_not_saved")
  })

  it("PASSWORD_CHANGE_BLOCKED_REASONS pins the 5-tuple drift guard", () => {
    expect([...PASSWORD_CHANGE_BLOCKED_REASONS]).toEqual([
      "busy",
      "current_password_missing",
      "new_password_too_short",
      "new_password_same_as_current",
      "password_not_saved",
    ])
  })
})

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Password-change error classifier
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

describe("AS.7.7 profile-helpers — classifyPasswordChangeError", () => {
  it("PASSWORD_CHANGE_ERROR_KIND covers 4 kinds", () => {
    expect(Object.values(PASSWORD_CHANGE_ERROR_KIND)).toEqual([
      "invalid_current_password",
      "weak_password",
      "rate_limited",
      "service_unavailable",
    ])
  })

  it("classifies 401 to invalid_current_password", () => {
    expect(classifyPasswordChangeError({ status: 401 }).kind).toBe(
      "invalid_current_password",
    )
  })

  it("classifies 422 to weak_password", () => {
    expect(classifyPasswordChangeError({ status: 422 }).kind).toBe(
      "weak_password",
    )
  })

  it("classifies 429 to rate_limited and parses retry-after", () => {
    const o = classifyPasswordChangeError({
      status: 429,
      retryAfter: "30",
    })
    expect(o.kind).toBe("rate_limited")
    expect(o.retryAfterSeconds).toBe(30)
  })

  it("classifies 500 / null status to service_unavailable", () => {
    expect(classifyPasswordChangeError({ status: 500 }).kind).toBe(
      "service_unavailable",
    )
    expect(classifyPasswordChangeError({ status: null }).kind).toBe(
      "service_unavailable",
    )
  })

  it("PASSWORD_CHANGE_ERROR_COPY pins canonical strings", () => {
    expect(PASSWORD_CHANGE_ERROR_COPY.invalid_current_password).toMatch(
      /current password didn't match/i,
    )
    expect(PASSWORD_CHANGE_ERROR_COPY.weak_password).toMatch(
      /too weak|recently/i,
    )
  })
})

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Session row formatter
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

describe("AS.7.7 profile-helpers — session row helpers", () => {
  it("sessionsRowFingerprint returns the token hint", () => {
    expect(
      sessionsRowFingerprint({
        tokenHint: "abc123",
        createdAt: 0,
        lastSeenAt: 0,
        ip: "0.0.0.0",
        userAgent: "",
        isCurrent: false,
      }),
    ).toBe("abc123")
  })

  it("formatRelativeTime — just-now / minutes / hours / days", () => {
    const now = 1_000_000
    expect(formatRelativeTime(now - 30, now)).toBe("just now")
    expect(formatRelativeTime(now - 120, now)).toBe("2 min ago")
    expect(formatRelativeTime(now - 3600 * 3, now)).toBe("3 h ago")
    expect(formatRelativeTime(now - 86400 * 4, now)).toBe("4 d ago")
    // Negative (clock skew) clamps to "just now"
    expect(formatRelativeTime(now + 30, now)).toBe("just now")
  })

  it("shortenUserAgent truncates long strings + handles empty", () => {
    expect(shortenUserAgent("")).toBe("Unknown device")
    expect(shortenUserAgent("Mozilla/5.0 (X11)")).toBe("Mozilla/5.0 (X11)")
    const long = "a".repeat(200)
    const truncated = shortenUserAgent(long)
    expect(truncated.endsWith("…")).toBe(true)
    expect(truncated.length).toBeLessThanOrEqual(60)
  })
})

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  API keys visibility gate
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

describe("AS.7.7 profile-helpers — apiKeysVisibility", () => {
  it("admin role gets the visible card", () => {
    expect(apiKeysVisibility({ userRole: "admin" })).toEqual({
      visible: true,
      reason: "ok",
    })
  })

  it("owner / super_admin also pass the gate", () => {
    expect(apiKeysVisibility({ userRole: "owner" }).visible).toBe(true)
    expect(apiKeysVisibility({ userRole: "super_admin" }).visible).toBe(true)
  })

  it("operator / member roles get the disabled card with not_admin reason", () => {
    expect(apiKeysVisibility({ userRole: "operator" })).toEqual({
      visible: false,
      reason: "not_admin",
    })
    expect(apiKeysVisibility({ userRole: "member" }).reason).toBe("not_admin")
  })

  it("null / empty role gets no_session reason", () => {
    expect(apiKeysVisibility({ userRole: null })).toEqual({
      visible: false,
      reason: "no_session",
    })
    expect(apiKeysVisibility({ userRole: "" }).reason).toBe("no_session")
  })

  it("role check is case-insensitive + trims whitespace", () => {
    expect(apiKeysVisibility({ userRole: "  Admin  " }).visible).toBe(true)
  })
})

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Delete-account confirmation gate
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

describe("AS.7.7 profile-helpers — deleteAccountBlockedReason", () => {
  const ok = {
    busy: false,
    typedConfirmation: DELETE_ACCOUNT_CONFIRM_PHRASE,
    acknowledgedIrreversible: true,
  }

  it("returns null when every gate has cleared", () => {
    expect(deleteAccountBlockedReason(ok)).toBeNull()
  })

  it("flags busy first", () => {
    expect(deleteAccountBlockedReason({ ...ok, busy: true })).toBe("busy")
  })

  it("flags confirmation mismatch (typo / lowercase)", () => {
    expect(
      deleteAccountBlockedReason({ ...ok, typedConfirmation: "delete" }),
    ).toBe("confirmation_mismatch")
    expect(
      deleteAccountBlockedReason({ ...ok, typedConfirmation: "" }),
    ).toBe("confirmation_mismatch")
  })

  it("flags un-acknowledged irreversible checkbox", () => {
    expect(
      deleteAccountBlockedReason({ ...ok, acknowledgedIrreversible: false }),
    ).toBe("irreversible_unacknowledged")
  })

  it("DELETE_ACCOUNT_BLOCKED_REASONS pins the 3-tuple drift guard", () => {
    expect([...DELETE_ACCOUNT_BLOCKED_REASONS]).toEqual([
      "busy",
      "confirmation_mismatch",
      "irreversible_unacknowledged",
    ])
  })

  it("DELETE_ACCOUNT_CONFIRM_PHRASE pins canonical phrase", () => {
    expect(DELETE_ACCOUNT_CONFIRM_PHRASE).toBe("DELETE")
  })
})

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  GDPR error classifier
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

describe("AS.7.7 profile-helpers — classifyGdprError", () => {
  it("GDPR_ERROR_KIND covers 4 kinds", () => {
    expect(Object.values(GDPR_ERROR_KIND)).toEqual([
      "not_implemented",
      "rate_limited",
      "service_unavailable",
      "unauthorised",
    ])
  })

  it("GDPR_ERROR_COPY pins canonical strings", () => {
    expect(GDPR_ERROR_COPY.not_implemented).toMatch(/isn't available/i)
    expect(GDPR_ERROR_COPY.unauthorised).toMatch(/session expired/i)
  })

  it("classifies 401 / 403 to unauthorised", () => {
    expect(classifyGdprError({ status: 401 }).kind).toBe("unauthorised")
    expect(classifyGdprError({ status: 403 }).kind).toBe("unauthorised")
  })

  it("classifies 404 / 405 / 501 to not_implemented", () => {
    expect(classifyGdprError({ status: 404 }).kind).toBe("not_implemented")
    expect(classifyGdprError({ status: 405 }).kind).toBe("not_implemented")
    expect(classifyGdprError({ status: 501 }).kind).toBe("not_implemented")
  })

  it("classifies 429 to rate_limited and parses retry-after", () => {
    const o = classifyGdprError({ status: 429, retryAfter: "60" })
    expect(o.kind).toBe("rate_limited")
    expect(o.retryAfterSeconds).toBe(60)
  })

  it("classifies 500 / unknown status to service_unavailable", () => {
    expect(classifyGdprError({ status: 500 }).kind).toBe("service_unavailable")
    expect(classifyGdprError({ status: null }).kind).toBe(
      "service_unavailable",
    )
  })
})
