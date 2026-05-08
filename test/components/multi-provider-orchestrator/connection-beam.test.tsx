/** OP-40 / MP.W4.4 - ConnectionBeam SVG contract. */

import { describe, expect, it } from "vitest"
import { render, screen } from "@testing-library/react"

import { ConnectionBeam } from "@/components/omnisight/multi-provider-orchestrator/ConnectionBeam"
import type { ProviderConstellationProvider } from "@/components/omnisight/multi-provider-orchestrator/ProviderConstellation"

function provider(
  overrides: Partial<ProviderConstellationProvider> = {},
): ProviderConstellationProvider {
  return {
    id: "openai",
    name: "OpenAI",
    slot: "top-left",
    allocationPercent: 52,
    quotaState: "healthy",
    activityLevel: 0.75,
    ...overrides,
  }
}

describe("OP-40 ConnectionBeam", () => {
  it("renders an animated SVG path with the connection-beam test id", () => {
    const { container } = render(<ConnectionBeam provider={provider()} />)

    const beam = screen.getByTestId("connection-beam")
    const path = container.querySelector("path")
    const animation = container.querySelector(
      "animate[attributeName='stroke-dashoffset']",
    )

    expect(beam.tagName.toLowerCase()).toBe("svg")
    expect(path).toBeTruthy()
    expect(path).toHaveAttribute("stroke-dasharray", "14 16")
    expect(animation).toBeTruthy()
    expect({
      testId: beam.getAttribute("data-testid"),
      path: path?.getAttribute("d"),
      animation: animation?.getAttribute("attributeName"),
    }).toMatchInlineSnapshot(`
      {
        "animation": "stroke-dashoffset",
        "path": "M 4 16 C 28 18, 54 34, 96 84",
        "testId": "connection-beam",
      }
    `)
  })

  it("scales intensity from provider activityLevel", () => {
    const { rerender } = render(
      <ConnectionBeam provider={provider({ activityLevel: 0 })} />,
    )
    const beam = screen.getByTestId("connection-beam")

    expect(beam).toHaveAttribute("data-mp-activity-level", "0.00")
    expect(beam.style.opacity).toBe("0.28")

    rerender(<ConnectionBeam provider={provider({ activityLevel: 1 })} />)

    expect(beam).toHaveAttribute("data-mp-activity-level", "1.00")
    expect(beam.style.opacity).toBe("1")
  })
})
