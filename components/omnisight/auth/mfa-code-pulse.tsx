"use client"

/**
 * AS.7.4 — 6-digit pulse animation.
 *
 * Renders the TOTP code input as a row of 6 cells. Each cell
 * surfaces three states via `data-as7-mfa-cell`:
 *
 *   - `empty`   → cell is awaiting input (dim border)
 *   - `filled`  → cell holds a digit (bright border, foreground
 *                 colour text)
 *   - `pulse`   → cell just received a fresh digit (brand-purple
 *                 glow + scale 1 → 1.18 → 1 keyframe replays once
 *                 per digit insertion)
 *
 * The pulse replay is owned by the parent — it bumps `pulseKey`
 * via `bumpPulseKey(prev)` and the leaf re-mounts the pulsing
 * cell via React `key={`${i}-${pulseKey}-${value[i] ?? "_"}`}` so
 * the keyframe restarts. Off / subtle motion levels strip the
 * pulse (the gating cascade lives in `styles/auth-visual.css`).
 *
 * Module-global state audit: pure presentation. The leaf only
 * reads the props.
 */

import { type CSSProperties } from "react"

import { TOTP_CODE_LENGTH } from "@/lib/auth/mfa-challenge-helpers"
import { getAuthVisualBudget } from "@/lib/auth-visual/motion-policy"
import type { MotionLevel } from "@/lib/motion-preferences"

interface MfaCodePulseProps {
  level: MotionLevel
  value: string
  /** Optional cell count override. Defaults to TOTP's 6. */
  length?: number
  /** Replay key for the per-cell pulse animation. Bumping this
   *  number re-mounts each cell so the keyframe restarts. */
  pulseKey?: number
  /** When the page transitions into the passed-check overlay it
   *  flips this prop on so every cell renders in the success-tinted
   *  state simultaneously. */
  passed?: boolean
  className?: string
  style?: CSSProperties
}

export function MfaCodePulse({
  level,
  value,
  length = TOTP_CODE_LENGTH,
  pulseKey = 0,
  passed = false,
  className,
  style,
}: MfaCodePulseProps) {
  const budget = getAuthVisualBudget(level)
  // The pulse keyframe is gated to motion levels with travelingLight
  // (normal / dramatic). subtle / off render the cells statically —
  // the colour change alone communicates progress.
  const pulseOn = budget.travelingLight ? "on" : "off"
  const cells = Array.from({ length }, (_, i) => value[i] ?? "")

  return (
    <div
      data-testid="as7-mfa-code-pulse"
      data-as7-mfa-pulse={pulseOn}
      data-as7-mfa-passed={passed ? "yes" : "no"}
      aria-hidden="true"
      className={["as7-mfa-pulse", className].filter(Boolean).join(" ")}
      style={style}
    >
      {cells.map((char, i) => {
        const filled = char.length > 0
        // The cell's React key incorporates the pulseKey + the char
        // itself so a digit-replace re-mounts the cell, restarting
        // the keyframe.
        const cellKey = `${i}-${pulseKey}-${char || "_"}`
        return (
          <span
            key={cellKey}
            data-testid={`as7-mfa-cell-${i}`}
            data-as7-mfa-cell={
              passed ? "passed" : filled ? "filled" : "empty"
            }
            className="as7-mfa-cell"
          >
            {filled ? char : ""}
          </span>
        )
      })}
    </div>
  )
}
