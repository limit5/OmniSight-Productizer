import { describe, expect, it } from "vitest"
import { render, screen } from "@testing-library/react"

import { QuotaPulse } from "@/components/omnisight/multi-provider-orchestrator/QuotaPulse"
import type { ProviderConstellationProvider } from "@/components/omnisight/multi-provider-orchestrator/ProviderConstellation"

function provider(
  id: string,
  activityLevel: number,
): ProviderConstellationProvider {
  return {
    id,
    name: id,
    slot: "top-right",
    allocationPercent: 25,
    quotaState: "healthy",
    activityLevel,
  }
}

describe("OP-42 QuotaPulse", () => {
  it("snapshots the pulse class for each activity tier", () => {
    const tiers = [
      ["fast", 0.7, "top-right"],
      ["slow", 0.3, "bottom-right"],
      ["off", 0.29, "top-left"],
    ] as const

    render(
      <div>
        {tiers.map(([id, activityLevel, position]) => (
          <QuotaPulse
            key={id}
            provider={provider(id, activityLevel)}
            position={position}
          />
        ))}
      </div>,
    )

    const pulseClasses = tiers.map(([id]) => {
      const pulse = screen.getByTestId(`mp-quota-pulse-${id}`)
      expect(pulse).toHaveClass(`pulse-${id}`)
      return pulse.getAttribute("data-mp-quota-pulse")
    })

    expect(pulseClasses).toMatchInlineSnapshot(`
      [
        "pulse-fast",
        "pulse-slow",
        "pulse-off",
      ]
    `)
  })
})
