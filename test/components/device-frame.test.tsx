/**
 * V6 #3 (TODO row 1550 / issue #322) — DeviceFrame component tests.
 *
 * Verifies the contract that `device-grid.tsx` (V6 #4) and the V6 #5
 * agent visual-context injector both depend on:
 *
 *   1. All six profile presets render and expose stable data attributes
 *      that the grid and the multimodal LLM context can key on.
 *   2. Native-pixel geometry helpers (`getDeviceOuterSize`,
 *      `computeDeviceScale`) are pure and deterministic — V6 #4 uses
 *      them to lay out a grid without instantiating DOM nodes first.
 *   3. The screen cut-out has the correct CSS-px dimensions so a
 *      caller-supplied 1179×2556 PNG `object-fit: cover` lines up with
 *      the active screen region.
 *   4. Loading / empty / screenshot states are mutually exclusive and
 *      produce the expected ARIA hooks.
 *   5. Click + keyboard activation invoke the handler so the grid can
 *      use a frame as a "select this device" target.
 */

import { describe, expect, it, vi } from "vitest"
import { render, screen, fireEvent } from "@testing-library/react"

import {
  DeviceFrame,
  DEVICE_PROFILES,
  DEVICE_PROFILE_IDS,
  computeDeviceScale,
  getDeviceOuterSize,
  getDeviceProfile,
  type DeviceProfileId,
} from "@/components/omnisight/device-frame"

const PROFILE_IDS: DeviceProfileId[] = [
  "iphone-15",
  "iphone-se",
  "ipad",
  "pixel-8",
  "galaxy-fold",
  "galaxy-tab",
]

describe("DeviceFrame — profile catalogue", () => {
  it("exposes the six required presets in stable order", () => {
    expect(DEVICE_PROFILE_IDS).toEqual(PROFILE_IDS)
  })

  it.each(PROFILE_IDS)("preset %s has positive native dimensions and bezels", (id) => {
    const p = DEVICE_PROFILES[id]
    expect(p.id).toBe(id)
    expect(p.label.length).toBeGreaterThan(0)
    expect(p.platform === "ios" || p.platform === "android").toBe(true)
    expect(["phone", "tablet", "foldable"]).toContain(p.form)
    expect(p.screenWidth).toBeGreaterThan(0)
    expect(p.screenHeight).toBeGreaterThan(0)
    // Portrait orientation is the canonical render — width must not
    // exceed height for any preset (caller can `transform: rotate(90deg)`
    // for a landscape view but the source-of-truth stays portrait).
    expect(p.screenWidth).toBeLessThan(p.screenHeight)
    expect(p.bezel.top).toBeGreaterThanOrEqual(0)
    expect(p.bezel.right).toBeGreaterThanOrEqual(0)
    expect(p.bezel.bottom).toBeGreaterThanOrEqual(0)
    expect(p.bezel.left).toBeGreaterThanOrEqual(0)
    expect(p.frameRadius).toBeGreaterThanOrEqual(0)
    expect(p.screenRadius).toBeGreaterThanOrEqual(0)
  })

  it("only the home-button profiles flag homeButton=true", () => {
    expect(DEVICE_PROFILES["iphone-se"].homeButton).toBe(true)
    expect(DEVICE_PROFILES["iphone-15"].homeButton).toBe(false)
    expect(DEVICE_PROFILES["pixel-8"].homeButton).toBe(false)
    expect(DEVICE_PROFILES["ipad"].homeButton).toBe(false)
    expect(DEVICE_PROFILES["galaxy-fold"].homeButton).toBe(false)
    expect(DEVICE_PROFILES["galaxy-tab"].homeButton).toBe(false)
  })

  it("iPhone 15 carries a Dynamic Island, Pixel 8 carries a hole-punch", () => {
    expect(DEVICE_PROFILES["iphone-15"].cutout.kind).toBe("island")
    expect(DEVICE_PROFILES["pixel-8"].cutout.kind).toBe("hole")
    expect(DEVICE_PROFILES["galaxy-fold"].cutout.kind).toBe("hole")
  })

  it("iPad and Galaxy Tab classify as tablets", () => {
    expect(DEVICE_PROFILES["ipad"].form).toBe("tablet")
    expect(DEVICE_PROFILES["galaxy-tab"].form).toBe("tablet")
  })

  it("Galaxy Fold classifies as foldable", () => {
    expect(DEVICE_PROFILES["galaxy-fold"].form).toBe("foldable")
  })

  it("DEVICE_PROFILES is frozen — accidental mutation throws or is ignored", () => {
    expect(Object.isFrozen(DEVICE_PROFILES)).toBe(true)
    expect(Object.isFrozen(DEVICE_PROFILE_IDS)).toBe(true)
  })

  it("getDeviceProfile returns the same object as the catalogue", () => {
    for (const id of PROFILE_IDS) {
      expect(getDeviceProfile(id)).toBe(DEVICE_PROFILES[id])
    }
  })

  it("getDeviceProfile throws on unknown id (defensive runtime guard)", () => {
    expect(() => getDeviceProfile("does-not-exist" as DeviceProfileId)).toThrow(
      /Unknown device profile/,
    )
  })
})

