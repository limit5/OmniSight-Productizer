/**
 * AS.7.1 — `lib/auth/login-form-helpers.ts` contract tests.
 *
 * Pins:
 *   - The 12-word RARE_WORD_POOL byte-equal to backend (drift guard)
 *   - The 4 FORM_PREFIXES pair set
 *   - Web-Crypto SHA-256 → field-name reproducibility (same input
 *     → same name across runs / browsers / Node)
 *   - The `classifyLoginError` truth table (5 kinds × representative
 *     status codes)
 *   - parseRetryAfter helpers (delta-seconds + HTTP-date)
 *   - bumpShakeKey
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import {
  ANONYMOUS_TENANT_ID,
  FORM_PATH_LOGIN,
  FORM_PREFIXES,
  HONEYPOT_INPUT_ATTRS,
  HONEYPOT_ROTATION_PERIOD_SECONDS,
  LOGIN_ERROR_COPY,
  LOGIN_ERROR_KIND,
  OS_HONEYPOT_CLASS,
  RARE_WORD_POOL,
  _hexDigestToWordIndex,
  bumpShakeKey,
  classifyLoginError,
  currentEpoch,
  expectedFieldNames,
  honeypotFieldName,
  loginHoneypotFieldName,
  parseRetryAfter,
} from "@/lib/auth/login-form-helpers"

describe("AS.7.1 login-form-helpers — constants drift guard", () => {
  it("RARE_WORD_POOL is byte-equal to backend (12 words, exact order)", () => {
    expect([...RARE_WORD_POOL]).toEqual([
      "fax_office",
      "secondary_address",
      "company_role",
      "alt_contact",
      "referral_source",
      "marketing_pref",
      "newsletter_freq",
      "preferred_language",
      "fax_number",
      "secondary_email",
      "alt_phone",
      "office_extension",
    ])
  })

  it("FORM_PREFIXES contains exactly the 4 backend-supported paths", () => {
    expect(Object.keys(FORM_PREFIXES).sort()).toEqual([
      "/api/v1/auth/contact",
      "/api/v1/auth/login",
      "/api/v1/auth/password-reset",
      "/api/v1/auth/signup",
    ])
    expect(FORM_PREFIXES["/api/v1/auth/login"]).toBe("lg_")
    expect(FORM_PREFIXES["/api/v1/auth/signup"]).toBe("sg_")
  })

  it("FORM_PATH_LOGIN matches the FORM_PREFIXES key", () => {
    expect(FORM_PATH_LOGIN).toBe("/api/v1/auth/login")
    expect(FORM_PREFIXES[FORM_PATH_LOGIN]).toBe("lg_")
  })

  it("ANONYMOUS_TENANT_ID is the canonical sentinel", () => {
    expect(ANONYMOUS_TENANT_ID).toBe("_anonymous")
  })

  it("HONEYPOT_ROTATION_PERIOD_SECONDS is 30 days", () => {
    expect(HONEYPOT_ROTATION_PERIOD_SECONDS).toBe(30 * 86400)
  })

  it("HONEYPOT_INPUT_ATTRS exposes the 7 frozen attrs (5 + 2 PM-ignores)", () => {
    expect(Object.keys(HONEYPOT_INPUT_ATTRS).sort()).toEqual([
      "aria-hidden",
      "aria-label",
      "autocomplete",
      "data-1p-ignore",
      "data-bwignore",
      "data-lpignore",
      "tabindex",
    ])
    expect(HONEYPOT_INPUT_ATTRS.tabindex).toBe("-1")
    expect(HONEYPOT_INPUT_ATTRS.autocomplete).toBe("off")
    expect(HONEYPOT_INPUT_ATTRS["aria-hidden"]).toBe("true")
  })

  it("OS_HONEYPOT_CLASS is byte-equal to backend", () => {
    expect(OS_HONEYPOT_CLASS).toBe("os-honeypot-field")
  })

  it("RARE_WORD_POOL is frozen — runtime assignment throws", () => {
    expect(() => {
      // @ts-expect-error — runtime assert that the array is frozen
      ;(RARE_WORD_POOL as string[])[0] = "patched"
    }).toThrow()
  })
})

describe("AS.7.1 login-form-helpers — currentEpoch", () => {
  it("floor-divides nowMs by the 30-day period", () => {
    expect(currentEpoch(0)).toBe(0)
    expect(currentEpoch(HONEYPOT_ROTATION_PERIOD_SECONDS * 1000)).toBe(1)
    expect(currentEpoch(HONEYPOT_ROTATION_PERIOD_SECONDS * 1000 - 1)).toBe(0)
    expect(currentEpoch(HONEYPOT_ROTATION_PERIOD_SECONDS * 1000 * 5 + 100)).toBe(5)
  })

  it("falls back to Date.now() when no nowMs supplied", () => {
    const before = Math.floor(
      Date.now() / 1000 / HONEYPOT_ROTATION_PERIOD_SECONDS,
    )
    const got = currentEpoch()
    const after = Math.floor(
      Date.now() / 1000 / HONEYPOT_ROTATION_PERIOD_SECONDS,
    )
    expect(got).toBeGreaterThanOrEqual(before)
    expect(got).toBeLessThanOrEqual(after)
  })
})

describe("AS.7.1 login-form-helpers — _hexDigestToWordIndex", () => {
  it("modulos a 64-hex-char digest into 0..11", () => {
    // Known hash for the empty string: e3b0c4...
    const idx = _hexDigestToWordIndex(
      "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    )
    expect(idx).toBeGreaterThanOrEqual(0)
    expect(idx).toBeLessThan(RARE_WORD_POOL.length)
  })

  it("digest 0...0 maps to index 0", () => {
    const idx = _hexDigestToWordIndex(
      "0000000000000000000000000000000000000000000000000000000000000000",
    )
    expect(idx).toBe(0)
  })

  it("digest f...f maps to a valid pool index", () => {
    const idx = _hexDigestToWordIndex(
      "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
    )
    expect(idx).toBeGreaterThanOrEqual(0)
    expect(idx).toBeLessThan(RARE_WORD_POOL.length)
  })
})

describe("AS.7.1 login-form-helpers — honeypotFieldName (Web Crypto)", () => {
  it("returns deterministic name for same (form, tenant, epoch) triple", async () => {
    const a = await honeypotFieldName(FORM_PATH_LOGIN, "_anonymous", 1234567)
    const b = await honeypotFieldName(FORM_PATH_LOGIN, "_anonymous", 1234567)
    expect(a).toBe(b)
  })

  it("starts with the correct form prefix", async () => {
    const name = await honeypotFieldName(FORM_PATH_LOGIN, "_anonymous", 999)
    expect(name).toMatch(/^lg_/)
    const stem = name.slice("lg_".length)
    expect(RARE_WORD_POOL).toContain(stem)
  })

  it("epoch shift produces a possibly-different name (different seed)", async () => {
    // Statistically the same word might land but the seed differs; we
    // just check the function returns a valid pool word for both.
    const a = await honeypotFieldName(FORM_PATH_LOGIN, "_anonymous", 100)
    const b = await honeypotFieldName(FORM_PATH_LOGIN, "_anonymous", 101)
    expect(a).toMatch(/^lg_/)
    expect(b).toMatch(/^lg_/)
  })

  it("throws on unknown form path", async () => {
    await expect(
      honeypotFieldName("/api/v1/unknown" as never, "_anonymous", 0),
    ).rejects.toThrow(/unknown form_path/)
  })
})

describe("AS.7.1 login-form-helpers — expectedFieldNames", () => {
  it("returns [current, prev] tuple", async () => {
    const [now, prev] = await expectedFieldNames(
      FORM_PATH_LOGIN,
      "_anonymous",
      HONEYPOT_ROTATION_PERIOD_SECONDS * 1000 * 5 + 1000,
    )
    expect(now).toMatch(/^lg_/)
    expect(prev).toMatch(/^lg_/)
  })
})

describe("AS.7.1 login-form-helpers — loginHoneypotFieldName", () => {
  it("threads the login form_path + anonymous tenant id", async () => {
    const a = await loginHoneypotFieldName(0)
    const expected = await honeypotFieldName(FORM_PATH_LOGIN, ANONYMOUS_TENANT_ID, 0)
    expect(a).toBe(expected)
  })
})

describe("AS.7.1 login-form-helpers — parseRetryAfter", () => {
  it("returns null on null / empty", () => {
    expect(parseRetryAfter(null)).toBeNull()
    expect(parseRetryAfter(undefined)).toBeNull()
    expect(parseRetryAfter("")).toBeNull()
    expect(parseRetryAfter("   ")).toBeNull()
  })

  it("parses delta-seconds form", () => {
    expect(parseRetryAfter("30")).toBe(30)
    expect(parseRetryAfter("0")).toBe(0)
    expect(parseRetryAfter(" 120 ")).toBe(120)
  })

  it("returns null on negative seconds", () => {
    expect(parseRetryAfter("-1")).toBeNull()
  })

  it("parses HTTP-date form (returns positive seconds from now)", () => {
    const future = new Date(Date.now() + 60_000).toUTCString()
    const got = parseRetryAfter(future)
    expect(got).not.toBeNull()
    expect(got!).toBeGreaterThan(50)
    expect(got!).toBeLessThan(70)
  })

  it("HTTP-date in the past returns 0", () => {
    const past = new Date(Date.now() - 60_000).toUTCString()
    expect(parseRetryAfter(past)).toBe(0)
  })

  it("garbage string returns null", () => {
    expect(parseRetryAfter("not a date")).toBeNull()
  })
})

describe("AS.7.1 login-form-helpers — classifyLoginError truth table", () => {
  it("423 → account_locked + accountLocked=true", () => {
    const got = classifyLoginError({ status: 423, retryAfter: "30" })
    expect(got.kind).toBe(LOGIN_ERROR_KIND.accountLocked)
    expect(got.accountLocked).toBe(true)
    expect(got.message).toBe(LOGIN_ERROR_COPY.account_locked)
    expect(got.retryAfterSeconds).toBe(30)
  })

  it("401 → invalid_credentials (no PII leak)", () => {
    const got = classifyLoginError({ status: 401 })
    expect(got.kind).toBe(LOGIN_ERROR_KIND.invalidCredentials)
    expect(got.accountLocked).toBe(false)
    expect(got.message).toBe(LOGIN_ERROR_COPY.invalid_credentials)
  })

  it("429 + bot_challenge_failed code → botChallenge", () => {
    const got = classifyLoginError({
      status: 429,
      errorCode: "bot_challenge_failed",
      retryAfter: "60",
    })
    expect(got.kind).toBe(LOGIN_ERROR_KIND.botChallenge)
    expect(got.retryAfterSeconds).toBe(60)
  })

  it("plain 429 without bot code → rate_limited", () => {
    const got = classifyLoginError({ status: 429, retryAfter: "10" })
    expect(got.kind).toBe(LOGIN_ERROR_KIND.rateLimited)
    expect(got.retryAfterSeconds).toBe(10)
  })

  it("status null → service_unavailable", () => {
    const got = classifyLoginError({ status: null })
    expect(got.kind).toBe(LOGIN_ERROR_KIND.serviceUnavailable)
  })

  it("status >= 500 → service_unavailable", () => {
    expect(classifyLoginError({ status: 500 }).kind).toBe(
      LOGIN_ERROR_KIND.serviceUnavailable,
    )
    expect(classifyLoginError({ status: 503 }).kind).toBe(
      LOGIN_ERROR_KIND.serviceUnavailable,
    )
  })

  it("unknown 4xx defaults to invalid_credentials (defensive — no PII leak)", () => {
    expect(classifyLoginError({ status: 400 }).kind).toBe(
      LOGIN_ERROR_KIND.invalidCredentials,
    )
    expect(classifyLoginError({ status: 418 }).kind).toBe(
      LOGIN_ERROR_KIND.invalidCredentials,
    )
  })

  it("LOGIN_ERROR_COPY pins canonical strings", () => {
    expect(LOGIN_ERROR_COPY.invalid_credentials).toBe(
      "Invalid email or password.",
    )
    expect(LOGIN_ERROR_COPY.account_locked).toMatch(/account is temporarily locked/i)
    expect(LOGIN_ERROR_COPY.rate_limited).toMatch(/too many attempts/i)
  })
})

describe("AS.7.1 login-form-helpers — bumpShakeKey", () => {
  it("monotonic increment", () => {
    expect(bumpShakeKey(0)).toBe(1)
    expect(bumpShakeKey(1)).toBe(2)
    expect(bumpShakeKey(99)).toBe(100)
  })
})

describe("AS.7.1 login-form-helpers — Web Crypto fallback", () => {
  // Save the real crypto so we can restore it after the test.
  let realCrypto: Crypto | undefined

  beforeEach(() => {
    realCrypto = (globalThis as { crypto?: Crypto }).crypto
  })
  afterEach(() => {
    if (realCrypto) {
      Object.defineProperty(globalThis, "crypto", {
        value: realCrypto,
        configurable: true,
        writable: true,
      })
    }
  })

  it("rejects with a clear error when SubtleCrypto is unavailable", async () => {
    Object.defineProperty(globalThis, "crypto", {
      value: undefined,
      configurable: true,
      writable: true,
    })
    await expect(
      honeypotFieldName(FORM_PATH_LOGIN, "_anonymous", 0),
    ).rejects.toThrow(/SubtleCrypto unavailable/)
  })
})
