/**
 * AS.7.3 — `lib/auth/password-reset-helpers.ts` contract tests.
 *
 * Pins the constants byte-equal to the backend, the classifier truth
 * tables for both stages (request → confirm), and the submit-gate
 * predicates' precedence cascades.
 */

import { describe, expect, it } from "vitest"

import {
  FORM_PATH_PASSWORD_RESET,
  PASSWORD_RESET_FORM_PREFIX,
  REQUEST_RESET_BLOCKED_REASONS,
  REQUEST_RESET_ERROR_COPY,
  REQUEST_RESET_ERROR_KIND,
  RESET_PASSWORD_BLOCKED_REASONS,
  RESET_PASSWORD_ERROR_COPY,
  RESET_PASSWORD_ERROR_KIND,
  classifyRequestResetError,
  classifyResetPasswordError,
  parsePasswordResetRetryAfter,
  passwordResetHoneypotFieldName,
  requestResetSubmitBlockedReason,
  resetPasswordSubmitBlockedReason,
} from "@/lib/auth/password-reset-helpers"
import {
  FORM_PREFIXES,
  RARE_WORD_POOL,
} from "@/lib/auth/login-form-helpers"

describe("AS.7.3 password-reset-helpers — constants drift guard", () => {
  it("FORM_PATH_PASSWORD_RESET byte-equal to the backend AS.6.4 path", () => {
    expect(FORM_PATH_PASSWORD_RESET).toBe("/api/v1/auth/password-reset")
  })

  it("PASSWORD_RESET_FORM_PREFIX wired through FORM_PREFIXES['pr_']", () => {
    expect(FORM_PREFIXES[FORM_PATH_PASSWORD_RESET]).toBe("pr_")
    expect(PASSWORD_RESET_FORM_PREFIX).toBe("pr_")
  })

  it("REQUEST_RESET_ERROR_KIND covers the 5 canonical kinds", () => {
    expect(Object.values(REQUEST_RESET_ERROR_KIND)).toEqual([
      "invalid_input",
      "rate_limited",
      "bot_challenge_failed",
      "email_oauth_only",
      "service_unavailable",
    ])
  })

  it("REQUEST_RESET_ERROR_COPY pins the exact UI strings", () => {
    expect(REQUEST_RESET_ERROR_COPY.invalid_input).toBe(
      "Please double-check the email address and try again.",
    )
    expect(REQUEST_RESET_ERROR_COPY.rate_limited).toBe(
      "Too many requests. Please wait a few minutes and retry.",
    )
    expect(REQUEST_RESET_ERROR_COPY.bot_challenge_failed).toBe(
      "Verification failed. Please refresh the page and try again.",
    )
    expect(REQUEST_RESET_ERROR_COPY.email_oauth_only).toBe(
      "This account signs in with a connected provider (Google, GitHub, etc.). Password reset does not apply — open the sign-in page and click your provider button.",
    )
    expect(REQUEST_RESET_ERROR_COPY.service_unavailable).toBe(
      "Password reset is temporarily unavailable. Please try again in a moment.",
    )
  })

  it("RESET_PASSWORD_ERROR_KIND covers the 6 canonical kinds", () => {
    expect(Object.values(RESET_PASSWORD_ERROR_KIND)).toEqual([
      "invalid_token",
      "expired_token",
      "weak_password",
      "rate_limited",
      "bot_challenge_failed",
      "service_unavailable",
    ])
  })

  it("RESET_PASSWORD_ERROR_COPY pins the exact UI strings", () => {
    expect(RESET_PASSWORD_ERROR_COPY.invalid_token).toBe(
      "This reset link is no longer valid. Please request a fresh link from the sign-in page.",
    )
    expect(RESET_PASSWORD_ERROR_COPY.expired_token).toBe(
      "This reset link has expired. Please request a fresh link from the sign-in page.",
    )
    expect(RESET_PASSWORD_ERROR_COPY.weak_password).toBe(
      "This password does not meet the strength requirements. Try a longer or more random one.",
    )
    expect(RESET_PASSWORD_ERROR_COPY.rate_limited).toBe(
      "Too many attempts. Please wait a few minutes and retry.",
    )
    expect(RESET_PASSWORD_ERROR_COPY.bot_challenge_failed).toBe(
      "Verification failed. Please refresh the page and try again.",
    )
    expect(RESET_PASSWORD_ERROR_COPY.service_unavailable).toBe(
      "Password reset is temporarily unavailable. Please try again in a moment.",
    )
  })

  it("REQUEST_RESET_BLOCKED_REASONS pins the 3-row drift-guard tuple", () => {
    expect([...REQUEST_RESET_BLOCKED_REASONS]).toEqual([
      "busy",
      "honeypot_pending",
      "email_invalid",
    ])
  })

  it("RESET_PASSWORD_BLOCKED_REASONS pins the 6-row drift-guard tuple", () => {
    expect([...RESET_PASSWORD_BLOCKED_REASONS]).toEqual([
      "busy",
      "token_missing",
      "honeypot_pending",
      "password_empty",
      "password_weak",
      "password_not_saved",
    ])
  })
})

