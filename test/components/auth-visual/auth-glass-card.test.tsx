/**
 * AS.7.0 — `AuthGlassCard` component tests.
 *
 * Focus areas:
 *   - Render shape + flicker attribute gating
 *   - imperativeHandle.focus()
 *   - rAF loop short-circuits when every motion source is 0
 */

import { describe, expect, it, vi } from "vitest"
import { createRef } from "react"
import { render, screen } from "@testing-library/react"

import {
  AuthGlassCard,
  type AuthGlassCardHandle,
} from "@/components/omnisight/auth/auth-glass-card"

describe("AuthGlassCard", () => {
  it("renders children inside the glass-card wrapper", () => {
    render(
      <AuthGlassCard level="dramatic">
        <span data-testid="child-marker">hello</span>
      </AuthGlassCard>,
    )
    expect(screen.getByTestId("as7-glass-card")).toBeInTheDocument()
    expect(screen.getByTestId("child-marker")).toBeInTheDocument()
  })

  it("at `dramatic` enables glow flicker", () => {
    render(<AuthGlassCard level="dramatic">x</AuthGlassCard>)
    expect(screen.getByTestId("as7-glass-card")).toHaveAttribute("data-as7-flicker", "on")
  })

  it("at `normal` keeps flicker off (battery courtesy)", () => {
    render(<AuthGlassCard level="normal">x</AuthGlassCard>)
    expect(screen.getByTestId("as7-glass-card")).toHaveAttribute("data-as7-flicker", "off")
  })

  it("at `off` keeps flicker off", () => {
    render(<AuthGlassCard level="off">x</AuthGlassCard>)
    expect(screen.getByTestId("as7-glass-card")).toHaveAttribute("data-as7-flicker", "off")
  })

  it("forwarded ref exposes a `focus()` method that lands on the wrapper", () => {
    const ref = createRef<AuthGlassCardHandle>()
    render(
      <AuthGlassCard level="dramatic" ref={ref}>
        <input data-testid="inner-input" />
      </AuthGlassCard>,
    )
    expect(ref.current).not.toBeNull()
    const focusSpy = vi.spyOn(screen.getByTestId("as7-glass-card"), "focus")
    ref.current?.focus()
    expect(focusSpy).toHaveBeenCalledTimes(1)
  })

  it("at `off` with parallaxFactor=0 the rAF loop is skipped (no transform written)", () => {
    render(
      <AuthGlassCard level="off" parallaxFactor={0}>
        x
      </AuthGlassCard>,
    )
    const card = screen.getByTestId("as7-glass-card")
    // The leaf clears the inline transform when every contributing
    // source is 0. (Empty string is the default, so this asserts
    // we did not accidentally write one.)
    expect(card.style.transform).toBe("")
  })
})
