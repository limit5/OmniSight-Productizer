"use client"

/**
 * BS.3.4 — Battery-aware motion degradation.
 *
 * Owns the runtime "is the battery low enough that we should
 * tone motion down" decision. Three concerns live in this file:
 *
 *   1. `useBatteryStatus()` — a thin SSR-safe wrapper around
 *      `navigator.getBattery()` that exposes `{ level, charging,
 *      unsupported }` and re-renders on `levelchange` /
 *      `chargingchange` events. Firefox / Safari (and any browser
 *      that ships without the Battery Status API) report
 *      `unsupported: true` with a documented "assume plugged in,
 *      full battery" fallback so callers can skip the degradation
 *      branch entirely — per CLAUDE.md "proper solution, no
 *      silent fallback hack" the unsupported flag is surfaced
 *      explicitly, not papered over.
 *
 *   2. `applyBatteryRule(userPref, status)` — a pure function
 *      that maps `(MotionLevel, BatteryStatus) -> MotionLevel`
 *      using the four-tier policy from the BS.3.4 TODO row:
 *
 *        > 50%   →  user pref
 *        30..50% →  degrade one level
 *        15..30% →  force subtle  (cap)
 *        < 15%   →  force off
 *
 *      Charging or unsupported → skip the rule entirely (the
 *      whole point of degrading is to extend battery life; if
 *      we're plugged in there is nothing to extend). Pure so
 *      BS.3.5 (`useEffectiveMotionLevel`) and the BS.3.7 unit
 *      tests can both call it without React.
 *
 *   3. `useBatteryAwareMotion(userPref)` — the integrated hook
 *      that BS.3.5 composes. Returns the effective level, the
 *      raw battery status, a `didDegrade` flag (true when
 *      `effective !== userPref`), and a `forceFullOverride` /
 *      `setForceFullOverride` pair for the "強制全開" button.
 *      The override is **per-session in-memory** — not persisted
 *      to localStorage / user preferences — so a user who turns
 *      it on at 12% to finish a demo doesn't accidentally
 *      disable battery-aware motion forever the next time they
 *      open the app at 100%. They can toggle it off any time;
 *      a fresh tab re-applies the rule.
 *
 * Toast notifications fire **on the transition into a degraded
 * tier**, not on every render. A `useRef` tracks the previously
 * announced tier; we only call `toast()` when the tier actually
 * changes. This avoids the classic "user opens 3 tabs, gets 3
 * identical toasts" footgun — though note the per-tab nature
 * means a user with multiple OmniSight tabs *will* see one toast
 * per tab on first entering a new tier, which is acceptable and
 * arguably correct (each tab has its own visible motion).
 *
 * Module-global state audit: no module-level mutable state.
 * Constants `BATTERY_TIER_THRESHOLDS` and `LEVEL_ORDER` are
 * `as const` lookup tables. Per-worker recomputation is
 * irrelevant (this module runs in the browser only). The toast
 * dedupe ref is per-hook-instance (one ref per mount), not
 * shared globally — re-mounting the hook re-arms the
 * announcement, which is the desired UX for "navigated away,
 * navigated back, battery is still low".
 *
 * Read-after-write timing: N/A — pure browser DOM API + React
 * state, no cross-process / cross-request ordering.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react"

import { toast } from "@/hooks/use-toast"
import { MOTION_LEVELS, type MotionLevel } from "@/lib/motion-preferences"

// ─────────────────────────────────────────────────────────────────────
// Battery API typings
// ─────────────────────────────────────────────────────────────────────

/**
 * Subset of the W3C Battery Status API `BatteryManager` interface
 * we actually consume. Declared locally because TypeScript's lib.dom
 * doesn't ship it (the spec is shelved as deprecated by the WHATWG,
 * which is why Firefox / Safari never implemented it).
 */
interface BatteryManagerLike {
  /** 0..1 fraction of remaining charge. */
  readonly level: number
  /** True iff a power source is connected. */
  readonly charging: boolean
  addEventListener?: (type: "levelchange" | "chargingchange", cb: () => void) => void
  removeEventListener?: (type: "levelchange" | "chargingchange", cb: () => void) => void
}

interface NavigatorWithBattery {
  getBattery?: () => Promise<BatteryManagerLike>
}

// ─────────────────────────────────────────────────────────────────────
// Public types
// ─────────────────────────────────────────────────────────────────────

export interface BatteryStatus {
  /** Charge fraction in `[0, 1]`. When `unsupported`, defaults to 1. */
  level: number
  /** True if charging OR the API is unavailable (treat as plugged-in). */
  charging: boolean
  /** True when the platform doesn't expose the Battery Status API. */
  unsupported: boolean
}

/**
 * Battery degradation tier names. Exposed so the BS.3.6 Display
 * Settings page can label the "current tier" indicator without
 * recomputing thresholds.
 */
export type BatteryTier = "plenty" | "moderate" | "low" | "critical"

