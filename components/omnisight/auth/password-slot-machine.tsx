"use client"

/**
 * AS.7.2 — Password slot-machine animation.
 *
 * Renders the per-column slot-reel for the auto-generated password.
 * The reducer in `lib/auth/password-slot-machine.ts` is pure; this
 * leaf only owns the rAF loop driving `tickSlotMachine` + the DOM
 * for each column.
 *
 * Behaviour:
 *
 *   - When `target` becomes a fresh non-empty string, restart the
 *     animation. Internally we key off `target + animationKey` so
 *     the parent can request a re-roll on the same target by
 *     bumping `animationKey` (e.g. when toggling style and the
 *     generator happens to produce the same string twice).
 *   - The cycle phase scrolls glyphs at ~24 fps; the collapse phase
 *     locks columns left-to-right with a 30 ms stagger; columns
 *     scale-flash for 180 ms when they lock.
 *   - At off / subtle motion levels the animation is **bypassed
 *     entirely** — the columns render their final glyphs straight
 *     away, no rAF loop runs, no scale flash plays. This honours
 *     the BS.3 motion-budget contract.
 *
 * Module-global state audit: leaf React state only (`useState` /
 * `useRef`). The reducer is pure; rAF callback is per-instance.
 * No module-level mutable container. Per-tab determinism is
 * guaranteed by the `_pickGlyph(frame, column)` deterministic
 * hash inside the reducer.
 *
 * Read-after-write timing audit: N/A — no async DB / network work.
 */

import { useEffect, useRef, useState } from "react"

import { getAuthVisualBudget } from "@/lib/auth-visual/motion-policy"
import type { MotionLevel } from "@/lib/motion-preferences"
import {
  SLOT_IDLE_STATE,
  SLOT_LOCK_FLASH_MS,
  SLOT_MAX_ANIMATED_COLUMNS,
  type SlotMachineState,
  startSlotMachine,
  tickSlotMachine,
} from "@/lib/auth/password-slot-machine"

interface PasswordSlotMachineProps {
  level: MotionLevel
  /** The pre-generated final password string. The reel will
   *  collapse onto this. */
  target: string
  /** Bump to request a re-animation on the same `target`. */
  animationKey?: number
  /** Fires once the slot machine reaches `settled` so the parent
   *  can flash the "saved to clipboard" toast or fire the keychain
   *  prompt. Will NOT fire at off / subtle (those bypass the
   *  animation; the parent already has the value). */
  onSettled?: (target: string) => void
  /** Optional class for the outer wrapper — used by the test to
   *  scope queries. */
  className?: string
}

export function PasswordSlotMachine({
  level,
  target,
  animationKey = 0,
  onSettled,
  className,
}: PasswordSlotMachineProps) {
  const budget = getAuthVisualBudget(level)
  const animateOn = budget.travelingLight  // normal / dramatic only

  const [state, setState] = useState<SlotMachineState>(SLOT_IDLE_STATE)
  const stateRef = useRef<SlotMachineState>(SLOT_IDLE_STATE)
  const rafRef = useRef<number | null>(null)
  const lastTimestampRef = useRef<number>(0)
  const settledFiredRef = useRef<boolean>(false)

  // Sync the ref with state so the rAF callback can read the
  // current snapshot without retriggering effects.
  stateRef.current = state

  // Restart animation whenever target / animationKey changes.
  useEffect(() => {
    if (rafRef.current !== null) {
      cancelAnimationFrame(rafRef.current)
      rafRef.current = null
    }
    settledFiredRef.current = false

    if (!target) {
      setState(SLOT_IDLE_STATE)
      return
    }

    if (!animateOn) {
      // BS.3 motion budget: skip the animation, jump straight to
      // settled. Lock columns to the final glyphs.
      const settled: SlotMachineState = Object.freeze({
        phase: "settled",
        target,
        columns: Object.freeze(target.split("")),
        locked: Object.freeze(new Array<boolean>(target.length).fill(true)),
        flashing: Object.freeze(new Array<boolean>(target.length).fill(false)),
        tickMs: 0,
        cycleFrame: 0,
      })
      setState(settled)
      // Don't fire onSettled at off / subtle — the parent already
      // owns `target`, no animation completion event to surface.
      return
    }

    setState(startSlotMachine(target))
    lastTimestampRef.current = 0

    const step = (ts: number) => {
      if (lastTimestampRef.current === 0) {
        lastTimestampRef.current = ts
      }
      const deltaMs = ts - lastTimestampRef.current
      lastTimestampRef.current = ts
      const next = tickSlotMachine({ state: stateRef.current, deltaMs })
      stateRef.current = next
      setState(next)
      if (next.phase === "settled") {
        if (!settledFiredRef.current) {
          settledFiredRef.current = true
          onSettled?.(next.target)
        }
        rafRef.current = null
        return
      }
      rafRef.current = requestAnimationFrame(step)
    }
    rafRef.current = requestAnimationFrame(step)

    return () => {
      if (rafRef.current !== null) {
        cancelAnimationFrame(rafRef.current)
        rafRef.current = null
      }
    }
    // animateOn flips on motion-level changes; recompute on it too.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target, animationKey, animateOn])

  if (!target) {
    return (
      <div
        data-testid="as7-password-slot-machine"
        data-as7-slot-phase="idle"
        className={["as7-slot-machine", className].filter(Boolean).join(" ")}
        aria-hidden="true"
      />
    )
  }

  return (
    <div
      data-testid="as7-password-slot-machine"
      data-as7-slot-phase={state.phase}
      data-as7-animate={animateOn ? "on" : "off"}
      className={["as7-slot-machine", className].filter(Boolean).join(" ")}
      aria-label="Generated password preview"
    >
      {state.columns.map((glyph, i) => {
        const isLocked = state.locked[i] ?? false
        const isFlashing = state.flashing[i] ?? false
        const isAnimated = i < SLOT_MAX_ANIMATED_COLUMNS
        return (
          <span
            key={i}
            data-testid={`as7-slot-col-${i}`}
            data-as7-slot-locked={isLocked ? "yes" : "no"}
            data-as7-slot-flash={isFlashing ? "on" : "off"}
            data-as7-slot-animated={isAnimated ? "yes" : "no"}
            className="as7-slot-col"
            style={{
              animationDuration: `${SLOT_LOCK_FLASH_MS}ms`,
            }}
          >
            {glyph}
          </span>
        )
      })}
    </div>
  )
}
