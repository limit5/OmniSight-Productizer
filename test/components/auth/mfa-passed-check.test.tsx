/**
 * AS.7.4 — `<MfaPassedCheck>` contract tests.
 *
 * Pins:
 *   - Duration table (off=0 / subtle=0 / normal=600 / dramatic=900)
 *   - Off / subtle render nothing but still call onComplete
 *   - Normal / dramatic render the SVG checkmark + ring
 *   - onComplete fires after the configured duration
 *   - Re-rendering with active=false unmounts the overlay
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { cleanup, render, screen } from "@testing-library/react"

import {
  MFA_PASSED_DURATION_BY_LEVEL,
  MfaPassedCheck,
} from "@/components/omnisight/auth/mfa-passed-check"

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

describe("AS.7.4 <MfaPassedCheck> — duration table", () => {
  it("MFA_PASSED_DURATION_BY_LEVEL pins the canonical milliseconds", () => {
    expect(MFA_PASSED_DURATION_BY_LEVEL).toEqual({
      off: 0,
      subtle: 0,
      normal: 600,
      dramatic: 900,
    })
  })

  it("monotonic in level (each tier ≥ previous)", () => {
    expect(MFA_PASSED_DURATION_BY_LEVEL.subtle).toBeGreaterThanOrEqual(
      MFA_PASSED_DURATION_BY_LEVEL.off,
    )
    expect(MFA_PASSED_DURATION_BY_LEVEL.normal).toBeGreaterThanOrEqual(
      MFA_PASSED_DURATION_BY_LEVEL.subtle,
    )
    expect(MFA_PASSED_DURATION_BY_LEVEL.dramatic).toBeGreaterThanOrEqual(
      MFA_PASSED_DURATION_BY_LEVEL.normal,
    )
  })
})

describe("AS.7.4 <MfaPassedCheck> — render gating", () => {
  it("off does NOT render the overlay even when active", () => {
    render(
      <MfaPassedCheck level="off" active onComplete={() => undefined} />,
    )
    expect(screen.queryByTestId("as7-mfa-passed-check")).toBeNull()
  })

  it("subtle does NOT render the overlay even when active", () => {
    render(
      <MfaPassedCheck
        level="subtle"
        active
        onComplete={() => undefined}
      />,
    )
    expect(screen.queryByTestId("as7-mfa-passed-check")).toBeNull()
  })

  it("normal renders the overlay when active", () => {
    render(
      <MfaPassedCheck
        level="normal"
        active
        onComplete={() => undefined}
      />,
    )
    expect(screen.getByTestId("as7-mfa-passed-check")).toBeInTheDocument()
  })

  it("dramatic renders the overlay when active", () => {
    render(
      <MfaPassedCheck
        level="dramatic"
        active
        onComplete={() => undefined}
      />,
    )
    expect(screen.getByTestId("as7-mfa-passed-check")).toBeInTheDocument()
  })

  it("active=false never renders the overlay", () => {
    render(
      <MfaPassedCheck
        level="dramatic"
        active={false}
        onComplete={() => undefined}
      />,
    )
    expect(screen.queryByTestId("as7-mfa-passed-check")).toBeNull()
  })
})

describe("AS.7.4 <MfaPassedCheck> — onComplete timer", () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  it("off level fires onComplete on the next microtask", () => {
    const onComplete = vi.fn()
    render(<MfaPassedCheck level="off" active onComplete={onComplete} />)
    expect(onComplete).not.toHaveBeenCalled()
    vi.runAllTimers()
    expect(onComplete).toHaveBeenCalledTimes(1)
  })

  it("normal level fires onComplete after 600ms", () => {
    const onComplete = vi.fn()
    render(
      <MfaPassedCheck level="normal" active onComplete={onComplete} />,
    )
    expect(onComplete).not.toHaveBeenCalled()
    vi.advanceTimersByTime(599)
    expect(onComplete).not.toHaveBeenCalled()
    vi.advanceTimersByTime(1)
    expect(onComplete).toHaveBeenCalledTimes(1)
  })

  it("dramatic level fires onComplete after 900ms", () => {
    const onComplete = vi.fn()
    render(
      <MfaPassedCheck level="dramatic" active onComplete={onComplete} />,
    )
    vi.advanceTimersByTime(900)
    expect(onComplete).toHaveBeenCalledTimes(1)
  })
})