describe("DeviceFrame — geometry helpers", () => {
  it("getDeviceOuterSize sums screen + bezels", () => {
    const p = DEVICE_PROFILES["iphone-15"]
    const outer = getDeviceOuterSize(p)
    expect(outer.width).toBe(p.screenWidth + p.bezel.left + p.bezel.right)
    expect(outer.height).toBe(p.screenHeight + p.bezel.top + p.bezel.bottom)
  })

  it.each(PROFILE_IDS)("getDeviceOuterSize is deterministic for %s", (id) => {
    const p = DEVICE_PROFILES[id]
    const a = getDeviceOuterSize(p)
    const b = getDeviceOuterSize(p)
    expect(a).toEqual(b)
    expect(a.width).toBeGreaterThan(p.screenWidth)
    expect(a.height).toBeGreaterThan(p.screenHeight)
  })

  it("computeDeviceScale = renderWidth / outerWidth", () => {
    const p = DEVICE_PROFILES["pixel-8"]
    const outer = getDeviceOuterSize(p)
    expect(computeDeviceScale(p, outer.width)).toBeCloseTo(1, 10)
    expect(computeDeviceScale(p, outer.width / 2)).toBeCloseTo(0.5, 10)
    expect(computeDeviceScale(p, outer.width * 3)).toBeCloseTo(3, 10)
  })

  it("computeDeviceScale rejects non-positive widths by returning 0", () => {
    const p = DEVICE_PROFILES["iphone-se"]
    expect(computeDeviceScale(p, 0)).toBe(0)
    expect(computeDeviceScale(p, -10)).toBe(0)
    expect(computeDeviceScale(p, Number.NaN)).toBe(0)
    expect(computeDeviceScale(p, Number.POSITIVE_INFINITY)).toBe(0)
  })
})

