/**
 * V7 row 1732 (#323 first bullet) — Contract tests for
 * `mobile-visual-annotator.tsx`.
 *
 * Mirrors `test/components/visual-annotator.test.tsx` but proves the
 * two mobile-specific contracts the backend server twin consumes:
 *
 *   1. Every payload carries the right `platform` / `framework` /
 *      `fileExt` triple — the agent skill uses this to route to
 *      SwiftUI / Compose / Flutter / RN files.
 *   2. `nativePixelBox` is computed from the device profile so the
 *      agent gets native-pixel coordinates regardless of the CSS-px
 *      size the operator sees.
 *
 * Coordinate note: jsdom does not run layout, so we pass an explicit
 * `getOverlayRect` seam (same pattern as the web visual-annotator
 * test).
 */

import { describe, expect, it, vi } from "vitest"
import { fireEvent, render, screen } from "@testing-library/react"

import {
  FRAMEWORK_TO_FILE_EXT,
  MOBILE_PLATFORM_TO_FRAMEWORK,
  MobileVisualAnnotator,
  defaultMobileAnnotatorIdFactory,
  defaultMobileAnnotatorNowIso,
  normalizedToNativePixels,
  resolveFileExt,
  resolveFramework,
  toMobileAgentPayload,
  toMobileAgentPayloads,
  type MobileVisualAnnotation,
  type MobileVisualAnnotationAgentPayload,
} from "@/components/omnisight/mobile-visual-annotator"

import { getDeviceProfile } from "@/components/omnisight/device-frame"
import type { OverlayRect } from "@/components/omnisight/visual-annotator"

// ─── Helpers ───────────────────────────────────────────────────────────────

function makeIdFactory(prefix = "mann"): () => string {
  let counter = 0
  return () => `${prefix}-${++counter}`
}

function makeNowIso(start = "2026-04-18T12:00:00.000Z"): () => string {
  let tick = 0
  const base = new Date(start).getTime()
  return () => new Date(base + tick++ * 1000).toISOString()
}

const DEFAULT_RECT: OverlayRect = { left: 0, top: 0, width: 200, height: 400 }

function makeAnnotation(
  overrides: Partial<MobileVisualAnnotation> = {},
): MobileVisualAnnotation {
  return {
    id: "seed-1",
    type: "rect",
    boundingBox: { x: 0.1, y: 0.1, w: 0.3, h: 0.3 },
    comment: "",
    cssSelector: null,
    componentHint: null,
    createdAt: "2026-04-18T10:00:00.000Z",
    updatedAt: "2026-04-18T10:00:00.000Z",
    ...overrides,
  }
}

function fireOverlayPointer(
  surface: HTMLElement,
  type: "pointerDown" | "pointerMove" | "pointerUp" | "pointerCancel",
  clientX: number,
  clientY: number,
  pointerId = 1,
) {
  fireEvent[type](surface, { clientX, clientY, button: 0, pointerId })
}

// ─── Pure helpers ─────────────────────────────────────────────────────────

describe("MOBILE_PLATFORM_TO_FRAMEWORK", () => {
  it("maps every workspace platform to its agent framework", () => {
    expect(MOBILE_PLATFORM_TO_FRAMEWORK).toEqual({
      ios: "swiftui",
      android: "jetpack-compose",
      flutter: "flutter",
      "react-native": "react-native",
    })
  })

  it("every framework has a distinct file extension", () => {
    const exts = Object.values(FRAMEWORK_TO_FILE_EXT)
    expect(new Set(exts).size).toBe(exts.length)
    expect(FRAMEWORK_TO_FILE_EXT.swiftui).toBe(".swift")
    expect(FRAMEWORK_TO_FILE_EXT["jetpack-compose"]).toBe(".kt")
    expect(FRAMEWORK_TO_FILE_EXT.flutter).toBe(".dart")
    expect(FRAMEWORK_TO_FILE_EXT["react-native"]).toBe(".tsx")
  })
})

describe("resolveFramework / resolveFileExt", () => {
  it("resolves each platform to the expected framework", () => {
    expect(resolveFramework("ios")).toBe("swiftui")
    expect(resolveFramework("android")).toBe("jetpack-compose")
    expect(resolveFramework("flutter")).toBe("flutter")
    expect(resolveFramework("react-native")).toBe("react-native")
  })

  it("resolves each framework to its canonical extension", () => {
    expect(resolveFileExt("swiftui")).toBe(".swift")
    expect(resolveFileExt("jetpack-compose")).toBe(".kt")
    expect(resolveFileExt("flutter")).toBe(".dart")
    expect(resolveFileExt("react-native")).toBe(".tsx")
  })
})

