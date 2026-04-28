"use client"

/**
 * AS.7.4 — ✓ passed animation overlay.
 *
 * Renders a fullscreen check overlay between the MFA submission
 * resolving truthy and the post-login navigation. The check itself
 * is a CSS keyframe (scale 0 → 1.2 → 1 with stroke-dasharray draw).
 *
 * Two motion levels:
 *   - off / subtle — the overlay is skipped entirely; the parent's
 *     onComplete callback fires on the next microtask so navigation
 *     is immediate.
 *   - normal       — 600ms total
 *   - dramatic     — 900ms total + ring expansion
 *
 * Mirrors the AS.7.1 `<WarpDriveTransition>` shape (fullscreen +
 * setTimeout + onComplete) so the page-side composition is the
 * same: trigger the overlay on success, the overlay calls back
 * when its keyframe finishes, the page does the navigation.
 *
 * Module-global state audit: leaf React state only (`useEffect`
 * timer to fire the callback).
 */

import { useEffect } from "react"

import { getAuthVisualBudget } from "@/lib/auth-visual/motion-policy"
import type { MotionLevel } from "@/lib/motion-preferences"

interface MfaPassedCheckProps {
  level: MotionLevel
  /** Whether the overlay is currently active. */
  active: boolean
  /** Fires once the check animation completes. */
  onComplete: () => void
}

export const MFA_PASSED_DURATION_BY_LEVEL: Readonly<
  Record<MotionLevel, number>
> = Object.freeze({
  off: 0,
  subtle: 0,
  normal: 600,
  dramatic: 900,
})

export function MfaPassedCheck({
  level,
  active,
  onComplete,
}: MfaPassedCheckProps) {
  const budget = getAuthVisualBudget(level)
  const duration = MFA_PASSED_DURATION_BY_LEVEL[level]
  // The animation is gated to motion levels normal / dramatic via
  // the budget's `travelingLight` proxy — same population that gets
  // the warp-drive transition gets the passed check. off / subtle
  // skip the overlay entirely.
  const renderCheck = active && budget.travelingLight && duration > 0

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

  if (!renderCheck) return null

  return (
    <div
      data-testid="as7-mfa-passed-check"
      data-as7-mfa-passed-active="yes"
      role="status"
      aria-live="polite"
      aria-label="Two-factor verification passed"
      className="as7-mfa-passed"
      style={
        {
          "--as7-mfa-passed-duration": `${duration}ms`,
        } as React.CSSProperties
      }
    >
      <div className="as7-mfa-passed-ring" aria-hidden="true" />
      <svg
        className="as7-mfa-passed-svg"
        viewBox="0 0 64 64"
        aria-hidden="true"
      >
        <circle
          cx="32"
          cy="32"
          r="28"
          className="as7-mfa-passed-circle"
          fill="none"
          stroke="currentColor"
          strokeWidth="3"
        />
        <path
          d="M18 33 L28 43 L46 23"
          className="as7-mfa-passed-stroke"
          fill="none"
          stroke="currentColor"
          strokeWidth="4"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    </div>
  )
}
