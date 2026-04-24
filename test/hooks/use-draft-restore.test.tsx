/**
 * Q.6 #300 (2026-04-24, checkbox 2) — useDraftRestore hook contract.
 *
 * The hook backs the one-shot ``GET /user/drafts/{slot_key}`` on
 * composer mount so a new device picks up the last server-side
 * draft. Tests cover:
 *
 *   1. Fires exactly once on mount with the given slot key.
 *   2. ``onRestore`` called with the fetched draft when content is
 *      non-empty.
 *   3. ``onRestore`` NOT called when the server returns an empty
 *      shape (``content=""`` — the "never typed here" miss).
 *   4. ``enabled = false`` short-circuits — no fetch, no callback.
 *   5. Reader rejection is swallowed (page load must not toast).
 *   6. Unmount before resolution does not call ``onRestore``.
 *   7. Rerendering the parent with a new callback identity does NOT
 *      re-trigger the fetch — mount-scoped, by design.
 */
import { act, renderHook } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { useDraftRestore } from "@/hooks/use-draft-restore"

afterEach(() => {
  vi.restoreAllMocks()
})

describe("useDraftRestore", () => {
  it("fires reader on mount with the slot key and calls onRestore on a hit", async () => {
    const reader = vi.fn().mockResolvedValue({
      slot_key: "invoke:main",
      content: "restored body",
      updated_at: 1000,
    })
    const onRestore = vi.fn()
    renderHook(() =>
      useDraftRestore({
        slotKey: "invoke:main",
        reader,
        onRestore,
      }),
    )
    await act(async () => {
      await Promise.resolve()
    })
    expect(reader).toHaveBeenCalledTimes(1)
    expect(reader).toHaveBeenCalledWith("invoke:main")
    expect(onRestore).toHaveBeenCalledTimes(1)
    expect(onRestore).toHaveBeenCalledWith({
      slot_key: "invoke:main",
      content: "restored body",
      updated_at: 1000,
    })
  })

  it("does NOT call onRestore when the server returns an empty shape", async () => {
    const reader = vi.fn().mockResolvedValue({
      slot_key: "chat:main",
      content: "",
      updated_at: null,
    })
    const onRestore = vi.fn()
    renderHook(() =>
      useDraftRestore({
        slotKey: "chat:main",
        reader,
        onRestore,
      }),
    )
    await act(async () => {
      await Promise.resolve()
    })
    expect(reader).toHaveBeenCalledTimes(1)
    expect(onRestore).not.toHaveBeenCalled()
  })

  it("does nothing when enabled=false", async () => {
    const reader = vi.fn().mockResolvedValue({
      slot_key: "invoke:main",
      content: "stuff",
      updated_at: 1,
    })
    const onRestore = vi.fn()
    renderHook(() =>
      useDraftRestore({
        slotKey: "invoke:main",
        reader,
        onRestore,
        enabled: false,
      }),
    )
    await act(async () => {
      await Promise.resolve()
    })
    expect(reader).not.toHaveBeenCalled()
    expect(onRestore).not.toHaveBeenCalled()
  })

  it("swallows reader rejection — restore must not throw", async () => {
    const reader = vi.fn().mockRejectedValue(new Error("boom"))
    const onRestore = vi.fn()
    renderHook(() =>
      useDraftRestore({
        slotKey: "invoke:main",
        reader,
        onRestore,
      }),
    )
    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(reader).toHaveBeenCalledTimes(1)
    expect(onRestore).not.toHaveBeenCalled()
  })

  it("does not call onRestore after unmount", async () => {
    let resolveRead: (value: unknown) => void = () => {}
    const reader = vi.fn().mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveRead = resolve
        }),
    )
    const onRestore = vi.fn()
    const { unmount } = renderHook(() =>
      useDraftRestore({
        slotKey: "invoke:main",
        reader,
        onRestore,
      }),
    )
    unmount()
    resolveRead({
      slot_key: "invoke:main",
      content: "late arrival",
      updated_at: 1,
    })
    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(onRestore).not.toHaveBeenCalled()
  })

  it("does not refetch when the onRestore identity changes", async () => {
    const reader = vi.fn().mockResolvedValue({
      slot_key: "invoke:main",
      content: "stuff",
      updated_at: 1,
    })
    let onRestore = vi.fn()
    const { rerender } = renderHook(
      ({ cb }: { cb: (draft: unknown) => void }) =>
        useDraftRestore({
          slotKey: "invoke:main",
          reader,
          onRestore: cb,
        }),
      { initialProps: { cb: onRestore } },
    )
    await act(async () => {
      await Promise.resolve()
    })
    expect(reader).toHaveBeenCalledTimes(1)

    // Swap the callback identity — rerender-only.
    onRestore = vi.fn()
    rerender({ cb: onRestore })
    await act(async () => {
      await Promise.resolve()
    })
    // Still exactly one fetch — mount-scoped effect.
    expect(reader).toHaveBeenCalledTimes(1)
  })
})
