/**
 * OP-88 / MP.W12.4 - Onboarding wizard mobile-aware wrappers.
 *
 * Covers the responsive wrapper contract for the multi-provider
 * onboarding wizard welcome step. Tailwind's responsive classes remain
 * present in jsdom; viewport setup documents the expected breakpoint
 * while assertions pin the mobile base classes vs desktop md classes.
 */

import { render, screen } from "@testing-library/react"
import { describe, expect, it } from "vitest"

import OnboardingWizardStep1 from "@/components/omnisight/multi-provider-orchestrator/OnboardingWizardStep1"

function setViewport(width: number, height = 667): void {
  Object.defineProperty(window, "innerWidth", { configurable: true, value: width })
  Object.defineProperty(window, "innerHeight", { configurable: true, value: height })
  window.dispatchEvent(new Event("resize"))
}

describe("OnboardingWizardStep1 - mobile-aware wrapper", () => {
  it("renders as a full-screen panel at iPhone SE width without a base rounded modal class", () => {
    setViewport(375)
    render(<OnboardingWizardStep1 />)

    const wrapper = screen.getByTestId("mp-onboarding-step-1")
    expect(wrapper).toHaveClass("h-screen")
    expect(wrapper).toHaveClass("flex")
    expect(wrapper).toHaveClass("flex-col")
    expect(wrapper).not.toHaveClass("rounded-2xl")
  })

  it("keeps the desktop rounded modal class at 1024px width", () => {
    setViewport(1024, 768)
    render(<OnboardingWizardStep1 />)

    expect(screen.getByTestId("mp-onboarding-step-1")).toHaveClass("md:rounded-2xl")
  })
})
