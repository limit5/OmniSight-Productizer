/**
 * AS.7.5 — `lib/auth/email-verify-helpers.ts` contract tests.
 *
 * Pins the constants byte-equal to the backend, the classifier truth
 * tables for both stages (verify-token → resend), and the submit-gate
 * predicate's precedence cascade.
 */

import { describe, expect, it } from "vitest"

import {
  EMAIL_VERIFY_ERROR_COPY,
  EMAIL_VERIFY_ERROR_KIND,
  FORM_PATH_RESEND_VERIFY_EMAIL,
  FORM_PATH_VERIFY_EMAIL,
  RESEND_VERIFY_EMAIL_BLOCKED_REASONS,
  RESEND_VERIFY_EMAIL_ERROR_COPY,
  RESEND_VERIFY_EMAIL_ERROR_KIND,
  classifyEmailVerifyError,
  classifyResendVerifyEmailError,
  parseEmailVerifyRetryAfter,
  resendVerifyEmailSubmitBlockedReason,
} from "@/lib/auth/email-verify-helpers"

describe("AS.7.5 email-verify-helpers — constants drift guard", () => {
  it("FORM_PATH_VERIFY_EMAIL byte-equal to the backend canonical path", () => {
    expect(FORM_PATH_VERIFY_EMAIL).toBe("/api/v1/auth/verify-email")
  })

  it("FORM_PATH_RESEND_VERIFY_EMAIL byte-equal to the backend canonical path", () => {
    expect(FORM_PATH_RESEND_VERIFY_EMAIL).toBe(
      "/api/v1/auth/verify-email/resend",
    )
  })

  it("EMAIL_VERIFY_ERROR_KIND covers the 6 canonical kinds", () => {
    expect(Object.values(EMAIL_VERIFY_ERROR_KIND)).toEqual([
      "invalid_token",
      "expired_token",
      "already_verified",
      "rate_limited",
      "bot_challenge_failed",
      "service_unavailable",
    ])
  })

  it("EMAIL_VERIFY_ERROR_COPY pins the exact UI strings", () => {
    expect(EMAIL_VERIFY_ERROR_COPY.invalid_token).toBe(
      "This verification link is no longer valid. Request a fresh link below to continue.",
    )
    expect(EMAIL_VERIFY_ERROR_COPY.expired_token).toBe(
      "This verification link has expired. Request a fresh link below — we'll send a new one to your email.",
    )
    expect(EMAIL_VERIFY_ERROR_COPY.already_verified).toBe(
      "This email is already verified. You can sign in now.",
    )
    expect(EMAIL_VERIFY_ERROR_COPY.rate_limited).toBe(
      "Too many attempts. Please wait a few minutes and retry.",
    )
    expect(EMAIL_VERIFY_ERROR_COPY.bot_challenge_failed).toBe(
      "Verification failed. Please refresh the page and try again.",
    )
    expect(EMAIL_VERIFY_ERROR_COPY.service_unavailable).toBe(
      "Email verification is temporarily unavailable. Please try again in a moment.",
    )
  })

  it("RESEND_VERIFY_EMAIL_ERROR_KIND covers the 5 canonical kinds", () => {
    expect(Object.values(RESEND_VERIFY_EMAIL_ERROR_KIND)).toEqual([
      "invalid_input",
      "already_verified",
      "rate_limited",
      "bot_challenge_failed",
      "service_unavailable",
    ])
  })

  it("RESEND_VERIFY_EMAIL_ERROR_COPY pins the exact UI strings", () => {
    expect(RESEND_VERIFY_EMAIL_ERROR_COPY.invalid_input).toBe(
      "Please double-check the email address and try again.",
    )
    expect(RESEND_VERIFY_EMAIL_ERROR_COPY.already_verified).toBe(
      "This email is already verified. You can sign in now.",
    )
    expect(RESEND_VERIFY_EMAIL_ERROR_COPY.rate_limited).toBe(
      "Too many requests. Please wait a few minutes and retry.",
    )
    expect(RESEND_VERIFY_EMAIL_ERROR_COPY.bot_challenge_failed).toBe(
      "Verification failed. Please refresh the page and try again.",
    )
    expect(RESEND_VERIFY_EMAIL_ERROR_COPY.service_unavailable).toBe(
      "Email verification is temporarily unavailable. Please try again in a moment.",
    )
  })

  it("RESEND_VERIFY_EMAIL_BLOCKED_REASONS pins the drift-guard tuple", () => {
    expect([...RESEND_VERIFY_EMAIL_BLOCKED_REASONS]).toEqual([
      "busy",
      "email_invalid",
    ])
  })
})