describe("AS.7.3 passwordResetHoneypotFieldName", () => {
  it("returns a name beginning with 'pr_' from the rare-word pool", async () => {
    const name = await passwordResetHoneypotFieldName()
    expect(name.startsWith("pr_")).toBe(true)
    const tail = name.slice(3)
    expect(RARE_WORD_POOL.includes(tail)).toBe(true)
  })

  it("is deterministic per nowMs (same input → same name)", async () => {
    const fixed = 1_700_000_000_000
    const a = await passwordResetHoneypotFieldName(fixed)
    const b = await passwordResetHoneypotFieldName(fixed)
    expect(a).toBe(b)
  })
})

describe("AS.7.3 parsePasswordResetRetryAfter", () => {
  it("returns null for empty / nullish input", () => {
    expect(parsePasswordResetRetryAfter(null)).toBeNull()
    expect(parsePasswordResetRetryAfter(undefined)).toBeNull()
    expect(parsePasswordResetRetryAfter("")).toBeNull()
    expect(parsePasswordResetRetryAfter("   ")).toBeNull()
  })

  it("parses delta-seconds form", () => {
    expect(parsePasswordResetRetryAfter("30")).toBe(30)
    expect(parsePasswordResetRetryAfter("0")).toBe(0)
    expect(parsePasswordResetRetryAfter(" 60 ")).toBe(60)
  })

  it("rejects negative integers", () => {
    expect(parsePasswordResetRetryAfter("-1")).toBeNull()
  })

  it("parses HTTP-date form", () => {
    const future = new Date(Date.now() + 90_000).toUTCString()
    const got = parsePasswordResetRetryAfter(future)
    expect(got).not.toBeNull()
    expect(got!).toBeGreaterThan(60)
    expect(got!).toBeLessThanOrEqual(90)
  })

  it("returns 0 for past HTTP-date", () => {
    const past = new Date(Date.now() - 60_000).toUTCString()
    expect(parsePasswordResetRetryAfter(past)).toBe(0)
  })

  it("returns null for garbage input", () => {
    expect(parsePasswordResetRetryAfter("not a date")).toBeNull()
  })
})

describe("AS.7.3 classifyRequestResetError — precedence truth table", () => {
  it("422 → invalid_input", () => {
    const o = classifyRequestResetError({ status: 422 })
    expect(o.kind).toBe("invalid_input")
    expect(o.message).toBe(REQUEST_RESET_ERROR_COPY.invalid_input)
    expect(o.retryAfterSeconds).toBeNull()
  })

  it("409 + errorCode=oauth_only → email_oauth_only", () => {
    const o = classifyRequestResetError({
      status: 409,
      errorCode: "oauth_only",
    })
    expect(o.kind).toBe("email_oauth_only")
    expect(o.message).toBe(REQUEST_RESET_ERROR_COPY.email_oauth_only)
  })

  it("409 with no code falls through to service_unavailable", () => {
    const o = classifyRequestResetError({ status: 409 })
    expect(o.kind).toBe("service_unavailable")
  })

  it("429 + errorCode=bot_challenge_failed → bot_challenge_failed", () => {
    const o = classifyRequestResetError({
      status: 429,
      errorCode: "bot_challenge_failed",
      retryAfter: "12",
    })
    expect(o.kind).toBe("bot_challenge_failed")
    expect(o.retryAfterSeconds).toBe(12)
  })

  it("429 (no code) → rate_limited carries Retry-After", () => {
    const o = classifyRequestResetError({ status: 429, retryAfter: "30" })
    expect(o.kind).toBe("rate_limited")
    expect(o.retryAfterSeconds).toBe(30)
  })

  it("500 / 503 / null status → service_unavailable", () => {
    expect(classifyRequestResetError({ status: 500 }).kind).toBe(
      "service_unavailable",
    )
    expect(classifyRequestResetError({ status: 503 }).kind).toBe(
      "service_unavailable",
    )
    expect(classifyRequestResetError({ status: null }).kind).toBe(
      "service_unavailable",
    )
  })

  it("unknown 4xx → service_unavailable (defensive default)", () => {
    expect(classifyRequestResetError({ status: 404 }).kind).toBe(
      "service_unavailable",
    )
    expect(classifyRequestResetError({ status: 418 }).kind).toBe(
      "service_unavailable",
    )
  })
})

