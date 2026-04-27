/**
 * AS.7.1 — `<WarpDriveTransition>` component tests.
 *
 * Pins:
 *   - WARP_DURATION_BY_LEVEL is monotonic in motion level
 *   - active=false → renders nothing
 *   - active=true at off/subtle → fires onComplete almost immediately
 *     (no overlay rendered)
 *   - active=true at dramatic → renders overlay with rings/streaks/bloom
 *     and fires onComplete after the duration
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { render, screen, cleanup } from "@testing-library/react"

import {
  WARP_DURATION_BY_LEVEL,
  WarpDriveTransition,
} from "@/components/omnisight/auth/warp-drive-transition"

afterEach(() => {
  cleanup()
})

describe("AS.7.1 WarpDriveTransition — duration table", () => {
  it("durations are monotonic in motion level", () => {
    expect(WARP_DURATION_BY_LEVEL.off).toBe(0)
    expect(WARP_DURATION_BY_LEVEL.subtle).toBe(0)
    expect(WARP_DURATION_BY_LEVEL.normal).toBeGreaterThan(0)
    expect(WARP_DURATION_BY_LEVEL.dramatic).toBeGreaterThanOrEqual(
      WARP_DURATION_BY_LEVEL.normal,
    )
  })
})

describe("AS.7.1 WarpDriveTransition — render gating", () => {
  it("active=false renders nothing", () => {
    render(
      <WarpDriveTransition
        level="dramatic"
        active={false}
        onComplete={() => undefined}
      />,
    )
    expect(screen.queryByTestId("as7-warp-drive")).toBeNull()
  })

  it("active=true at `off` does NOT render the overlay", () => {
    render(
      <WarpDriveTransition
        level="off"
        active
        onComplete={() => undefined}
      />,
    )
    expect(screen.queryByTestId("as7-warp-drive")).toBeNull()
  })

  it("active=true at `subtle` does NOT render the overlay", () => {
    render(
      <WarpDriveTransition
        level="subtle"
        active
        onComplete={() => undefined}
      />,
    )
    expect(screen.queryByTestId("as7-warp-drive")).toBeNull()
  })

  it("active=true at `dramatic` renders the overlay with all 3 layers", () => {
    render(
      <WarpDriveTransition
        level="dramatic"
        active
        onComplete={() => undefined}
      />,
    )
    const overlay = screen.getByTestId("as7-warp-drive")
    expect(overlay).toBeInTheDocument()
    expect(overlay.querySelector(".as7-warp-rings")).not.toBeNull()
    expect(overlay.querySelector(".as7-warp-streaks")).not.toBeNull()
    expect(overlay.querySelector(".as7-warp-bloom")).not.toBeNull()
  })

  it("forwards the duration as a CSS variable on the host", () => {
    render(
      <WarpDriveTransition
        level="dramatic"
        active
        onComplete={() => undefined}
      />,
    )
    const overlay = screen.getByTestId("as7-warp-drive")
    expect(overlay.style.getPropertyValue("--as7-warp-duration")).toBe(
      `${WARP_DURATION_BY_LEVEL.dramatic}ms`,
    )
  })
})

describe("AS.7.1 WarpDriveTransition — onComplete timing", () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  it("at `off` onComplete fires immediately (next microtask)", () => {
    const onComplete = vi.fn()
    render(
      <WarpDriveTransition level="off" active onComplete={onComplete} />,
    )
    expect(onComplete).not.toHaveBeenCalled()
    vi.advanceTimersByTime(0)
    expect(onComplete).toHaveBeenCalledTimes(1)
  })

  it("at `dramatic` onComplete fires after the duration", () => {
    const onComplete = vi.fn()
    render(
      <WarpDriveTransition
        level="dramatic"
        active
        onComplete={onComplete}
      />,
    )
    expect(onComplete).not.toHaveBeenCalled()
    vi.advanceTimersByTime(WARP_DURATION_BY_LEVEL.dramatic - 1)
    expect(onComplete).not.toHaveBeenCalled()
    vi.advanceTimersByTime(1)
    expect(onComplete).toHaveBeenCalledTimes(1)
  })
})
