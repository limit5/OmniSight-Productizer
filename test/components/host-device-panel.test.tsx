/**
 * H3 — HostDevicePanel SSE integration.
 *
 * Verifies that when a `host.metrics.tick` SSE event arrives, the
 * panel's SYSTEM INFO block switches from the "awaiting SSE" placeholder
 * to the live CPU / memory values carried on the tick, and the SSE
 * status pill flips from "SSE WAITING" to "SSE LIVE".
 */

import { describe, expect, it, vi, beforeEach } from "vitest"
import { act, render, screen } from "@testing-library/react"

vi.mock("@/lib/api", () => ({
  subscribeEvents: vi.fn(),
  getAllHostMetrics: vi.fn().mockResolvedValue({ tenants: [] }),
  getMyHostMetrics: vi.fn().mockResolvedValue({ tenant: null }),
}))

vi.mock("@/lib/auth-context", () => ({
  useAuth: vi.fn().mockReturnValue({ user: null }),
}))

import { HostDevicePanel } from "@/components/omnisight/host-device-panel"
import * as api from "@/lib/api"
import { primeSSE as _primeSSE } from "../helpers/sse"

const primeSSE = () => _primeSSE(api)

describe("HostDevicePanel — host.metrics.tick SSE wiring", () => {
  beforeEach(() => { vi.clearAllMocks() })

  it("starts in 'SSE WAITING' state and flips to 'SSE LIVE' once a tick arrives", () => {
    const sse = primeSSE()
    render(<HostDevicePanel />)

    const status = screen.getByTestId("host-sse-status")
    expect(status.textContent).toContain("SSE WAITING")

    act(() => {
      sse.emit({
        event: "host.metrics.tick",
        data: {
          host: {
            cpu_percent: 42.5,
            mem_percent: 55,
            mem_used_gb: 8,
            mem_total_gb: 32,
            disk_percent: 61,
            disk_used_gb: 300,
            disk_total_gb: 512,
            loadavg_1m: 3.7,
            loadavg_5m: 2.9,
            loadavg_15m: 2.2,
            sampled_at: 1700000000,
          },
          docker: {
            container_count: 7,
            total_mem_reservation_bytes: 0,
            source: "sdk",
            sampled_at: 1700000000,
          },
          baseline: {
            cpu_cores: 16,
            mem_total_gb: 64,
            disk_total_gb: 512,
            cpu_model: "AMD Ryzen 9 7950X",
          },
          high_pressure: false,
          sampled_at: 1700000000,
        },
      })
    })

    expect(screen.getByTestId("host-sse-status").textContent).toContain("SSE LIVE")
    // SSE-driven CPU usage shown (rounded to 2dp by HostInfoSection).
    expect(screen.getByText(/42\.50%/)).toBeInTheDocument()
    // Baseline populates CPU model when /system/info hasn't landed.
    expect(screen.getByText(/AMD Ryzen 9 7950X/)).toBeInTheDocument()
    // Memory: 8 GiB used / 32 GiB total derived from tick (NOT baseline's 64).
    expect(screen.getByText(/8\.0 GB \/ 32 GB/)).toBeInTheDocument()
  })

  it("uses the SSE baseline for cpu_model when hostInfo prop is missing", () => {
    const sse = primeSSE()
    render(<HostDevicePanel />)
    act(() => {
      sse.emit({
        event: "host.metrics.tick",
        data: {
          host: {
            cpu_percent: 1, mem_percent: 1, mem_used_gb: 1, mem_total_gb: 64,
            disk_percent: 1, disk_used_gb: 1, disk_total_gb: 512,
            loadavg_1m: 0.1, loadavg_5m: 0.1, loadavg_15m: 0.1,
            sampled_at: 1700000000,
          },
          docker: { container_count: 0, total_mem_reservation_bytes: 0, source: "unavailable", sampled_at: 0 },
          baseline: { cpu_cores: 16, mem_total_gb: 64, disk_total_gb: 512, cpu_model: "Baseline CPU" },
          high_pressure: false,
          sampled_at: 1700000000,
        },
      })
    })
    expect(screen.getByText(/Baseline CPU/)).toBeInTheDocument()
    // 16C / arch comes from the panel — arch fell back to "--" because
    // no /system/info prop was passed; that's fine for this assertion.
    expect(screen.getByText(/16C \/ --/)).toBeInTheDocument()
  })
})
