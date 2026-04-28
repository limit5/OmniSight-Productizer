"use client"

/**
 * AS.7.7 — OAuth orbit satellites primitive.
 *
 * Pure visual layout: an avatar bubble at the center plus two
 * concentric rings of provider satellites — the inner ring carries
 * the linked providers, the outer ring carries the available ones.
 * The satellites themselves are reused `<OAuthEnergySphere>`
 * primitives so the brand-color halo + iconography stay byte-equal
 * to the AS.7.1 sign-in surface.
 *
 * Visual spec (AS.7.7 row):
 *   - Round 320×320px stage, avatar at center.
 *   - Inner ring radius 110px (linked providers).
 *   - Outer ring radius 165px (available providers).
 *   - `dramatic` motion level animates a slow CCW rotation
 *     (32s for the inner, 48s for the outer — the offset means
 *     the rings counter-rotate visually).
 *   - `normal` keeps the layout but disables the rotation.
 *   - `subtle` / `off` collapse the orbit into a flat grid.
 *
 * Module-global state audit (per implement_phase_step.md SOP §1):
 *   - Pure presentation; layout is a pure function of the props.
 *   - The orbit math is the helper module's
 *     `oauthOrbitState()` — see file-level docstring.
 */

import type { ReactNode } from "react"

import { getAuthVisualBudget } from "@/lib/auth-visual/motion-policy"
import type { MotionLevel } from "@/lib/motion-preferences"
import {
  oauthOrbitState,
  ORBIT_RADIUS_BY_TIER,
  type LinkedOAuthIdentity,
  type OrbitSatellitePosition,
} from "@/lib/auth/profile-helpers"
import type { OAuthProviderId } from "@/lib/auth/oauth-providers"

interface OAuthOrbitSatellitesProps {
  level: MotionLevel
  linked: readonly LinkedOAuthIdentity[]
  /** Renders the provider's icon. Returning the cached AS.7.1
   *  `<OAuthProviderIcon />` element here keeps the orbit
   *  primitive icon-agnostic. */
  renderIcon: (id: OAuthProviderId) => ReactNode
  /** Optional avatar element. The page can pass an `<img>` or any
   *  ReactNode; defaults to a generic glyph. */
  avatar?: ReactNode
  /** When the user clicks an inner-ring (linked) satellite, the
   *  page is responsible for handling the disconnect gesture. */
  onDisconnect?: (id: OAuthProviderId) => void
  /** When the user clicks an outer-ring (available) satellite,
   *  the page is responsible for handling the connect gesture. */
  onConnect?: (id: OAuthProviderId) => void
  /** Disable every satellite (e.g. while the page is mid-submit). */
  disabled?: boolean
}

function Satellite({
  position,
  level,
  renderIcon,
  onDisconnect,
  onConnect,
  disabled,
}: {
  position: OrbitSatellitePosition
  level: MotionLevel
  renderIcon: OAuthOrbitSatellitesProps["renderIcon"]
  onDisconnect: OAuthOrbitSatellitesProps["onDisconnect"]
  onConnect: OAuthOrbitSatellitesProps["onConnect"]
  disabled: boolean
}) {
  const provider = position.provider
  const handler = position.isLinked ? onDisconnect : onConnect
  const callable = !disabled && Boolean(handler)
  const budget = getAuthVisualBudget(level)
  const haloAnim = budget.glowFlicker ? "on" : "off"
  const style = {
    "--as7-oauth-brand": provider.brandColor,
    "--as7-oauth-halo": provider.haloColor,
    "--as7-orbit-x": `${position.xPx}px`,
    "--as7-orbit-y": `${position.yPx}px`,
  } as React.CSSProperties

  const sharedClass =
    "as7-orbit-satellite as7-oauth-sphere" +
    (position.ring === "inner" ? " as7-orbit-satellite-inner" : "")
  const tierAttr = position.ring === "inner" ? "primary" : "secondary"

  if (!callable) {
    return (
      <span
        data-testid={`as7-orbit-satellite-${provider.id}`}
        data-as7-orbit-ring={position.ring}
        data-as7-orbit-linked={position.isLinked ? "yes" : "no"}
        data-as7-tier={tierAttr}
        data-as7-halo={haloAnim}
        data-as7-disabled={disabled ? "on" : "off"}
        aria-disabled={disabled ? "true" : undefined}
        aria-label={`${provider.displayName} (${position.isLinked ? "linked" : "available"})`}
        className={sharedClass}
        style={style}
      >
        <span className="as7-oauth-sphere-halo" aria-hidden="true" />
        <span className="as7-oauth-sphere-inner" aria-hidden="true">
          {renderIcon(provider.id)}
        </span>
      </span>
    )
  }

  return (
    <button
      type="button"
      data-testid={`as7-orbit-satellite-${provider.id}`}
      data-as7-orbit-ring={position.ring}
      data-as7-orbit-linked={position.isLinked ? "yes" : "no"}
      data-as7-tier={tierAttr}
      data-as7-halo={haloAnim}
      aria-label={
        position.isLinked
          ? `Disconnect ${provider.displayName}`
          : `Connect ${provider.displayName}`
      }
      title={
        position.isLinked
          ? `Disconnect ${provider.displayName}`
          : `Connect ${provider.displayName}`
      }
      onClick={() => handler && handler(provider.id)}
      className={sharedClass}
      style={style}
    >
      <span className="as7-oauth-sphere-halo" aria-hidden="true" />
      <span className="as7-oauth-sphere-inner" aria-hidden="true">
        {renderIcon(provider.id)}
      </span>
    </button>
  )
}

