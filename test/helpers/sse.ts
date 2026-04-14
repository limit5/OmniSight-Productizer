/**
 * Shared SSE priming helper for component tests.
 *
 * Every test under test/components/* mocks @/lib/api and stubs
 * subscribeEvents with a listener-capturing impl so the test can push
 * fake events into the component. The bookkeeping was duplicated across
 * three files; consolidate here.
 *
 * Usage:
 *   vi.mock("@/lib/api", () => ({ subscribeEvents: vi.fn(), ... }))
 *   import * as api from "@/lib/api"
 *   ...
 *   const sse = primeSSE(api)
 *   sse.emit({ event: "mode_changed", data: {...} })
 *   expect(sse.closeCount()).toBe(1)
 */

import { vi } from "vitest"

export type SSEListener = (ev: { event: string; data: unknown }) => void

export interface SSEPrime {
  listeners: SSEListener[]
  emit: (ev: { event: string; data: unknown }) => void
  closeCount: () => number
}

export function primeSSE(api: {
  subscribeEvents: unknown
}): SSEPrime {
  const listeners: SSEListener[] = []
  let closed = 0
  const handle = { close: () => { closed++ }, readyState: 1 }
  ;(api.subscribeEvents as ReturnType<typeof vi.fn>).mockImplementation(
    (fn: SSEListener) => {
      listeners.push(fn)
      return handle
    },
  )
  return {
    listeners,
    emit: (ev) => listeners.forEach((l) => l(ev)),
    closeCount: () => closed,
  }
}
