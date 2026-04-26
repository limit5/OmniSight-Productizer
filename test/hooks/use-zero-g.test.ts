/**
 * BS.3.7 — `hooks/use-zero-g.ts` motion-library contract tests.
 *
 * Five motion hooks + the spring-press helper, each gated by the
 * effective motion level. We mock `@/hooks/use-effective-motion-level`
 * so each test can drive a specific level without standing up the
 * full BS.3.5 resolver chain (battery API, prefers-reduced-motion,
 * persisted user pref).
 *
 * Notes on jsdom quirks:
 *
 *   - `getBoundingClientRect` returns a zero-rect by default; the
 *     hooks early-return on `width === 0 || height === 0`, so each
 *     ref-based test mocks it to a non-zero rect.
 *
 *   - Refs created inside the hook are normally assigned by JSX
 *     committing the rendered element. `renderHook` doesn't render
 *     JSX, so we assign `ref.current` synchronously inside the
 *     render callback — by the time the post-commit `useEffect`
 *     runs, the ref points at the fake element.
 *
 *   - `requestAnimationFrame` is used by the scroll-parallax and
 *     cursor-distance-glow hooks to batch updates. We yield with a
 *     `requestAnimationFrame` Promise and wrap the wait in `act()`
 *     so React state updates flush.
 */

import type { CSSProperties, RefObject } from "react"
import { act, renderHook } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import type { MotionLevel } from "@/lib/motion-preferences"

// Mutable state read by the mocked `useEffectiveMotionLevel`. Each
// test sets `mockLevel = "..."` before `renderHook`.
let mockLevel: MotionLevel = "dramatic"

vi.mock("@/hooks/use-effective-motion-level", () => ({
  useEffectiveMotionLevel: () => mockLevel,
  usePrefersReducedMotion: () => false,
}))

import {
  MOTION_AMPLITUDE_BY_LEVEL,
  MOTION_LIFT_BY_LEVEL,
  useCursorDistanceGlow,
  useCursorMagneticTilt,
  useFloatingCard,
  useGlassReflection,
  useScrollParallax,
  useSpringPress,
} from "@/hooks/use-zero-g"

// ─────────────────────────────────────────────────────────────────────
// Helpers
// ─────────────────────────────────────────────────────────────────────

function makeRectEl(rect: { width: number; height: number; left: number; top: number }) {
  const el = document.createElement("div")
  document.body.appendChild(el)
  el.getBoundingClientRect = () => ({
    ...rect,
    right: rect.left + rect.width,
    bottom: rect.top + rect.height,
    x: rect.left,
    y: rect.top,
    toJSON: () => ({}),
  })
  return el
}

async function nextFrame() {
  await act(async () => {
    await new Promise<void>((resolve) =>
      window.requestAnimationFrame(() => resolve()),
    )
  })
}

afterEach(() => {
  document.body.innerHTML = ""
  mockLevel = "dramatic"
})

// ─────────────────────────────────────────────────────────────────────
// constants
// ─────────────────────────────────────────────────────────────────────

describe("MOTION_AMPLITUDE_BY_LEVEL / MOTION_LIFT_BY_LEVEL", () => {
  it("expose the documented per-level multipliers", () => {
    expect(MOTION_AMPLITUDE_BY_LEVEL).toEqual({
      off: 0,
      subtle: 0.5,
      normal: 1.0,
      dramatic: 1.5,
    })
    expect(MOTION_LIFT_BY_LEVEL).toEqual({
      off: "0px",
      subtle: "1px",
      normal: "3px",
      dramatic: "5px",
    })
  })
})

// ─────────────────────────────────────────────────────────────────────
// useFloatingCard
// ─────────────────────────────────────────────────────────────────────

describe("useFloatingCard", () => {
  it("returns an empty class and amplitude 0 when motion is off", () => {
    mockLevel = "off"
    const { result } = renderHook(() => useFloatingCard("a"))
    expect(result.current.className).toBe("")
    expect((result.current.style as Record<string, unknown>)["--motion-amplitude"]).toBe(0)
  })

  it("applies the variant class with subtle amplitude/lift values", () => {
    mockLevel = "subtle"
    const { result } = renderHook(() => useFloatingCard("c"))
    expect(result.current.className).toBe("float-card-c")
    const style = result.current.style as Record<string, unknown>
    expect(style["--motion-amplitude"]).toBe(0.5)
    expect(style["--motion-lift"]).toBe("1px")
  })

  it("uses the dramatic-tier amplitude/lift for the dramatic level", () => {
    mockLevel = "dramatic"
    const { result } = renderHook(() => useFloatingCard("b"))
    expect(result.current.className).toBe("float-card-b")
    const style = result.current.style as Record<string, unknown>
    expect(style["--motion-amplitude"]).toBe(1.5)
    expect(style["--motion-lift"]).toBe("5px")
  })
})

