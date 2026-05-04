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
import enMessages from "../messages/en.json"

// FX.9.9 — global next-intl shim for vitest.
//
// Most component tests render auth pages / dashboards directly without
// wrapping in `<I18nProvider>` (which is the source of the
// `<NextIntlClientProvider>`). Calling `useTranslations()` outside that
// provider throws. Rather than touch every existing test to add a
// provider wrapper, we install a deterministic mock that resolves
// dot-keys against `messages/en.json` — the canonical baseline the
// drift-guard test pins everyone else to. This keeps test text
// assertions identical to what would render under the real provider in
// the default English locale.
//
// `t.rich`, `t.markup`, `t.has`, `t.raw` are not used by the migrated
// components and intentionally not stubbed; tests that need them can
// still re-mock `next-intl` per-file to override.
function resolveDotKey(bundle: Record<string, unknown>, dotted: string): string | undefined {
  let cursor: unknown = bundle
  for (const seg of dotted.split(".")) {
    if (cursor && typeof cursor === "object" && seg in (cursor as Record<string, unknown>)) {
      cursor = (cursor as Record<string, unknown>)[seg]
    } else {
      return undefined
    }
  }
  return typeof cursor === "string" ? cursor : undefined
}
function interpolate(text: string, params?: Record<string, string | number>): string {
  if (!params) return text
  return Object.entries(params).reduce(
    (acc, [k, v]) => acc.replace(new RegExp(`\\{${k}\\}`, "g"), String(v)),
    text,
  )
}
vi.mock("next-intl", async () => {
  const actual = await vi.importActual<typeof import("next-intl")>("next-intl")
  return {
    ...actual,
    useTranslations: (namespace?: string) => {
      const t = (key: string, params?: Record<string, string | number>) => {
        const dotted = namespace ? `${namespace}.${key}` : key
        const text = resolveDotKey(enMessages as Record<string, unknown>, dotted)
        return text === undefined ? dotted : interpolate(text, params)
      }
      // next-intl exposes `.rich` / `.has`; our migrated components
      // don't call them, but stub them for forward compat so any future
      // call site doesn't blow up.
      ;(t as unknown as { rich: typeof t }).rich = t
      ;(t as unknown as { has: (k: string) => boolean }).has = (k: string) =>
        resolveDotKey(enMessages as Record<string, unknown>, namespace ? `${namespace}.${k}` : k) !== undefined
      return t
    },
  }
})

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
  // Listeners can be attached for data events (MessageEvent) or
  // connection-level events (plain Event — e.g. "error", "open"),
  // so the store is widened to accept both.
  private listeners: Record<string, Array<(e: Event) => void>> = {}

  constructor(url: string) {
    this.url = url
    MockEventSource._instances.push(this)
  }
  addEventListener(type: string, listener: (e: Event) => void) {
    ;(this.listeners[type] ||= []).push(listener)
  }
  removeEventListener(type: string, listener: (e: Event) => void) {
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
    for (const l of this.listeners["open"] || []) l(ev)
    this.onopen?.(ev)
  }
  /**
   * N5: simulate a connection-level error. Real EventSource fires
   * `onerror` AND any `addEventListener("error", …)` listeners; we honour
   * both so tests can exercise both attach styles.
   */
  emitError() {
    const err = new Event("error")
    for (const l of this.listeners["error"] || []) l(err)
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
