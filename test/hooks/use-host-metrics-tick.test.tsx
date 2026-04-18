/**
 * H3 — useHostMetricsTick contract.
 *
 * Verifies the hook:
 *   1. Starts disconnected with empty history.
 *   2. Captures latest snapshot + baseline + highPressure from SSE ticks.
 *   3. Maintains a 60-point rolling ring buffer (oldest evicted).
 *   4. Ignores non-host SSE events.
 *   5. Closes its subscription on unmount.
 */

import { act, renderHook } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

vi.mock("@/lib/api", () => ({
  subscribeEvents: vi.fn(),
}))

import * as api from "@/lib/api"
import type { HostMetricsTickEvent } from "@/lib/api"
import { primeSSE as _primeSSE } from "../helpers/sse"
import {
  HOST_METRICS_HISTORY_SIZE,
  useHostMetricsTick,
} from "@/hooks/use-host-metrics-tick"

const primeSSE = () => _primeSSE(api)

function mkTick(overrides: Partial<HostMetricsTickEvent["host"]> = {}, extra: Partial<HostMetricsTickEvent> = {}): HostMetricsTickEvent {
  return {
    host: {
      cpu_percent: 12,
      mem_percent: 34,
      mem_used_gb: 16,
      mem_total_gb: 64,
      disk_percent: 40,
      disk_used_gb: 200,
      disk_total_gb: 512,
      loadavg_1m: 3.2,
      loadavg_5m: 2.8,
      loadavg_15m: 2.5,
      sampled_at: 1_700_000_000,
      ...overrides,
    },
    docker: {
      container_count: 4,
      total_mem_reservation_bytes: 1024 * 1024 * 1024,
      source: "sdk",
      sampled_at: 1_700_000_000,
    },
    baseline: {
      cpu_cores: 16,
      mem_total_gb: 64,
      disk_total_gb: 512,
      cpu_model: "AMD Ryzen 9 7950X",
    },
    high_pressure: false,
    sampled_at: 1_700_000_000,
    ...extra,
  }
}

afterEach(() => { vi.clearAllMocks() })

describe("useHostMetricsTick", () => {
  it("starts disconnected with empty history and null latest/baseline", () => {
    primeSSE()
    const { result } = renderHook(() => useHostMetricsTick())
    expect(result.current.latest).toBeNull()
    expect(result.current.baseline).toBeNull()
    expect(result.current.history).toEqual([])
    expect(result.current.connected).toBe(false)
    expect(result.current.highPressure).toBe(false)
  })

  it("captures latest, baseline, and connected after a tick", () => {
    const sse = primeSSE()
    const { result } = renderHook(() => useHostMetricsTick())
    act(() => {
      sse.emit({ event: "host.metrics.tick", data: mkTick({}, { high_pressure: true }) })
    })
    expect(result.current.connected).toBe(true)
    expect(result.current.latest?.host.cpu_percent).toBe(12)
    expect(result.current.baseline?.cpu_cores).toBe(16)
    expect(result.current.highPressure).toBe(true)
    expect(result.current.history).toHaveLength(1)
    expect(result.current.history[0].cpu_percent).toBe(12)
    expect(result.current.history[0].container_count).toBe(4)
  })

  it("caps the history ring buffer at HOST_METRICS_HISTORY_SIZE (60)", () => {
    const sse = primeSSE()
    const { result } = renderHook(() => useHostMetricsTick())
    act(() => {
      for (let i = 0; i < HOST_METRICS_HISTORY_SIZE + 5; i++) {
        sse.emit({
          event: "host.metrics.tick",
          data: mkTick({ cpu_percent: i, sampled_at: 1_700_000_000 + i }),
        })
      }
    })
    expect(result.current.history).toHaveLength(HOST_METRICS_HISTORY_SIZE)
    // Oldest entries were evicted: history[0] corresponds to the 5th tick emitted.
    expect(result.current.history[0].cpu_percent).toBe(5)
    expect(result.current.history.at(-1)?.cpu_percent).toBe(HOST_METRICS_HISTORY_SIZE + 4)
  })

  it("ignores unrelated SSE events", () => {
    const sse = primeSSE()
    const { result } = renderHook(() => useHostMetricsTick())
    act(() => {
      sse.emit({ event: "heartbeat", data: { subscribers: 1 } })
      sse.emit({ event: "agent_update", data: { agent_id: "a", status: "ok", thought_chain: "", timestamp: "t" } })
    })
    expect(result.current.latest).toBeNull()
    expect(result.current.history).toEqual([])
    expect(result.current.connected).toBe(false)
  })

  it("closes the SSE subscription on unmount", () => {
    const sse = primeSSE()
    const { unmount } = renderHook(() => useHostMetricsTick())
    expect(sse.closeCount()).toBe(0)
    unmount()
    expect(sse.closeCount()).toBe(1)
  })
})