// ─────────────────────────────────────────────────────────────────────
// useCursorMagneticTilt — `normal | dramatic` only (ADR §5.7)
// ─────────────────────────────────────────────────────────────────────

describe("useCursorMagneticTilt", () => {
  it("returns no transform style and attaches no listener at subtle level", () => {
    mockLevel = "subtle"
    const el = makeRectEl({ width: 200, height: 200, left: 0, top: 0 })
    const addSpy = vi.spyOn(el, "addEventListener")

    renderHook(() => {
      const tilt = useCursorMagneticTilt<HTMLDivElement>()
      ;(tilt.ref as RefObject<HTMLDivElement | null>).current = el
      return tilt
    })

    expect(addSpy).not.toHaveBeenCalled()
  })

  it("at dramatic level writes --motion-tilt-x/y on pointermove and resets on leave", async () => {
    mockLevel = "dramatic"
    const el = makeRectEl({ width: 200, height: 200, left: 0, top: 0 })

    const { result } = renderHook(() => {
      const tilt = useCursorMagneticTilt<HTMLDivElement>({ maxTiltDeg: 10 })
      ;(tilt.ref as RefObject<HTMLDivElement | null>).current = el
      return tilt
    })

    expect((result.current.style as CSSProperties).transform).toContain(
      "perspective(800px)",
    )

    // Pointer at top-right corner — clientX=200, clientY=0 (centre 100,100).
    el.dispatchEvent(
      new MouseEvent("pointermove", { clientX: 200, clientY: 0, bubbles: true }),
    )

    const tiltX = el.style.getPropertyValue("--motion-tilt-x")
    const tiltY = el.style.getPropertyValue("--motion-tilt-y")
    // ny = (0 - 100)/100 = -1, nx = (200 - 100)/100 = +1, amp=1.5, max=10
    // tiltX = -ny * max * amp = 15 ; tiltY = nx * max * amp = 15
    expect(parseFloat(tiltX)).toBeCloseTo(15, 1)
    expect(parseFloat(tiltY)).toBeCloseTo(15, 1)

    el.dispatchEvent(new MouseEvent("pointerleave"))
    expect(el.style.getPropertyValue("--motion-tilt-x")).toBe("0deg")
    expect(el.style.getPropertyValue("--motion-tilt-y")).toBe("0deg")
  })
})

// ─────────────────────────────────────────────────────────────────────
// useGlassReflection — `dramatic` only (ADR §5.7)
// ─────────────────────────────────────────────────────────────────────

describe("useGlassReflection", () => {
  it("at normal level returns empty class and attaches no listener", () => {
    mockLevel = "normal"
    const el = makeRectEl({ width: 100, height: 100, left: 0, top: 0 })
    const addSpy = vi.spyOn(el, "addEventListener")

    const { result } = renderHook(() => {
      const ref = useGlassReflection<HTMLDivElement>()
      ;(ref.ref as RefObject<HTMLDivElement | null>).current = el
      return ref
    })

    expect(result.current.className).toBe("")
    expect(addSpy).not.toHaveBeenCalled()
  })

  it("at dramatic level adds the holo-reflect-glass class and writes --reflect-x/y in %", () => {
    mockLevel = "dramatic"
    const el = makeRectEl({ width: 200, height: 100, left: 0, top: 0 })

    const { result } = renderHook(() => {
      const ref = useGlassReflection<HTMLDivElement>()
      ;(ref.ref as RefObject<HTMLDivElement | null>).current = el
      return ref
    })

    expect(result.current.className).toBe("holo-reflect-glass")

    el.dispatchEvent(
      new MouseEvent("pointermove", { clientX: 50, clientY: 25, bubbles: true }),
    )

    expect(el.style.getPropertyValue("--reflect-x")).toBe("25.00%")
    expect(el.style.getPropertyValue("--reflect-y")).toBe("25.00%")

    el.dispatchEvent(new MouseEvent("pointerleave"))
    expect(el.style.getPropertyValue("--reflect-x")).toBe("50%")
  })
})

// ─────────────────────────────────────────────────────────────────────
// useScrollParallax — translate-Y bound to scrollY
// ─────────────────────────────────────────────────────────────────────

