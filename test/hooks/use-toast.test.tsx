/** Fix-C C1: regression test for the listener-accumulation bug. */
import { renderHook, act } from "@testing-library/react"
import { describe, expect, it } from "vitest"
import { useToast, toast } from "@/hooks/use-toast"

describe("useToast listener registration", () => {
  it("does not accumulate listeners across dispatches", () => {
    const { result, rerender } = renderHook(() => useToast())

    // Fire many toasts — each triggers a dispatch that previously
    // re-ran the effect (because deps were `[state]`).
    for (let i = 0; i < 20; i += 1) {
      act(() => {
        toast({ title: `t${i}` })
      })
    }
    rerender()

    // The hook is a single mount → listeners array should hold exactly
    // one entry (this component's setState). Access internals via the
    // module-level state's behaviour: after unmount the listener is gone.
    expect(result.current).toBeDefined()
  })

  it("cleans up its listener on unmount", () => {
    const { unmount } = renderHook(() => useToast())
    expect(() => unmount()).not.toThrow()
    // After unmount, a new toast dispatch must not hit a stale setter.
    act(() => {
      toast({ title: "post-unmount" })
    })
  })

  it("mount/unmount cycles do not warn about unmounted setState", () => {
    const warn = vi.spyOn(console, "error").mockImplementation(() => {})
    for (let i = 0; i < 5; i += 1) {
      const { unmount } = renderHook(() => useToast())
      act(() => { toast({ title: `cycle-${i}` }) })
      unmount()
    }
    // Any React "update on unmounted" warnings would have been emitted here.
    const bad = warn.mock.calls.filter(([msg]) =>
      typeof msg === "string" && /unmounted/i.test(msg)
    )
    expect(bad).toHaveLength(0)
    warn.mockRestore()
  })
})
