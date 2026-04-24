/**
 * H3 row 1528 — high-pressure threshold visual marking.
 *
 * The HostDevicePanel's CPU / Mem / Disk live cards must change color +
 * carry a `data-pressure` attribute reflecting traffic-light pressure
 * bands:
 *   - normal (< 70%) — emerald + no badge
 *   - warn   (70-85%) — hardware-orange (amber)
 *   - critical (≥ 85%) — critical-red + AlertCircle badge + pulse
 *
 * The threshold 85% mirrors the H2 backend `host_cpu_high` precondition
 * (cpu_pct < 85 to acquire a sandbox slot), so the UI flips red at the
 * same point the coordinator starts deferring sandbox launches.
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

import {
  HostDevicePanel,
  PRESSURE_CRITICAL_PCT,
  PRESSURE_WARN_PCT,
  pressureColorVar,
  pressureLevel,
} from "@/components/omnisight/host-device-panel"
import * as api from "@/lib/api"
import { primeSSE as _primeSSE } from "../helpers/sse"

const primeSSE = () => _primeSSE(api)

const tickWith = (cpu: number, mem = 25, disk = 25) => ({
  event: "host.metrics.tick" as const,
  data: {
    host: {
      cpu_percent: cpu,
      mem_percent: mem,
      mem_used_gb: 8,
      mem_total_gb: 32,
      disk_percent: disk,
      disk_used_gb: 100,
      disk_total_gb: 512,
      loadavg_1m: 1.0,
      loadavg_5m: 1.0,
      loadavg_15m: 1.0,
      sampled_at: 1700000000,
    },
    docker: {
      container_count: 3,
      total_mem_reservation_bytes: 0,
      source: "sdk" as const,
      sampled_at: 1700000000,
    },
    baseline: {
      cpu_cores: 16,
      mem_total_gb: 32,
      disk_total_gb: 512,
      cpu_model: "Test CPU",
    },
    high_pressure: cpu >= PRESSURE_CRITICAL_PCT,
    sampled_at: 1700000000,
  },
})

describe("pressureLevel helper", () => {
  it("returns 'normal' below the warn threshold", () => {
    expect(pressureLevel(0)).toBe("normal")
    expect(pressureLevel(50)).toBe("normal")
    expect(pressureLevel(PRESSURE_WARN_PCT - 0.01)).toBe("normal")
  })

  it("returns 'warn' from the warn threshold up to (but not including) the critical threshold", () => {
    expect(pressureLevel(PRESSURE_WARN_PCT)).toBe("warn")
    expect(pressureLevel(75)).toBe("warn")
    expect(pressureLevel(PRESSURE_CRITICAL_PCT - 0.01)).toBe("warn")
  })

  it("returns 'critical' at and above the critical threshold", () => {
    expect(pressureLevel(PRESSURE_CRITICAL_PCT)).toBe("critical")
    expect(pressureLevel(90)).toBe("critical")
    expect(pressureLevel(100)).toBe("critical")
    expect(pressureLevel(150)).toBe("critical")
  })

  it("treats NaN / Infinity as 'normal' (defensive against bad ticks)", () => {
    expect(pressureLevel(Number.NaN)).toBe("normal")
    expect(pressureLevel(Number.POSITIVE_INFINITY)).toBe("normal")
  })
})

describe("pressureColorVar helper", () => {
  it("emits the emerald CSS var for normal pressure", () => {
    expect(pressureColorVar(50)).toBe("var(--validation-emerald)")
  })

  it("emits the hardware-orange CSS var for warn pressure", () => {
    expect(pressureColorVar(75)).toBe("var(--hardware-orange)")
  })

  it("emits the critical-red CSS var for critical pressure", () => {
    expect(pressureColorVar(90)).toBe("var(--critical-red)")
  })
})

describe("HostDevicePanel — pressure-band visual marking", () => {
  beforeEach(() => { vi.clearAllMocks() })

  it("marks CPU card 'normal' at 50% with emerald-coloured value text", () => {
    const sse = primeSSE()
    render(<HostDevicePanel />)
    act(() => { sse.emit(tickWith(50)) })
    const card = screen.getByTestId("metric-cpu")
    expect(card.getAttribute("data-pressure")).toBe("normal")
    const value = screen.getByTestId("metric-cpu-value")
    expect(value).toHaveStyle({ color: "var(--validation-emerald)" })
    // No critical badge in normal state.
    expect(value.querySelector('[aria-label="critical"]')).toBeNull()
    // Tooltip explains the threshold band.
    expect(card.getAttribute("title")).toMatch(/normal/i)
    expect(card.getAttribute("title")).toMatch(/CPU 50/)
  })

  it("marks CPU card 'warn' at 75% with hardware-orange coloured value text", () => {
    const sse = primeSSE()
    render(<HostDevicePanel />)
    act(() => { sse.emit(tickWith(75)) })
    const card = screen.getByTestId("metric-cpu")
    expect(card.getAttribute("data-pressure")).toBe("warn")
    const value = screen.getByTestId("metric-cpu-value")
    expect(value).toHaveStyle({ color: "var(--hardware-orange)" })
    expect(value.querySelector('[aria-label="critical"]')).toBeNull()
    expect(card.getAttribute("title")).toMatch(/WARN/)
    // Title should mention both threshold ends so the operator sees the band.
    expect(card.getAttribute("title")).toContain(`${PRESSURE_WARN_PCT}-${PRESSURE_CRITICAL_PCT}`)
  })

  it("marks CPU card 'critical' at 90% with red value text + AlertCircle badge + pulse", () => {
    const sse = primeSSE()
    render(<HostDevicePanel />)
    act(() => { sse.emit(tickWith(90)) })
    const card = screen.getByTestId("metric-cpu")
    expect(card.getAttribute("data-pressure")).toBe("critical")
    const value = screen.getByTestId("metric-cpu-value")
    expect(value).toHaveStyle({ color: "var(--critical-red)" })
    // Critical badge appears (AlertCircle icon with aria-label="critical").
    expect(value.querySelector('[aria-label="critical"]')).not.toBeNull()
    // Pulse animation flags the card for attention.
    expect(value.className).toMatch(/animate-pulse/)
    // Tooltip mentions coordinator deferral consequence.
    expect(card.getAttribute("title")).toMatch(/CRITICAL/)
    expect(card.getAttribute("title")).toMatch(/coordinator/i)
  })

  it("flips at the exact 85% boundary (boundary-inclusive critical)", () => {
    const sse = primeSSE()
    render(<HostDevicePanel />)
    // Just below: warn
    act(() => { sse.emit(tickWith(84.9)) })
    expect(screen.getByTestId("metric-cpu").getAttribute("data-pressure")).toBe("warn")
    // Exactly 85: critical (matches H2 backend `host_cpu_high` precondition).
    act(() => { sse.emit(tickWith(85)) })
    expect(screen.getByTestId("metric-cpu").getAttribute("data-pressure")).toBe("critical")
  })

  it("flips at the exact 70% boundary (boundary-inclusive warn)", () => {
    const sse = primeSSE()
    render(<HostDevicePanel />)
    // Just below: normal
    act(() => { sse.emit(tickWith(69.9)) })
    expect(screen.getByTestId("metric-cpu").getAttribute("data-pressure")).toBe("normal")
    // Exactly 70: warn
    act(() => { sse.emit(tickWith(70)) })
    expect(screen.getByTestId("metric-cpu").getAttribute("data-pressure")).toBe("warn")
  })

  it("applies the same threshold logic to Memory and Disk cards", () => {
    const sse = primeSSE()
    render(<HostDevicePanel />)
    // CPU normal, mem warn, disk critical — independent per metric.
    act(() => { sse.emit(tickWith(10, 80, 95)) })
    expect(screen.getByTestId("metric-cpu").getAttribute("data-pressure")).toBe("normal")
    expect(screen.getByTestId("metric-mem").getAttribute("data-pressure")).toBe("warn")
    expect(screen.getByTestId("metric-disk").getAttribute("data-pressure")).toBe("critical")
    // Disk at 95% must show the critical badge.
    const diskVal = screen.getByTestId("metric-disk-value")
    expect(diskVal).toHaveStyle({ color: "var(--critical-red)" })
    expect(diskVal.querySelector('[aria-label="critical"]')).not.toBeNull()
  })

  it("colours the CPU sparkline red when CPU is critical", () => {
    const sse = primeSSE()
    render(<HostDevicePanel />)
    // Need ≥ 2 ticks for the sparkline to render an SVG (vs. dash placeholder).
    act(() => {
      sse.emit(tickWith(90))
      sse.emit(tickWith(95))
    })
    const spark = screen.getByTestId("sparkline-cpu")
    expect(spark.tagName.toLowerCase()).toBe("svg")
    const polyline = spark.querySelector("polyline")
    expect(polyline?.getAttribute("stroke")).toBe("var(--critical-red)")
  })

  it("falls back to the metric's identity colour before any tick lands", () => {
    primeSSE()
    render(<HostDevicePanel />)
    // No tick → tick is null → color falls back to identity (orange for CPU).
    const cpuVal = screen.getByTestId("metric-cpu-value")
    expect(cpuVal).toHaveStyle({ color: "var(--hardware-orange)" })
    expect(screen.getByTestId("metric-cpu").getAttribute("data-pressure")).toBe("normal")
  })
})