describe("useScrollParallax", () => {
  beforeEach(() => {
    Object.defineProperty(window, "scrollY", {
      writable: true,
      configurable: true,
      value: 0,
    })
  })

  it("returns an empty style and attaches no scroll listener when motion is off", () => {
    mockLevel = "off"
    const addSpy = vi.spyOn(window, "addEventListener")
    const { result } = renderHook(() => useScrollParallax({ speed: 0.5 }))
    expect(result.current.style).toEqual({})
    expect(
      addSpy.mock.calls.filter((call) => call[0] === "scroll"),
    ).toHaveLength(0)
  })

  it("computes a translate3d offset on scroll and clamps to maxOffsetPx", async () => {
    mockLevel = "normal"
    const { result } = renderHook(() =>
      useScrollParallax({ speed: 0.5, maxOffsetPx: 40 }),
    )

    // Initial render: scrollY=0 → translate3d(0, 0.00px, 0).
    expect((result.current.style.transform as string)).toContain(
      "translate3d(0, 0.00px, 0)",
    )

    await act(async () => {
      ;(window as unknown as { scrollY: number }).scrollY = 200
      window.dispatchEvent(new Event("scroll"))
    })
    await nextFrame()

    // raw = 200 * 0.5 * amp(1.0) = 100, clamped to maxOffsetPx=40.
    expect((result.current.style.transform as string)).toContain(
      "translate3d(0, 40.00px, 0)",
    )
  })
})

// ─────────────────────────────────────────────────────────────────────
// useCursorDistanceGlow — Layer 4 catalog cards
// ─────────────────────────────────────────────────────────────────────

describe("useCursorDistanceGlow", () => {
  it("returns an empty class and attaches no document listener at off", () => {
    mockLevel = "off"
    const addSpy = vi.spyOn(document, "addEventListener")
    const { result } = renderHook(() => useCursorDistanceGlow())
    expect(result.current.className).toBe("")
    expect(
      addSpy.mock.calls.filter((call) => call[0] === "pointermove"),
    ).toHaveLength(0)
  })

  it("at dramatic writes a max --glow-intensity when the cursor sits over the element centre", async () => {
    mockLevel = "dramatic"
    const el = makeRectEl({ width: 200, height: 200, left: 0, top: 0 })

    const { result } = renderHook(() => {
      const glow = useCursorDistanceGlow<HTMLDivElement>({ maxDistancePx: 240 })
      ;(glow.ref as RefObject<HTMLDivElement | null>).current = el
      return glow
    })

    expect(result.current.className).toBe("cursor-distance-glow")

    document.dispatchEvent(
      new MouseEvent("pointermove", { clientX: 100, clientY: 100, bubbles: true }),
    )
    await nextFrame()

    expect(parseFloat(el.style.getPropertyValue("--glow-intensity"))).toBeCloseTo(
      1,
      2,
    )
  })

  it("removes its document listeners on unmount", () => {
    mockLevel = "dramatic"
    const removeSpy = vi.spyOn(document, "removeEventListener")
    const el = makeRectEl({ width: 100, height: 100, left: 0, top: 0 })

    const { unmount } = renderHook(() => {
      const glow = useCursorDistanceGlow<HTMLDivElement>()
      ;(glow.ref as RefObject<HTMLDivElement | null>).current = el
      return glow
    })

    unmount()
    expect(
      removeSpy.mock.calls.some((call) => call[0] === "pointermove"),
    ).toBe(true)
  })
})

// ─────────────────────────────────────────────────────────────────────
// useSpringPress — Layer 8 click feedback (level-independent)
// ─────────────────────────────────────────────────────────────────────

describe("useSpringPress", () => {
  it("toggles data-pressing across pointer down / up / leave", () => {
    const { result } = renderHook(() => useSpringPress())
    expect(result.current.className).toBe("spring-press")
    expect(result.current.pressProps["data-pressing"]).toBeUndefined()

    act(() => {
      result.current.pressProps.onPointerDown()
    })
    expect(result.current.pressProps["data-pressing"]).toBe("true")

    act(() => {
      result.current.pressProps.onPointerUp()
    })
    expect(result.current.pressProps["data-pressing"]).toBeUndefined()

    act(() => {
      result.current.pressProps.onPointerDown()
    })
    expect(result.current.pressProps["data-pressing"]).toBe("true")

    act(() => {
      result.current.pressProps.onPointerLeave()
    })
    expect(result.current.pressProps["data-pressing"]).toBeUndefined()
  })
})
