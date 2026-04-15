/**
 * Phase 49-Fix N2 — integration test for the shared SSE manager.
 *
 * Every component test under test/components/* mocks the `@/lib/api`
 * module wholesale, so they never execute the real `subscribeEvents`
 * implementation. This file is deliberately the opposite: it imports
 * the real module and verifies the ref-counting contract that 48-Fix
 * Batch A introduced:
 *
 *   - 2 subscribers → only 1 EventSource is ever constructed
 *   - unsubscribing one keeps the stream alive
 *   - unsubscribing the last one closes it
 *   - a new subscribe after total teardown builds a fresh EventSource
 *   - events fanning out reach every active listener
 *   - errors propagate to every error listener
 */

import { describe, expect, it, vi, beforeEach } from "vitest"

// Track every EventSource constructed during a test so we can assert
// counts. Each test resets the counter in beforeEach.
let ctorCount = 0
// Expose the most recently constructed instance directly — no proxy
// object needed; a TrackedEventSource already has readyState, close,
// fire, and fireError as real members.
let latestInstance: TrackedEventSource | null = null

class TrackedEventSource {
  static readonly CONNECTING = 0
  static readonly OPEN = 1
  static readonly CLOSED = 2
  readonly CONNECTING = 0
  readonly OPEN = 1
  readonly CLOSED = 2
  url: string
  readyState = 1
  onerror: ((e: Event) => void) | null = null
  onmessage: ((e: MessageEvent) => void) | null = null
  onopen: ((e: Event) => void) | null = null
  private listeners: Record<string, Array<(e: Event) => void>> = {}

  constructor(url: string) {
    this.url = url
    ctorCount++
    latestInstance = this // eslint-disable-line @typescript-eslint/no-this-alias
  }
  addEventListener(type: string, listener: (e: Event) => void) {
    ;(this.listeners[type] ||= []).push(listener)
  }
  removeEventListener(type: string, listener: (e: Event) => void) {
    this.listeners[type] = (this.listeners[type] || []).filter(l => l !== listener)
  }
  close() {
    this.readyState = 2
    this.listeners = {}
  }
  fire(type: string, data: unknown) {
    if (this.readyState === 2) return
    const ev = new MessageEvent(type, { data: JSON.stringify(data) })
    for (const l of this.listeners[type] || []) l(ev)
  }
  fireError() {
    this.onerror?.(new Event("error"))
  }
}

// Swap the global EventSource BEFORE importing lib/api so the shared
// manager picks up the tracked implementation. Also re-import `lib/api`
// fresh each test via vi.resetModules() to defeat the module-scoped
// _sharedES singleton between scenarios.
;(globalThis as unknown as { EventSource: typeof TrackedEventSource }).EventSource = TrackedEventSource

beforeEach(async () => {
  ctorCount = 0
  latestInstance = null
  vi.resetModules()
})

async function importApi() {
  return await import("@/lib/api")
}

describe("shared SSE manager", () => {
  it("two subscribers share a single EventSource", async () => {
    const { subscribeEvents } = await importApi()
    const a = subscribeEvents(() => {})
    const b = subscribeEvents(() => {})
    expect(ctorCount).toBe(1)
    // Both handles report the shared stream as OPEN
    expect(a.readyState).toBe(1)
    expect(b.readyState).toBe(1)
  })

  it("unsubscribing one keeps the stream alive; last unsubscribe closes it", async () => {
    const { subscribeEvents } = await importApi()
    const a = subscribeEvents(() => {})
    const b = subscribeEvents(() => {})
    expect(latestInstance?.readyState).toBe(1)
    a.close()
    // still open because b is alive
    expect(latestInstance?.readyState).toBe(1)
    b.close()
    // last subscriber left → stream torn down
    expect(latestInstance?.readyState).toBe(2)
  })

  it("a resubscribe after full teardown builds a new EventSource", async () => {
    const { subscribeEvents } = await importApi()
    const a = subscribeEvents(() => {})
    a.close()
    expect(ctorCount).toBe(1)
    expect(latestInstance?.readyState).toBe(2)
    const b = subscribeEvents(() => {})
    expect(ctorCount).toBe(2)  // a *new* one
    expect(latestInstance?.readyState).toBe(1)
    b.close()
  })

  it("fans an event out to every active listener", async () => {
    const { subscribeEvents } = await importApi()
    const seenA: string[] = []
    const seenB: string[] = []
    const a = subscribeEvents(ev => { seenA.push(ev.event) })
    const b = subscribeEvents(ev => { seenB.push(ev.event) })
    // Fire an event known to be in SSE_EVENT_TYPES
    latestInstance?.fire("heartbeat", { subscribers: 2 })
    expect(seenA).toEqual(["heartbeat"])
    expect(seenB).toEqual(["heartbeat"])
    a.close(); b.close()
  })

  it("a listener that throws does not block other listeners", async () => {
    const { subscribeEvents } = await importApi()
    const seenB: string[] = []
    const spy = vi.spyOn(console, "warn").mockImplementation(() => {})
    const a = subscribeEvents(() => { throw new Error("boom") })
    const b = subscribeEvents(ev => { seenB.push(ev.event) })
    latestInstance?.fire("heartbeat", { subscribers: 2 })
    expect(seenB).toEqual(["heartbeat"])
    expect(spy).toHaveBeenCalled()
    a.close(); b.close()
  })

  it("error listeners fire on connection error", async () => {
    const { subscribeEvents } = await importApi()
    const errs: unknown[] = []
    const a = subscribeEvents(() => {}, (e) => { errs.push(e) })
    latestInstance?.fireError()
    expect(errs.length).toBe(1)
    a.close()
  })

  it("closing the same handle twice is a no-op", async () => {
    const { subscribeEvents } = await importApi()
    const a = subscribeEvents(() => {})
    a.close()
    a.close()
    // The stream was already torn down by the first close; second close
    // must not explode and ctorCount should stay at 1.
    expect(ctorCount).toBe(1)
  })
})