export function OAuthOrbitSatellites({
  level,
  linked,
  renderIcon,
  avatar,
  onDisconnect,
  onConnect,
  disabled = false,
}: OAuthOrbitSatellitesProps) {
  const state = oauthOrbitState({ linked })
  const orbitMode =
    level === "off" || level === "subtle"
      ? "flat"
      : level === "dramatic"
      ? "rotating"
      : "static"
  const stageStyle = {
    "--as7-orbit-inner-radius": `${ORBIT_RADIUS_BY_TIER.inner}px`,
    "--as7-orbit-outer-radius": `${ORBIT_RADIUS_BY_TIER.outer}px`,
  } as React.CSSProperties

  return (
    <div
      data-testid="as7-orbit-stage"
      data-as7-orbit-mode={orbitMode}
      data-as7-orbit-linked-count={state.linkedCount}
      data-as7-orbit-available-count={state.availableCount}
      className="as7-orbit-stage"
      style={stageStyle}
    >
      {orbitMode === "flat" ? (
        // Reduced-motion / off fallback: render the satellites as a
        // simple grid below the avatar so the page never depends on
        // the absolute-position layout.
        <div className="as7-orbit-flat" data-testid="as7-orbit-flat">
          <div className="as7-orbit-avatar" aria-hidden="true">
            {avatar ?? (
              <span aria-hidden="true" className="as7-orbit-avatar-glyph">
                ◉
              </span>
            )}
          </div>
          <div className="as7-orbit-flat-grid">
            {[...state.innerRing, ...state.outerRing].map((position) => (
              <Satellite
                key={position.id}
                position={position}
                level={level}
                renderIcon={renderIcon}
                onDisconnect={onDisconnect}
                onConnect={onConnect}
                disabled={disabled}
              />
            ))}
          </div>
        </div>
      ) : (
        <>
          <div
            className="as7-orbit-avatar"
            data-testid="as7-orbit-avatar"
            aria-hidden="true"
          >
            {avatar ?? (
              <span aria-hidden="true" className="as7-orbit-avatar-glyph">
                ◉
              </span>
            )}
          </div>
          <div
            className="as7-orbit-ring as7-orbit-ring-inner"
            data-testid="as7-orbit-ring-inner"
            data-as7-orbit-spin={orbitMode === "rotating" ? "on" : "off"}
            aria-hidden="true"
          />
          <div
            className="as7-orbit-ring as7-orbit-ring-outer"
            data-testid="as7-orbit-ring-outer"
            data-as7-orbit-spin={orbitMode === "rotating" ? "on" : "off"}
            aria-hidden="true"
          />
          {state.innerRing.map((position) => (
            <Satellite
              key={position.id}
              position={position}
              level={level}
              renderIcon={renderIcon}
              onDisconnect={onDisconnect}
              onConnect={onConnect}
              disabled={disabled}
            />
          ))}
          {state.outerRing.map((position) => (
            <Satellite
              key={position.id}
              position={position}
              level={level}
              renderIcon={renderIcon}
              onDisconnect={onDisconnect}
              onConnect={onConnect}
              disabled={disabled}
            />
          ))}
        </>
      )}
    </div>
  )
}
