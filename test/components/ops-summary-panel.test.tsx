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
import { fireEvent, render, screen, waitFor } from "@testing-library/react"

vi.mock("@/lib/api", () => ({
  getOpsSummary: vi.fn(),
  forceTurboOverride: vi.fn(),
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

// ─────────────────────────────────────────────────────────────────────────
// H3 row 1527 — Force turbo manual override button.
// Button sits under the COORDINATOR row, pops a native window.confirm with
// the OOM warning, then POSTs /coordinator/force-turbo on acceptance. The
// backend writes a Phase-53 audit row and SSE-broadcasts the override.
// ─────────────────────────────────────────────────────────────────────────
describe("OpsSummaryPanel — H3 row 1527 force turbo override", () => {
  beforeEach(() => { vi.clearAllMocks() })

  it("renders the Force turbo button inside the coordinator section", async () => {
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
      expect(screen.getByTestId("ops-force-turbo-btn")).toBeInTheDocument()
    })
    const btn = screen.getByTestId("ops-force-turbo-btn")
    expect(btn.textContent).toContain("Force turbo")
    // Title carries the OOM warning — a11y: aria-label also mentions OOM.
    expect(btn.getAttribute("title")).toContain("OOM")
    expect(btn.getAttribute("aria-label")).toContain("OOM")
  })

  it("cancelling the confirm dialog does NOT call the backend", async () => {
    ;(api.getOpsSummary as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...baseOps,
      coordinator: {
        capacity_max: 12,
        effective_budget: 4,
        queue_depth: 0,
        deferred_5m: 0,
        derated: true,
        derate_reason: "CPU 87% > threshold",
      },
    })
    const force = api.forceTurboOverride as ReturnType<typeof vi.fn>
    force.mockResolvedValue({
      applied: true, cleared_turbo_derate: true, reset_capacity_derate: true,
      before: { turbo_derate_active: true, capacity_derate_ratio: 0.25 },
      after: {
        turbo_derate_active: false, capacity_derate_ratio: 1.0,
        restored_to_budget: 8, manual_override: true, at: 1,
      },
    })
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false)

    render(<OpsSummaryPanel />)
    await waitFor(() => expect(screen.getByTestId("ops-force-turbo-btn")).toBeInTheDocument())
    fireEvent.click(screen.getByTestId("ops-force-turbo-btn"))

    expect(confirmSpy).toHaveBeenCalledTimes(1)
    // OOM warning must be in the dialog body.
    const warn = confirmSpy.mock.calls[0][0] as string
    expect(warn).toContain("OOM")
    // Backend NOT called when operator declines.
    expect(force).not.toHaveBeenCalled()
    confirmSpy.mockRestore()
  })

  it("accepting the confirm dialog POSTs the override and shows a success message", async () => {
    ;(api.getOpsSummary as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...baseOps,
      coordinator: {
        capacity_max: 12,
        effective_budget: 4,
        queue_depth: 0,
        deferred_5m: 0,
        derated: true,
        derate_reason: "CPU 87% > threshold",
      },
    })
    const force = api.forceTurboOverride as ReturnType<typeof vi.fn>
    force.mockResolvedValue({
      applied: true,
      cleared_turbo_derate: true,
      reset_capacity_derate: true,
      before: { turbo_derate_active: true, capacity_derate_ratio: 0.25 },
      after: {
        turbo_derate_active: false,
        capacity_derate_ratio: 1.0,
        restored_to_budget: 8,
        manual_override: true,
        at: 1,
      },
    })
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true)

    render(<OpsSummaryPanel />)
    await waitFor(() => expect(screen.getByTestId("ops-force-turbo-btn")).toBeInTheDocument())
    fireEvent.click(screen.getByTestId("ops-force-turbo-btn"))

    await waitFor(() => expect(force).toHaveBeenCalledTimes(1))
    // Confirm=true is the flag the backend requires.
    expect(force).toHaveBeenCalledWith({ confirm: true })

    await waitFor(() => {
      expect(screen.getByTestId("ops-force-turbo-msg")).toBeInTheDocument()
    })
    const msg = screen.getByTestId("ops-force-turbo-msg")
    expect(msg.textContent).toContain("Force turbo applied")
    expect(msg.textContent).toContain("turbo-derate cleared")
    expect(msg.textContent).toContain("capacity-derate reset")
    confirmSpy.mockRestore()
  })

  it("surfaces a failure message when the backend returns an error", async () => {
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
    const force = api.forceTurboOverride as ReturnType<typeof vi.fn>
    force.mockRejectedValue(new Error("403 forbidden"))
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true)

    render(<OpsSummaryPanel />)
    await waitFor(() => expect(screen.getByTestId("ops-force-turbo-btn")).toBeInTheDocument())
    fireEvent.click(screen.getByTestId("ops-force-turbo-btn"))

    await waitFor(() => {
      expect(screen.getByTestId("ops-force-turbo-msg")).toBeInTheDocument()
    })
    const msg = screen.getByTestId("ops-force-turbo-msg")
    expect(msg.textContent).toContain("Force turbo failed")
    expect(msg.textContent).toContain("403 forbidden")
    confirmSpy.mockRestore()
  })
})

