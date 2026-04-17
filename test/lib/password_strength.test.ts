import { describe, expect, it } from "vitest"
import {
  estimatePasswordStrength,
  PASSWORD_MIN_LENGTH,
  PASSWORD_MIN_SCORE,
} from "@/lib/password_strength"

describe("estimatePasswordStrength — K7-unified gate (≥12 chars + score ≥ 3)", () => {
  it("empty input does not pass and hints for length + score", () => {
    const r = estimatePasswordStrength("")
    expect(r.passes).toBe(false)
    expect(r.score).toBe(0)
    expect(r.hint.toLowerCase()).toContain(String(PASSWORD_MIN_SCORE))
  })

  it("short password (<12) never passes even with class diversity", () => {
    const r = estimatePasswordStrength("Aa1!Bb2@")
    expect(r.passes).toBe(false)
    expect(r.score).toBeLessThanOrEqual(1)
    expect(r.hint).toMatch(new RegExp(`${PASSWORD_MIN_LENGTH}`))
  })

  it("flags common-password substrings as very weak regardless of length", () => {
    const r = estimatePasswordStrength("Correctpassword!!1")
    expect(r.passes).toBe(false)
    expect(r.score).toBe(0)
    expect(r.label).toBe("very-weak")
  })

  it("flags the shipping default credential as very weak", () => {
    const r = estimatePasswordStrength("omnisight-admin")
    expect(r.passes).toBe(false)
    expect(r.score).toBe(0)
  })

  it("penalises straight sequences like 'abcd'/'1234'", () => {
    const weak = estimatePasswordStrength("XYZabcdefghij12345")
    const strong = estimatePasswordStrength("Tr0ub4dor&3xtra-m")
    expect(weak.score).toBeLessThanOrEqual(strong.score)
  })

  it("penalises triple-repeated characters", () => {
    const r = estimatePasswordStrength("Zzzbbb-qwerty-!!aa")
    // Sequence 'qwerty' + triple-repeat penalty — below the gate.
    expect(r.passes).toBe(false)
  })

  it("passes with 4-class diversity + length ≥ 14 (score ≥ 3)", () => {
    const r = estimatePasswordStrength("Hunter2-Secret!Day")
    expect(r.passes).toBe(true)
    expect(r.score).toBeGreaterThanOrEqual(PASSWORD_MIN_SCORE)
  })

  it("reaches top band for long high-entropy passwords", () => {
    const r = estimatePasswordStrength("Zx9!mP#vQ2&fL7@tRbK4")
    expect(r.passes).toBe(true)
    expect(r.score).toBe(4)
    expect(r.label).toBe("strong")
  })

  it("single-class long passwords cap at score ≤ 1", () => {
    const r = estimatePasswordStrength("aaaaaaaaaaaaaaaa")
    expect(r.passes).toBe(false)
  })

  it("exposes label matching zxcvbn 0–4 bands", () => {
    const bands = new Set(
      [
        estimatePasswordStrength(""),
        estimatePasswordStrength("short"),
        estimatePasswordStrength("okaypass-12ab"),
        estimatePasswordStrength("Hunter2-Secret!Day"),
        estimatePasswordStrength("Zx9!mP#vQ2&fL7@tRbK4"),
      ].map((r) => r.label),
    )
    expect(bands.size).toBeGreaterThanOrEqual(3)
  })
})
