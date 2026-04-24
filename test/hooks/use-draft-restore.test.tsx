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
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { useDraftRestore } from "@/hooks/use-draft-restore"
import {
  _resetDraftSyncListenersForTests,
  onDraftSynced,
  readDraftLocalEntry,
  writeDraftLocalEntry,
  type DraftSyncEvent,
} from "@/lib/draft-sync-bus"

beforeEach(() => {
  window.localStorage.clear()
  _resetDraftSyncListenersForTests()
})

afterEach(() => {
  vi.restoreAllMocks()
  window.localStorage.clear()
  _resetDraftSyncListenersForTests()
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

  it("emits draft_synced event when remote updated_at > local echo (conflict toast trigger)", async () => {
    // Simulate: this device wrote the slot at t=1000 (local echo), then
    // peer device wrote at t=2000 on the server. On restore, we should
    // adopt the remote and fire a sync event.
    writeDraftLocalEntry("invoke:main", { content: "old local", updated_at: 1000 })
    const reader = vi.fn().mockResolvedValue({
      slot_key: "invoke:main",
      content: "newer remote",
      updated_at: 2000,
    })
    const onRestore = vi.fn()
    const received: DraftSyncEvent[] = []
    onDraftSynced((ev) => { received.push(ev) })

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
    expect(onRestore).toHaveBeenCalledTimes(1)
    expect(onRestore).toHaveBeenCalledWith({
      slot_key: "invoke:main",
      content: "newer remote",
      updated_at: 2000,
    })
    expect(received).toHaveLength(1)
    expect(received[0]).toEqual({
      slotKey: "invoke:main",
      content: "newer remote",
      remoteUpdatedAt: 2000,
      localUpdatedAt: 1000,
    })
    // Local storage is refreshed to the adopted row so the next restore
    // treats this device as up-to-date.
    expect(readDraftLocalEntry("invoke:main")).toEqual({
      content: "newer remote",
      updated_at: 2000,
    })
  })

  it("emits draft_synced with localUpdatedAt=null on a fresh device (no local echo)", async () => {
    // New device: no local echo at all. Server returns a real draft a
    // peer wrote. The toast must still fire so the operator knows the
    // composer was pre-populated from elsewhere.
    const reader = vi.fn().mockResolvedValue({
      slot_key: "chat:main",
      content: "from the other laptop",
      updated_at: 555,
    })
    const onRestore = vi.fn()
    const received: DraftSyncEvent[] = []
    onDraftSynced((ev) => { received.push(ev) })

    renderHook(() =>
      useDraftRestore({
        slotKey: "chat:main",
        reader,
        onRestore,
      }),
    )
    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(onRestore).toHaveBeenCalledTimes(1)
    expect(received).toHaveLength(1)
    expect(received[0]).toMatchObject({
      slotKey: "chat:main",
      content: "from the other laptop",
      remoteUpdatedAt: 555,
      localUpdatedAt: null,
    })
  })

  it("does NOT emit when remote updated_at matches the local echo (same device)", async () => {
    // This device just wrote the slot — the server row and local echo
    // agree. Adopt the row but skip the toast.
    writeDraftLocalEntry("invoke:main", { content: "same", updated_at: 777 })
    const reader = vi.fn().mockResolvedValue({
      slot_key: "invoke:main",
      content: "same",
      updated_at: 777,
    })
    const onRestore = vi.fn()
    const received: DraftSyncEvent[] = []
    onDraftSynced((ev) => { received.push(ev) })

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
    expect(onRestore).toHaveBeenCalledTimes(1)
    expect(received).toHaveLength(0)
  })

  it("does NOT emit when remote updated_at is older than the local echo", async () => {
    // Edge: clock skew / we won the race — server row is stale. No
    // toast, per the last-writer-wins spec.
    writeDraftLocalEntry("invoke:main", { content: "fresh local", updated_at: 9999 })
    const reader = vi.fn().mockResolvedValue({
      slot_key: "invoke:main",
      content: "stale remote",
      updated_at: 1,
    })
    const onRestore = vi.fn()
    const received: DraftSyncEvent[] = []
    onDraftSynced((ev) => { received.push(ev) })

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
    // onRestore still fires — the spec says restore always adopts
    // non-empty content; only the toast is gated on remote > local.
    expect(onRestore).toHaveBeenCalledTimes(1)
    expect(received).toHaveLength(0)
  })

  it("emits when remote == local timestamp but content diverges (tie-break)", async () => {
    // Very rare: two devices write inside the same second with the
    // same wall-clock. The ts matches, but content doesn't. Surface
    // the toast so the operator at least knows the composer changed
    // underneath them.
    writeDraftLocalEntry("chat:main", { content: "local edit", updated_at: 100 })
    const reader = vi.fn().mockResolvedValue({
      slot_key: "chat:main",
      content: "peer edit",
      updated_at: 100,
    })
    const onRestore = vi.fn()
    const received: DraftSyncEvent[] = []
    onDraftSynced((ev) => { received.push(ev) })

    renderHook(() =>
      useDraftRestore({
        slotKey: "chat:main",
        reader,
        onRestore,
      }),
    )
    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(received).toHaveLength(1)
    expect(received[0]).toMatchObject({
      slotKey: "chat:main",
      content: "peer edit",
      remoteUpdatedAt: 100,
      localUpdatedAt: 100,
    })
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
