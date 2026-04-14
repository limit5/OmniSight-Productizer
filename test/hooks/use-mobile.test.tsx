/** Fix-D D7: useIsMobile matchMedia contract. */
import { renderHook, act } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { useIsMobile } from "@/hooks/use-mobile"

type Listener = () => void

function installMatchMedia(matches: boolean) {
  const listeners = new Set<Listener>()
  const mqlLike = {
    matches,
    media: "",
    onchange: null,
    addEventListener: (_type: string, cb: Listener) => listeners.add(cb),
    removeEventListener: (_type: string, cb: Listener) => listeners.delete(cb),
    addListener: (cb: Listener) => listeners.add(cb),
    removeListener: (cb: Listener) => listeners.delete(cb),
    dispatchEvent: () => true,
  }
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    configurable: true,
    value: vi.fn().mockReturnValue(mqlLike),
  })
  return { listeners, mqlLike }
}

function setInnerWidth(w: number) {
  Object.defineProperty(window, "innerWidth", {
    writable: true, configurable: true, value: w,
  })
}

describe("useIsMobile", () => {
  beforeEach(() => {
    installMatchMedia(false)
    setInnerWidth(1024)  // desktop
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it("returns false on desktop widths", () => {
    setInnerWidth(1280)
    const { result } = renderHook(() => useIsMobile())
    expect(result.current).toBe(false)
  })

  it("returns true below 768px", () => {
    setInnerWidth(500)
    const { result } = renderHook(() => useIsMobile())
    expect(result.current).toBe(true)
  })

  it("returns true exactly at 767px (boundary)", () => {
    setInnerWidth(767)
    const { result } = renderHook(() => useIsMobile())
    expect(result.current).toBe(true)
  })

  it("returns false at exactly 768px (boundary)", () => {
    setInnerWidth(768)
    const { result } = renderHook(() => useIsMobile())
    expect(result.current).toBe(false)
  })

  it("reacts to media-query change events", () => {
    const { listeners } = installMatchMedia(false)
    setInnerWidth(1200)
    const { result } = renderHook(() => useIsMobile())
    expect(result.current).toBe(false)

    // Simulate resize crossing the breakpoint
    act(() => {
      setInnerWidth(500)
      listeners.forEach(cb => cb())
    })
    expect(result.current).toBe(true)
  })

  it("registers and removes the matchMedia listener", () => {
    const addSpy = vi.fn()
    const removeSpy = vi.fn()
    Object.defineProperty(window, "matchMedia", {
      writable: true, configurable: true,
      value: vi.fn().mockReturnValue({
        matches: false, media: "", onchange: null,
        addEventListener: addSpy, removeEventListener: removeSpy,
        addListener: () => {}, removeListener: () => {}, dispatchEvent: () => true,
      }),
    })
    const { unmount } = renderHook(() => useIsMobile())
    expect(addSpy).toHaveBeenCalledTimes(1)
    unmount()
    expect(removeSpy).toHaveBeenCalledTimes(1)
  })
})
