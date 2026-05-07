/**
 * OP-51 / MP.W5.6 - Multi-provider orchestrator onboarding tour tests.
 *
 * Covers:
 *   - Closed / open render paths.
 *   - Skip path via CTA, close button, backdrop, and Escape.
 *   - Step transition via buttons and keyboard.
 *   - Initial-step resolution by id and bounded numeric index.
 *   - Tooltip / spotlight visibility when anchors exist or are absent.
 */

import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import {
  ONBOARDING_TOUR_ANCHOR_ATTR,
  ONBOARDING_TOUR_STEPS,
  OnboardingTour,
  type OnboardingTourAnchor,
} from "@/components/omnisight/multi-provider-orchestrator/OnboardingTour"

const ANCHOR_RECTS: Record<OnboardingTourAnchor, DOMRectInit> = {
  start: { x: 40, y: 50, width: 120, height: 36 },
  sphere: { x: 180, y: 90, width: 72, height: 72 },
  color: { x: 280, y: 120, width: 64, height: 64 },
  core: { x: 420, y: 170, width: 96, height: 96 },
  beam: { x: 540, y: 260, width: 140, height: 20 },
  slider: { x: 620, y: 330, width: 220, height: 32 },
}

function installAnchor(id: OnboardingTourAnchor): HTMLElement {
  const el = document.createElement("button")
  el.type = "button"
  el.setAttribute(ONBOARDING_TOUR_ANCHOR_ATTR, id)
  el.getBoundingClientRect = () => DOMRect.fromRect(ANCHOR_RECTS[id])
  document.body.appendChild(el)
  return el
}

function installAllAnchors(): void {
  for (const step of ONBOARDING_TOUR_STEPS) {
    installAnchor(step.id)
  }
}

function activeTour(): HTMLElement {
  return screen.getByTestId("mp-onboarding-tour")
}

beforeEach(() => {
  vi.useFakeTimers()
  if (!Element.prototype.scrollIntoView) {
    Element.prototype.scrollIntoView = () => undefined
  }
  Object.defineProperty(window, "innerWidth", { configurable: true, value: 1024 })
  Object.defineProperty(window, "innerHeight", { configurable: true, value: 768 })
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  document.body.innerHTML = ""
})

describe("OnboardingTour — render gating", () => {
  it("does not render when open=false", () => {
    installAllAnchors()
    render(<OnboardingTour open={false} />)

    expect(screen.queryByTestId("mp-onboarding-tour")).not.toBeInTheDocument()
  })

  it("renders the first tooltip and spotlight when open with a matching anchor", () => {
    installAllAnchors()
    render(<OnboardingTour />)

    expect(activeTour()).toHaveAttribute("data-mp-onboarding-step", "start")
    expect(screen.getByText("1 / 6 - Start the workshop")).toBeInTheDocument()
    expect(screen.getByText(/Use Start to enter the multi-provider planning surface/)).toBeInTheDocument()

    const spotlight = screen.getByTestId("mp-onboarding-tour-spotlight")
    expect(spotlight).toHaveStyle({
      top: `${(ANCHOR_RECTS.start.y ?? 0) - 4}px`,
      left: `${(ANCHOR_RECTS.start.x ?? 0) - 4}px`,
      width: `${(ANCHOR_RECTS.start.width ?? 0) + 8}px`,
      height: `${(ANCHOR_RECTS.start.height ?? 0) + 8}px`,
    })
  })

  it("keeps the tooltip visible without a spotlight when the current anchor is missing", () => {
    render(<OnboardingTour initialStep="core" />)

    expect(activeTour()).toHaveAttribute("data-mp-onboarding-step", "core")
    expect(screen.getByText("4 / 6 - Project core")).toBeInTheDocument()
    expect(screen.queryByTestId("mp-onboarding-tour-spotlight")).not.toBeInTheDocument()
  })
})

