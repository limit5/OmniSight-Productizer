/**
 * Phase 49B — BudgetStrategyPanel tests.
 *
 * 1. Initial load paints the current strategy + tuning.
 * 2. Clicking a card PUTs and reflects the new tuning.
 * 3. SSE budget_strategy_changed updates from a peer.
 * 4. Error banner surfaces a RETRY button that re-invokes the fetcher.
 */

import { describe, expect, it, vi, beforeEach } from "vitest"
import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

vi.mock("@/lib/api", () => ({
  getBudgetStrategy: vi.fn(),
  setBudgetStrategy: vi.fn(),
  subscribeEvents: vi.fn(),
}))

import { BudgetStrategyPanel } from "@/components/omnisight/budget-strategy-panel"
import * as api from "@/lib/api"
import { primeSSE as _primeSSE } from "../helpers/sse"

const primeSSE = () => _primeSSE(api)

const balancedTuning = {
  strategy: "balanced", model_tier: "default", max_retries: 2,
  downgrade_at_usage_pct: 90, freeze_at_usage_pct: 100, prefer_parallel: false,
} as const

const costSaverTuning = {
  strategy: "cost_saver", model_tier: "budget", max_retries: 1,
  downgrade_at_usage_pct: 70, freeze_at_usage_pct: 95, prefer_parallel: false,
} as const

describe("BudgetStrategyPanel", () => {
  beforeEach(() => { vi.clearAllMocks() })

  it("renders the current strategy + tuning on mount", async () => {
    ;(api.getBudgetStrategy as ReturnType<typeof vi.fn>).mockResolvedValue({
      strategy: "balanced", tuning: balancedTuning, available: [balancedTuning],
    })
    primeSSE()
    render(<BudgetStrategyPanel />)
    await waitFor(() => {
      expect(screen.getByRole("radio", { checked: true })).toHaveAccessibleName(/BALANCED/)
    })
    // tuning readout
    expect(screen.getByText("DEFAULT")).toBeInTheDocument()
    expect(screen.getByText("2")).toBeInTheDocument()
    expect(screen.getByText("90%")).toBeInTheDocument()
    expect(screen.getByText("100%")).toBeInTheDocument()
  })

  it("switches strategy when a card is clicked", async () => {
    const user = userEvent.setup()
    ;(api.getBudgetStrategy as ReturnType<typeof vi.fn>).mockResolvedValue({
      strategy: "balanced", tuning: balancedTuning, available: [balancedTuning],
    })
    ;(api.setBudgetStrategy as ReturnType<typeof vi.fn>).mockResolvedValue({
      strategy: "cost_saver", tuning: costSaverTuning,
    })
    primeSSE()
    render(<BudgetStrategyPanel />)
    await waitFor(() => screen.getByRole("radio", { checked: true }))
    await user.click(screen.getByRole("radio", { name: /COST SAVER/ }))
    expect(api.setBudgetStrategy).toHaveBeenCalledWith("cost_saver")
    await waitFor(() => {
      expect(screen.getByRole("radio", { checked: true })).toHaveAccessibleName(/COST SAVER/)
    })
    expect(screen.getByText("BUDGET")).toBeInTheDocument()
    expect(screen.getByText("70%")).toBeInTheDocument()
  })

  it("updates on SSE budget_strategy_changed from a peer", async () => {
    ;(api.getBudgetStrategy as ReturnType<typeof vi.fn>).mockResolvedValue({
      strategy: "balanced", tuning: balancedTuning, available: [balancedTuning],
    })
    const sse = primeSSE()
    render(<BudgetStrategyPanel />)
    await waitFor(() => screen.getByRole("radio", { checked: true }))
    sse.emit({
      event: "budget_strategy_changed",
      data: { strategy: "cost_saver", previous: "balanced", tuning: costSaverTuning },
    })
    await waitFor(() => {
      expect(screen.getByRole("radio", { checked: true })).toHaveAccessibleName(/COST SAVER/)
    })
  })

  it("shows RETRY on error and re-invokes the fetcher", async () => {
    const user = userEvent.setup()
    const fetcher = api.getBudgetStrategy as ReturnType<typeof vi.fn>
    fetcher.mockRejectedValueOnce(new Error("401 unauthorized"))
    fetcher.mockResolvedValueOnce({
      strategy: "balanced", tuning: balancedTuning, available: [balancedTuning],
    })
    primeSSE()
    render(<BudgetStrategyPanel />)
    await screen.findByText(/401 unauthorized/)
    await user.click(screen.getByRole("button", { name: "RETRY" }))
    await waitFor(() => {
      expect(screen.getByRole("radio", { checked: true })).toHaveAccessibleName(/BALANCED/)
    })
    expect(fetcher).toHaveBeenCalledTimes(2)
  })
})