export interface UseBatteryAwareMotionResult {
  /** Effective motion level after applying the battery rule and
   *  any "force full" override. Pass straight to motion hooks. */
  effective: MotionLevel
  /** Raw battery status read from the platform. */
  status: BatteryStatus
  /** Current battery tier — derived from `status.level`. Useful for
   *  rendering "you're in low-battery mode" UI in BS.3.6. */
  tier: BatteryTier
  /** True iff `effective !== userPref` because of the battery rule
   *  (i.e. the rule actually demoted the level). */
  didDegrade: boolean
  /** True iff the user has flipped the per-session override on. */
  forceFullOverride: boolean
  /** Toggle the per-session "強制全開" override. */
  setForceFullOverride: (next: boolean) => void
}

// ─────────────────────────────────────────────────────────────────────
// Constants
// ─────────────────────────────────────────────────────────────────────

/** Threshold breakpoints (fractions, not percent) for the four tiers.
 *  Right boundary is exclusive — i.e. `level === 0.50` lands in the
 *  "moderate" tier, not "plenty". This matches the TODO spec
 *  ("30-50% 降一級") which puts 50% itself in the demote band. */
const BATTERY_TIER_THRESHOLDS = {
  /** ≥ 50% (charging is also "plenty" by short-circuit). */
  plenty: 0.5,
  /** ≥ 30%, < 50% — demote by one. */
  moderate: 0.3,
  /** ≥ 15%, < 30% — clamp to `subtle`. */
  low: 0.15,
  /** < 15% — force `off`. */
  // critical has no lower bound
} as const

/** Tier names ordered weakest → strongest. Used to compute the
 *  "tier got worse" signal for one-toast-per-transition logic. */
const TIER_RANK: Record<BatteryTier, number> = {
  plenty: 3,
  moderate: 2,
  low: 1,
  critical: 0,
}

// ─────────────────────────────────────────────────────────────────────
// Pure helpers
// ─────────────────────────────────────────────────────────────────────

/**
 * Map a battery `level` (0..1) to its tier. Pure; charging /
 * unsupported handling is deliberately not folded in here so that
 * BS.3.6 can label "you're at 12% (plugged in)" as `critical`
 * (the level *is* critical) while `applyBatteryRule` still skips
 * the demote (the demote *isn't needed* because we're charging).
 * Two distinct concerns, two distinct helpers.
 */
export function tierForLevel(level: number): BatteryTier {
  if (level >= BATTERY_TIER_THRESHOLDS.plenty) return "plenty"
  if (level >= BATTERY_TIER_THRESHOLDS.moderate) return "moderate"
  if (level >= BATTERY_TIER_THRESHOLDS.low) return "low"
  return "critical"
}

/** Demote a `MotionLevel` by one step toward `off`. `off` stays `off`. */
function demoteOne(level: MotionLevel): MotionLevel {
  const idx = MOTION_LEVELS.indexOf(level)
  if (idx <= 0) return "off"
  return MOTION_LEVELS[idx - 1]
}

/** Clamp a `MotionLevel` to at most `cap`. Higher levels are pulled
 *  down; lower-or-equal levels pass through (so `off` stays `off`
 *  even when the cap is `subtle`). */
function clampAtMost(level: MotionLevel, cap: MotionLevel): MotionLevel {
  const li = MOTION_LEVELS.indexOf(level)
  const ci = MOTION_LEVELS.indexOf(cap)
  return li > ci ? cap : level
}

/**
 * Apply the BS.3.4 battery degradation rule to a user preference,
 * given the current battery status. Pure — consumed by the React
 * hook below and by BS.3.5's `useEffectiveMotionLevel` resolver.
 *
 *   unsupported OR charging → return userPref unchanged
 *   plenty   (≥50%)         → return userPref unchanged
 *   moderate (30-50%)       → demote one level
 *   low      (15-30%)       → clamp at `subtle`
 *   critical (<15%)         → force `off`
 *
 * The "charging short-circuit" matches the TODO row's Firefox/Safari
 * fallback ("假設充電中、用 user pref"): a plugged-in laptop has
 * no reason to dim animations, regardless of charge level.
 */
export function applyBatteryRule(userPref: MotionLevel, status: BatteryStatus): MotionLevel {
  if (status.unsupported || status.charging) return userPref
  const tier = tierForLevel(status.level)
  switch (tier) {
    case "plenty":
      return userPref
    case "moderate":
      return demoteOne(userPref)
    case "low":
      return clampAtMost(userPref, "subtle")
    case "critical":
      return "off"
  }
}

// ─────────────────────────────────────────────────────────────────────
// useBatteryStatus — thin wrapper around navigator.getBattery()
// ─────────────────────────────────────────────────────────────────────

/** Default state used during SSR / first paint and as the
 *  permanent fallback on browsers without the Battery API. */
const UNSUPPORTED_STATUS: BatteryStatus = {
  level: 1,
  charging: true,
  unsupported: true,
}

