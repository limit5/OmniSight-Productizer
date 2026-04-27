/**
 * AS.7.0 — `AuthBrandWordmark` component tests.
 */

import { describe, expect, it, beforeEach, vi } from "vitest"
import { act, render, screen } from "@testing-library/react"

import { AuthBrandWordmark } from "@/components/omnisight/auth/auth-brand-wordmark"

describe("AuthBrandWordmark", () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  it("renders the default OmniSight wordmark", () => {
    render(<AuthBrandWordmark level="dramatic" />)
    expect(screen.getByTestId("as7-wordmark")).toHaveTextContent("OmniSight")
  })

  it("respects an explicit label prop", () => {
    render(<AuthBrandWordmark level="dramatic" label="Welcome" />)
    expect(screen.getByTestId("as7-wordmark")).toHaveTextContent("Welcome")
  })

  it("at `dramatic` level enables breathe + traveling-light", () => {
    render(<AuthBrandWordmark level="dramatic" />)
    const el = screen.getByTestId("as7-wordmark")
    expect(el).toHaveAttribute("data-as7-breathe", "on")
    expect(el).toHaveAttribute("data-as7-traveling-light", "on")
  })

  it("at `subtle` keeps breathe but disables traveling-light", () => {
    render(<AuthBrandWordmark level="subtle" />)
    const el = screen.getByTestId("as7-wordmark")
    expect(el).toHaveAttribute("data-as7-breathe", "on")
    expect(el).toHaveAttribute("data-as7-traveling-light", "off")
  })

  it("at `off` disables every effect", () => {
    render(<AuthBrandWordmark level="off" />)
    const el = screen.getByTestId("as7-wordmark")
    expect(el).toHaveAttribute("data-as7-breathe", "off")
    expect(el).toHaveAttribute("data-as7-traveling-light", "off")
    expect(el).toHaveAttribute("data-as7-bloom", "off")
  })

  it("bloomKey > 0 triggers a one-shot bloom that auto-clears", async () => {
    const { rerender } = render(<AuthBrandWordmark level="dramatic" bloomKey={0} />)
    const el = screen.getByTestId("as7-wordmark")
    expect(el).toHaveAttribute("data-as7-bloom", "off")

    rerender(<AuthBrandWordmark level="dramatic" bloomKey={1} />)
    expect(el).toHaveAttribute("data-as7-bloom", "on")

    // Advance past the 600 ms one-shot — bloom should auto-clear.
    // The `setTimeout` callback queues a React state update, so the
    // timer-advance must run inside `act()` for React to flush.
    act(() => {
      vi.advanceTimersByTime(700)
    })
    expect(el).toHaveAttribute("data-as7-bloom", "off")
  })

  it("bloom is suppressed at `off` level (no breathe AND no traveling)", () => {
    const { rerender } = render(<AuthBrandWordmark level="off" bloomKey={1} />)
    const el = screen.getByTestId("as7-wordmark")
    expect(el).toHaveAttribute("data-as7-bloom", "off")

    rerender(<AuthBrandWordmark level="off" bloomKey={2} />)
    expect(el).toHaveAttribute("data-as7-bloom", "off")
  })
})
