/**
 * AS.7.2 — `lib/auth/signup-form-helpers.ts` contract tests.
 *
 * Pins:
 *   - FORM_PATH_SIGNUP byte-equal to backend AS.6.4 constant
 *   - SIGNUP_FORM_PREFIX wired through `FORM_PREFIXES["sg_"]`
 *   - signupHoneypotFieldName produces a valid `sg_*` name
 *   - parseSignupRetryAfter handles delta-seconds + HTTP-date
 *   - classifySignupError truth table (6 kinds × representative status)
 *   - SIGNUP_ERROR_COPY string table
 *   - looksLikeEmail simple positive / negative cases
 *   - signupSubmitBlockedReason precedence
 *   - SIGNUP_BLOCKED_REASONS drift guard
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import {
  FORM_PATH_SIGNUP,
  SIGNUP_BLOCKED_REASONS,
  SIGNUP_EMAIL_REGEX,
  SIGNUP_ERROR_COPY,
  SIGNUP_ERROR_KIND,
  SIGNUP_FORM_PREFIX,
  classifySignupError,
  looksLikeEmail,
  parseSignupRetryAfter,
  signupHoneypotFieldName,
  signupSubmitBlockedReason,
} from "@/lib/auth/signup-form-helpers"
import { FORM_PREFIXES, RARE_WORD_POOL } from "@/lib/auth/login-form-helpers"

describe("AS.7.2 signup-form-helpers — constants drift guard", () => {
  it("FORM_PATH_SIGNUP byte-equal to the backend AS.6.4 path", () => {
    expect(FORM_PATH_SIGNUP).toBe("/api/v1/auth/signup")
  })

  it("SIGNUP_FORM_PREFIX wired through FORM_PREFIXES['sg_']", () => {
    expect(FORM_PREFIXES[FORM_PATH_SIGNUP]).toBe("sg_")
    expect(SIGNUP_FORM_PREFIX).toBe("sg_")
  })

  it("SIGNUP_ERROR_KIND covers 6 canonical kinds", () => {
    expect(Object.values(SIGNUP_ERROR_KIND)).toEqual([
      "invalid_input",
      "weak_password",
      "rate_limited",
      "bot_challenge_failed",
      "registration_failed",
      "service_unavailable",
    ])
  })

  it("SIGNUP_ERROR_COPY pins the exact UI strings", () => {
    expect(SIGNUP_ERROR_COPY.invalid_input).toBe(
      "Please double-check the email address and try again.",
    )
    expect(SIGNUP_ERROR_COPY.weak_password).toBe(
      "This password does not meet the strength requirements. Try a longer or more random one.",
    )
    expect(SIGNUP_ERROR_COPY.rate_limited).toBe(
      "Too many signup attempts. Please wait a few minutes and retry.",
    )
    expect(SIGNUP_ERROR_COPY.bot_challenge_failed).toBe(
      "Verification failed. Please refresh the page and try again.",
    )
    expect(SIGNUP_ERROR_COPY.registration_failed).toBe(
      "Sign-up could not be completed. Please try again.",
    )
    expect(SIGNUP_ERROR_COPY.service_unavailable).toBe(
      "Sign-up is temporarily unavailable. Please try again in a moment.",
    )
  })

  it("SIGNUP_BLOCKED_REASONS pins the 7-row drift-guard tuple", () => {
    expect([...SIGNUP_BLOCKED_REASONS]).toEqual([
      "busy",
      "honeypot_pending",
      "email_invalid",
      "password_empty",
      "password_weak",
      "password_not_saved",
      "tos_not_accepted",
    ])
  })
})

describe("AS.7.2 signupHoneypotFieldName", () => {
  it("returns a name beginning with 'sg_' from the rare-word pool", async () => {
    // Web Crypto is available in jsdom + node 20; this hits the
    // real digest path.
    const name = await signupHoneypotFieldName()
    expect(name.startsWith("sg_")).toBe(true)
    const tail = name.slice(3)
    expect(RARE_WORD_POOL.includes(tail)).toBe(true)
  })

  it("is deterministic per nowMs (same input → same name)", async () => {
    const fixed = 1_700_000_000_000  // arbitrary frozen instant
    const a = await signupHoneypotFieldName(fixed)
    const b = await signupHoneypotFieldName(fixed)
    expect(a).toBe(b)
  })
})

describe("AS.7.2 parseSignupRetryAfter", () => {
  it("returns null for empty / nullish input", () => {
    expect(parseSignupRetryAfter(null)).toBeNull()
    expect(parseSignupRetryAfter(undefined)).toBeNull()
    expect(parseSignupRetryAfter("")).toBeNull()
    expect(parseSignupRetryAfter("   ")).toBeNull()
  })

  it("parses delta-seconds form", () => {
    expect(parseSignupRetryAfter("30")).toBe(30)
    expect(parseSignupRetryAfter("0")).toBe(0)
    expect(parseSignupRetryAfter(" 60 ")).toBe(60)
  })

  it("rejects negative integers (RFC §10.2.3)", () => {
    expect(parseSignupRetryAfter("-1")).toBeNull()
  })

  it("parses HTTP-date form", () => {
    const future = new Date(Date.now() + 90_000).toUTCString()
    const got = parseSignupRetryAfter(future)
    expect(got).not.toBeNull()
    expect(got!).toBeGreaterThan(60)
    expect(got!).toBeLessThanOrEqual(90)
  })

  it("returns 0 for past HTTP-date", () => {
    const past = new Date(Date.now() - 60_000).toUTCString()
    expect(parseSignupRetryAfter(past)).toBe(0)
  })

  it("returns null for garbage input", () => {
    expect(parseSignupRetryAfter("not a date")).toBeNull()
  })
})

describe("AS.7.2 classifySignupError — precedence truth table", () => {
  it("422 → invalid_input", () => {
    const o = classifySignupError({ status: 422 })
    expect(o.kind).toBe("invalid_input")
    expect(o.message).toBe(SIGNUP_ERROR_COPY.invalid_input)
    expect(o.retryAfterSeconds).toBeNull()
  })

  it("400 + errorCode=weak_password → weak_password", () => {
    const o = classifySignupError({
      status: 400,
      errorCode: "weak_password",
    })
    expect(o.kind).toBe("weak_password")
  })

  it("400 with no code falls through to registration_failed", () => {
    const o = classifySignupError({ status: 400 })
    expect(o.kind).toBe("registration_failed")
  })

  it("429 + errorCode=bot_challenge_failed → bot_challenge_failed", () => {
    const o = classifySignupError({
      status: 429,
      errorCode: "bot_challenge_failed",
      retryAfter: "12",
    })
    expect(o.kind).toBe("bot_challenge_failed")
    expect(o.retryAfterSeconds).toBe(12)
  })

  it("429 (no code) → rate_limited carries Retry-After", () => {
    const o = classifySignupError({ status: 429, retryAfter: "30" })
    expect(o.kind).toBe("rate_limited")
    expect(o.retryAfterSeconds).toBe(30)
  })

  it("409 → registration_failed (enum-resist contract)", () => {
    const o = classifySignupError({ status: 409 })
    expect(o.kind).toBe("registration_failed")
  })

  it("500 / null status → service_unavailable", () => {
    expect(classifySignupError({ status: 500 }).kind).toBe(
      "service_unavailable",
    )
    expect(classifySignupError({ status: 503 }).kind).toBe(
      "service_unavailable",
    )
    expect(classifySignupError({ status: null }).kind).toBe(
      "service_unavailable",
    )
  })

  it("anything else → registration_failed (defensive default)", () => {
    expect(classifySignupError({ status: 418 }).kind).toBe(
      "registration_failed",
    )
    expect(classifySignupError({ status: 404 }).kind).toBe(
      "registration_failed",
    )
  })
})

describe("AS.7.2 looksLikeEmail", () => {
  it("accepts a typical email", () => {
    expect(looksLikeEmail("user@example.com")).toBe(true)
    expect(looksLikeEmail("alice.bob+tag@sub.example.co.uk")).toBe(true)
  })

  it("rejects empty / whitespace / missing parts", () => {
    expect(looksLikeEmail("")).toBe(false)
    expect(looksLikeEmail("user")).toBe(false)
    expect(looksLikeEmail("user@")).toBe(false)
    expect(looksLikeEmail("@example.com")).toBe(false)
    expect(looksLikeEmail("user@example")).toBe(false)
  })

  it("rejects strings over 254 chars (RFC envelope limit)", () => {
    const long = "a".repeat(249) + "@x.io"  // 254 chars
    expect(looksLikeEmail(long)).toBe(true)
    const tooLong = "a".repeat(250) + "@x.io"  // 255 chars
    expect(looksLikeEmail(tooLong)).toBe(false)
  })

  it("regex object exposed for consumers", () => {
    expect(SIGNUP_EMAIL_REGEX.test("a@b.c")).toBe(true)
  })
})

describe("AS.7.2 signupSubmitBlockedReason — precedence", () => {
  const base = {
    email: "user@example.com",
    password: "abc123ABC!@#xyz",
    passwordPasses: true,
    hasSaved: true,
    hasAcceptedTos: true,
    busy: false,
    honeypotResolved: true,
  }

  it("returns null when every gate cleared", () => {
    expect(signupSubmitBlockedReason(base)).toBeNull()
  })

  it("busy short-circuits over every other failure", () => {
    expect(
      signupSubmitBlockedReason({
        ...base,
        busy: true,
        email: "",
      }),
    ).toBe("busy")
  })

  it("honeypot_pending blocks before email check", () => {
    expect(
      signupSubmitBlockedReason({
        ...base,
        honeypotResolved: false,
      }),
    ).toBe("honeypot_pending")
  })

  it("invalid email blocks before password check", () => {
    expect(
      signupSubmitBlockedReason({
        ...base,
        email: "not-an-email",
        password: "",
      }),
    ).toBe("email_invalid")
  })

  it("empty password vs weak password are distinct branches", () => {
    expect(
      signupSubmitBlockedReason({
        ...base,
        password: "",
      }),
    ).toBe("password_empty")
    expect(
      signupSubmitBlockedReason({
        ...base,
        passwordPasses: false,
      }),
    ).toBe("password_weak")
  })

  it("password_not_saved blocks before tos check", () => {
    expect(
      signupSubmitBlockedReason({
        ...base,
        hasSaved: false,
        hasAcceptedTos: false,
      }),
    ).toBe("password_not_saved")
  })

  it("tos_not_accepted is the last gate", () => {
    expect(
      signupSubmitBlockedReason({
        ...base,
        hasAcceptedTos: false,
      }),
    ).toBe("tos_not_accepted")
  })
})
