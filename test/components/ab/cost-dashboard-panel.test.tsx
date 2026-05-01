/**
 * AB.6.6 — CostDashboardPanel tests.
 */

import { describe, expect, it, vi } from "vitest"
import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

import { CostDashboardPanel } from "@/components/omnisight/ab/cost-dashboard-panel"
import type {
  BudgetAlert,
  BudgetCap,
  CostSnapshot,
} from "@/components/omnisight/ab/types"

function _snap(over: Partial<CostSnapshot> = {}): CostSnapshot {
  return {
    scope: { kind: "priority", key: "HD" },
    spend_today_usd: 1.0,
    spend_month_usd: 30.0,
    budget: null,
    ...over,
  }
}

function _budget(over: Partial<BudgetCap> = {}): BudgetCap {
  return {
    scope: { kind: "priority", key: "HD" },
    daily_limit_usd: 10.0,
    monthly_limit_usd: 200.0,
    per_batch_limit_usd: null,
    enabled: true,
    ...over,
  }
}

function _alert(over: Partial<BudgetAlert> = {}): BudgetAlert {
  return {
    alert_id: "a1",
    scope: { kind: "priority", key: "HD" },
    period: "daily",
    level: "warn_80",
    threshold_usd: 10.0,
    observed_usd: 8.5,
    action: "notify",
    fired_at: new Date(Date.now() - 30_000).toISOString(),
    ...over,
  }
}

describe("CostDashboardPanel", () => {
  it("renders empty states when no data", () => {
    render(
      <CostDashboardPanel snapshots={[]} alerts={[]} budgets={[]} />,
    )
    expect(screen.getByTestId("cost-snapshots-empty")).toBeInTheDocument()
    expect(screen.getByTestId("cost-alerts-empty")).toBeInTheDocument()
    expect(screen.getByTestId("cost-budgets-empty")).toBeInTheDocument()
  })

  it("renders snapshot row with USD formatting", () => {
    render(
      <CostDashboardPanel
        snapshots={[
          _snap({
            spend_today_usd: 7.5,
            spend_month_usd: 150.0,
            budget: _budget(),
          }),
        ]}
        alerts={[]}
        budgets={[]}
      />,
    )
    const row = screen.getByTestId("cost-snapshot-priority-HD")
    expect(row).toHaveTextContent("$7.50")
    expect(row).toHaveTextContent("$10.00")
    expect(row).toHaveTextContent("$150.00")
    expect(row).toHaveTextContent("$200.00")
  })

  it("emphasises spend at 80%+ of cap (yellow)", () => {
    render(
      <CostDashboardPanel
        snapshots={[
          _snap({
            spend_today_usd: 8.5,
            budget: _budget({ daily_limit_usd: 10.0 }),
          }),
        ]}
        alerts={[]}
        budgets={[]}
      />,
    )
    const row = screen.getByTestId("cost-snapshot-priority-HD")
    expect(row.innerHTML).toMatch(/text-yellow-700[^"]*">\s*\$8\.50/)
  })

  it("renders all three alert levels with correct icons + bg", () => {
    render(
      <CostDashboardPanel
        snapshots={[]}
        alerts={[
          _alert({ alert_id: "w", level: "warn_80" }),
          _alert({ alert_id: "c", level: "cap_100", action: "throttle" }),
          _alert({ alert_id: "o", level: "over_120", action: "block" }),
        ]}
        budgets={[]}
      />,
    )
    expect(screen.getByTestId("cost-alert-warn_80")).toBeInTheDocument()
    expect(screen.getByTestId("cost-alert-cap_100")).toBeInTheDocument()
    expect(screen.getByTestId("cost-alert-over_120")).toBeInTheDocument()
    // Levels carry their distinguishing background classes
    expect(
      screen.getByTestId("cost-alert-over_120").className,
    ).toContain("bg-red-50")
    expect(
      screen.getByTestId("cost-alert-warn_80").className,
    ).toContain("bg-yellow-50")
  })

  it("renders disabled budget row in muted style", () => {
    render(
      <CostDashboardPanel
        snapshots={[]}
        alerts={[]}
        budgets={[_budget({ enabled: false })]}
      />,
    )
    const row = screen.getByTestId("cost-budget-priority-HD")
    expect(row.className).toContain("text-gray-400")
  })

  it("counts only enabled budgets in header", () => {
    render(
      <CostDashboardPanel
        snapshots={[]}
        alerts={[]}
        budgets={[
          _budget({ enabled: true }),
          _budget({
            enabled: false,
            scope: { kind: "workspace", key: "dev" },
          }),
        ]}
      />,
    )
    expect(screen.getByText(/1 enabled/)).toBeInTheDocument()
  })

  it("invokes onConfigureBudget on scope click", async () => {
    const onConfigureBudget = vi.fn()
    const user = userEvent.setup()
    render(
      <CostDashboardPanel
        snapshots={[]}
        alerts={[]}
        budgets={[_budget()]}
        onConfigureBudget={onConfigureBudget}
      />,
    )
    const button = screen
      .getByTestId("cost-budget-priority-HD")
      .querySelector("button")!
    await user.click(button)
    expect(onConfigureBudget).toHaveBeenCalledWith("priority::HD")
  })
})