describe("AS.7.5 parseEmailVerifyRetryAfter", () => {
  it("returns null on empty / missing input", () => {
    expect(parseEmailVerifyRetryAfter(null)).toBeNull()
    expect(parseEmailVerifyRetryAfter(undefined)).toBeNull()
    expect(parseEmailVerifyRetryAfter("")).toBeNull()
    expect(parseEmailVerifyRetryAfter("   ")).toBeNull()
  })

  it("parses delta-seconds form", () => {
    expect(parseEmailVerifyRetryAfter("30")).toBe(30)
    expect(parseEmailVerifyRetryAfter("0")).toBe(0)
    expect(parseEmailVerifyRetryAfter(" 60 ")).toBe(60)
  })

  it("rejects negative delta-seconds outright (RFC 9110 §10.2.3)", () => {
    expect(parseEmailVerifyRetryAfter("-5")).toBeNull()
  })

  it("parses HTTP-date form into seconds-from-now", () => {
    const future = new Date(Date.now() + 30_000).toUTCString()
    const parsed = parseEmailVerifyRetryAfter(future)
    expect(parsed).not.toBeNull()
    expect(parsed!).toBeGreaterThanOrEqual(29)
    expect(parsed!).toBeLessThanOrEqual(31)
  })

  it("clamps past HTTP-date to 0", () => {
    const past = new Date(Date.now() - 60_000).toUTCString()
    expect(parseEmailVerifyRetryAfter(past)).toBe(0)
  })

  it("returns null on unparseable garbage", () => {
    expect(parseEmailVerifyRetryAfter("not-a-date")).toBeNull()
  })
})

describe("AS.7.5 classifyEmailVerifyError truth table", () => {
  it("410 → expired_token", () => {
    const out = classifyEmailVerifyError({
      status: 410,
      errorCode: null,
      retryAfter: null,
    })
    expect(out.kind).toBe("expired_token")
    expect(out.message).toBe(EMAIL_VERIFY_ERROR_COPY.expired_token)
    expect(out.retryAfterSeconds).toBeNull()
  })

  it("400 + errorCode=expired_token → expired_token", () => {
    const out = classifyEmailVerifyError({
      status: 400,
      errorCode: "expired_token",
      retryAfter: null,
    })
    expect(out.kind).toBe("expired_token")
  })

  it("400 + errorCode=invalid_token → invalid_token", () => {
    const out = classifyEmailVerifyError({
      status: 400,
      errorCode: "invalid_token",
      retryAfter: null,
    })
    expect(out.kind).toBe("invalid_token")
  })

  it("409 + errorCode=already_verified → already_verified", () => {
    const out = classifyEmailVerifyError({
      status: 409,
      errorCode: "already_verified",
      retryAfter: null,
    })
    expect(out.kind).toBe("already_verified")
    expect(out.message).toBe(EMAIL_VERIFY_ERROR_COPY.already_verified)
  })

  it("401 → invalid_token", () => {
    const out = classifyEmailVerifyError({
      status: 401,
      errorCode: null,
      retryAfter: null,
    })
    expect(out.kind).toBe("invalid_token")
  })

  it("404 → invalid_token", () => {
    const out = classifyEmailVerifyError({
      status: 404,
      errorCode: null,
      retryAfter: null,
    })
    expect(out.kind).toBe("invalid_token")
  })

  it("429 + errorCode=bot_challenge_failed → bot_challenge_failed", () => {
    const out = classifyEmailVerifyError({
      status: 429,
      errorCode: "bot_challenge_failed",
      retryAfter: "12",
    })
    expect(out.kind).toBe("bot_challenge_failed")
    expect(out.retryAfterSeconds).toBe(12)
  })

  it("429 → rate_limited", () => {
    const out = classifyEmailVerifyError({
      status: 429,
      errorCode: null,
      retryAfter: "30",
    })
    expect(out.kind).toBe("rate_limited")
    expect(out.retryAfterSeconds).toBe(30)
  })

  it("500 → service_unavailable", () => {
    const out = classifyEmailVerifyError({
      status: 500,
      errorCode: null,
      retryAfter: null,
    })
    expect(out.kind).toBe("service_unavailable")
  })

  it("null status → service_unavailable", () => {
    const out = classifyEmailVerifyError({
      status: null,
      errorCode: null,
      retryAfter: null,
    })
    expect(out.kind).toBe("service_unavailable")
  })

  it("unknown 4xx → service_unavailable defensive default", () => {
    const out = classifyEmailVerifyError({
      status: 418,
      errorCode: null,
      retryAfter: null,
    })
    expect(out.kind).toBe("service_unavailable")
  })

  it("400 without errorCode → service_unavailable defensive", () => {
    const out = classifyEmailVerifyError({
      status: 400,
      errorCode: null,
      retryAfter: null,
    })
    expect(out.kind).toBe("service_unavailable")
  })
})

