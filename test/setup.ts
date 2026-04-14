/**
 * Phase 49A — Vitest setup.
 *
 * Loaded once per test run (see vitest.config.ts setupFiles). Adds
 * jest-dom matchers and polyfills for browser APIs that jsdom omits but
 * our components assume — most notably EventSource (components subscribe
 * to SSE on mount).
 */

import "@testing-library/jest-dom/vitest"
import { afterEach, vi } from "vitest"
import { cleanup } from "@testing-library/react"

// ─── Polyfills ───

// jsdom does not ship EventSource. Provide a minimal stand-in that
// individual tests can re-override to emit events. Constructors return
// an object that mimics the subset of the API our app uses.
class MockEventSource {
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
  private listeners: Record<string, Array<(e: MessageEvent) => void>> = {}

  constructor(url: string) {
    this.url = url
    MockEventSource._instances.push(this)
  }
  addEventListener(type: string, listener: (e: MessageEvent) => void) {
    ;(this.listeners[type] ||= []).push(listener)
  }
  removeEventListener(type: string, listener: (e: MessageEvent) => void) {
    this.listeners[type] = (this.listeners[type] || []).filter(l => l !== listener)
  }
  /**
   * Dispatch a fake SSE event. Real EventSource calls both the matching
   * `addEventListener` listeners AND the property handler (`onmessage`
   * for default-typed "message" events), so mirror that — otherwise
   * components that use only `onmessage =` slip through our tests.
   */
  emit(type: string, data: unknown) {
    if (this.readyState === 2) return  // closed — drop like the real API
    const payload = new MessageEvent(type, { data: JSON.stringify(data) })
    for (const l of this.listeners[type] || []) l(payload)
    if (type === "message") this.onmessage?.(payload)
  }
  /** Fake the `open` handshake — property and addEventListener paths. */
  emitOpen() {
    const ev = new Event("open")
    for (const l of this.listeners["open"] || []) l(ev as MessageEvent)
    this.onopen?.(ev)
  }
  /**
   * N5: simulate a connection-level error. Real EventSource fires
   * `onerror` AND any `addEventListener("error", …)` listeners; we honour
   * both so tests can exercise both attach styles.
   */
  emitError() {
    const err = new Event("error")
    for (const l of this.listeners["error"] || []) l(err as MessageEvent)
    this.onerror?.(err)
  }
  /**
   * N1: close() now clears listeners so stale handlers from a prior
   * test can't fire across boundaries. Mirrors what a torn-down real
   * EventSource effectively does (the GC collects it shortly after).
   */
  close() {
    this.readyState = 2
    this.listeners = {}
    this.onerror = null
    this.onmessage = null
    this.onopen = null
  }

  /** Instances created during the current test — cleared in afterEach. */
  static _instances: MockEventSource[] = []
  static latest(): MockEventSource | undefined {
    return MockEventSource._instances[MockEventSource._instances.length - 1]
  }
  static reset() {
    MockEventSource._instances = []
  }
}

// Type-cast through `unknown` so TS accepts the minimal shim.
;(globalThis as unknown as { EventSource: typeof MockEventSource }).EventSource = MockEventSource
// Export for tests via the module's symbol.
;(globalThis as unknown as { __MockEventSource: typeof MockEventSource }).__MockEventSource = MockEventSource

// ─── Per-test reset ───

afterEach(() => {
  cleanup()
  MockEventSource.reset()
  vi.restoreAllMocks()
  vi.clearAllTimers()
  vi.useRealTimers()
})