describe("normalizedToNativePixels", () => {
  const profile = getDeviceProfile("iphone-15") // 1179 × 2556

  it("converts fractional coords to native-pixel coords rounded to the int", () => {
    const box = normalizedToNativePixels(
      { x: 0, y: 0, w: 1, h: 1 },
      profile,
    )
    expect(box).toEqual({ x: 0, y: 0, w: profile.screenWidth, h: profile.screenHeight })
  })

  it("handles zero-sized click annotations", () => {
    const box = normalizedToNativePixels(
      { x: 0.5, y: 0.5, w: 0, h: 0 },
      profile,
    )
    expect(box.w).toBe(0)
    expect(box.h).toBe(0)
    expect(box.x).toBe(Math.round(0.5 * profile.screenWidth))
    expect(box.y).toBe(Math.round(0.5 * profile.screenHeight))
  })

  it("clamps the w/h so the box never escapes the screen bottom-right", () => {
    const box = normalizedToNativePixels(
      { x: 0.9, y: 0.9, w: 1, h: 1 },
      profile,
    )
    expect(box.x + box.w).toBeLessThanOrEqual(profile.screenWidth)
    expect(box.y + box.h).toBeLessThanOrEqual(profile.screenHeight)
  })

  it("clamps negative/NaN coordinates to zero before multiplication", () => {
    const box = normalizedToNativePixels(
      { x: Number.NaN, y: -0.5, w: Number.POSITIVE_INFINITY, h: 0.1 },
      profile,
    )
    expect(box.x).toBe(0)
    expect(box.y).toBe(0)
    expect(box.w).toBe(0) // NaN clamped to 0 means w*screenWidth=0
    expect(box.h).toBe(Math.round(0.1 * profile.screenHeight))
  })
})

describe("toMobileAgentPayload", () => {
  it("emits the framework+fileExt triple from the platform + device", () => {
    const a = makeAnnotation({
      type: "rect",
      boundingBox: { x: 0.1, y: 0.2, w: 0.3, h: 0.4 },
      comment: "Shrink the send button",
      componentHint: "sendButton",
    })
    const payload = toMobileAgentPayload(a, "ios", "iphone-15")
    expect(payload.platform).toBe("ios")
    expect(payload.framework).toBe("swiftui")
    expect(payload.fileExt).toBe(".swift")
    expect(payload.device).toBe("iphone-15")
    expect(payload.screenWidth).toBe(1179)
    expect(payload.screenHeight).toBe(2556)
    expect(payload.boundingBox).toEqual({ x: 0.1, y: 0.2, w: 0.3, h: 0.4 })
    expect(payload.nativePixelBox.x).toBe(Math.round(0.1 * 1179))
    expect(payload.nativePixelBox.y).toBe(Math.round(0.2 * 2556))
    expect(payload.componentHint).toBe("sendButton")
    expect(payload.comment).toBe("Shrink the send button")
  })

  it("maps Android → jetpack-compose with .kt extension", () => {
    const payload = toMobileAgentPayload(makeAnnotation(), "android", "pixel-8")
    expect(payload.framework).toBe("jetpack-compose")
    expect(payload.fileExt).toBe(".kt")
  })

  it("defaults componentHint to null when absent", () => {
    const a = makeAnnotation({ componentHint: undefined })
    const payload = toMobileAgentPayload(a, "flutter", "pixel-8")
    expect(payload.componentHint).toBeNull()
    expect(payload.framework).toBe("flutter")
    expect(payload.fileExt).toBe(".dart")
  })

  it("handles click (zero-sized) annotations", () => {
    const a = makeAnnotation({
      type: "click",
      boundingBox: { x: 0.5, y: 0.5, w: 0, h: 0 },
    })
    const payload = toMobileAgentPayload(a, "react-native", "galaxy-tab")
    expect(payload.type).toBe("click")
    expect(payload.framework).toBe("react-native")
    expect(payload.fileExt).toBe(".tsx")
    expect(payload.nativePixelBox.w).toBe(0)
    expect(payload.nativePixelBox.h).toBe(0)
  })

  it("toMobileAgentPayloads preserves annotation order", () => {
    const list: MobileVisualAnnotation[] = [
      makeAnnotation({ id: "a", comment: "first" }),
      makeAnnotation({
        id: "b",
        type: "click",
        boundingBox: { x: 0.1, y: 0.1, w: 0, h: 0 },
        comment: "second",
      }),
    ]
    const payloads = toMobileAgentPayloads(list, "android", "pixel-8")
    expect(payloads).toHaveLength(2)
    expect(payloads[0].comment).toBe("first")
    expect(payloads[1].comment).toBe("second")
  })
})

