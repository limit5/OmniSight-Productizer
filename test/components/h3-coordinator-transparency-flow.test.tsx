/**
 * H3 row 1529 — Component-level integration test for the "host load +
 * coordinator transparency" operator flow.
 *
 * Covers the cross-panel sequence the H3 epic delivered (rows 1521-1528)
 * end-to-end inside JSDOM, complementing the per-row unit tests that
 * verify each panel in isolation:
 *
 *   1. Operator opens dashboard
 *   2. Backend pushes a host.metrics.tick at 90% CPU
 *      → HostDevicePanel CPU card flips to data-pressure="critical"
 *      → SSE LIVE pill engages
 *   3. /ops/summary refresh comes back with derated coordinator
 *      → OpsSummaryPanel COORDINATOR section renders
 *      → DerateBadge appears with "supervised" target + reason tooltip
 *      → Force turbo button is rendered + carries OOM warning a11y label
 *   4. Operator clicks Force turbo → confirms → POST is dispatched
 *      with confirm:true → success message appears + onApplied refresh
 *      pulls a fresh /ops/summary that shows the cleared (non-derated)
 *      state and the badge disappears.
 *
 * This is the JSDOM mirror of the Playwright e2e/h3-host-load-coordinator
 * spec — JSDOM gives us deterministic SSE injection, the Playwright spec
 * exercises the live backend wiring (Caddy/Next.js rewrite proxy,
 * EventSource through the browser, real /ops/summary HTTP).
 */

import { describe, expect, it, vi, beforeEach } from "vitest"
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react"

// HostDevicePanel pulls in subscribeEvents + getAllHostMetrics +
// getMyHostMetrics; OpsSummaryPanel pulls in getOpsSummary +
// forceTurboOverride. Mock the union so both can co-exist in one test.
vi.mock("@/lib/api", () => ({
  subscribeEvents: vi.fn(),
  getAllHostMetrics: vi.fn().mockResolvedValue({ tenants: [] }),
  getMyHostMetrics: vi.fn().mockResolvedValue({ tenant: null }),
  getOpsSummary: vi.fn(),
  forceTurboOverride: vi.fn(),
}))

vi.mock("@/lib/auth-context", () => ({
  useAuth: vi.fn().mockReturnValue({ user: null }),
}))

import { HostDevicePanel } from "@/components/omnisight/host-device-panel"
import { OpsSummaryPanel } from "@/components/omnisight/ops-summary-panel"
import * as api from "@/lib/api"
import type { OpsSummary } from "@/lib/api"
import { primeSSE as _primeSSE } from "../helpers/sse"

const primeSSE = () => _primeSSE(api)

const baseOps: OpsSummary = {
  checked_at: 1700000000,
  uptime_s: 120,
  daily_cost_usd: 0.12,
  hourly_cost_usd: 0.005,
  token_frozen: false,
  budget_level: "normal",
  decisions_pending: 0,
  sse_subscribers: 3,
  watchdog_age_s: 15,
}

const criticalCpuTick = (cpu = 92) => ({
  event: "host.metrics.tick" as const,
  data: {
    host: {
      cpu_percent: cpu,
      mem_percent: 55,
      mem_used_gb: 8,
      mem_total_gb: 32,
      disk_percent: 40,
      disk_used_gb: 200,
      disk_total_gb: 512,
      loadavg_1m: 5.2,
      loadavg_5m: 4.8,
      loadavg_15m: 4.1,
      sampled_at: 1700000000,
    },
    docker: {
      container_count: 9,
      total_mem_reservation_bytes: 0,
      source: "sdk" as const,
      sampled_at: 1700000000,
    },
    baseline: {
      cpu_cores: 16,
      mem_total_gb: 64,
      disk_total_gb: 512,
      cpu_model: "AMD Ryzen 9 7950X",
    },
    high_pressure: cpu >= 85,
    sampled_at: 1700000000,
  },
})

