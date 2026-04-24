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
 *
 * ZZ.B1 #304-1 checkbox 3 (2026-04-24): ``TurnTimeline`` now calls
 * ``fetchTurnHistory`` on mount to seed the ring buffer from
 * ``GET /runtime/turns``. Component tests do not have a real fetch
 * pipeline and the vi.mock factory spreads ``actual`` which would
 * surface the live implementation. The helper now stubs
 * ``fetchTurnHistory`` alongside ``subscribeEvents`` with an empty
 * history so existing tests keep behaving as if the endpoint returned
 * no rows (matches the pre-checkbox-3 empty-state baseline). Tests
 * that want to assert backfill behaviour can pass ``history`` to
 * override the default.
 */

import { vi } from "vitest"

export type SSEListener = (ev: { event: string; data: unknown }) => void

export interface SSEPrime {
  listeners: SSEListener[]
  emit: (ev: { event: string; data: unknown }) => void
  closeCount: () => number
}

export interface PrimeSSEOptions {
  /** Pre-seeded ``turn.complete`` rows the component will read from
   *  ``fetchTurnHistory`` on mount. Defaults to empty. */
  history?: unknown[]
  /** ZZ.B3 #304-3 checkbox 2: pre-seeded burn-rate series for the
   *  TokenUsageStats Row 1 sparkline. Defaults to an empty series →
   *  the sparkline degrades to its <2-point empty state and the
   *  badge renders "$—/hr" per the component's NULL-vs-genuine-zero
   *  contract. */
  burnRatePoints?: Array<{
    timestamp: string
    tokens_per_hour: number
    cost_per_hour: number
  }>
  burnRateWindow?: "15m" | "1h" | "24h"
}

export function primeSSE(
  api: {
    subscribeEvents: unknown
    fetchTurnHistory?: unknown
    fetchTokenBurnRate?: unknown
  },
  opts: PrimeSSEOptions = {},
): SSEPrime {
  const listeners: SSEListener[] = []
  let closed = 0
  const handle = { close: () => { closed++ }, readyState: 1 }
  ;(api.subscribeEvents as ReturnType<typeof vi.fn>).mockImplementation(
    (fn: SSEListener) => {
      listeners.push(fn)
      return handle
    },
  )
  // ZZ.B1 checkbox 3: stub history endpoint so TurnTimeline's mount
  // fetch resolves to an empty buffer by default.
  const fth = api.fetchTurnHistory as ReturnType<typeof vi.fn> | undefined
  if (fth && typeof fth.mockImplementation === "function") {
    fth.mockResolvedValue({
      turns: opts.history ?? [],
      count: (opts.history ?? []).length,
    })
  }
  // ZZ.B3 #304-3 checkbox 2: stub burn-rate endpoint so
  // TokenUsageStats' Row 1 sparkline mount fetch resolves offline.
  const fbr = api.fetchTokenBurnRate as ReturnType<typeof vi.fn> | undefined
  if (fbr && typeof fbr.mockImplementation === "function") {
    fbr.mockResolvedValue({
      window: opts.burnRateWindow ?? "1h",
      bucket_seconds: 60,
      points: opts.burnRatePoints ?? [],
    })
  }
  return {
    listeners,
    emit: (ev) => listeners.forEach((l) => l(ev)),
    closeCount: () => closed,
  }
}
