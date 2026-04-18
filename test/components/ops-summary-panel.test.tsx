/**
 * H3 row 1524 — OpsSummaryPanel coordinator transparency.
 *
 * Verifies that when /ops/summary's response carries a `coordinator`
 * block, the panel renders three KPI tiles (queue depth, deferred 5m,
 * effective concurrency budget) with correct tones:
 *   • derated → EFF BUDGET shows warn tone + derate reason tooltip
 *   • queue_depth > 5 → QUEUE tile switches to warn tone
 *   • deferred_5m > 20 → DEFERRED 5m tile switches to warn tone
 *   • missing coordinator (older backend) → section is hidden
 */

import { describe, expect, it, vi, beforeEach } from "vitest"
import { render, screen, waitFor } from "@testing-library/react"

vi.mock("@/lib/api", () => ({
  getOpsSummary: vi.fn(),
}))

import { OpsSummaryPanel } from "@/components/omnisight/ops-summary-panel"
import * as api from "@/lib/api"
import type { OpsSummary } from "@/lib/api"

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

describe("OpsSummaryPanel — H3 row 1524 coordinator transparency", () => {
  beforeEach(() => { vi.clearAllMocks() })

  it("renders QUEUE / DEFERRED 5m / EFF BUDGET tiles from coordinator snapshot", async () => {
    ;(api.getOpsSummary as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...baseOps,
      coordinator: {
        capacity_max: 12,
        effective_budget: 12,
        queue_depth: 2,
        deferred_5m: 5,
        derated: false,
        derate_reason: null,
      },
    })
    render(<OpsSummaryPanel />)

    await waitFor(() => {
      expect(screen.getByTestId("ops-coordinator-section")).toBeInTheDocument()
    })
    // Section labels present
    expect(screen.getByText("COORDINATOR")).toBeInTheDocument()
    expect(screen.getByText("QUEUE")).toBeInTheDocument()
    expect(screen.getByText("DEFERRED 5m")).toBeInTheDocument()
    expect(screen.getByText("EFF BUDGET")).toBeInTheDocument()
    // Value rendering
    expect(screen.getByText("2")).toBeInTheDocument()
    expect(screen.getByText("5")).toBeInTheDocument()
    expect(screen.getByTestId("ops-eff-budget").textContent).toContain("12/12")
  })

  it("warns on EFF BUDGET when the coordinator is derated and shows reason", async () => {
    ;(api.getOpsSummary as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...baseOps,
      coordinator: {
        capacity_max: 12,
        effective_budget: 6,
        queue_depth: 0,
        deferred_5m: 0,
        derated: true,
        derate_reason: "CPU 87% > threshold",
      },
    })
    render(<OpsSummaryPanel />)

    await waitFor(() => {
      expect(screen.getByTestId("ops-eff-budget")).toBeInTheDocument()
    })
    const tile = screen.getByTestId("ops-eff-budget")
    expect(tile.textContent).toContain("6/12")
    // Warn tone uses the FUI orange CSS variable — the value span carries it.
    const valueSpan = tile.querySelector("div.font-mono.font-semibold") as HTMLElement
    expect(valueSpan.className).toContain("fui-orange")
    // Tooltip surfaces the derate reason so hover reveals why.
    expect(tile.getAttribute("title")).toContain("CPU 87%")
  })

  it("marks QUEUE warn when queue_depth > 5 and DEFERRED warn when > 20", async () => {
    ;(api.getOpsSummary as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...baseOps,
      coordinator: {
        capacity_max: 12,
        effective_budget: 12,
        queue_depth: 7,
        deferred_5m: 25,
        derated: false,
        derate_reason: null,
      },
    })
    render(<OpsSummaryPanel />)

    await waitFor(() => {
      expect(screen.getByTestId("ops-coordinator-section")).toBeInTheDocument()
    })
    const row = screen.getByTestId("ops-coordinator-row")
    const kpis = row.querySelectorAll("div.font-mono.font-semibold")
    // kpis are rendered in order: QUEUE, DEFERRED 5m, EFF BUDGET
    expect(kpis[0].textContent).toBe("7")
    expect(kpis[0].className).toContain("fui-orange")
    expect(kpis[1].textContent).toBe("25")
    expect(kpis[1].className).toContain("fui-orange")
  })

  it("hides the coordinator section when the backend omits it (older API)", async () => {
    ;(api.getOpsSummary as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...baseOps,
    })
    render(<OpsSummaryPanel />)

    await waitFor(() => {
      expect(screen.getByText("OPS SUMMARY")).toBeInTheDocument()
    })
    expect(screen.queryByTestId("ops-coordinator-section")).toBeNull()
    expect(screen.queryByText("COORDINATOR")).toBeNull()
  })
})