describe("AS.7.3 classifyResetPasswordError — precedence truth table", () => {
  it("410 → expired_token", () => {
    const o = classifyResetPasswordError({ status: 410 })
    expect(o.kind).toBe("expired_token")
    expect(o.message).toBe(RESET_PASSWORD_ERROR_COPY.expired_token)
  })

  it("400 + errorCode=expired_token → expired_token", () => {
    const o = classifyResetPasswordError({
      status: 400,
      errorCode: "expired_token",
    })
    expect(o.kind).toBe("expired_token")
  })

  it("400 + errorCode=invalid_token → invalid_token", () => {
    const o = classifyResetPasswordError({
      status: 400,
      errorCode: "invalid_token",
    })
    expect(o.kind).toBe("invalid_token")
  })

  it("400 + errorCode=weak_password → weak_password", () => {
    const o = classifyResetPasswordError({
      status: 400,
      errorCode: "weak_password",
    })
    expect(o.kind).toBe("weak_password")
  })

  it("401 / 404 → invalid_token", () => {
    expect(classifyResetPasswordError({ status: 401 }).kind).toBe(
      "invalid_token",
    )
    expect(classifyResetPasswordError({ status: 404 }).kind).toBe(
      "invalid_token",
    )
  })

  it("429 + errorCode=bot_challenge_failed → bot_challenge_failed", () => {
    const o = classifyResetPasswordError({
      status: 429,
      errorCode: "bot_challenge_failed",
    })
    expect(o.kind).toBe("bot_challenge_failed")
  })

  it("429 (no code) → rate_limited", () => {
    const o = classifyResetPasswordError({ status: 429, retryAfter: "20" })
    expect(o.kind).toBe("rate_limited")
    expect(o.retryAfterSeconds).toBe(20)
  })

  it("500 / null status → service_unavailable", () => {
    expect(classifyResetPasswordError({ status: 500 }).kind).toBe(
      "service_unavailable",
    )
    expect(classifyResetPasswordError({ status: null }).kind).toBe(
      "service_unavailable",
    )
  })

  it("anything else → service_unavailable (defensive default)", () => {
    expect(classifyResetPasswordError({ status: 418 }).kind).toBe(
      "service_unavailable",
    )
  })
})

describe("AS.7.3 requestResetSubmitBlockedReason — precedence", () => {
  const base = {
    email: "user@example.com",
    busy: false,
    honeypotResolved: true,
  }

  it("returns null when every gate cleared", () => {
    expect(requestResetSubmitBlockedReason(base)).toBeNull()
  })

  it("busy short-circuits over every other failure", () => {
    expect(
      requestResetSubmitBlockedReason({
        ...base,
        busy: true,
        email: "",
      }),
    ).toBe("busy")
  })

  it("honeypot_pending blocks before email check", () => {
    expect(
      requestResetSubmitBlockedReason({
        ...base,
        honeypotResolved: false,
        email: "",
      }),
    ).toBe("honeypot_pending")
  })

  it("invalid email is the last gate", () => {
    expect(
      requestResetSubmitBlockedReason({
        ...base,
        email: "not-an-email",
      }),
    ).toBe("email_invalid")
  })
})

describe("AS.7.3 resetPasswordSubmitBlockedReason — precedence", () => {
  const base = {
    token: "abc.def.ghi",
    password: "abc123ABC!@#xyz",
    passwordPasses: true,
    hasSaved: true,
    busy: false,
    honeypotResolved: true,
  }

  it("returns null when every gate cleared", () => {
    expect(resetPasswordSubmitBlockedReason(base)).toBeNull()
  })

  it("busy short-circuits over every other failure", () => {
    expect(
      resetPasswordSubmitBlockedReason({
        ...base,
        busy: true,
        token: "",
        password: "",
      }),
    ).toBe("busy")
  })

  it("token_missing blocks before honeypot check", () => {
    expect(
      resetPasswordSubmitBlockedReason({
        ...base,
        token: "",
        honeypotResolved: false,
      }),
    ).toBe("token_missing")
  })

  it("honeypot_pending blocks before password check", () => {
    expect(
      resetPasswordSubmitBlockedReason({
        ...base,
        honeypotResolved: false,
      }),
    ).toBe("honeypot_pending")
  })

  it("empty password vs weak password are distinct branches", () => {
    expect(
      resetPasswordSubmitBlockedReason({
        ...base,
        password: "",
      }),
    ).toBe("password_empty")
    expect(
      resetPasswordSubmitBlockedReason({
        ...base,
        passwordPasses: false,
      }),
    ).toBe("password_weak")
  })

  it("password_not_saved is the last gate", () => {
    expect(
      resetPasswordSubmitBlockedReason({
        ...base,
        hasSaved: false,
      }),
    ).toBe("password_not_saved")
  })
})
