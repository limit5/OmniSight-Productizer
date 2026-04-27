"use client"

/**
 * AS.7.1 — OAuth provider energy sphere button.
 *
 * Renders one round 56×56 provider button with a brand-color halo.
 * The 5-primary buttons appear in a row below the password field;
 * the secondary 6 vendors live behind a "More" expand toggle the
 * page renders with the same primitive at a smaller size.
 *
 * Visual spec (AS.7.1 row):
 *   - Round shape, brand color flat fill, brand-color halo at
 *     8px..24px radius driven by CSS keyframes when motion budget
 *     allows it.
 *   - Hover: halo intensifies (CSS-only, `:hover` selector).
 *   - Click: navigates via `<a href>` so the backend can set its
 *     in-flight `omnisight_oauth_flow` HttpOnly cookie on the 302
 *     redirect (fetch() with redirect:follow strips set-cookie, so
 *     a hard nav is required here).
 *
 * Provider icons are rendered as inline SVG; the page passes the
 * SVG element via the `icon` prop so this primitive stays free of
 * a giant icon catalog. The brand color drives both the halo and
 * the SVG fill via a CSS variable on the element.
 *
 * Module-global state audit: pure presentation component, no state.
 */

import type { ReactNode } from "react"

import { getAuthVisualBudget } from "@/lib/auth-visual/motion-policy"
import type { MotionLevel } from "@/lib/motion-preferences"
import type { OAuthProviderInfo } from "@/lib/auth/oauth-providers"

interface OAuthEnergySphereProps {
  level: MotionLevel
  provider: OAuthProviderInfo
  href: string
  icon: ReactNode
  /** "primary" renders 56×56 with full halo; "secondary" 40×40 with
   *  a quieter halo for the More dropdown. */
  size?: "primary" | "secondary"
  /** Disable the button (e.g. while the page is mid-submit). */
  disabled?: boolean
}

export function OAuthEnergySphere({
  level,
  provider,
  href,
  icon,
  size = "primary",
  disabled = false,
}: OAuthEnergySphereProps) {
  const budget = getAuthVisualBudget(level)
  // Halo pulse is only enabled when the motion budget allows the
  // glass-card's neon flicker — same gate, since the energy sphere
  // is rendered against the glass card and we want their animation
  // gating to stay coherent.
  const haloAnim = budget.glowFlicker ? "on" : "off"

  const style = {
    "--as7-oauth-brand": provider.brandColor,
    "--as7-oauth-halo": provider.haloColor,
  } as React.CSSProperties

  if (disabled) {
    return (
      <span
        data-testid={`as7-oauth-${provider.id}`}
        data-as7-tier={size}
        data-as7-disabled="on"
        aria-disabled="true"
        aria-label={provider.displayName}
        className="as7-oauth-sphere"
        style={style}
      >
        <span className="as7-oauth-sphere-inner" aria-hidden="true">
          {icon}
        </span>
      </span>
    )
  }

  return (
    <a
      data-testid={`as7-oauth-${provider.id}`}
      data-as7-tier={size}
      data-as7-halo={haloAnim}
      href={href}
      aria-label={`Sign in with ${provider.displayName}`}
      title={`Sign in with ${provider.displayName}`}
      className="as7-oauth-sphere"
      style={style}
    >
      <span className="as7-oauth-sphere-halo" aria-hidden="true" />
      <span className="as7-oauth-sphere-inner" aria-hidden="true">
        {icon}
      </span>
    </a>
  )
}
