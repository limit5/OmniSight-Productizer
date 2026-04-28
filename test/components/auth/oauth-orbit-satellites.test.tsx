/**
 * AS.7.7 — `<OAuthOrbitSatellites>` component tests.
 *
 * Pins:
 *   - Render mode (rotating vs static vs flat) per motion level
 *   - Linked vs available satellite partition + data attributes
 *   - Connect / disconnect handlers receive the right provider id
 *   - Disabled state suppresses click handlers + renders <span>
 *   - Avatar passes through; falls back to glyph
 */

import { describe, expect, it, vi } from "vitest"
import { cleanup, render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

import { OAuthOrbitSatellites } from "@/components/omnisight/auth/oauth-orbit-satellites"

const TEST_LINKED = [{ provider: "google" }] as const

function renderIcon(id: string) {
  return <span data-testid={`icon-${id}`}>{id[0]}</span>
}

describe("AS.7.7 OAuthOrbitSatellites", () => {
  it("renders the avatar + 11 satellites in dramatic mode (rotating)", () => {
    render(
      <OAuthOrbitSatellites
        level="dramatic"
        linked={TEST_LINKED}
        renderIcon={renderIcon}
      />,
    )
    const stage = screen.getByTestId("as7-orbit-stage")
    expect(stage).toHaveAttribute("data-as7-orbit-mode", "rotating")
    expect(stage).toHaveAttribute("data-as7-orbit-linked-count", "1")
    expect(stage).toHaveAttribute("data-as7-orbit-available-count", "10")
    // Inner ring carries the linked provider; outer carries the rest.
    expect(screen.getByTestId("as7-orbit-ring-inner")).toHaveAttribute(
      "data-as7-orbit-spin",
      "on",
    )
    expect(screen.getByTestId("as7-orbit-ring-outer")).toHaveAttribute(
      "data-as7-orbit-spin",
      "on",
    )
    cleanup()
  })

  it("renders static (no spin) in normal motion", () => {
    render(
      <OAuthOrbitSatellites
        level="normal"
        linked={TEST_LINKED}
        renderIcon={renderIcon}
      />,
    )
    expect(screen.getByTestId("as7-orbit-stage")).toHaveAttribute(
      "data-as7-orbit-mode",
      "static",
    )
    expect(screen.getByTestId("as7-orbit-ring-inner")).toHaveAttribute(
      "data-as7-orbit-spin",
      "off",
    )
    cleanup()
  })

  it("collapses to flat layout in subtle motion", () => {
    render(
      <OAuthOrbitSatellites
        level="subtle"
        linked={TEST_LINKED}
        renderIcon={renderIcon}
      />,
    )
    expect(screen.getByTestId("as7-orbit-stage")).toHaveAttribute(
      "data-as7-orbit-mode",
      "flat",
    )
    expect(screen.getByTestId("as7-orbit-flat")).toBeInTheDocument()
    expect(screen.queryByTestId("as7-orbit-ring-inner")).toBeNull()
    cleanup()
  })

  it("collapses to flat layout when motion is off", () => {
    render(
      <OAuthOrbitSatellites
        level="off"
        linked={TEST_LINKED}
        renderIcon={renderIcon}
      />,
    )
    expect(screen.getByTestId("as7-orbit-stage")).toHaveAttribute(
      "data-as7-orbit-mode",
      "flat",
    )
    cleanup()
  })

  it("marks the inner-ring satellite as linked + outer as available", () => {
    render(
      <OAuthOrbitSatellites
        level="dramatic"
        linked={TEST_LINKED}
        renderIcon={renderIcon}
      />,
    )
    expect(
      screen.getByTestId("as7-orbit-satellite-google"),
    ).toHaveAttribute("data-as7-orbit-linked", "yes")
    expect(
      screen.getByTestId("as7-orbit-satellite-google"),
    ).toHaveAttribute("data-as7-orbit-ring", "inner")
    expect(
      screen.getByTestId("as7-orbit-satellite-github"),
    ).toHaveAttribute("data-as7-orbit-linked", "no")
    expect(
      screen.getByTestId("as7-orbit-satellite-github"),
    ).toHaveAttribute("data-as7-orbit-ring", "outer")
    cleanup()
  })

  it("calls onDisconnect with the right provider id from inner ring", async () => {
    const user = userEvent.setup()
    const onDisconnect = vi.fn()
    render(
      <OAuthOrbitSatellites
        level="dramatic"
        linked={TEST_LINKED}
        renderIcon={renderIcon}
        onDisconnect={onDisconnect}
      />,
    )
    await user.click(screen.getByTestId("as7-orbit-satellite-google"))
    expect(onDisconnect).toHaveBeenCalledWith("google")
    cleanup()
  })

  it("calls onConnect with the right provider id from outer ring", async () => {
    const user = userEvent.setup()
    const onConnect = vi.fn()
    render(
      <OAuthOrbitSatellites
        level="dramatic"
        linked={[]}
        renderIcon={renderIcon}
        onConnect={onConnect}
      />,
    )
    await user.click(screen.getByTestId("as7-orbit-satellite-github"))
    expect(onConnect).toHaveBeenCalledWith("github")
    cleanup()
  })

  it("renders satellites as <span> when no handler is provided", () => {
    render(
      <OAuthOrbitSatellites
        level="dramatic"
        linked={[]}
        renderIcon={renderIcon}
      />,
    )
    const node = screen.getByTestId("as7-orbit-satellite-github")
    expect(node.tagName.toLowerCase()).toBe("span")
    cleanup()
  })

  it("renders disabled satellites as <span> with aria-disabled", () => {
    render(
      <OAuthOrbitSatellites
        level="dramatic"
        linked={TEST_LINKED}
        renderIcon={renderIcon}
        onConnect={vi.fn()}
        onDisconnect={vi.fn()}
        disabled
      />,
    )
    const node = screen.getByTestId("as7-orbit-satellite-google")
    expect(node.tagName.toLowerCase()).toBe("span")
    expect(node).toHaveAttribute("aria-disabled", "true")
    cleanup()
  })

  it("threads custom avatar element into the stage", () => {
    render(
      <OAuthOrbitSatellites
        level="dramatic"
        linked={[]}
        renderIcon={renderIcon}
        avatar={<span data-testid="my-avatar">A</span>}
      />,
    )
    expect(screen.getByTestId("my-avatar")).toBeInTheDocument()
    cleanup()
  })

  it("falls back to the glyph when no avatar provided", () => {
    render(
      <OAuthOrbitSatellites
        level="dramatic"
        linked={[]}
        renderIcon={renderIcon}
      />,
    )
    expect(screen.getByTestId("as7-orbit-avatar")).toBeInTheDocument()
    cleanup()
  })

  it("forwards the brand color via CSS variable on the satellite element", () => {
    render(
      <OAuthOrbitSatellites
        level="dramatic"
        linked={[]}
        renderIcon={renderIcon}
      />,
    )
    const node = screen.getByTestId(
      "as7-orbit-satellite-google",
    ) as HTMLElement
    // Google brand: #4285F4
    expect(node.style.getPropertyValue("--as7-oauth-brand").toLowerCase()).toBe(
      "#4285f4",
    )
    cleanup()
  })
})