describe("OnboardingTour — step transitions", () => {
  it("Next advances the step and Back returns to the previous step", () => {
    installAllAnchors()
    render(<OnboardingTour />)

    const back = screen.getByRole("button", { name: /back/i })
    const next = screen.getByRole("button", { name: /next/i })
    expect(back).toBeDisabled()

    fireEvent.click(next)
    expect(activeTour()).toHaveAttribute("data-mp-onboarding-step", "sphere")
    expect(screen.getByText("2 / 6 - Provider sphere")).toBeInTheDocument()
    expect(back).not.toBeDisabled()

    fireEvent.click(back)
    expect(activeTour()).toHaveAttribute("data-mp-onboarding-step", "start")
    expect(back).toBeDisabled()
  })

  it("ArrowRight and ArrowLeft mirror the step buttons", () => {
    installAllAnchors()
    render(<OnboardingTour />)

    fireEvent.keyDown(window, { key: "ArrowRight" })
    expect(activeTour()).toHaveAttribute("data-mp-onboarding-step", "sphere")

    fireEvent.keyDown(window, { key: "ArrowLeft" })
    expect(activeTour()).toHaveAttribute("data-mp-onboarding-step", "start")
  })

  it("initialStep accepts a step id", () => {
    installAllAnchors()
    render(<OnboardingTour initialStep="beam" />)

    expect(activeTour()).toHaveAttribute("data-mp-onboarding-step", "beam")
    expect(screen.getByText("5 / 6 - Allocation beam")).toBeInTheDocument()
  })

  it("initialStep clamps numeric values into the valid step range", () => {
    installAllAnchors()
    render(<OnboardingTour initialStep={99} />)

    expect(activeTour()).toHaveAttribute("data-mp-onboarding-step", "slider")
    expect(screen.getByText("6 / 6 - Cheap / Fast slider")).toBeInTheDocument()
  })

  it("Done on the final step closes and calls onClose once", () => {
    installAllAnchors()
    const onClose = vi.fn()
    render(<OnboardingTour initialStep="slider" onClose={onClose} />)

    fireEvent.click(screen.getByRole("button", { name: /done/i }))

    expect(onClose).toHaveBeenCalledTimes(1)
    expect(screen.queryByTestId("mp-onboarding-tour")).not.toBeInTheDocument()
  })
})

describe("OnboardingTour — skip path", () => {
  it("Skip tour closes and calls onClose once", () => {
    installAllAnchors()
    const onClose = vi.fn()
    render(<OnboardingTour onClose={onClose} />)

    fireEvent.click(screen.getByText("Skip tour"))

    expect(onClose).toHaveBeenCalledTimes(1)
    expect(screen.queryByTestId("mp-onboarding-tour")).not.toBeInTheDocument()
  })

  it("Escape, close icon, and backdrop each close the tour through the skip path", () => {
    installAllAnchors()
    const onEscapeClose = vi.fn()
    const { unmount } = render(<OnboardingTour onClose={onEscapeClose} />)

    fireEvent.keyDown(window, { key: "Escape" })
    expect(onEscapeClose).toHaveBeenCalledTimes(1)
    expect(screen.queryByTestId("mp-onboarding-tour")).not.toBeInTheDocument()
    unmount()

    const onIconClose = vi.fn()
    const iconRender = render(<OnboardingTour onClose={onIconClose} />)
    fireEvent.click(screen.getAllByRole("button", { name: /skip tour/i })[1])
    expect(onIconClose).toHaveBeenCalledTimes(1)
    expect(screen.queryByTestId("mp-onboarding-tour")).not.toBeInTheDocument()
    iconRender.unmount()

    const onBackdropClose = vi.fn()
    render(<OnboardingTour onClose={onBackdropClose} />)
    fireEvent.click(screen.getAllByRole("button", { name: /skip tour/i })[0])
    expect(onBackdropClose).toHaveBeenCalledTimes(1)
    expect(screen.queryByTestId("mp-onboarding-tour")).not.toBeInTheDocument()
  })
})
