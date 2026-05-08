/**
 * OP-96 / MP.W14.2 — ProviderConstellation Grok coming-soon slot.
 *
 * Mirrors the OP-92 Gemini planned-provider coverage: Grok is rendered
 * as a muted disabled sphere and advertises its planned release version
 * through the shared ProviderEnergySphere comingVersion tooltip.
 */

import { describe, expect, it } from "vitest"
import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

import {
  ProviderConstellation,
  type ProviderConstellationProvider,
} from "@/components/omnisight/multi-provider-orchestrator/ProviderConstellation"

class MockResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

globalThis.ResizeObserver = MockResizeObserver

const ACTIVE_PROVIDERS: ProviderConstellationProvider[] = [
  {
    id: "openai",
    name: "OpenAI",
    slot: "top-left",
    allocationPercent: 55,
    quotaState: "healthy",
  },
  {
    id: "anthropic",
    name: "Anthropic",
    slot: "top-right",
    allocationPercent: 45,
    quotaState: "watch",
  },
]

function renderConstellation(
  providers: ProviderConstellationProvider[] = ACTIVE_PROVIDERS,
) {
  return render(
    <ProviderConstellation
      providers={providers}
      taskSummary={{
        title: "Daily work",
        taskCount: 4,
        estimatedTokens: 180_000,
      }}
      tradeoffValue={50}
    />,
  )
}

describe("OP-96 ProviderConstellation Grok coming slot", () => {
  it("renders a grok provider entry as a muted disabled coming v0.6.0 sphere", async () => {
    const user = userEvent.setup()

    renderConstellation([
      ...ACTIVE_PROVIDERS,
      {
        id: "grok",
        name: "Grok",
        slot: "bottom-right",
        allocationPercent: 0,
        quotaState: "unavailable",
      },
    ])

    const grok = screen.getByTestId("mp-provider-sphere-grok")
    expect(grok).toHaveAttribute("aria-disabled", "true")
    expect(grok).toHaveClass("opacity-50")
    expect(grok).toHaveAttribute("title", "Grok. Coming v0.6.0")

    await user.hover(grok)
    expect(
      await screen.findByTestId("mp-provider-sphere-grok-coming-tooltip"),
    ).toHaveTextContent("Coming v0.6.0")
  })

  it("shows two active providers plus Gemini and Grok coming spheres", () => {
    const { container } = renderConstellation()

    expect(screen.getAllByText("OpenAI").length).toBeGreaterThan(0)
    expect(screen.getAllByText("Anthropic").length).toBeGreaterThan(0)
    expect(screen.getByTestId("mp-provider-sphere-gemini")).toHaveAttribute(
      "aria-disabled",
      "true",
    )
    expect(screen.getByTestId("mp-provider-sphere-grok")).toHaveAttribute(
      "aria-disabled",
      "true",
    )
    expect(
      container.querySelectorAll(
        "#mp-provider-constellation .absolute.z-20",
      ),
    ).toHaveLength(4)
  })
})