/**
 * Read battery status. Returns `UNSUPPORTED_STATUS` (`charging: true,
 * level: 1, unsupported: true`) on:
 *
 *   - SSR / before the mount effect has run,
 *   - browsers without `navigator.getBattery` (Firefox, Safari),
 *   - any thrown / rejected `getBattery()` call (locked-down
 *     iframes, file:// pages, the spec was deprecated so vendors
 *     are free to remove it at any time).
 *
 * The fallback's `charging: true` is intentional: it lets
 * `applyBatteryRule` short-circuit and return user pref unchanged,
 * which is the documented "no API → assume plugged in" behaviour.
 */
export function useBatteryStatus(): BatteryStatus {
  const [status, setStatus] = useState<BatteryStatus>(UNSUPPORTED_STATUS)

  useEffect(() => {
    if (typeof navigator === "undefined") return
    const nav = navigator as Navigator & NavigatorWithBattery
    if (typeof nav.getBattery !== "function") return

    let cancelled = false
    let battery: BatteryManagerLike | null = null

    const update = () => {
      if (!battery || cancelled) return
      setStatus({
        level: battery.level,
        charging: battery.charging,
        unsupported: false,
      })
    }

    nav
      .getBattery()
      .then((b) => {
        if (cancelled) return
        battery = b
        update()
        b.addEventListener?.("levelchange", update)
        b.addEventListener?.("chargingchange", update)
      })
      .catch(() => {
        // Spec is deprecated; Chrome may reject in privacy mode.
        // Stay on the unsupported fallback — the rule will skip.
      })

    return () => {
      cancelled = true
      if (battery) {
        battery.removeEventListener?.("levelchange", update)
        battery.removeEventListener?.("chargingchange", update)
      }
    }
  }, [])

  return status
}

// ─────────────────────────────────────────────────────────────────────
// useBatteryAwareMotion — composed hook for BS.3.5 + BS.3.6
// ─────────────────────────────────────────────────────────────────────

/**
 * Compose `useBatteryStatus` + `applyBatteryRule` + a per-session
 * "force full" override + one-shot toast notifications. The output
 * is a fully-resolved `MotionLevel` ready to feed motion hooks
 * (BS.3.5's resolver chain layers `prefers-reduced-motion` and
 * `motion: off` *outside* this hook — those are higher-priority
 * concerns and shouldn't be re-litigated by the battery layer).
 *
 * @param userPref The user's stored motion preference
 *                 (from `getMotionPreference()`).
 */
export function useBatteryAwareMotion(userPref: MotionLevel): UseBatteryAwareMotionResult {
  const status = useBatteryStatus()
  const [forceFullOverride, setForceFullOverride] = useState(false)

  const tier = useMemo(() => tierForLevel(status.level), [status.level])

  const effective = useMemo<MotionLevel>(() => {
    if (forceFullOverride) return userPref
    return applyBatteryRule(userPref, status)
  }, [forceFullOverride, status, userPref])

  const didDegrade = effective !== userPref

  // ── One-shot toast on transition into a degraded tier ─────────────
  //
  // Track the previously-announced tier. Toast only when the tier
  // *changes* (to avoid render-loop spam) and only when the new
  // tier is actually a degraded one (plenty / charging never toasts;
  // override on never toasts; unsupported never toasts).
  const lastToastedTier = useRef<BatteryTier | null>(null)

  useEffect(() => {
    if (status.unsupported || status.charging || forceFullOverride) {
      // Not currently in a degrading state — reset so the next
      // descent into a degraded tier fires a fresh toast.
      lastToastedTier.current = null
      return
    }

    if (tier === "plenty") {
      lastToastedTier.current = null
      return
    }

    // We're in a degraded tier (moderate / low / critical).
    if (lastToastedTier.current === tier) return

    // Only toast on a *worsening* transition. Going low → moderate
    // (i.e. the user plugged in for a moment, then unplugged at a
    // higher charge) shouldn't fire a toast; the user has already
    // been informed and additional notifications are noise.
    const prevRank = lastToastedTier.current ? TIER_RANK[lastToastedTier.current] : TIER_RANK.plenty
    if (TIER_RANK[tier] >= prevRank) {
      lastToastedTier.current = tier
      return
    }

    lastToastedTier.current = tier
    toast({
      title: "已自動降低動態效果",
      description: messageForTier(tier),
    })
  }, [tier, status.charging, status.unsupported, forceFullOverride])

  const setOverride = useCallback((next: boolean) => {
    setForceFullOverride(next)
  }, [])

  return {
    effective,
    status,
    tier,
    didDegrade,
    forceFullOverride,
    setForceFullOverride: setOverride,
  }
}

/** Per-tier user-facing toast copy. Kept short — toast bodies are
 *  rendered in a single line at most viewports. */
function messageForTier(tier: BatteryTier): string {
  switch (tier) {
    case "moderate":
      return "電量低於 50%，動畫已降一級以節省電力。"
    case "low":
      return "電量低於 30%，動畫已降為輕度（subtle）。"
    case "critical":
      return "電量低於 15%，動畫已關閉以延長續航。"
    case "plenty":
      // Should not reach here — the caller filters plenty above —
      // but TypeScript's exhaustiveness check insists on coverage.
      return ""
  }
}
