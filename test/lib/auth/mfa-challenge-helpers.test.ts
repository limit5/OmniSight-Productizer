/**
 * AS.7.4 — `lib/auth/mfa-challenge-helpers.ts` contract tests.
 *
 * Pins the constants byte-equal to the backend, the classifier truth
 * table, the format predicates, the submit-gate predicate's
 * precedence cascade, and the method-selector dispatch.
 */

import { describe, expect, it } from "vitest"

import {
  BACKUP_CODE_LENGTH,
  MFA_CHALLENGE_BLOCKED_REASONS,
  MFA_CHALLENGE_ERROR_COPY,
  MFA_CHALLENGE_ERROR_KIND,
  MFA_METHOD_COPY,
  MFA_METHOD_KIND,
  MFA_METHOD_KINDS_ORDERED,
  TOTP_CODE_LENGTH,
  bumpPulseKey,
  classifyMfaChallengeError,
  looksLikeBackupCode,
  looksLikeTotpCode,
  mfaChallengeSubmitBlockedReason,
  normaliseMfaInput,
  parseMfaRetryAfter,
  selectableMethods,
} from "@/lib/auth/mfa-challenge-helpers"

describe("AS.7.4 mfa-challenge-helpers — constants drift guard", () => {
  it("MFA_METHOD_KIND covers the 3 canonical kinds", () => {
    expect(Object.values(MFA_METHOD_KIND)).toEqual([
      "totp",
      "webauthn",
      "backup_code",
    ])
  })

  it("MFA_METHOD_KINDS_ORDERED matches MFA_METHOD_KIND values", () => {
    expect([...MFA_METHOD_KINDS_ORDERED]).toEqual([
      "totp",
      "webauthn",
      "backup_code",
    ])
  })

  it("MFA_METHOD_COPY pins the canonical labels", () => {
    expect(MFA_METHOD_COPY.totp.label).toBe("Authenticator")
    expect(MFA_METHOD_COPY.webauthn.label).toBe("Security key")
    expect(MFA_METHOD_COPY.backup_code.label).toBe("Backup code")
    expect(MFA_METHOD_COPY.totp.placeholder).toBe("000000")
    expect(MFA_METHOD_COPY.backup_code.placeholder).toBe("xxxx-xxxx")
  })

  it("MFA_METHOD_COPY pins each hint string", () => {
    expect(MFA_METHOD_COPY.totp.hint).toBe(
      "Open your authenticator app and enter the 6-digit code.",
    )
    expect(MFA_METHOD_COPY.webauthn.hint).toBe(
      "Use your registered security key or platform biometric.",
    )
    expect(MFA_METHOD_COPY.backup_code.hint).toBe(
      "Enter one of your one-time backup codes (xxxx-xxxx).",
    )
  })

  it("TOTP / backup-code length constants match the backend heuristic", () => {
    expect(TOTP_CODE_LENGTH).toBe(6)
    expect(BACKUP_CODE_LENGTH).toBe(9)
  })

  it("MFA_CHALLENGE_ERROR_KIND covers the 6 canonical kinds", () => {
    expect(Object.values(MFA_CHALLENGE_ERROR_KIND)).toEqual([
      "invalid_code",
      "expired_challenge",
      "rate_limited",
      "bot_challenge_failed",
      "webauthn_failed",
      "service_unavailable",
    ])
  })

  it("MFA_CHALLENGE_ERROR_COPY pins the exact UI strings", () => {
    expect(MFA_CHALLENGE_ERROR_COPY.invalid_code).toBe(
      "That code is not valid. Double-check the digits and try again.",
    )
    expect(MFA_CHALLENGE_ERROR_COPY.expired_challenge).toBe(
      "This challenge has expired. Please sign in again from the start.",
    )
    expect(MFA_CHALLENGE_ERROR_COPY.rate_limited).toBe(
      "Too many attempts. Please wait a few minutes and retry.",
    )
    expect(MFA_CHALLENGE_ERROR_COPY.bot_challenge_failed).toBe(
      "Verification failed. Please refresh the page and try again.",
    )
    expect(MFA_CHALLENGE_ERROR_COPY.webauthn_failed).toBe(
      "Security-key verification did not complete. Please try again or pick another method.",
    )
    expect(MFA_CHALLENGE_ERROR_COPY.service_unavailable).toBe(
      "Two-factor verification is temporarily unavailable. Please try again in a moment.",
    )
  })

  it("MFA_CHALLENGE_BLOCKED_REASONS pins the 2-row drift-guard tuple", () => {
    expect([...MFA_CHALLENGE_BLOCKED_REASONS]).toEqual([
      "busy",
      "code_invalid",
    ])
  })
})

