"use client"

/**
 * AS.7.8 — Onboarding celebration burst.
 *
 * Renders the terminal "you're all set" reward at the end of the
 * `/onboarding` wizard:
 *
 *   - 30 brand-tinted particles erupt outward from the centre. Each
 *     particle's angle / distance / hue / delay is computed by the
 *     deterministic golden-ratio hash in
 *     `lib/auth/onboarding-helpers::buildCelebrationParticles` so the
 *     layout is byte-identical between SSR / vitest / browser.
 *   - "Welcome aboard, X!" wordmark rises beneath the centre with a
 *     translateY + scale-in keyframe.
 *   - Once the burst settles the leaf fires `onComplete()` so the
 *     parent page can `router.replace("/")`.
 *
 * Two motion levels:
 *   - off / subtle — the burst is skipped; the wordmark renders flat
 *     and `onComplete` fires on the next microtask so navigation
 *     happens immediately.
 *   - normal      — 1500 ms total burst (12 inner ring px-ish travel)
 *   - dramatic    — 2400 ms total burst (longer travel + more colours)
 *
 * Mirrors the AS.7.4 `<MfaPassedCheck>` and AS.7.1
 * `<WarpDriveTransition>` shape (active prop + setTimeout +
 * onComplete) so the parent page composition is consistent.
 *
 * Module-global state audit (per implement_phase_step.md SOP §1):
 *   - Leaf React state only — `useEffect` for the navigation timer.
 *     Particle layout comes from a pure helper called at render
 *     time. No module-level mutable container.
 */

import { useEffect, useMemo } from "react"

import {
  buildCelebrationParticles,
  CELEBRATION_DURATION_BY_LEVEL,
  formatWelcomeAboard,
  type CelebrationMotionLevel,
} from "@/lib/auth/onboarding-helpers"

interface OnboardingCelebrationBurstProps {
  level: CelebrationMotionLevel
  /** Whether the burst is currently active. The burst stays mounted
   *  but inactive until the parent flips this so the parent can pre-
   *  layout the DOM (avoiding a single-frame burst flash). */
  active: boolean
  /** Resolved display name for the "Welcome aboard, X!" copy. Empty
   *  string / null falls back to the bare phrase. */
  displayName: string | null
  /** Fires once the burst animation completes. */
  onComplete: () => void
}

export { CELEBRATION_DURATION_BY_LEVEL }

export function OnboardingCelebrationBurst({
  level,
  active,
  displayName,
  onComplete,
}: OnboardingCelebrationBurstProps) {
  const duration = CELEBRATION_DURATION_BY_LEVEL[level]
  const particles = useMemo(
    () => buildCelebrationParticles(level),
    [level],
  )
  const welcome = formatWelcomeAboard(displayName)

  useEffect(() => {
    if (!active) return
    if (duration === 0) {
      // off / subtle — fire on next microtask so the parent's
      // setActive(true) + router.replace() ordering still flushes.
      const id = window.setTimeout(onComplete, 0)
      return () => window.clearTimeout(id)
    }
    const id = window.setTimeout(onComplete, duration)
    return () => window.clearTimeout(id)
  }, [active, duration, onComplete])

  if (!active) return null

  return (
    <div
      data-testid="as7-burst-stage"
      data-as7-burst-active="yes"
      data-as7-burst-level={level}
      role="status"
      aria-live="polite"
      aria-label={welcome}
      className="as7-burst-stage"
    >
      {particles.length > 0 ? (
        <div
          data-testid="as7-burst-particles"
          aria-hidden="true"
          className="as7-burst-particles"
        >
          {particles.map((p) => (
            <span
              key={p.index}
              data-testid={`as7-burst-particle-${p.index}`}
              data-as7-burst-particle-index={p.index}
              className="as7-burst-particle"
              style={
                {
                  "--as7-burst-x": `${p.xPx}px`,
                  "--as7-burst-y": `${p.yPx}px`,
                  "--as7-burst-delay": `${p.delayMs}ms`,
                  "--as7-burst-duration": `${p.durationMs}ms`,
                  "--as7-burst-hue": p.hue,
                } as React.CSSProperties
              }
            />
          ))}
        </div>
      ) : null}
      <div
        data-testid="as7-burst-welcome"
        data-as7-burst-welcome-level={level}
        className="as7-burst-welcome"
      >
        <span className="as7-burst-welcome-text">{welcome}</span>
      </div>
    </div>
  )
}