// ─────────────────────────────────────────────────────────────────────────
// H4a row 2583 — Adaptive (AIMD) budget transparency.
// New section in OpsSummaryPanel that surfaces the current host-level
// AIMD budget plus a 5-min trace of state-changing events (init / AI / MD
// / hard_cap / floor / hold) so operators see the rise/fall history at a
// glance instead of having to tail audit logs.
// ─────────────────────────────────────────────────────────────────────────
describe("OpsSummaryPanel — H4a row 2583 AIMD budget + 5min trace", () => {
  beforeEach(() => { vi.clearAllMocks() })

  const aimdBase = (): NonNullable<OpsSummary["aimd"]> => ({
    budget: 8,
    capacity_max: 12,
    floor: 2,
    init_budget: 6,
    last_reason: "additive_increase",
    last_ai_at: 1700000000,
    pressure_clock_started_at: null,
    thresholds: {
      cpu_ai_pct: 70, mem_ai_pct: 70,
      cpu_md_pct: 85, mem_md_pct: 85,
      ai_interval_s: 30, md_persistence_s: 10,
    },
    trace: [
      { timestamp: 1, budget: 6, reason: "init",              cpu_percent: 0,  mem_percent: 0  },
      { timestamp: 31, budget: 7, reason: "additive_increase", cpu_percent: 10, mem_percent: 12 },
      { timestamp: 61, budget: 8, reason: "additive_increase", cpu_percent: 11, mem_percent: 14 },
    ],
  })

  it("renders the AIMD section with current budget, last-reason pill, and trace", async () => {
    ;(api.getOpsSummary as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...baseOps,
      aimd: aimdBase(),
    })
    render(<OpsSummaryPanel />)

    await waitFor(() => {
      expect(screen.getByTestId("ops-aimd-section")).toBeInTheDocument()
    })
    expect(screen.getByText("AIMD BUDGET")).toBeInTheDocument()
    // Budget tile shows current / capacity_max
    expect(screen.getByTestId("ops-aimd-budget").textContent).toContain("8/12")
    // Last-reason pill colors the current state — additive_increase = green tag.
    const pill = screen.getByTestId("ops-aimd-reason")
    expect(pill.getAttribute("data-reason")).toBe("additive_increase")
    expect(pill.textContent).toContain("AI+")
    // Trace sparkline rendered with the right number of points.
    const trace = screen.getByTestId("ops-aimd-trace")
    expect(trace.getAttribute("data-points")).toBe("3")
    expect(trace.tagName.toLowerCase()).toBe("svg")
  })

  it("shows MD½ pill and warn tone on 5m AI/MD counters when budget halved", async () => {
    ;(api.getOpsSummary as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...baseOps,
      aimd: {
        ...aimdBase(),
        budget: 3,
        last_reason: "multiplicative_decrease",
        trace: [
          { timestamp: 1, budget: 6, reason: "init",                    cpu_percent: 0,  mem_percent: 0  },
          { timestamp: 31, budget: 7, reason: "additive_increase",       cpu_percent: 10, mem_percent: 12 },
          { timestamp: 61, budget: 3, reason: "multiplicative_decrease", cpu_percent: 95, mem_percent: 30 },
        ],
      },
    })
    render(<OpsSummaryPanel />)

    await waitFor(() => {
      expect(screen.getByTestId("ops-aimd-section")).toBeInTheDocument()
    })
    const pill = screen.getByTestId("ops-aimd-reason")
    expect(pill.getAttribute("data-reason")).toBe("multiplicative_decrease")
    expect(pill.textContent).toContain("MD")
    // Counters tile reports +1 AI and -1 MD over the 5-min window.
    const counts = screen.getByTestId("ops-aimd-counts")
    expect(counts.textContent).toContain("+1/-1")
    // MD ≥ 1 → warn tone (orange) so the operator notices the halving.
    const valueSpan = counts.querySelector("div.font-mono.font-semibold") as HTMLElement
    expect(valueSpan.className).toContain("fui-orange")
  })

  it("renders a budget=floor tile with 'bad' tone after a sustained MD spiral", async () => {
    ;(api.getOpsSummary as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...baseOps,
      aimd: {
        ...aimdBase(),
        budget: 2,                  // == floor
        last_reason: "floor",
        trace: [
          { timestamp: 1, budget: 6, reason: "init",                    cpu_percent: 0,  mem_percent: 0  },
          { timestamp: 30, budget: 3, reason: "multiplicative_decrease", cpu_percent: 95, mem_percent: 50 },
          { timestamp: 60, budget: 2, reason: "multiplicative_decrease", cpu_percent: 95, mem_percent: 50 },
          { timestamp: 90, budget: 2, reason: "floor",                   cpu_percent: 95, mem_percent: 50 },
        ],
      },
    })
    render(<OpsSummaryPanel />)

    await waitFor(() => {
      expect(screen.getByTestId("ops-aimd-section")).toBeInTheDocument()
    })
    const tile = screen.getByTestId("ops-aimd-budget")
    expect(tile.textContent).toContain("2/12")
    // budget at floor → bad tone (red).
    const valueSpan = tile.querySelector("div.font-mono.font-semibold") as HTMLElement
    expect(valueSpan.className).toContain("critical-red")
  })

  it("renders an empty-state placeholder when fewer than 2 trace points exist", async () => {
    ;(api.getOpsSummary as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...baseOps,
      aimd: {
        ...aimdBase(),
        // Only the cold-start INIT entry — sparkline can't draw a line.
        trace: [
          { timestamp: 1, budget: 6, reason: "init", cpu_percent: 0, mem_percent: 0 },
        ],
      },
    })
    render(<OpsSummaryPanel />)

    await waitFor(() => {
      expect(screen.getByTestId("ops-aimd-section")).toBeInTheDocument()
    })
    const trace = screen.getByTestId("ops-aimd-trace")
    expect(trace.getAttribute("data-empty")).toBe("true")
  })

  it("hides the AIMD section entirely when the backend omits it (older API)", async () => {
    ;(api.getOpsSummary as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...baseOps,
    })
    render(<OpsSummaryPanel />)

    await waitFor(() => {
      expect(screen.getByText("OPS SUMMARY")).toBeInTheDocument()
    })
    expect(screen.queryByTestId("ops-aimd-section")).toBeNull()
    expect(screen.queryByText("AIMD BUDGET")).toBeNull()
  })
})
