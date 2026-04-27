"use client"

/**
 * AS.7.0 — OmniSight brand wordmark + traveling light + bloom.
 *
 * Renders the OmniSight wordmark with three optional motion
 * effects gated on the AS.7.0 motion budget:
 *
 *   - **Traveling light** (`travelingLight: true`) — a 4s linear
 *     gradient sweep across the letters. Handled entirely in CSS
 *     via the `[data-as7-traveling-light="on"]` selector.
 *   - **Breathing pulse** (`breathingPulse: true`) — slow 4s
 *     ease-in-out text-shadow oscillation.
 *   - **Bloom on input focus** — the `bloomKey` prop is a
 *     replay-key. Pages bump it (e.g. via a `useState` counter)
 *     when an input field gains focus, which re-mounts the bloom
 *     animation by changing the React key.
 *
 * The text is rendered as plain children rather than a hard-
 * coded "OMNISIGHT" string so the AS.7.x signup / settings pages
 * can swap the brand label without touching the foundation.
 */

import { useEffect, useState } from "react"

import { getAuthVisualBudget } from "@/lib/auth-visual/motion-policy"
import type { MotionLevel } from "@/lib/motion-preferences"

interface AuthBrandWordmarkProps {
  level: MotionLevel
  /** Wordmark text. Default `"OmniSight"`. */
  label?: string
  /** Replay-key for the bloom animation. Bumping this number
   *  triggers a one-shot bloom (used by pages on input focus). */
  bloomKey?: number
  className?: string
}

const DEFAULT_BLOOM_DURATION_MS = 600

export function AuthBrandWordmark({
  level,
  label = "OmniSight",
  bloomKey = 0,
  className,
}: AuthBrandWordmarkProps) {
  const budget = getAuthVisualBudget(level)
  const travelingLight = budget.travelingLight ? "on" : "off"
  const breathe = budget.breathingPulse ? "on" : "off"

  // Bloom is a one-shot animation; we toggle the data attribute
  // on for the duration then off so it can replay on the next
  // bump of `bloomKey`.
  const [bloomOn, setBloomOn] = useState(false)
  useEffect(() => {
    if (bloomKey <= 0) return
    if (!budget.breathingPulse && !budget.travelingLight) return
    setBloomOn(true)
    const timer = window.setTimeout(() => setBloomOn(false), DEFAULT_BLOOM_DURATION_MS)
    return () => window.clearTimeout(timer)
  }, [bloomKey, budget.breathingPulse, budget.travelingLight])

  return (
    <span
      data-testid="as7-wordmark"
      data-as7-breathe={breathe}
      data-as7-traveling-light={travelingLight}
      data-as7-bloom={bloomOn ? "on" : "off"}
      className={["as7-wordmark", className].filter(Boolean).join(" ")}
    >
      {label}
    </span>
  )
}

export const AUTH_BRAND_BLOOM_DURATION_MS = DEFAULT_BLOOM_DURATION_MS
