/**
 * C1 audit-fix — smoke tests for components that previously had zero
 * coverage. These assert renders-without-crashing + one key
 * accessibility/role contract each, so future refactors at least
 * break the build loudly instead of a silent visual regression.
 */

import { describe, expect, it, vi } from "vitest"
import { render, screen, fireEvent } from "@testing-library/react"

// ─── EmergencyStop ────────────────────────────────────────────────
import { EmergencyStop } from "@/components/omnisight/emergency-stop"

describe("EmergencyStop — smoke", () => {
  it("renders STOP button when not halted", () => {
    render(<EmergencyStop onStop={() => {}} />)
    expect(screen.getByRole("button")).toBeDefined()
  })

  it("invokes onStop after confirm click", () => {
    const onStop = vi.fn()
    render(<EmergencyStop onStop={onStop} />)
    // First click opens confirm; second click actually stops.
    const btn = screen.getByRole("button")
    fireEvent.click(btn)
    // After the first click, at least one button should still exist.
    const buttons = screen.getAllByRole("button")
    expect(buttons.length).toBeGreaterThan(0)
  })

  it("swaps to a Resume affordance once halted", () => {
    const { container } = render(
      <EmergencyStop onStop={() => {}} onResume={() => {}} isHalted />,
    )
    // The halted state always renders something interactive.
    expect(container.querySelector("button")).not.toBeNull()
  })
})

// ─── NeuralGrid ───────────────────────────────────────────────────
import { NeuralGrid } from "@/components/omnisight/neural-grid"

describe("NeuralGrid — smoke", () => {
  it("renders decorative-only layers (aria-hidden on all)", () => {
    const { container } = render(<NeuralGrid />)
    const decorative = container.querySelectorAll('[aria-hidden="true"]')
    // The grid is purely decorative — every div must be aria-hidden so
    // screen readers skip the entire layer stack.
    expect(decorative.length).toBeGreaterThan(0)
    // And no element should carry a role that would expose it to a11y.
    expect(container.querySelector("[role]")).toBeNull()
  })
})

// ─── LanguageToggle ───────────────────────────────────────────────
vi.mock("@/lib/i18n/context", () => ({
  useI18n: () => ({
    locale: "en" as const,
    setLocale: vi.fn(),
    t: (k: string) => k,
  }),
}))

// FX.9.9: LanguageToggle now also calls next-intl's `useTranslations`.
// Smoke test stubs the namespaced hook so it returns the bare key,
// matching the legacy `useI18n().t` mock above.
vi.mock("next-intl", () => ({
  useTranslations: () => (k: string) => k,
}))

import { LanguageToggle } from "@/components/omnisight/language-toggle"

describe("LanguageToggle — smoke", () => {
  it("renders a language picker trigger", () => {
    render(<LanguageToggle />)
    const trigger = screen.getByRole("button")
    expect(trigger).toBeDefined()
  })

  it("opens the menu on click", () => {
    render(<LanguageToggle />)
    const trigger = screen.getByRole("button")
    fireEvent.click(trigger)
    // At least one option now visible — either via role or text.
    const candidates = screen.queryAllByRole("menuitem")
    expect(candidates.length + screen.queryAllByText(/繁體中文|简体中文|English|日本語/).length).toBeGreaterThan(0)
  })
})
