import { describe, expect, it, beforeEach, afterEach } from "vitest"
import { act, renderHook } from "@testing-library/react"

import {
  DEFAULT_UI_MODE,
  getUiMode,
  setUiMode,
  subscribeUiMode,
  useUiMode,
} from "@/hooks/use-ui-mode"

describe("ui-mode dial", () => {
  beforeEach(() => {
    window.localStorage.clear()
  })
  afterEach(() => {
    window.localStorage.clear()
  })

  it("defaults to focus and persists a set", () => {
    expect(getUiMode()).toBe(DEFAULT_UI_MODE)
    expect(DEFAULT_UI_MODE).toBe("focus")
    setUiMode("immersive")
    expect(getUiMode()).toBe("immersive")
    expect(window.localStorage.getItem("omnisight:ui-mode")).toBe("immersive")
  })

  it("notifies subscribers on same-tab change", () => {
    const seen: string[] = []
    const unsub = subscribeUiMode((m) => seen.push(m))
    setUiMode("immersive")
    setUiMode("focus")
    unsub()
    setUiMode("immersive") // after unsub — should NOT be seen
    expect(seen).toEqual(["immersive", "focus"])
  })

  it("useUiMode reflects, toggles, and stays in sync across instances", () => {
    const a = renderHook(() => useUiMode())
    const b = renderHook(() => useUiMode())

    expect(a.result.current.mode).toBe("focus")
    expect(a.result.current.immersive).toBe(false)

    act(() => { a.result.current.toggle() })
    expect(a.result.current.mode).toBe("immersive")
    expect(a.result.current.immersive).toBe(true)
    // the second instance picks the change up via the shared event bus
    expect(b.result.current.mode).toBe("immersive")

    act(() => { b.result.current.setMode("focus") })
    expect(a.result.current.mode).toBe("focus")
  })
})