describe("AS.7.4 selectableMethods — backend-method dispatch", () => {
  it("totp + webauthn → totp + webauthn + backup_code (3 tabs)", () => {
    expect([...selectableMethods(["totp", "webauthn"])]).toEqual([
      "totp",
      "webauthn",
      "backup_code",
    ])
  })

  it("totp only → totp + backup_code (no webauthn)", () => {
    expect([...selectableMethods(["totp"])]).toEqual(["totp", "backup_code"])
  })

  it("webauthn only → webauthn (no backup_code)", () => {
    expect([...selectableMethods(["webauthn"])]).toEqual(["webauthn"])
  })

  it("empty array (defensive) returns the full 3-tuple", () => {
    expect([...selectableMethods([])]).toEqual([
      "totp",
      "webauthn",
      "backup_code",
    ])
  })

  it("unknown method label falls back to the full tuple", () => {
    expect([...selectableMethods(["sms"])]).toEqual([
      "totp",
      "webauthn",
      "backup_code",
    ])
  })

  it("case-insensitive against the backend's enum casing", () => {
    expect([...selectableMethods(["TOTP", "WebAuthn"])]).toEqual([
      "totp",
      "webauthn",
      "backup_code",
    ])
  })
})

describe("AS.7.4 looksLikeTotpCode", () => {
  it("accepts 6-digit numeric strings", () => {
    expect(looksLikeTotpCode("123456")).toBe(true)
    expect(looksLikeTotpCode("000000")).toBe(true)
    expect(looksLikeTotpCode("999999")).toBe(true)
  })

  it("rejects empty / partial / over-length", () => {
    expect(looksLikeTotpCode("")).toBe(false)
    expect(looksLikeTotpCode("12345")).toBe(false)
    expect(looksLikeTotpCode("1234567")).toBe(false)
  })

  it("rejects non-digit chars", () => {
    expect(looksLikeTotpCode("12345a")).toBe(false)
    expect(looksLikeTotpCode("12 456")).toBe(false)
    expect(looksLikeTotpCode("123-56")).toBe(false)
  })
})

describe("AS.7.4 looksLikeBackupCode", () => {
  it("accepts xxxx-xxxx with letters / digits", () => {
    expect(looksLikeBackupCode("abcd-1234")).toBe(true)
    expect(looksLikeBackupCode("0000-0000")).toBe(true)
    expect(looksLikeBackupCode("ABCD-1234")).toBe(true)
  })

  it("rejects empty / partial / wrong length", () => {
    expect(looksLikeBackupCode("")).toBe(false)
    expect(looksLikeBackupCode("abcd1234")).toBe(false)
    expect(looksLikeBackupCode("abcd-12345")).toBe(false)
  })

  it("rejects missing or misplaced separator", () => {
    expect(looksLikeBackupCode("abcd1-234")).toBe(false)
    expect(looksLikeBackupCode("abc-d-234")).toBe(false)
  })

  it("rejects symbols other than the separator", () => {
    expect(looksLikeBackupCode("abcd-12_4")).toBe(false)
    expect(looksLikeBackupCode("ab d-1234")).toBe(false)
  })
})

describe("AS.7.4 normaliseMfaInput", () => {
  it("strips non-digit chars from a TOTP value", () => {
    expect(normaliseMfaInput(MFA_METHOD_KIND.totp, "12 34a56")).toBe("123456")
    expect(normaliseMfaInput(MFA_METHOD_KIND.totp, "1-2-3-4-5-6")).toBe(
      "123456",
    )
  })

  it("clamps TOTP value to 6 digits", () => {
    expect(normaliseMfaInput(MFA_METHOD_KIND.totp, "1234567890")).toBe(
      "123456",
    )
  })

  it("preserves the backup-code separator + lowercases letters", () => {
    expect(normaliseMfaInput(MFA_METHOD_KIND.backupCode, "ABCD-1234")).toBe(
      "abcd-1234",
    )
    expect(normaliseMfaInput(MFA_METHOD_KIND.backupCode, "  abcd-1234  ")).toBe(
      "abcd-1234",
    )
  })

  it("WebAuthn returns empty regardless of input", () => {
    expect(normaliseMfaInput(MFA_METHOD_KIND.webauthn, "anything")).toBe("")
  })
})

describe("AS.7.4 parseMfaRetryAfter", () => {
  it("returns null for empty / nullish input", () => {
    expect(parseMfaRetryAfter(null)).toBeNull()
    expect(parseMfaRetryAfter(undefined)).toBeNull()
    expect(parseMfaRetryAfter("")).toBeNull()
    expect(parseMfaRetryAfter("   ")).toBeNull()
  })

  it("parses delta-seconds form", () => {
    expect(parseMfaRetryAfter("30")).toBe(30)
    expect(parseMfaRetryAfter("0")).toBe(0)
    expect(parseMfaRetryAfter(" 60 ")).toBe(60)
  })

  it("rejects negative integers", () => {
    expect(parseMfaRetryAfter("-1")).toBeNull()
  })

  it("parses HTTP-date form", () => {
    const future = new Date(Date.now() + 90_000).toUTCString()
    const got = parseMfaRetryAfter(future)
    expect(got).not.toBeNull()
    expect(got!).toBeGreaterThan(60)
    expect(got!).toBeLessThanOrEqual(90)
  })

  it("returns 0 for past HTTP-date", () => {
    const past = new Date(Date.now() - 60_000).toUTCString()
    expect(parseMfaRetryAfter(past)).toBe(0)
  })

  it("returns null for garbage input", () => {
    expect(parseMfaRetryAfter("not a date")).toBeNull()
  })
})

