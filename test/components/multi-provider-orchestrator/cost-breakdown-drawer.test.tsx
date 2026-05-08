import { describe, expect, it, vi } from "vitest"
import { fireEvent, render, screen } from "@testing-library/react"

import { CostBreakdownDrawer } from "@/components/omnisight/multi-provider-orchestrator/CostBreakdownDrawer"
import type { ProviderConstellationProvider } from "@/components/omnisight/multi-provider-orchestrator/ProviderConstellation"

type CostProvider = ProviderConstellationProvider & {
  tokensIn: number
  tokensOut: number
  totalUsd: number
}

const PROVIDERS: CostProvider[] = [
  { id: "openai", name: "OpenAI", slot: "top-left", allocationPercent: 45, quotaState: "healthy", tokensIn: 12_000, tokensOut: 3_200, totalUsd: 0.241 },
  { id: "anthropic", name: "Anthropic", slot: "top-right", allocationPercent: 35, quotaState: "watch", tokensIn: 18_500, tokensOut: 4_400, totalUsd: 0.612 },
  { id: "groq", name: "Groq", slot: "bottom-left", allocationPercent: 20, quotaState: "healthy", tokensIn: 7_000, tokensOut: 1_100, totalUsd: 0.037 },
]

describe("OP-43 CostBreakdownDrawer", () => {
  it("snapshots three provider rows and a summed footer", () => {
    render(<CostBreakdownDrawer open onClose={vi.fn()} providers={PROVIDERS} />)

    expect(screen.getByTestId("mp-cost-breakdown-drawer")).toBeInTheDocument()
    expect([
      screen.getByTestId("mp-cost-breakdown-row-openai").textContent,
      screen.getByTestId("mp-cost-breakdown-row-anthropic").textContent,
      screen.getByTestId("mp-cost-breakdown-row-groq").textContent,
      screen.getByTestId("mp-cost-breakdown-sum-row").textContent,
    ]).toMatchInlineSnapshot(`
      [
        "OpenAI12,0003,200$0.241",
        "Anthropic18,5004,400$0.612",
        "Groq7,0001,100$0.037",
        "Total37,5008,700$0.890",
      ]
    `)
  })

  it("closes from Escape and the close button", () => {
    const onClose = vi.fn()
    render(<CostBreakdownDrawer open onClose={onClose} providers={PROVIDERS} />)

    fireEvent.keyDown(window, { key: "Escape" })
    expect(onClose).toHaveBeenCalledTimes(1)

    fireEvent.click(screen.getByRole("button", { name: "Close cost breakdown" }))
    expect(onClose).toHaveBeenCalledTimes(2)
  })
})
