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

// ─────────────────────────────────────────────────────────────────────────
// H3 row 1526 — overload Badge: "Coordinator auto-derated to <mode>" with
// hover tooltip surfacing the raw `derate_reason` set by the Coordinator.
// ─────────────────────────────────────────────────────────────────────────
describe("OpsSummaryPanel — H3 row 1526 derate badge", () => {
  beforeEach(() => { vi.clearAllMocks() })

  it("renders the auto-derate badge with target mode label and reason tooltip when derated", async () => {
    ;(api.getOpsSummary as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...baseOps,
      coordinator: {
        capacity_max: 12,
        effective_budget: 4, // ratio = 0.33 → supervised rung
        queue_depth: 3,
        deferred_5m: 12,
        derated: true,
        derate_reason: "CPU 87% > threshold",
      },
    })
    render(<OpsSummaryPanel />)

    await waitFor(() => {
      expect(screen.getByTestId("ops-derate-badge")).toBeInTheDocument()
    })
    const badge = screen.getByTestId("ops-derate-badge")
    // Label calls out the target mode the Coordinator dropped toward.
    expect(badge.textContent).toContain("Coordinator auto-derated to supervised")
    expect(badge.getAttribute("data-derate-target")).toBe("supervised")
    // Tooltip exposes the raw reason from the backend snapshot.
    expect(badge.getAttribute("title")).toContain("CPU 87% > threshold")
    expect(badge.getAttribute("title")).toContain("effective 4 / 12 tokens")
    // Warning visual (FUI orange) so the operator notices on hover.
    expect(badge.className).toContain("fui-orange")
    // Accessible label so screen readers surface the same info.
    expect(badge.getAttribute("role")).toBe("status")
    expect(badge.getAttribute("aria-label")).toContain("CPU 87%")
  })

  it("hides the badge entirely when coordinator.derated is false", async () => {
    ;(api.getOpsSummary as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...baseOps,
      coordinator: {
        capacity_max: 12,
        effective_budget: 12,
        queue_depth: 0,
        deferred_5m: 0,
        derated: false,
        derate_reason: null,
      },
    })
    render(<OpsSummaryPanel />)

    await waitFor(() => {
      expect(screen.getByTestId("ops-coordinator-section")).toBeInTheDocument()
    })
    // Coordinator section visible, badge not present.
    expect(screen.queryByTestId("ops-derate-badge")).toBeNull()
  })

  it("falls back to a sensible tooltip when the backend omits a reason string", async () => {
    ;(api.getOpsSummary as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...baseOps,
      coordinator: {
        capacity_max: 12,
        effective_budget: 6,
        queue_depth: 0,
        deferred_5m: 0,
        derated: true,
        // Backend may report derated=true with a missing reason if the
        // sweep raced the audit emit — surface a fallback so hover still
        // explains *what* happened.
        derate_reason: null,
      },
    })
    render(<OpsSummaryPanel />)

    await waitFor(() => {
      expect(screen.getByTestId("ops-derate-badge")).toBeInTheDocument()
    })
    const badge = screen.getByTestId("ops-derate-badge")
    expect(badge.getAttribute("title")).toContain("Reason unavailable")
  })

  it("labels heavy derates as manual when the effective budget collapses", async () => {
    ;(api.getOpsSummary as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...baseOps,
      coordinator: {
        capacity_max: 12,
        effective_budget: 1, // ratio ≈ 0.083 → manual rung
        queue_depth: 8,
        deferred_5m: 30,
        derated: true,
        derate_reason: "MEM 96% > threshold",
      },
    })
    render(<OpsSummaryPanel />)

    await waitFor(() => {
      expect(screen.getByTestId("ops-derate-badge")).toBeInTheDocument()
    })
    const badge = screen.getByTestId("ops-derate-badge")
    expect(badge.getAttribute("data-derate-target")).toBe("manual")
    expect(badge.textContent).toContain("Coordinator auto-derated to manual")
    expect(badge.getAttribute("title")).toContain("MEM 96%")
  })
})
