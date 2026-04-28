/**
 * AS.7.2 — `<PasswordStrengthMeter>` component tests.
 *
 * Pins:
 *   - Empty password: skipped breach status, neutral hint
 *   - Weak password: warn status, segments lit per score
 *   - Strong password + ok HIBP: emerald status copy
 *   - Breached password: bad status copy + count
 *   - disableBreachCheck=true short-circuits the network
 *   - 5-segment fill matrix (score 0..4)
 */

import { afterEach, describe, expect, it, vi } from "vitest"
import {
  cleanup,
  render,
  screen,
  waitFor,
} from "@testing-library/react"

import { PasswordStrengthMeter } from "@/components/omnisight/auth/password-strength-meter"

afterEach(() => cleanup())

describe("AS.7.2 PasswordStrengthMeter — empty / disabled paths", () => {
  it("empty password: status=skipped, neutral hint", () => {
    render(<PasswordStrengthMeter password="" disableBreachCheck />)
    const meter = screen.getByTestId("as7-password-strength-meter")
    expect(meter).toHaveAttribute("data-as7-strength-passes", "no")
    expect(meter).toHaveAttribute("data-as7-breach-status", "skipped")
    const status = screen.getByTestId("as7-strength-status")
    expect(status).toHaveAttribute("data-as7-strength-kind", "neutral")
  })

  it("disableBreachCheck=true never calls fetch", () => {
    const fetchImpl = vi.fn() as unknown as typeof fetch
    render(
      <PasswordStrengthMeter
        password="abc"
        disableBreachCheck
        fetchImpl={fetchImpl}
        breachDebounceMs={0}
      />,
    )
    expect(fetchImpl).not.toHaveBeenCalled()
  })
})

describe("AS.7.2 PasswordStrengthMeter — strong password + ok HIBP", () => {
  it("renders emerald 'never seen in any HIBP breach' line", async () => {
    // Spy fetch to return a valid HIBP body where the suffix isn't
    // present — count = 0 → status='ok'.
    const fetchImpl = vi.fn(async () => ({
      ok: true,
      text: async () => "ABCDEF0011223344556677889900AABBCCDDEEFF11:42",
    })) as unknown as typeof fetch

    render(
      <PasswordStrengthMeter
        password="J4rg0n!Cipher#Strong#42"  // 23 chars, 4 classes
        breachDebounceMs={0}
        fetchImpl={fetchImpl}
      />,
    )

    await waitFor(() => {
      const meter = screen.getByTestId("as7-password-strength-meter")
      expect(meter).toHaveAttribute("data-as7-breach-status", "ok")
    })
    const status = screen.getByTestId("as7-strength-status")
    expect(status.textContent).toMatch(/never seen in any HIBP breach/i)
    expect(status).toHaveAttribute("data-as7-strength-kind", "ok")
  })
})

describe("AS.7.2 PasswordStrengthMeter — breached password", () => {
  it("renders 'Found in N breaches' copy", async () => {
    // Pre-computed: SHA-1("P@ssw0rd") =
    //   21BD12DC183F740EE76F27B78EB39C8AD972A757
    const suffix = "2DC183F740EE76F27B78EB39C8AD972A757"
    const fetchImpl = vi.fn(async () => ({
      ok: true,
      text: async () => `${suffix}:184412\nOTHER:1`,
    })) as unknown as typeof fetch

    render(
      <PasswordStrengthMeter
        password="P@ssw0rd"
        breachDebounceMs={0}
        fetchImpl={fetchImpl}
      />,
    )

    await waitFor(() => {
      const meter = screen.getByTestId("as7-password-strength-meter")
      expect(meter).toHaveAttribute("data-as7-breach-status", "breached")
    })
    const status = screen.getByTestId("as7-strength-status")
    expect(status.textContent).toMatch(/Found in 184,412 known breaches/)
    expect(status).toHaveAttribute("data-as7-strength-kind", "bad")
  })
})

describe("AS.7.2 PasswordStrengthMeter — weak password warn", () => {
  it("password='abc' yields warn status + zero filled segments past 1", () => {
    render(<PasswordStrengthMeter password="abc" disableBreachCheck />)
    const meter = screen.getByTestId("as7-password-strength-meter")
    expect(meter).toHaveAttribute("data-as7-strength-passes", "no")
    const status = screen.getByTestId("as7-strength-status")
    expect(status).toHaveAttribute("data-as7-strength-kind", "warn")
  })
})

describe("AS.7.2 PasswordStrengthMeter — meter renders 5 segments", () => {
  it("renders five `as7-strength-seg-*` slots", () => {
    render(<PasswordStrengthMeter password="abc" disableBreachCheck />)
    for (let i = 0; i < 5; i += 1) {
      expect(screen.getByTestId(`as7-strength-seg-${i}`)).toBeInTheDocument()
    }
  })
})