describe("H3 — full operator flow (host load → coordinator derate → force turbo)", () => {
  beforeEach(() => { vi.clearAllMocks() })

  it("flips host card to critical, surfaces derate badge, then clears via Force turbo", async () => {
    const sse = primeSSE()
    const getOps = api.getOpsSummary as ReturnType<typeof vi.fn>
    const force = api.forceTurboOverride as ReturnType<typeof vi.fn>

    // --- Initial /ops/summary: derated state ----------------------
    getOps.mockResolvedValue({
      ...baseOps,
      coordinator: {
        capacity_max: 12,
        effective_budget: 4, // ratio ≈ 0.33 → "supervised" rung
        queue_depth: 6,      // > 5 → warn tone on QUEUE
        deferred_5m: 22,     // > 20 → warn tone on DEFERRED
        derated: true,
        derate_reason: "CPU 92% > threshold",
      },
    } satisfies OpsSummary)

    // Mount both panels side-by-side — same dashboard, same fixtures.
    render(
      <>
        <HostDevicePanel />
        <OpsSummaryPanel />
      </>,
    )

    // Step 1 — operator opens dashboard. Initial state for HostDevicePanel
    // is "SSE WAITING" until a tick arrives. CPU value falls back to
    // identity color (hardware-orange).
    expect(screen.getByTestId("host-sse-status").textContent).toContain("SSE WAITING")
    expect(screen.getByTestId("metric-cpu").getAttribute("data-pressure")).toBe("normal")

    // Step 2 — backend pushes host.metrics.tick at 92% CPU.
    act(() => { sse.emit(criticalCpuTick(92)) })

    expect(screen.getByTestId("host-sse-status").textContent).toContain("SSE LIVE")
    const cpuCard = screen.getByTestId("metric-cpu")
    expect(cpuCard.getAttribute("data-pressure")).toBe("critical")
    expect(cpuCard.getAttribute("title")).toMatch(/CRITICAL/)
    expect(cpuCard.getAttribute("title")).toMatch(/coordinator/i)
    const cpuVal = screen.getByTestId("metric-cpu-value")
    expect(cpuVal).toHaveStyle({ color: "var(--critical-red)" })
    // Pulse animation flags the card for attention; AlertCircle is the
    // critical badge inserted into the value span.
    expect(cpuVal.className).toMatch(/animate-pulse/)
    expect(cpuVal.querySelector('[aria-label="critical"]')).not.toBeNull()
    // Reference-rig BASELINE pill stays pinned regardless of tick payload.
    expect(screen.getByTestId("host-baseline").textContent?.replace(/\s+/g, " "))
      .toContain("BASELINE 16c / 64GB / 512GB")

    // Step 3 — OpsSummaryPanel hydrates from the derated /ops/summary
    // payload. COORDINATOR section + DerateBadge + Force turbo button
    // must all be present.
    await waitFor(() => {
      expect(screen.getByTestId("ops-coordinator-section")).toBeInTheDocument()
    })
    expect(screen.getByTestId("ops-eff-budget").textContent).toContain("4/12")
    const badge = screen.getByTestId("ops-derate-badge")
    expect(badge.getAttribute("data-derate-target")).toBe("supervised")
    expect(badge.textContent).toContain("Coordinator auto-derated to supervised")
    expect(badge.getAttribute("title")).toContain("CPU 92%")
    expect(badge.getAttribute("title")).toContain("effective 4 / 12 tokens")

    // QUEUE > 5 and DEFERRED 5m > 20 → both light up amber.
    const row = screen.getByTestId("ops-coordinator-row")
    const kpis = row.querySelectorAll("div.font-mono.font-semibold")
    expect(kpis[0].textContent).toBe("6")
    expect(kpis[0].className).toContain("fui-orange")
    expect(kpis[1].textContent).toBe("22")
    expect(kpis[1].className).toContain("fui-orange")

    // Force turbo button rendered with OOM-warning a11y label.
    const btn = screen.getByTestId("ops-force-turbo-btn")
    expect(btn.getAttribute("aria-label")).toContain("OOM")
    expect(btn.getAttribute("title")).toContain("OOM")

    // Step 4 — operator clicks Force turbo, accepts the confirm dialog.
    // Stub the *next* /ops/summary call to return the cleared state so
    // the post-success refresh tears the badge down.
    getOps.mockResolvedValueOnce({
      ...baseOps,
      coordinator: {
        capacity_max: 12,
        effective_budget: 12,
        queue_depth: 0,
        deferred_5m: 0,
        derated: false,
        derate_reason: null,
      },
    } satisfies OpsSummary)
    force.mockResolvedValue({
      applied: true,
      cleared_turbo_derate: true,
      reset_capacity_derate: true,
      before: { turbo_derate_active: true, capacity_derate_ratio: 0.33 },
      after: {
        turbo_derate_active: false,
        capacity_derate_ratio: 1.0,
        restored_to_budget: 12,
        manual_override: true,
        at: 1700000001,
      },
    })
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true)

    fireEvent.click(btn)

    // POST goes through with confirm:true (the backend rejects without
    // it; the frontend confirm dialog is the gate).
    await waitFor(() => expect(force).toHaveBeenCalledTimes(1))
    expect(force).toHaveBeenCalledWith({ confirm: true })
    expect(confirmSpy).toHaveBeenCalledTimes(1)
    // Dialog body must mention OOM so the operator can't dismiss
    // without seeing the consequence.
    expect(confirmSpy.mock.calls[0][0] as string).toContain("OOM")

    // Success message lists what got cleared.
    await waitFor(() => {
      expect(screen.getByTestId("ops-force-turbo-msg")).toBeInTheDocument()
    })
    const msg = screen.getByTestId("ops-force-turbo-msg")
    expect(msg.textContent).toContain("Force turbo applied")
    expect(msg.textContent).toContain("turbo-derate cleared")
    expect(msg.textContent).toContain("capacity-derate reset")

    // The success path triggers refresh() which pulls the queued
    // cleared snapshot. Badge must vanish; effective budget returns
    // to capacity_max.
    await waitFor(() => {
      expect(screen.queryByTestId("ops-derate-badge")).toBeNull()
    })
    expect(screen.getByTestId("ops-eff-budget").textContent).toContain("12/12")

    confirmSpy.mockRestore()
  })

  it("Force turbo cancellation never reaches the backend even when derated", async () => {
    primeSSE()
    const getOps = api.getOpsSummary as ReturnType<typeof vi.fn>
    const force = api.forceTurboOverride as ReturnType<typeof vi.fn>

    getOps.mockResolvedValue({
      ...baseOps,
      coordinator: {
        capacity_max: 12,
        effective_budget: 4,
        queue_depth: 0,
        deferred_5m: 0,
        derated: true,
        derate_reason: "CPU 88% > threshold",
      },
    } satisfies OpsSummary)
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false)

    render(<OpsSummaryPanel />)
    await waitFor(() => expect(screen.getByTestId("ops-force-turbo-btn")).toBeInTheDocument())
    fireEvent.click(screen.getByTestId("ops-force-turbo-btn"))

    expect(confirmSpy).toHaveBeenCalledTimes(1)
    // No backend call. No success / failure pill rendered.
    expect(force).not.toHaveBeenCalled()
    expect(screen.queryByTestId("ops-force-turbo-msg")).toBeNull()
    // Badge stays — operator declined to clear it.
    expect(screen.getByTestId("ops-derate-badge")).toBeInTheDocument()

    confirmSpy.mockRestore()
  })

  it("Force turbo backend failure surfaces a red error message but keeps the badge", async () => {
    primeSSE()
    const getOps = api.getOpsSummary as ReturnType<typeof vi.fn>
    const force = api.forceTurboOverride as ReturnType<typeof vi.fn>

    getOps.mockResolvedValue({
      ...baseOps,
      coordinator: {
        capacity_max: 12,
        effective_budget: 6,
        queue_depth: 1,
        deferred_5m: 4,
        derated: true,
        derate_reason: "MEM 90% > threshold",
      },
    } satisfies OpsSummary)
    force.mockRejectedValue(new Error("403 forbidden"))
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true)

    render(<OpsSummaryPanel />)
    await waitFor(() => expect(screen.getByTestId("ops-force-turbo-btn")).toBeInTheDocument())
    fireEvent.click(screen.getByTestId("ops-force-turbo-btn"))

    await waitFor(() => expect(screen.getByTestId("ops-force-turbo-msg")).toBeInTheDocument())
    const msg = screen.getByTestId("ops-force-turbo-msg")
    expect(msg.textContent).toContain("Force turbo failed")
    expect(msg.textContent).toContain("403 forbidden")
    // Badge still there — backend never flipped state.
    expect(screen.getByTestId("ops-derate-badge")).toBeInTheDocument()

    confirmSpy.mockRestore()
  })

  it("Per-axis pressure: cpu normal + mem warn + disk critical render independently in the same tick", () => {
    const sse = primeSSE()
    render(<HostDevicePanel />)
    act(() => {
      sse.emit({
        event: "host.metrics.tick",
        data: {
          host: {
            cpu_percent: 30, mem_percent: 78, mem_used_gb: 25, mem_total_gb: 32,
            disk_percent: 88, disk_used_gb: 450, disk_total_gb: 512,
            loadavg_1m: 1.5, loadavg_5m: 1.2, loadavg_15m: 1.0,
            sampled_at: 1700000000,
          },
          docker: {
            container_count: 4, total_mem_reservation_bytes: 0,
            source: "sdk", sampled_at: 1700000000,
          },
          baseline: {
            cpu_cores: 16, mem_total_gb: 32, disk_total_gb: 512,
            cpu_model: "Test CPU",
          },
          high_pressure: false,
          sampled_at: 1700000000,
        },
      })
    })
    expect(screen.getByTestId("metric-cpu").getAttribute("data-pressure")).toBe("normal")
    expect(screen.getByTestId("metric-mem").getAttribute("data-pressure")).toBe("warn")
    expect(screen.getByTestId("metric-disk").getAttribute("data-pressure")).toBe("critical")
    // Critical disk badge present; warn mem has no critical badge.
    expect(screen.getByTestId("metric-disk-value")
      .querySelector('[aria-label="critical"]')).not.toBeNull()
    expect(screen.getByTestId("metric-mem-value")
      .querySelector('[aria-label="critical"]')).toBeNull()
  })

  it("Hides the COORDINATOR section entirely when an older backend omits it", async () => {
    primeSSE()
    const getOps = api.getOpsSummary as ReturnType<typeof vi.fn>
    getOps.mockResolvedValue({ ...baseOps })  // no coordinator key

    render(<OpsSummaryPanel />)
    await waitFor(() => expect(screen.getByText("OPS SUMMARY")).toBeInTheDocument())
    expect(screen.queryByTestId("ops-coordinator-section")).toBeNull()
    expect(screen.queryByTestId("ops-derate-badge")).toBeNull()
    // Force turbo button only lives inside COORDINATOR section, so it
    // also disappears with older backends.
    expect(screen.queryByTestId("ops-force-turbo-btn")).toBeNull()
  })
})