describe("defaultMobileAnnotatorIdFactory / defaultMobileAnnotatorNowIso", () => {
  it("produces distinct ids on consecutive calls", () => {
    const a = defaultMobileAnnotatorIdFactory()
    const b = defaultMobileAnnotatorIdFactory()
    expect(a).not.toEqual(b)
    expect(a.length).toBeGreaterThan(0)
  })

  it("defaultMobileAnnotatorNowIso returns an ISO-8601 string", () => {
    const iso = defaultMobileAnnotatorNowIso()
    expect(() => new Date(iso).toISOString()).not.toThrow()
    expect(iso).toMatch(/^\d{4}-\d{2}-\d{2}T/)
  })
})

// ─── Rendering ────────────────────────────────────────────────────────────

describe("rendering", () => {
  it("renders toolbar + device frame wrap + annotation surface", () => {
    render(<MobileVisualAnnotator screenshotUrl="/shot.png" />)
    expect(screen.getByTestId("mobile-visual-annotator")).toBeInTheDocument()
    expect(screen.getByTestId("mobile-visual-annotator-toolbar")).toBeInTheDocument()
    expect(screen.getByTestId("mobile-visual-annotator-device-frame")).toBeInTheDocument()
    expect(screen.getByTestId("mobile-visual-annotator-surface")).toBeInTheDocument()
  })

  it("echoes platform + framework + device as data attributes", () => {
    render(
      <MobileVisualAnnotator
        platform="android"
        device="pixel-8"
        screenshotUrl="/shot.png"
      />,
    )
    const root = screen.getByTestId("mobile-visual-annotator")
    expect(root.getAttribute("data-platform")).toBe("android")
    expect(root.getAttribute("data-framework")).toBe("jetpack-compose")
    expect(root.getAttribute("data-device")).toBe("pixel-8")
  })

  it("defaults to iOS / iPhone 15 / rect mode", () => {
    render(<MobileVisualAnnotator screenshotUrl="/shot.png" />)
    const root = screen.getByTestId("mobile-visual-annotator")
    expect(root.getAttribute("data-platform")).toBe("ios")
    expect(root.getAttribute("data-device")).toBe("iphone-15")
    expect(root.getAttribute("data-mode")).toBe("rect")
  })

  it("shows empty-state hint when no annotations", () => {
    render(<MobileVisualAnnotator screenshotUrl="/shot.png" />)
    expect(screen.getByTestId("mobile-visual-annotator-summary").textContent).toContain(
      "No annotations yet",
    )
  })

  it("summary shows framework + fileExt when annotations exist", () => {
    render(
      <MobileVisualAnnotator
        screenshotUrl="/shot.png"
        platform="flutter"
        device="pixel-8"
        annotations={[makeAnnotation({ id: "x" })]}
      />,
    )
    const summary = screen.getByTestId("mobile-visual-annotator-summary").textContent ?? ""
    expect(summary).toContain("1 annotation")
    expect(summary).toContain("flutter")
    expect(summary).toContain(".dart")
  })
})

// ─── Rectangle drawing ───────────────────────────────────────────────────