describe("AS.7.4 classifyMfaChallengeError — precedence truth table", () => {
  it("errorCode=webauthn_failed shortcircuits over status", () => {
    const o = classifyMfaChallengeError({
      status: 200,
      errorCode: "webauthn_failed",
    })
    expect(o.kind).toBe("webauthn_failed")
    expect(o.message).toBe(MFA_CHALLENGE_ERROR_COPY.webauthn_failed)
  })

  it("410 → expired_challenge", () => {
    const o = classifyMfaChallengeError({ status: 410 })
    expect(o.kind).toBe("expired_challenge")
  })

  it("401 + errorCode=mfa_challenge_expired → expired_challenge", () => {
    const o = classifyMfaChallengeError({
      status: 401,
      errorCode: "mfa_challenge_expired",
    })
    expect(o.kind).toBe("expired_challenge")
  })

  it("401 (no code) → invalid_code", () => {
    const o = classifyMfaChallengeError({ status: 401 })
    expect(o.kind).toBe("invalid_code")
  })

  it("422 → invalid_code", () => {
    const o = classifyMfaChallengeError({ status: 422 })
    expect(o.kind).toBe("invalid_code")
  })

  it("429 + errorCode=bot_challenge_failed → bot_challenge_failed", () => {
    const o = classifyMfaChallengeError({
      status: 429,
      errorCode: "bot_challenge_failed",
      retryAfter: "12",
    })
    expect(o.kind).toBe("bot_challenge_failed")
    expect(o.retryAfterSeconds).toBe(12)
  })

  it("429 (no code) → rate_limited carries Retry-After", () => {
    const o = classifyMfaChallengeError({ status: 429, retryAfter: "30" })
    expect(o.kind).toBe("rate_limited")
    expect(o.retryAfterSeconds).toBe(30)
  })

  it("500 / 503 / null status → service_unavailable", () => {
    expect(classifyMfaChallengeError({ status: 500 }).kind).toBe(
      "service_unavailable",
    )
    expect(classifyMfaChallengeError({ status: 503 }).kind).toBe(
      "service_unavailable",
    )
    expect(classifyMfaChallengeError({ status: null }).kind).toBe(
      "service_unavailable",
    )
  })

  it("unknown 4xx → invalid_code (defensive default)", () => {
    expect(classifyMfaChallengeError({ status: 404 }).kind).toBe(
      "invalid_code",
    )
    expect(classifyMfaChallengeError({ status: 418 }).kind).toBe(
      "invalid_code",
    )
  })
})

describe("AS.7.4 mfaChallengeSubmitBlockedReason — precedence", () => {
  it("returns null when TOTP value is 6 digits + not busy", () => {
    expect(
      mfaChallengeSubmitBlockedReason({
        kind: MFA_METHOD_KIND.totp,
        value: "123456",
        busy: false,
      }),
    ).toBeNull()
  })

  it("returns null when backup-code value is xxxx-xxxx + not busy", () => {
    expect(
      mfaChallengeSubmitBlockedReason({
        kind: MFA_METHOD_KIND.backupCode,
        value: "abcd-1234",
        busy: false,
      }),
    ).toBeNull()
  })

  it("returns null for WebAuthn regardless of value", () => {
    expect(
      mfaChallengeSubmitBlockedReason({
        kind: MFA_METHOD_KIND.webauthn,
        value: "",
        busy: false,
      }),
    ).toBeNull()
  })

  it("busy short-circuits over every other failure", () => {
    expect(
      mfaChallengeSubmitBlockedReason({
        kind: MFA_METHOD_KIND.totp,
        value: "",
        busy: true,
      }),
    ).toBe("busy")
    expect(
      mfaChallengeSubmitBlockedReason({
        kind: MFA_METHOD_KIND.webauthn,
        value: "",
        busy: true,
      }),
    ).toBe("busy")
  })

  it("malformed TOTP value → code_invalid", () => {
    expect(
      mfaChallengeSubmitBlockedReason({
        kind: MFA_METHOD_KIND.totp,
        value: "12345",
        busy: false,
      }),
    ).toBe("code_invalid")
  })

  it("malformed backup-code value → code_invalid", () => {
    expect(
      mfaChallengeSubmitBlockedReason({
        kind: MFA_METHOD_KIND.backupCode,
        value: "abcd1234",
        busy: false,
      }),
    ).toBe("code_invalid")
  })
})

describe("AS.7.4 bumpPulseKey", () => {
  it("strictly increases", () => {
    expect(bumpPulseKey(0)).toBe(1)
    expect(bumpPulseKey(7)).toBe(8)
    expect(bumpPulseKey(-2)).toBe(-1)
  })
})
