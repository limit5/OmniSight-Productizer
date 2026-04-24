/**
 * Q.6 #300 (2026-04-24, checkbox 1) — useDraftPersistence hook contract.
 *
 * The hook backs the 500 ms debounce write from the INVOKE command bar
 * and the workspace chat composer. Tests cover:
 *
 *   1. First render does NOT fire — would clobber server-side row
 *      with whatever local state the parent restored.
 *   2. After 500 ms of quiet, the writer is called once with the
 *      latest value.
 *   3. Rapid typing collapses to a single trailing call (debounce).
 *   4. ``enabled = false`` short-circuits — no timer, no call.
 *   5. Unmount during the debounce window cancels the pending call.
 *   6. Writer rejection is swallowed (typing must not surface errors).
 */
import { act, renderHook } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import {
  DRAFT_DEBOUNCE_MS,
  useDraftPersistence,
} from "@/hooks/use-draft-persistence"
import { readDraftLocalEntry } from "@/lib/draft-sync-bus"

beforeEach(() => {
  vi.useFakeTimers()
  window.localStorage.clear()
})

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
  window.localStorage.clear()
})

describe("useDraftPersistence", () => {
  it("does NOT call writer on first mount", () => {
    const writer = vi.fn().mockResolvedValue({})
    renderHook(() =>
      useDraftPersistence({
        slotKey: "invoke:main",
        value: "loaded from local-storage",
        writer,
      }),
    )
    // Even after the debounce window passes, the first-mount value
    // must not be persisted — that would overwrite the server-side
    // row with the local cache before the restore flow has a chance.
    act(() => {
      vi.advanceTimersByTime(DRAFT_DEBOUNCE_MS + 50)
    })
    expect(writer).not.toHaveBeenCalled()
  })

  it("fires writer 500ms after the value changes", () => {
    const writer = vi.fn().mockResolvedValue({})
    let value = "initial"
    const { rerender } = renderHook(() =>
      useDraftPersistence({
        slotKey: "invoke:main",
        value,
        writer,
      }),
    )
    value = "typed"
    rerender()
    expect(writer).not.toHaveBeenCalled()

    act(() => {
      vi.advanceTimersByTime(499)
    })
    expect(writer).not.toHaveBeenCalled()

    act(() => {
      vi.advanceTimersByTime(1)
    })
    expect(writer).toHaveBeenCalledTimes(1)
    expect(writer).toHaveBeenCalledWith("invoke:main", "typed")
  })

  it("debounces rapid typing into a single trailing call", () => {
    const writer = vi.fn().mockResolvedValue({})
    let value = "a"
    const { rerender } = renderHook(() =>
      useDraftPersistence({
        slotKey: "chat:main",
        value,
        writer,
      }),
    )

    // Five quick keystrokes within the debounce window.
    for (const ch of ["ab", "abc", "abcd", "abcde", "abcdef"]) {
      value = ch
      act(() => {
        vi.advanceTimersByTime(100)
      })
      rerender()
    }
    // Total elapsed ≈ 500 ms split across 5 ticks; the debounce
    // resets each rerender, so no fire yet.
    expect(writer).not.toHaveBeenCalled()

    // Quiet for 500 ms — single trailing call wins.
    act(() => {
      vi.advanceTimersByTime(DRAFT_DEBOUNCE_MS)
    })
    expect(writer).toHaveBeenCalledTimes(1)
    expect(writer).toHaveBeenCalledWith("chat:main", "abcdef")
  })

  it("does not call writer when enabled=false even after value change", () => {
    const writer = vi.fn().mockResolvedValue({})
    let value = "x"
    const { rerender } = renderHook(() =>
      useDraftPersistence({
        slotKey: "invoke:main",
        value,
        writer,
        enabled: false,
      }),
    )
    value = "y"
    rerender()
    act(() => {
      vi.advanceTimersByTime(DRAFT_DEBOUNCE_MS + 50)
    })
    expect(writer).not.toHaveBeenCalled()
  })

  it("cancels pending writer on unmount", () => {
    const writer = vi.fn().mockResolvedValue({})
    let value = "x"
    const { rerender, unmount } = renderHook(() =>
      useDraftPersistence({
        slotKey: "invoke:main",
        value,
        writer,
      }),
    )
    value = "typed"
    rerender()
    act(() => {
      vi.advanceTimersByTime(200)
    })
    unmount()
    act(() => {
      vi.advanceTimersByTime(DRAFT_DEBOUNCE_MS)
    })
    expect(writer).not.toHaveBeenCalled()
  })

  it("swallows writer rejection — typing must not throw", async () => {
    const writer = vi.fn().mockRejectedValue(new Error("boom"))
    let value = "x"
    const { rerender } = renderHook(() =>
      useDraftPersistence({
        slotKey: "invoke:main",
        value,
        writer,
      }),
    )
    value = "typed"
    rerender()
    act(() => {
      vi.advanceTimersByTime(DRAFT_DEBOUNCE_MS)
    })
    // Allow microtasks to drain so any unhandled rejection would
    // surface — none should.
    await Promise.resolve()
    expect(writer).toHaveBeenCalledTimes(1)
    // No assertion needed beyond "the test did not crash" — vitest
    // would fail the test on an unhandled rejection.
  })

  it("echoes server {content, updated_at} into local storage after a successful PUT", async () => {
    // Q.6 checkbox 4 — the echoed pair is what the next restore
    // compares against when deciding whether to surface the toast.
    const writer = vi.fn().mockResolvedValue({
      slot_key: "invoke:main",
      content: "typed",
      updated_at: 12345.6,
    })
    let value = "x"
    const { rerender } = renderHook(() =>
      useDraftPersistence({
        slotKey: "invoke:main",
        value,
        writer,
      }),
    )
    value = "typed"
    rerender()
    act(() => {
      vi.advanceTimersByTime(DRAFT_DEBOUNCE_MS)
    })
    expect(writer).toHaveBeenCalledTimes(1)
    // Drain microtasks so the .then() that writes localStorage runs.
    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(readDraftLocalEntry("invoke:main")).toEqual({
      content: "typed",
      updated_at: 12345.6,
    })
  })

  it("skips local echo when writer returns a non-DraftResponse shape", async () => {
    // Guards against tests that stub the writer with ``{}`` — we must
    // not crash or write bogus data.
    const writer = vi.fn().mockResolvedValue({})
    let value = "x"
    const { rerender } = renderHook(() =>
      useDraftPersistence({
        slotKey: "invoke:main",
        value,
        writer,
      }),
    )
    value = "typed"
    rerender()
    act(() => {
      vi.advanceTimersByTime(DRAFT_DEBOUNCE_MS)
    })
    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(readDraftLocalEntry("invoke:main")).toBeNull()
  })

  it("skips local echo when persistLocalEcho=false even on a shaped response", async () => {
    const writer = vi.fn().mockResolvedValue({
      slot_key: "invoke:main",
      content: "typed",
      updated_at: 1,
    })
    let value = "x"
    const { rerender } = renderHook(() =>
      useDraftPersistence({
        slotKey: "invoke:main",
        value,
        writer,
        persistLocalEcho: false,
      }),
    )
    value = "typed"
    rerender()
    act(() => {
      vi.advanceTimersByTime(DRAFT_DEBOUNCE_MS)
    })
    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(readDraftLocalEntry("invoke:main")).toBeNull()
  })

  it("uses a custom debounce window when supplied", () => {
    const writer = vi.fn().mockResolvedValue({})
    let value = "x"
    const { rerender } = renderHook(() =>
      useDraftPersistence({
        slotKey: "invoke:main",
        value,
        writer,
        debounceMs: 50,
      }),
    )
    value = "y"
    rerender()
    act(() => {
      vi.advanceTimersByTime(49)
    })
    expect(writer).not.toHaveBeenCalled()
    act(() => {
      vi.advanceTimersByTime(2)
    })
    expect(writer).toHaveBeenCalledTimes(1)
  })
})
