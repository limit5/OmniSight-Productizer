"use client"

/**
 * AS.7.1 — Account-locked frozen overlay.
 *
 * Pure presentation: when the login attempt returns 423 the page
 * mounts this overlay over the glass card. Renders:
 *
 *   - Blue tint (semi-transparent #38bdf8 wash)
 *   - Frozen overlay (CSS-only frosted texture via `backdrop-filter`)
 *   - Chill effect (slow shimmer keyframe at motion levels normal /
 *     dramatic; static at off / subtle)
 *   - A retry-after countdown when the backend included
 *     `Retry-After`. The countdown is driven by the parent (passed
 *     in as `remainingSeconds`) so the overlay stays a leaf
 *     component.
 *
 * Module-global state audit: pure presentation, no state. The
 * countdown decrement is owned by the parent.
 */

import type { CSSProperties } from "react"

import { getAuthVisualBudget } from "@/lib/auth-visual/motion-policy"
import type { MotionLevel } from "@/lib/motion-preferences"

interface AccountLockedOverlayProps {
  level: MotionLevel
  /** Remaining countdown seconds, parent-managed. `null` means the
   *  backend didn't supply Retry-After; the overlay still renders
   *  but no countdown is shown. */
  remainingSeconds: number | null
  /** Optional explicit message override. Defaults to the canonical
   *  copy. */
  message?: string
  className?: string
  style?: CSSProperties
}

export function AccountLockedOverlay({
  level,
  remainingSeconds,
  message,
  className,
  style,
}: AccountLockedOverlayProps) {
  const budget = getAuthVisualBudget(level)
  // Chill shimmer reuses the wordmark breathing-pulse gate — the
  // same population that opted out of subtle animations should also
  // opt out of the frozen-overlay shimmer.
  const chill = budget.breathingPulse ? "on" : "off"

  return (
    <div
      data-testid="as7-account-locked-overlay"
      data-as7-chill={chill}
      role="alert"
      aria-live="assertive"
      className={["as7-account-locked", className].filter(Boolean).join(" ")}
      style={style}
    >
      <div className="as7-account-locked-frost" aria-hidden="true" />
      <div className="as7-account-locked-content">
        <div className="as7-account-locked-icon" aria-hidden="true">❄</div>
        <p className="as7-account-locked-title">Account Locked</p>
        <p className="as7-account-locked-message">
          {message ??
            "Too many failed attempts. This account is temporarily locked."}
        </p>
        {remainingSeconds !== null && remainingSeconds > 0 ? (
          <p
            className="as7-account-locked-countdown"
            data-testid="as7-account-locked-countdown"
            aria-live="polite"
          >
            Retry in {remainingSeconds}s
          </p>
        ) : null}
      </div>
    </div>
  )
}