describe("AS.7.5 classifyResendVerifyEmailError truth table", () => {
  it("422 → invalid_input", () => {
    const out = classifyResendVerifyEmailError({
      status: 422,
      errorCode: null,
      retryAfter: null,
    })
    expect(out.kind).toBe("invalid_input")
  })

  it("409 + errorCode=already_verified → already_verified", () => {
    const out = classifyResendVerifyEmailError({
      status: 409,
      errorCode: "already_verified",
      retryAfter: null,
    })
    expect(out.kind).toBe("already_verified")
    expect(out.message).toBe(
      RESEND_VERIFY_EMAIL_ERROR_COPY.already_verified,
    )
  })

  it("429 + errorCode=bot_challenge_failed → bot_challenge_failed", () => {
    const out = classifyResendVerifyEmailError({
      status: 429,
      errorCode: "bot_challenge_failed",
      retryAfter: "5",
    })
    expect(out.kind).toBe("bot_challenge_failed")
    expect(out.retryAfterSeconds).toBe(5)
  })

  it("429 → rate_limited", () => {
    const out = classifyResendVerifyEmailError({
      status: 429,
      errorCode: null,
      retryAfter: "60",
    })
    expect(out.kind).toBe("rate_limited")
    expect(out.retryAfterSeconds).toBe(60)
  })

  it("503 → service_unavailable", () => {
    const out = classifyResendVerifyEmailError({
      status: 503,
      errorCode: null,
      retryAfter: null,
    })
    expect(out.kind).toBe("service_unavailable")
  })

  it("null status → service_unavailable", () => {
    const out = classifyResendVerifyEmailError({
      status: null,
      errorCode: null,
      retryAfter: null,
    })
    expect(out.kind).toBe("service_unavailable")
  })

  it("unknown 4xx → service_unavailable defensive default", () => {
    const out = classifyResendVerifyEmailError({
      status: 418,
      errorCode: null,
      retryAfter: null,
    })
    expect(out.kind).toBe("service_unavailable")
  })

  it("409 without already_verified errorCode → service_unavailable defensive", () => {
    const out = classifyResendVerifyEmailError({
      status: 409,
      errorCode: null,
      retryAfter: null,
    })
    expect(out.kind).toBe("service_unavailable")
  })
})

describe("AS.7.5 resendVerifyEmailSubmitBlockedReason precedence", () => {
  it("returns null when every gate clears", () => {
    expect(
      resendVerifyEmailSubmitBlockedReason({
        email: "user@example.com",
        busy: false,
      }),
    ).toBeNull()
  })

  it("busy beats every other gate", () => {
    expect(
      resendVerifyEmailSubmitBlockedReason({
        email: "",
        busy: true,
      }),
    ).toBe("busy")
  })

  it("empty email → email_invalid", () => {
    expect(
      resendVerifyEmailSubmitBlockedReason({
        email: "",
        busy: false,
      }),
    ).toBe("email_invalid")
  })

  it("malformed email (no @) → email_invalid", () => {
    expect(
      resendVerifyEmailSubmitBlockedReason({
        email: "not-an-email",
        busy: false,
      }),
    ).toBe("email_invalid")
  })

  it("malformed email (no domain dot) → email_invalid", () => {
    expect(
      resendVerifyEmailSubmitBlockedReason({
        email: "user@localhost",
        busy: false,
      }),
    ).toBe("email_invalid")
  })

  it("malformed email (multiple @) → email_invalid", () => {
    expect(
      resendVerifyEmailSubmitBlockedReason({
        email: "user@@example.com",
        busy: false,
      }),
    ).toBe("email_invalid")
  })

  it("super-long email → email_invalid", () => {
    const email = "a".repeat(255) + "@example.com"
    expect(
      resendVerifyEmailSubmitBlockedReason({
        email,
        busy: false,
      }),
    ).toBe("email_invalid")
  })
})
