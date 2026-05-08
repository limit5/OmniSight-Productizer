/**
 * OP-92 / MP.W13.2 — ProviderEnergySphere coming-soon state.
 *
 * Pins the Gemini planned-provider treatment without changing the
 * active sphere path: coming providers are muted, disabled, and explain
 * the planned version through the standard Radix tooltip wrapper.
 */

import { describe, expect, it, vi } from "vitest"
import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

import {
  ProviderEnergySphere,
  type ProviderEnergySphereProvider,
} from "@/components/omnisight/multi-provider-orchestrator/ProviderEnergySphere"
import { ProviderConstellation } from "@/components/omnisight/multi-provider-orchestrator/ProviderConstellation"
import { getProvider } from "@/lib/auth/oauth-providers"

const GOOGLE = getProvider("google")
const GEMINI: ProviderEnergySphereProvider = {
  id: "gemini",
  displayName: "Gemini",
  brandColor: "var(--muted-foreground)",
}

class MockResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

globalThis.ResizeObserver = MockResizeObserver

describe("OP-92 ProviderEnergySphere comingVersion", () => {
  it("renders comingVersion as a muted disabled sphere with tooltip text", async () => {
    const user = userEvent.setup()

    render(
      <ProviderEnergySphere
        level="normal"
        provider={GEMINI}
        icon={<span>G</span>}
        onSelect={vi.fn()}
        comingVersion="v0.5.0"
      />,
    )

    const sphere = screen.getByTestId("mp-provider-sphere-gemini")
    expect(sphere).toHaveAttribute("aria-disabled", "true")
    expect(sphere).toHaveClass("opacity-50")
    expect(sphere.style.getPropertyValue("--mp-provider-ring")).toBe(
      "var(--muted-foreground)",
    )

    await user.hover(sphere)
    const tooltip = await screen.findByTestId(
      "mp-provider-sphere-gemini-coming-tooltip",
    )
    expect(tooltip).toHaveTextContent("Coming v0.5.0")
    expect([sphere.outerHTML, tooltip.outerHTML]).toMatchInlineSnapshot(`
      [
        "<span data-testid="mp-provider-sphere-gemini" data-mp-provider-tier="primary" data-mp-quota-tier="gray" data-mp-quota-pulse="off" data-mp-selected="false" aria-disabled="true" aria-label="Gemini. Coming v0.5.0" title="Gemini. Coming v0.5.0" class="relative inline-flex shrink-0 items-center justify-center overflow-visible rounded-full transition-[width,height,box-shadow,transform,opacity] duration-200 ease-out focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--artifact-purple)] focus-visible:ring-offset-2 focus-visible:ring-offset-background opacity-50 cursor-not-allowed " style="--mp-provider-brand: var(--muted-foreground); --mp-provider-ring: var(--muted-foreground); width: 64px; height: 64px; background: radial-gradient(circle at 32% 28%, rgba(255,255,255,0.36), transparent 30%), var(--mp-provider-brand); box-shadow: 0 0 0 1px color-mix(in srgb, var(--muted-foreground) 58%, transparent), 0 0 22px color-mix(in srgb, var(--muted-foreground) 36%, transparent);" data-state="delayed-open" data-slot="tooltip-trigger" aria-describedby="radix-_r_0_"><span aria-hidden="true" data-testid="mp-provider-sphere-gemini-quota-ring" class="absolute -inset-1 rounded-full border-2 border-[var(--mp-provider-ring)] " style="opacity: 0.55; box-shadow: 0 0 18px color-mix(in srgb, var(--muted-foreground) 46%, transparent);"></span><span aria-hidden="true" class="absolute inset-[18%] rounded-full bg-black/18"></span><span class="relative z-10 flex h-[58%] w-[58%] items-center justify-center [&amp;_svg]:h-full [&amp;_svg]:w-full" aria-hidden="true"><span>G</span></span></span>",
        "<div data-side="top" data-align="center" data-state="delayed-open" data-slot="tooltip-content" class="animate-in fade-in-0 zoom-in-95 data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=closed]:zoom-out-95 data-[side=bottom]:slide-in-from-top-2 data-[side=left]:slide-in-from-right-2 data-[side=right]:slide-in-from-left-2 data-[side=top]:slide-in-from-bottom-2 z-50 w-fit origin-(--radix-tooltip-content-transform-origin) rounded-md px-3 py-1.5 text-balance border border-[var(--border)] bg-[var(--card)] font-mono text-[10px] tracking-wide text-[var(--foreground)]" data-testid="mp-provider-sphere-gemini-coming-tooltip" style="--radix-tooltip-content-transform-origin: var(--radix-popper-transform-origin); --radix-tooltip-content-available-width: var(--radix-popper-available-width); --radix-tooltip-content-available-height: var(--radix-popper-available-height); --radix-tooltip-trigger-width: var(--radix-popper-anchor-width); --radix-tooltip-trigger-height: var(--radix-popper-anchor-height);">Coming v0.5.0<span style="position: absolute; bottom: 0px; transform: translateY(100%); left: 0px;"><svg class="bg-foreground fill-foreground z-50 size-2.5 translate-y-[calc(-50%_-_2px)] rotate-45 rounded-[2px]" style="display: block;" width="10" height="5" viewBox="0 0 30 10" preserveAspectRatio="none"><polygon points="0,0 30,0 15,10"></polygon></svg></span><span id="radix-_r_0_" role="tooltip" style="position: absolute; border: 0px; width: 1px; height: 1px; padding: 0px; margin: -1px; overflow: hidden; clip: rect(0px, 0px, 0px, 0px); white-space: nowrap; word-wrap: normal;">Coming v0.5.0</span></div>",
      ]
    `)
  })

  it("preserves active behaviour without comingVersion", async () => {
    const user = userEvent.setup()
    const onSelect = vi.fn()

    render(
      <ProviderEnergySphere
        level="normal"
        provider={GOOGLE}
        icon={<span>G</span>}
        onSelect={onSelect}
      />,
    )

    const sphere = screen.getByTestId("mp-provider-sphere-google")
    expect(sphere.tagName.toLowerCase()).toBe("button")
    expect(sphere).not.toHaveAttribute("aria-disabled")
    expect(sphere).not.toHaveClass("opacity-50")
    expect(screen.queryByText("Coming v0.5.0")).toBeNull()

    await user.click(sphere)
    expect(onSelect).toHaveBeenCalledTimes(1)
  })

  it("adds the Gemini coming slot to ProviderConstellation", () => {
    render(
      <ProviderConstellation
        providers={[
          {
            id: "anthropic",
            name: "Anthropic",
            slot: "top-left",
            allocationPercent: 40,
            quotaState: "healthy",
          },
          {
            id: "openai",
            name: "OpenAI",
            slot: "top-right",
            allocationPercent: 35,
            quotaState: "watch",
          },
          {
            id: "groq",
            name: "Groq",
            slot: "bottom-left",
            allocationPercent: 25,
            quotaState: "healthy",
          },
        ]}
        taskSummary={{
          title: "Daily work",
          taskCount: 3,
          estimatedTokens: 120_000,
        }}
        tradeoffValue={50}
      />,
    )

    const gemini = screen.getByTestId("mp-provider-sphere-gemini")
    expect(gemini).toHaveAttribute("aria-disabled", "true")
    expect(gemini).toHaveClass("opacity-50")
    expect(gemini).toHaveAttribute("title", "Gemini. Coming v0.5.0")
  })
})