describe("DeviceFrame — rendering", () => {
  it("renders a bezel + screen + label and exposes stable data-attrs", () => {
    render(
      <DeviceFrame
        device="iphone-15"
        screenshotUrl="https://example.invalid/shot.png"
        showLabel
        data-testid="frame"
      />,
    )

    const figure = screen.getByTestId("frame")
    expect(figure).toBeInTheDocument()
    expect(figure.tagName.toLowerCase()).toBe("figure")
    expect(figure.getAttribute("data-device")).toBe("iphone-15")
    expect(figure.getAttribute("data-platform")).toBe("ios")
    expect(figure.getAttribute("data-form")).toBe("phone")
    expect(figure.getAttribute("aria-label")).toBe("iPhone 15 device frame")

    expect(screen.getByTestId("frame-bezel")).toBeInTheDocument()
    expect(screen.getByTestId("frame-screen")).toBeInTheDocument()
    expect(screen.getByTestId("frame-label").textContent).toBe("iPhone 15")
  })

  it("renders the supplied screenshot when not loading/empty", () => {
    render(
      <DeviceFrame
        device="pixel-8"
        screenshotUrl="https://cdn.invalid/p8.png"
        alt="Pixel 8 capture"
        data-testid="p8"
      />,
    )
    const img = screen.getByTestId("p8-screenshot") as HTMLImageElement
    expect(img.tagName.toLowerCase()).toBe("img")
    expect(img.getAttribute("src")).toBe("https://cdn.invalid/p8.png")
    expect(img.getAttribute("alt")).toBe("Pixel 8 capture")
    // No loading shimmer when the screenshot is present.
    expect(screen.queryByRole("status")).not.toBeInTheDocument()
  })

  it("alt falls back to the device label when no alt prop is supplied", () => {
    render(
      <DeviceFrame
        device="ipad"
        screenshotUrl="https://cdn.invalid/ipad.png"
        data-testid="ipad"
      />,
    )
    expect(screen.getByTestId("ipad-screenshot").getAttribute("alt")).toBe(
      "iPad screenshot",
    )
  })

  it("loading=true renders a shimmer with aria role=status, no <img>", () => {
    render(<DeviceFrame device="iphone-15" loading data-testid="frame" />)
    expect(screen.queryByTestId("frame-screenshot")).not.toBeInTheDocument()
    const status = screen.getByRole("status")
    expect(status.getAttribute("aria-label")).toBe("loading screenshot")
  })

  it("empty=true renders a placeholder string, no <img>", () => {
    render(<DeviceFrame device="galaxy-tab" empty data-testid="frame" />)
    expect(screen.queryByTestId("frame-screenshot")).not.toBeInTheDocument()
    expect(screen.getByText("no screenshot")).toBeInTheDocument()
  })

  it("loading takes precedence over empty (mutually exclusive states)", () => {
    render(<DeviceFrame device="galaxy-fold" loading empty data-testid="frame" />)
    expect(screen.getByRole("status")).toBeInTheDocument()
    expect(screen.queryByText("no screenshot")).not.toBeInTheDocument()
  })

  it("missing screenshotUrl + no loading + no empty paints just the bezel", () => {
    render(<DeviceFrame device="iphone-15" data-testid="frame" />)
    expect(screen.queryByTestId("frame-screenshot")).not.toBeInTheDocument()
    expect(screen.queryByRole("status")).not.toBeInTheDocument()
    expect(screen.queryByText("no screenshot")).not.toBeInTheDocument()
    expect(screen.getByTestId("frame-bezel")).toBeInTheDocument()
  })

  it("scales the figure to the requested width and proportional height", () => {
    render(<DeviceFrame device="pixel-8" width={200} data-testid="p8" />)
    const figure = screen.getByTestId("p8")
    const outer = getDeviceOuterSize(DEVICE_PROFILES["pixel-8"])
    const expectedHeight = (outer.height / outer.width) * 200
    // jsdom returns the literal style strings — no layout pass.
    expect(figure.style.width).toBe("200px")
    // Height includes the figure (no label by default) — allow a 0.5px
    // tolerance against floating-point rounding inside React's style.
    const renderedHeight = parseFloat(figure.style.height)
    expect(renderedHeight).toBeGreaterThan(expectedHeight - 0.5)
    expect(renderedHeight).toBeLessThan(expectedHeight + 0.5)
  })

  it("clamps tiny widths up to the MIN_WIDTH so the frame never disappears", () => {
    render(<DeviceFrame device="iphone-se" width={2} data-testid="se" />)
    // The bezel must still have non-zero width even if the caller asked
    // for nonsense — V6 #4 grid resizers can otherwise drag a frame to 0.
    const bezel = screen.getByTestId("se-bezel") as HTMLDivElement
    expect(parseFloat(bezel.style.width)).toBeGreaterThan(0)
  })

  it("falls back to the default width when given NaN", () => {
    render(<DeviceFrame device="iphone-15" width={Number.NaN} data-testid="frame" />)
    const figure = screen.getByTestId("frame") as HTMLElement
    // 280px is the documented default — guards against silent regressions.
    expect(figure.style.width).toBe("280px")
  })

  it("home-button profiles render an extra home pill, others do not", () => {
    const { rerender } = render(
      <DeviceFrame device="iphone-se" data-testid="se" />,
    )
    expect(screen.getByTestId("se-home")).toBeInTheDocument()

    rerender(<DeviceFrame device="iphone-15" data-testid="se" />)
    expect(screen.queryByTestId("se-home")).not.toBeInTheDocument()
  })

  it.each(PROFILE_IDS)("renders a non-empty bezel for %s", (id) => {
    render(<DeviceFrame device={id} data-testid={`f-${id}`} />)
    const bezel = screen.getByTestId(`f-${id}-bezel`) as HTMLDivElement
    const screenEl = screen.getByTestId(`f-${id}-screen`) as HTMLDivElement
    expect(parseFloat(bezel.style.width)).toBeGreaterThan(0)
    expect(parseFloat(bezel.style.height)).toBeGreaterThan(0)
    expect(parseFloat(screenEl.style.width)).toBeGreaterThan(0)
    expect(parseFloat(screenEl.style.height)).toBeGreaterThan(0)
    // Screen must fit *inside* the bezel — proves the bezel padding
    // is being applied symmetrically.
    expect(parseFloat(screenEl.style.width)).toBeLessThanOrEqual(
      parseFloat(bezel.style.width),
    )
    expect(parseFloat(screenEl.style.height)).toBeLessThanOrEqual(
      parseFloat(bezel.style.height),
    )
  })
})