describe("rectangle drawing", () => {
  it("pointer drag creates a rect annotation with correct normalised box", () => {
    const onChange = vi.fn()
    const onSelect = vi.fn()
    render(
      <MobileVisualAnnotator
        screenshotUrl="/s.png"
        idFactory={makeIdFactory("a")}
        nowIso={makeNowIso()}
        getOverlayRect={() => DEFAULT_RECT}
        onAnnotationsChange={onChange}
        onSelectionChange={onSelect}
      />,
    )
    const surface = screen.getByTestId("mobile-visual-annotator-surface")
    fireOverlayPointer(surface, "pointerDown", 40, 40)
    fireOverlayPointer(surface, "pointerMove", 120, 200)
    fireOverlayPointer(surface, "pointerUp", 120, 200)

    expect(onChange).toHaveBeenCalledTimes(1)
    const list = onChange.mock.calls[0][0] as MobileVisualAnnotation[]
    expect(list).toHaveLength(1)
    expect(list[0].type).toBe("rect")
    // 40..120 in a 200-wide rect = 0.2..0.6
    expect(list[0].boundingBox.x).toBeCloseTo(0.2)
    expect(list[0].boundingBox.w).toBeCloseTo(0.4)
    // 40..200 in a 400-tall rect = 0.1..0.5
    expect(list[0].boundingBox.y).toBeCloseTo(0.1)
    expect(list[0].boundingBox.h).toBeCloseTo(0.4)
    expect(onSelect).toHaveBeenCalledWith(list[0].id)
  })

  it("a tiny drag is promoted into a click annotation", () => {
    const onChange = vi.fn()
    render(
      <MobileVisualAnnotator
        screenshotUrl="/s.png"
        idFactory={makeIdFactory()}
        nowIso={makeNowIso()}
        getOverlayRect={() => DEFAULT_RECT}
        onAnnotationsChange={onChange}
      />,
    )
    const surface = screen.getByTestId("mobile-visual-annotator-surface")
    fireOverlayPointer(surface, "pointerDown", 50, 50)
    fireOverlayPointer(surface, "pointerMove", 51, 51)
    fireOverlayPointer(surface, "pointerUp", 51, 51)
    const list = onChange.mock.calls.at(-1)![0] as MobileVisualAnnotation[]
    expect(list).toHaveLength(1)
    expect(list[0].type).toBe("click")
    expect(list[0].boundingBox.w).toBe(0)
    expect(list[0].boundingBox.h).toBe(0)
  })
})

// ─── Click mode ──────────────────────────────────────────────────────────

describe("click mode", () => {
  it("single click places a zero-sized pin", () => {
    const onChange = vi.fn()
    render(
      <MobileVisualAnnotator
        screenshotUrl="/s.png"
        defaultMode="click"
        idFactory={makeIdFactory()}
        nowIso={makeNowIso()}
        getOverlayRect={() => DEFAULT_RECT}
        onAnnotationsChange={onChange}
      />,
    )
    const surface = screen.getByTestId("mobile-visual-annotator-surface")
    fireOverlayPointer(surface, "pointerDown", 100, 200)
    const list = onChange.mock.calls.at(-1)![0] as MobileVisualAnnotation[]
    expect(list).toHaveLength(1)
    expect(list[0].type).toBe("click")
    expect(list[0].boundingBox.x).toBeCloseTo(0.5) // 100/200
    expect(list[0].boundingBox.y).toBeCloseTo(0.5) // 200/400
  })
})

// ─── Component hint editor ───────────────────────────────────────────────

describe("component hint editor", () => {
  it("editing the hint updates the annotation's componentHint", () => {
    const onChange = vi.fn()
    render(
      <MobileVisualAnnotator
        screenshotUrl="/s.png"
        platform="ios"
        defaultAnnotations={[makeAnnotation({ id: "a" })]}
        defaultSelectedId="a"
        onAnnotationsChange={onChange}
      />,
    )
    const hint = screen.getByTestId("mobile-visual-annotator-hint-a") as HTMLInputElement
    fireEvent.change(hint, { target: { value: "sendButton" } })
    const list = onChange.mock.calls.at(-1)![0] as MobileVisualAnnotation[]
    expect(list[0].componentHint).toBe("sendButton")
  })

  it("trims whitespace and stores empty strings as null", () => {
    const onChange = vi.fn()
    render(
      <MobileVisualAnnotator
        screenshotUrl="/s.png"
        defaultAnnotations={[makeAnnotation({ id: "a", componentHint: "old" })]}
        defaultSelectedId="a"
        onAnnotationsChange={onChange}
      />,
    )
    const hint = screen.getByTestId("mobile-visual-annotator-hint-a") as HTMLInputElement
    fireEvent.change(hint, { target: { value: "   " } })
    const list = onChange.mock.calls.at(-1)![0] as MobileVisualAnnotation[]
    expect(list[0].componentHint).toBeNull()
  })

  it("placeholder text reflects the active framework", () => {
    const { unmount } = render(
      <MobileVisualAnnotator
        screenshotUrl="/s.png"
        platform="ios"
        defaultAnnotations={[makeAnnotation({ id: "a" })]}
        defaultSelectedId="a"
      />,
    )
    const hint = screen.getByTestId("mobile-visual-annotator-hint-a") as HTMLInputElement
    expect(hint.placeholder).toContain("Accessibility identifier")
    unmount()

    render(
      <MobileVisualAnnotator
        screenshotUrl="/s.png"
        platform="android"
        defaultAnnotations={[makeAnnotation({ id: "b" })]}
        defaultSelectedId="b"
      />,
    )
    const hint2 = screen.getByTestId("mobile-visual-annotator-hint-b") as HTMLInputElement
    expect(hint2.placeholder).toContain("testTag")
  })
})

