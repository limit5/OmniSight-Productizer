"use client"

/**
 * AS.7.1 — Warp drive transition overlay.
 *
 * Renders a fullscreen warp-drive "stretch" effect after a
 * successful login, then runs a callback so the page can
 * `router.replace(next)`. The animation is purely cosmetic and
 * doesn't gate the navigation if motion is at off / subtle —
 * those levels skip the overlay entirely and the callback fires
 * synchronously on the next microtask.
 *
 * Visual:
 *   - Concentric expanding rings + center bloom (CSS keyframes)
 *   - Star streak overlay (CSS-only, repeating-linear-gradient)
 *   - 800ms total duration at dramatic / 500ms at normal
 *
 * Module-global state audit: leaf React state only (`useEffect`
 * timer to fire the callback). No module-level mutable container.
 */

import { useEffect } from "react"

import { getAuthVisualBudget } from "@/lib/auth-visual/motion-policy"
import type { MotionLevel } from "@/lib/motion-preferences"

interface WarpDriveTransitionProps {
  level: MotionLevel
  /** Whether the overlay is currently active. */
  active: boolean
  /** Fires once the warp animation completes. The page should
   *  navigate to the post-login destination from this callback. */
  onComplete: () => void
}

export const WARP_DURATION_BY_LEVEL: Readonly<Record<MotionLevel, number>> =
  Object.freeze({
    off: 0,
    subtle: 0,
    normal: 500,
    dramatic: 800,
  })

export function WarpDriveTransition({
  level,
  active,
  onComplete,
}: WarpDriveTransitionProps) {
  const budget = getAuthVisualBudget(level)
  const duration = WARP_DURATION_BY_LEVEL[level]
  // The warp is gated to motion levels normal/dramatic via the
  // budget's `renderShader` proxy — same population that gets the
  // nebula gets the warp.
  const renderWarp = active && budget.renderShader && duration > 0

  useEffect(() => {
    if (!active) return
    if (duration === 0) {
      // Skip the animation; fire the navigation callback on the
      // next microtask so the parent's `setActive(true) + replace()`
      // ordering still gives React a chance to flush.
      const id = window.setTimeout(onComplete, 0)
      return () => window.clearTimeout(id)
    }
    const id = window.setTimeout(onComplete, duration)
    return () => window.clearTimeout(id)
  }, [active, duration, onComplete])

  if (!renderWarp) return null

  return (
    <div
      data-testid="as7-warp-drive"
      data-as7-warp="active"
      aria-hidden="true"
      className="as7-warp-drive"
      style={{ "--as7-warp-duration": `${duration}ms` } as React.CSSProperties}
    >
      <div className="as7-warp-rings" />
      <div className="as7-warp-streaks" />
      <div className="as7-warp-bloom" />
    </div>
  )
}
