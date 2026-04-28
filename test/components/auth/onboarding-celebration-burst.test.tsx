/**
 * AS.7.8 — `<OnboardingCelebrationBurst>` contract tests.
 *
 * Pins:
 *   - Duration table (off=0 / subtle=0 / normal=1500 / dramatic=2400)
 *   - Off / subtle render the welcome wordmark statically; particles
 *     are not emitted
 *   - Normal / dramatic render exactly 30 particles + welcome wordmark
 *   - onComplete fires after the configured duration
 *   - Re-rendering with active=false unmounts the burst entirely
 *   - displayName threads into the wordmark copy
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { cleanup, render, screen } from "@testing-library/react"

import {
  CELEBRATION_DURATION_BY_LEVEL,
  OnboardingCelebrationBurst,
} from "@/components/omnisight/auth/onboarding-celebration-burst"

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

describe("AS.7.8 <OnboardingCelebrationBurst> — duration table", () => {
  it("CELEBRATION_DURATION_BY_LEVEL pins the canonical milliseconds", () => {
    expect(CELEBRATION_DURATION_BY_LEVEL).toEqual({
      off: 0,
      subtle: 0,
      normal: 1500,
      dramatic: 2400,
    })
  })

  it("monotonic in level (each tier ≥ previous)", () => {
    expect(CELEBRATION_DURATION_BY_LEVEL.subtle).toBeGreaterThanOrEqual(
      CELEBRATION_DURATION_BY_LEVEL.off,
    )
    expect(CELEBRATION_DURATION_BY_LEVEL.normal).toBeGreaterThanOrEqual(
      CELEBRATION_DURATION_BY_LEVEL.subtle,
    )
    expect(CELEBRATION_DURATION_BY_LEVEL.dramatic).toBeGreaterThanOrEqual(
      CELEBRATION_DURATION_BY_LEVEL.normal,
    )
  })
})

describe("AS.7.8 <OnboardingCelebrationBurst> — render gating", () => {
  it("active=false never renders the stage", () => {
    render(
      <OnboardingCelebrationBurst
        level="dramatic"
        active={false}
        displayName="Yi"
        onComplete={() => undefined}
      />,
    )
    expect(screen.queryByTestId("as7-burst-stage")).toBeNull()
  })

  it("off level: stage renders, no particles, welcome wordmark visible", () => {
    render(
      <OnboardingCelebrationBurst
        level="off"
        active
        displayName="Yi"
        onComplete={() => undefined}
      />,
    )
    expect(screen.getByTestId("as7-burst-stage")).toHaveAttribute(
      "data-as7-burst-level",
      "off",
    )
    expect(screen.queryByTestId("as7-burst-particles")).toBeNull()
    expect(screen.getByTestId("as7-burst-welcome")).toHaveTextContent(
      "Welcome aboard, Yi",
    )
  })

  it("subtle level: stage renders, no particles, welcome wordmark visible", () => {
    render(
      <OnboardingCelebrationBurst
        level="subtle"
        active
        displayName="Yi"
        onComplete={() => undefined}
      />,
    )
    expect(screen.queryByTestId("as7-burst-particles")).toBeNull()
    expect(screen.getByTestId("as7-burst-welcome")).toBeInTheDocument()
  })

  it("normal level: 30 particles emitted", () => {
    render(
      <OnboardingCelebrationBurst
        level="normal"
        active
        displayName="Yi"
        onComplete={() => undefined}
      />,
    )
    expect(screen.getByTestId("as7-burst-particles")).toBeInTheDocument()
    for (let i = 0; i < 30; i += 1) {
      expect(
        screen.getByTestId(`as7-burst-particle-${i}`),
      ).toBeInTheDocument()
    }
  })

  it("dramatic level: 30 particles emitted", () => {
    render(
      <OnboardingCelebrationBurst
        level="dramatic"
        active
        displayName="Yi"
        onComplete={() => undefined}
      />,
    )
    expect(screen.getByTestId("as7-burst-particles")).toBeInTheDocument()
    expect(
      screen.getByTestId("as7-burst-particle-29"),
    ).toBeInTheDocument()
  })

  it("particle CSS variables are applied via inline style", () => {
    render(
      <OnboardingCelebrationBurst
        level="dramatic"
        active
        displayName="Yi"
        onComplete={() => undefined}
      />,
    )
    const first = screen.getByTestId("as7-burst-particle-0")
    const styleAttr = first.getAttribute("style") ?? ""
    expect(styleAttr).toMatch(/--as7-burst-x:/)
    expect(styleAttr).toMatch(/--as7-burst-hue:/)
  })
})

describe("AS.7.8 <OnboardingCelebrationBurst> — display name", () => {
  it("falls back to bare phrase when displayName is null", () => {
    render(
      <OnboardingCelebrationBurst
        level="dramatic"
        active
        displayName={null}
        onComplete={() => undefined}
      />,
    )
    expect(screen.getByTestId("as7-burst-welcome")).toHaveTextContent(
      "Welcome aboard",
    )
    // Should not include the comma form when no display name.
    expect(
      screen.getByTestId("as7-burst-welcome").textContent,
    ).not.toMatch(/,/)
  })

  it("includes the trimmed display name when provided", () => {
    render(
      <OnboardingCelebrationBurst
        level="normal"
        active
        displayName="  Casey  "
        onComplete={() => undefined}
      />,
    )
    expect(screen.getByTestId("as7-burst-welcome")).toHaveTextContent(
      "Welcome aboard, Casey",
    )
  })
})

describe("AS.7.8 <OnboardingCelebrationBurst> — onComplete timer", () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  it("off level fires onComplete on the next microtask", () => {
    const onComplete = vi.fn()
    render(
      <OnboardingCelebrationBurst
        level="off"
        active
        displayName="Yi"
        onComplete={onComplete}
      />,
    )
    expect(onComplete).not.toHaveBeenCalled()
    vi.runAllTimers()
    expect(onComplete).toHaveBeenCalledTimes(1)
  })

  it("normal level fires onComplete after 1500ms", () => {
    const onComplete = vi.fn()
    render(
      <OnboardingCelebrationBurst
        level="normal"
        active
        displayName="Yi"
        onComplete={onComplete}
      />,
    )
    vi.advanceTimersByTime(1499)
    expect(onComplete).not.toHaveBeenCalled()
    vi.advanceTimersByTime(1)
    expect(onComplete).toHaveBeenCalledTimes(1)
  })

  it("dramatic level fires onComplete after 2400ms", () => {
    const onComplete = vi.fn()
    render(
      <OnboardingCelebrationBurst
        level="dramatic"
        active
        displayName="Yi"
        onComplete={onComplete}
      />,
    )
    vi.advanceTimersByTime(2400)
    expect(onComplete).toHaveBeenCalledTimes(1)
  })

  it("active=false does not start the timer", () => {
    const onComplete = vi.fn()
    render(
      <OnboardingCelebrationBurst
        level="dramatic"
        active={false}
        displayName="Yi"
        onComplete={onComplete}
      />,
    )
    vi.advanceTimersByTime(5000)
    expect(onComplete).not.toHaveBeenCalled()
  })
})
