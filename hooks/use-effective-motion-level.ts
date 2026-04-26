"use client"

/**
 * BS.3.5 — Integrated resolver for the effective motion level.
 *
 * Layers the four signals BS.3.1..BS.3.4 surface, in priority order:
 *
 *   1. `prefers-reduced-motion: reduce` (OS / WCAG 2.3.3 R25.2)
 *   2. user pref `motion: off` (the user explicitly turned it off)
 *   3. battery rule (BS.3.4 `applyBatteryRule` via `useBatteryAwareMotion`)
 *   4. user preference (BS.3.3 `getMotionPreference`)
 *
 * Replaces the BS.3.2 placeholder in `hooks/use-zero-g.ts` without
 * changing its public signature: every motion hook still calls
 * `useEffectiveMotionLevel(): MotionLevel` and gets the same shape
 * back. Lifting the resolver out of `use-zero-g.ts` also collapses
 * the temporary BS.3.2 / BS.3.3 dual `MotionLevel` definitions —
 * `use-zero-g.ts` now imports the type from `@/lib/motion-preferences`,
 * which is the SoT.
 *
 * **Resolver semantics**
 *
 * Step 1 is a hard short-circuit: if the OS reports
 * `prefers-reduced-motion: reduce`, we return `"off"` regardless of
 * any other signal. This is the R25.2 last-line-of-defence for
 * vestibular / migraine-affected users; nothing — not the user's
 * own preference, not the battery override — gets to override the
 * accessibility flag. (The battery rule's force-full override
 * exists for a different population: a user demoing on low
 * battery. That override does NOT touch the OS flag.)
 *
 * Steps 2..4 collapse into a single call: `useBatteryAwareMotion`
 * already handles "user pref is off → return off" (because
 * `applyBatteryRule(off, ...) === off` for every battery state —
 * `demoteOne(off) === off`, `clampAtMost(off, "subtle") === off`,
 * the `critical` branch returns `"off"` outright). So the chain
 * `motion: off > 電池規則 > user pref` is automatic given BS.3.4's
 * pure rule.
 *
 * **Module-global state audit** (per implement_phase_step.md Step 1):
 *
 *   - No module-level mutable state. All state is per-hook
 *     (`useState` / `useRef` / `useEffect`).
 *   - The `subscribeMotionPreference` event-bus added in BS.3.3 is
 *     a `window` event listener — per-tab, not cross-process. It
 *     lets a `setMotionPreference()` call in one tab refresh the
 *     hooks in the same tab without a full remount; cross-tab
 *     propagation is out of scope (server-of-record handles that
 *     on the next mount).
 *   - Browser-only hook; the uvicorn `--workers N` model doesn't
 *     apply (answer #1: no shared state to synchronise).
 *
 * **Read-after-write timing audit**: N/A — pure browser DOM API +
 * React state, no cross-process / cross-request ordering.
 *
 * **R25.1 mitigation**: the `MediaQueryList` is subscribed via
 * `addEventListener('change', ...)` so toggling
 * `prefers-reduced-motion` at the OS level after the page loads
 * propagates immediately. Cleanup removes the listener on unmount.
 */

import { useEffect, useState } from "react"

import { useBatteryAwareMotion } from "@/lib/battery-aware-motion"
import {
  DEFAULT_MOTION_LEVEL,
  getMotionPreference,
  type MotionLevel,
  subscribeMotionPreference,
} from "@/lib/motion-preferences"

// ─────────────────────────────────────────────────────────────────────
// usePrefersReducedMotion — OS-level a11y signal (R25.1 + R25.2)
// ─────────────────────────────────────────────────────────────────────

/**
 * Live-tracking wrapper around the
 * `(prefers-reduced-motion: reduce)` media query. Returns `false`
 * during SSR and on browsers without `matchMedia`, then re-renders
 * whenever the OS flag toggles. Subscribing on `'change'` is the
 * R25.1 mitigation — without it the hook only ever reads the
 * first value and goes stale when the user flips the OS toggle
 * mid-session.
 *
 * Exported so the BS.3.6 Display Settings page can render the OS
 * flag as a read-only indicator next to the user's preference.
 */
export function usePrefersReducedMotion(): boolean {
  const [reduced, setReduced] = useState<boolean>(false)

  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
      return
    }
    const mql = window.matchMedia("(prefers-reduced-motion: reduce)")
    const apply = () => setReduced(mql.matches)
    apply()
    if (typeof mql.addEventListener === "function") {
      mql.addEventListener("change", apply)
      return () => mql.removeEventListener("change", apply)
    }
    // Older Safari only ships the `addListener` deprecated API.
    const legacy = mql as unknown as {
      addListener?: (cb: () => void) => void
      removeListener?: (cb: () => void) => void
    }
    legacy.addListener?.(apply)
    return () => legacy.removeListener?.(apply)
  }, [])

  return reduced
}

// ─────────────────────────────────────────────────────────────────────
// useUserMotionPreference — async-loaded persisted preference
// ─────────────────────────────────────────────────────────────────────

/**
 * Loads the user's persisted motion preference once on mount and
 * keeps it in sync with same-tab `setMotionPreference()` writes via
 * the BS.3.3 event bus. Until the first fetch resolves the hook
 * returns `DEFAULT_MOTION_LEVEL` (dramatic), matching the BS.3.3
 * fallback contract — any motion that briefly renders before the
 * preference loads is the documented default, not a flicker bug.
 *
 * The `cancelled` flag guards against the classic stale-resolution
 * race (component unmounts mid-fetch, then the fetch resolves and
 * tries to setState on a dead component).
 */
function useUserMotionPreference(): MotionLevel {
  const [pref, setPref] = useState<MotionLevel>(DEFAULT_MOTION_LEVEL)

  useEffect(() => {
    let cancelled = false

    void getMotionPreference()
      .then((value) => {
        if (!cancelled) setPref(value)
      })
      .catch(() => {
        // `getMotionPreference` already swallows 404 / network errors
        // and falls back to the default, so a rejection here is rare
        // (e.g. an unexpected throw from `lib/api.ts`). Stay on the
        // default — UI is still functional, just at the documented
        // fallback level.
      })

    const unsubscribe = subscribeMotionPreference((next) => {
      if (!cancelled) setPref(next)
    })

    return () => {
      cancelled = true
      unsubscribe()
    }
  }, [])

  return pref
}

// ─────────────────────────────────────────────────────────────────────
// useEffectiveMotionLevel — the integrated resolver (BS.3.5 contract)
// ─────────────────────────────────────────────────────────────────────

/**
 * Returns the motion level that consumers should actually animate
 * at. Composes the three signals above plus the BS.3.4 battery
 * rule into a single `MotionLevel`, applying the priority order
 * documented at the top of this file.
 *
 * Hook ordering note — every code path calls every nested hook
 * unconditionally so React's rules-of-hooks invariants hold across
 * renders even when the OS reduce-motion flag toggles or the user
 * pref reloads. The OS short-circuit happens in the *return*, not
 * by skipping hook calls.
 */
export function useEffectiveMotionLevel(): MotionLevel {
  const reducedMotion = usePrefersReducedMotion()
  const userPref = useUserMotionPreference()
  const { effective: batteryAdjusted } = useBatteryAwareMotion(userPref)

  if (reducedMotion) return "off"
  return batteryAdjusted
}