// ─── Send to agent ───────────────────────────────────────────────────────

describe("send to agent", () => {
  it("button is disabled when there are no annotations", () => {
    const onSend = vi.fn()
    render(
      <MobileVisualAnnotator screenshotUrl="/s.png" onSendToAgent={onSend} />,
    )
    const btn = screen.getByTestId("mobile-visual-annotator-send") as HTMLButtonElement
    expect(btn.disabled).toBe(true)
  })

  it("button is hidden when no onSendToAgent is provided", () => {
    render(<MobileVisualAnnotator screenshotUrl="/s.png" />)
    expect(
      screen.queryByTestId("mobile-visual-annotator-send"),
    ).not.toBeInTheDocument()
  })

  it("clicking Send hands the structured payload list to the caller", async () => {
    const onSend = vi.fn()
    render(
      <MobileVisualAnnotator
        screenshotUrl="/s.png"
        platform="ios"
        device="iphone-15"
        defaultAnnotations={[
          makeAnnotation({
            id: "a",
            comment: "Tighten spacing",
            componentHint: "toolbar",
          }),
        ]}
        onSendToAgent={onSend}
      />,
    )
    const btn = screen.getByTestId("mobile-visual-annotator-send") as HTMLButtonElement
    fireEvent.click(btn)
    expect(onSend).toHaveBeenCalledTimes(1)
    const payloads = onSend.mock.calls[0][0] as MobileVisualAnnotationAgentPayload[]
    expect(payloads).toHaveLength(1)
    expect(payloads[0].platform).toBe("ios")
    expect(payloads[0].framework).toBe("swiftui")
    expect(payloads[0].fileExt).toBe(".swift")
    expect(payloads[0].componentHint).toBe("toolbar")
    expect(payloads[0].comment).toBe("Tighten spacing")
    expect(payloads[0].nativePixelBox.x).toBe(Math.round(0.1 * 1179))
  })
})

// ─── Clear + select + keyboard delete ────────────────────────────────────

describe("bulk mutations", () => {
  it("Clear removes all annotations and deselects", () => {
    const onChange = vi.fn()
    const onSelect = vi.fn()
    render(
      <MobileVisualAnnotator
        screenshotUrl="/s.png"
        defaultAnnotations={[makeAnnotation({ id: "a" }), makeAnnotation({ id: "b" })]}
        defaultSelectedId="a"
        onAnnotationsChange={onChange}
        onSelectionChange={onSelect}
      />,
    )
    fireEvent.click(screen.getByTestId("mobile-visual-annotator-clear"))
    const list = onChange.mock.calls.at(-1)![0] as MobileVisualAnnotation[]
    expect(list).toEqual([])
    expect(onSelect).toHaveBeenCalledWith(null)
  })

  it("keyboard Delete removes the selected annotation", () => {
    const onChange = vi.fn()
    render(
      <MobileVisualAnnotator
        screenshotUrl="/s.png"
        defaultAnnotations={[makeAnnotation({ id: "a" })]}
        defaultSelectedId="a"
        onAnnotationsChange={onChange}
      />,
    )
    const surface = screen.getByTestId("mobile-visual-annotator-surface")
    surface.focus()
    fireEvent.keyDown(surface, { key: "Delete" })
    const list = onChange.mock.calls.at(-1)![0] as MobileVisualAnnotation[]
    expect(list).toEqual([])
  })
})
