/**
 * Q.6 #300 (2026-04-24, checkbox 4) — draft-sync-bus contract.
 *
 * Covers the local-storage accessors + the in-process pub/sub bus
 * that ``useDraftRestore`` uses to fire「從他裝置同步了草稿」toasts
 * when the server's ``updated_at`` beats this device's cache.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import {
  _resetDraftSyncListenersForTests,
  clearDraftLocalEntry,
  emitDraftSynced,
  onDraftSynced,
  readDraftLocalEntry,
  writeDraftLocalEntry,
} from "@/lib/draft-sync-bus"

beforeEach(() => {
  window.localStorage.clear()
  _resetDraftSyncListenersForTests()
})

afterEach(() => {
  window.localStorage.clear()
  _resetDraftSyncListenersForTests()
  vi.restoreAllMocks()
})

describe("draft-sync-bus — local storage accessors", () => {
  it("reads null for a slot that has never been written", () => {
    expect(readDraftLocalEntry("invoke:main")).toBeNull()
  })

  it("round-trips {content, updated_at} through localStorage", () => {
    writeDraftLocalEntry("invoke:main", { content: "hello", updated_at: 1234.5 })
    expect(readDraftLocalEntry("invoke:main")).toEqual({
      content: "hello",
      updated_at: 1234.5,
    })
  })

  it("isolates per slot key", () => {
    writeDraftLocalEntry("invoke:main", { content: "A", updated_at: 1 })
    writeDraftLocalEntry("chat:main", { content: "B", updated_at: 2 })
    expect(readDraftLocalEntry("invoke:main")).toEqual({
      content: "A",
      updated_at: 1,
    })
    expect(readDraftLocalEntry("chat:main")).toEqual({
      content: "B",
      updated_at: 2,
    })
  })

  it("returns null when only half the pair is present (schema guard)", () => {
    // Simulate a torn write where only the content key made it to disk.
    window.localStorage.setItem("omnisight:draft:invoke:main:content", "only content")
    expect(readDraftLocalEntry("invoke:main")).toBeNull()
  })

  it("returns null when updated_at is not a finite number", () => {
    window.localStorage.setItem("omnisight:draft:invoke:main:content", "x")
    window.localStorage.setItem("omnisight:draft:invoke:main:updated_at", "not-a-number")
    expect(readDraftLocalEntry("invoke:main")).toBeNull()
  })

  it("clearDraftLocalEntry removes the pair", () => {
    writeDraftLocalEntry("invoke:main", { content: "x", updated_at: 1 })
    expect(readDraftLocalEntry("invoke:main")).not.toBeNull()
    clearDraftLocalEntry("invoke:main")
    expect(readDraftLocalEntry("invoke:main")).toBeNull()
  })

  it("swallows write failures so typing never throws", () => {
    // Patch setItem to throw the way Safari private mode + quota do.
    const spy = vi
      .spyOn(Storage.prototype, "setItem")
      .mockImplementation(() => {
        throw new Error("QuotaExceededError")
      })
    expect(() =>
      writeDraftLocalEntry("invoke:main", { content: "x", updated_at: 1 }),
    ).not.toThrow()
    spy.mockRestore()
  })
})

describe("draft-sync-bus — onDraftSynced pub/sub", () => {
  it("delivers emitted events to every subscriber", () => {
    const a = vi.fn()
    const b = vi.fn()
    onDraftSynced(a)
    onDraftSynced(b)

    emitDraftSynced({
      slotKey: "invoke:main",
      content: "hello from peer",
      remoteUpdatedAt: 42,
      localUpdatedAt: 10,
    })

    expect(a).toHaveBeenCalledTimes(1)
    expect(b).toHaveBeenCalledTimes(1)
    expect(a).toHaveBeenCalledWith({
      slotKey: "invoke:main",
      content: "hello from peer",
      remoteUpdatedAt: 42,
      localUpdatedAt: 10,
    })
  })

  it("unsubscribe detaches the listener", () => {
    const l = vi.fn()
    const off = onDraftSynced(l)
    off()
    emitDraftSynced({
      slotKey: "chat:main",
      content: "x",
      remoteUpdatedAt: 1,
      localUpdatedAt: null,
    })
    expect(l).not.toHaveBeenCalled()
  })

  it("does NOT let a throwing listener starve the others", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {})
    const bad = vi.fn().mockImplementation(() => {
      throw new Error("listener exploded")
    })
    const good = vi.fn()
    onDraftSynced(bad)
    onDraftSynced(good)

    emitDraftSynced({
      slotKey: "invoke:main",
      content: "x",
      remoteUpdatedAt: 1,
      localUpdatedAt: null,
    })

    expect(bad).toHaveBeenCalledTimes(1)
    expect(good).toHaveBeenCalledTimes(1)
    expect(warn).toHaveBeenCalled()
    warn.mockRestore()
  })
})
