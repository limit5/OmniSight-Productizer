/**
 * AS.7.1 — `<OAuthEnergySphere>` component tests.
 *
 * Pins:
 *   - Render shape (anchor + halo + inner icon slot)
 *   - href + aria-label round-trip
 *   - `data-as7-tier` for primary vs. secondary sizing
 *   - `data-as7-halo` gating per motion budget (off/subtle/normal vs. dramatic)
 *   - Disabled state renders a span (no anchor) + aria-disabled
 */

import { describe, expect, it } from "vitest"
import { render, screen } from "@testing-library/react"

import { OAuthEnergySphere } from "@/components/omnisight/auth/oauth-energy-sphere"
import { getProvider } from "@/lib/auth/oauth-providers"

const GOOGLE = getProvider("google")

describe("AS.7.1 OAuthEnergySphere", () => {
  it("renders an anchor with the provided href + aria-label", () => {
    render(
      <OAuthEnergySphere
        level="dramatic"
        provider={GOOGLE}
        href="/api/v1/auth/oauth/google/authorize"
        icon={<span data-testid="icon">G</span>}
      />,
    )
    const sphere = screen.getByTestId("as7-oauth-google")
    expect(sphere.tagName.toLowerCase()).toBe("a")
    expect(sphere).toHaveAttribute("href", "/api/v1/auth/oauth/google/authorize")
    expect(sphere).toHaveAttribute("aria-label", "Sign in with Google")
    expect(screen.getByTestId("icon")).toBeInTheDocument()
  })

  it("primary tier sphere — data-as7-tier=primary by default", () => {
    render(
      <OAuthEnergySphere
        level="dramatic"
        provider={GOOGLE}
        href="/x"
        icon={<span>G</span>}
      />,
    )
    expect(screen.getByTestId("as7-oauth-google")).toHaveAttribute(
      "data-as7-tier",
      "primary",
    )
  })

  it("secondary tier sphere — data-as7-tier=secondary", () => {
    render(
      <OAuthEnergySphere
        level="dramatic"
        provider={GOOGLE}
        href="/x"
        size="secondary"
        icon={<span>G</span>}
      />,
    )
    expect(screen.getByTestId("as7-oauth-google")).toHaveAttribute(
      "data-as7-tier",
      "secondary",
    )
  })

  it("at `dramatic` halo pulse is on", () => {
    render(
      <OAuthEnergySphere
        level="dramatic"
        provider={GOOGLE}
        href="/x"
        icon={<span>G</span>}
      />,
    )
    expect(screen.getByTestId("as7-oauth-google")).toHaveAttribute(
      "data-as7-halo",
      "on",
    )
  })

  it("at `normal` halo pulse is off (battery courtesy)", () => {
    render(
      <OAuthEnergySphere
        level="normal"
        provider={GOOGLE}
        href="/x"
        icon={<span>G</span>}
      />,
    )
    expect(screen.getByTestId("as7-oauth-google")).toHaveAttribute(
      "data-as7-halo",
      "off",
    )
  })

  it("at `off` halo pulse is off", () => {
    render(
      <OAuthEnergySphere
        level="off"
        provider={GOOGLE}
        href="/x"
        icon={<span>G</span>}
      />,
    )
    expect(screen.getByTestId("as7-oauth-google")).toHaveAttribute(
      "data-as7-halo",
      "off",
    )
  })

  it("disabled renders a span (no anchor) + aria-disabled=true", () => {
    render(
      <OAuthEnergySphere
        level="dramatic"
        provider={GOOGLE}
        href="/x"
        icon={<span>G</span>}
        disabled
      />,
    )
    const sphere = screen.getByTestId("as7-oauth-google")
    expect(sphere.tagName.toLowerCase()).toBe("span")
    expect(sphere).toHaveAttribute("aria-disabled", "true")
    expect(sphere).not.toHaveAttribute("href")
  })

  it("forwards brand color as a CSS variable on the host", () => {
    render(
      <OAuthEnergySphere
        level="dramatic"
        provider={GOOGLE}
        href="/x"
        icon={<span>G</span>}
      />,
    )
    const sphere = screen.getByTestId("as7-oauth-google")
    expect(sphere.style.getPropertyValue("--as7-oauth-brand")).toBe(
      GOOGLE.brandColor,
    )
    expect(sphere.style.getPropertyValue("--as7-oauth-halo")).toBe(
      GOOGLE.haloColor,
    )
  })
})