describe("DeviceFrame — interaction", () => {
  it("becomes a button when onClick is provided", () => {
    const onClick = vi.fn()
    render(<DeviceFrame device="iphone-15" onClick={onClick} data-testid="frame" />)
    const figure = screen.getByTestId("frame")
    expect(figure.getAttribute("role")).toBe("button")
    expect(figure.getAttribute("tabindex")).toBe("0")
  })

  it("is non-interactive when onClick is omitted", () => {
    render(<DeviceFrame device="iphone-15" data-testid="frame" />)
    const figure = screen.getByTestId("frame")
    expect(figure.getAttribute("role")).toBeNull()
    expect(figure.getAttribute("tabindex")).toBeNull()
  })

  it("invokes onClick on click", () => {
    const onClick = vi.fn()
    render(<DeviceFrame device="pixel-8" onClick={onClick} data-testid="frame" />)
    fireEvent.click(screen.getByTestId("frame"))
    expect(onClick).toHaveBeenCalledTimes(1)
  })

  it("invokes onClick on Enter and Space (keyboard a11y)", () => {
    const onClick = vi.fn()
    render(<DeviceFrame device="ipad" onClick={onClick} data-testid="frame" />)
    const figure = screen.getByTestId("frame")
    fireEvent.keyDown(figure, { key: "Enter" })
    fireEvent.keyDown(figure, { key: " " })
    expect(onClick).toHaveBeenCalledTimes(2)
  })

  it("ignores unrelated keys", () => {
    const onClick = vi.fn()
    render(<DeviceFrame device="ipad" onClick={onClick} data-testid="frame" />)
    fireEvent.keyDown(screen.getByTestId("frame"), { key: "Tab" })
    fireEvent.keyDown(screen.getByTestId("frame"), { key: "Escape" })
    expect(onClick).not.toHaveBeenCalled()
  })

  it("merges custom className with the base class", () => {
    render(
      <DeviceFrame
        device="iphone-15"
        className="my-custom-class"
        data-testid="frame"
      />,
    )
    const figure = screen.getByTestId("frame")
    expect(figure.className).toContain("omnisight-device-frame")
    expect(figure.className).toContain("my-custom-class")
  })
})
