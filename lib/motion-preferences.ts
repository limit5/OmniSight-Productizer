"use client"

/**
 * BS.3.3 — Motion preference SoT.
 *
 * Owns the persisted side of the motion-level setting:
 *
 *   - The four-level `MotionLevel` type union,
 *   - The `DEFAULT_MOTION_LEVEL` (dramatic, per ADR §5.2),
 *   - The `MOTION_PREFERENCE_KEY` used against the user-preferences
 *     API (J4), and
 *   - `getMotionPreference` / `setMotionPreference` async helpers
 *     that wrap `getUserPreference` / `setUserPreference` from
 *     `lib/api.ts`.
 *
 * The integrated resolver `useEffectiveMotionLevel()` (BS.3.5) layers
 *   prefers-reduced-motion > motion: off > battery rule > user pref
 * on top of this stored preference; this module owns only the
 * persisted preference itself, not the runtime resolution chain.
 *
 * NOTE — `MotionLevel` is intentionally also exported from
 * `hooks/use-zero-g.ts` (BS.3.2). Both definitions must stay in
 * sync until BS.3.5 lifts the integrated hook out and the
 * remaining hooks switch to importing the type from this SoT.
 */

import { getUserPreference, setUserPreference } from "@/lib/api"

/** All supported motion levels, ordered weakest → strongest. The
 *  `as const` tuple lets us derive both the `MotionLevel` type and
 *  a runtime array from a single source — used by the type guard
 *  below and by the BS.3.6 Display Settings 4-radio UI. */
export const MOTION_LEVELS = ["off", "subtle", "normal", "dramatic"] as const

export type MotionLevel = (typeof MOTION_LEVELS)[number]

/** Default per ADR §5.2: new users land on the full 8-layer
 *  experience until they tone it down. Used as the fallback when:
 *  the user has never written a preference, the stored value is
 *  malformed, or the user-preferences fetch fails. */
export const DEFAULT_MOTION_LEVEL: MotionLevel = "dramatic"

/** User-preferences API key. Stored verbatim in the J4
 *  `user_preferences` table keyed by `(tenant_id, user_id)`.
 *  Renaming requires a data migration — the value is part of the
 *  persisted contract, not a private constant. */
export const MOTION_PREFERENCE_KEY = "motion_level"

/** Type guard for raw values coming back from the user-preferences
 *  API. Lets `getMotionPreference()` degrade to the default on
 *  malformed values (legacy entry, hand-edited row, schema drift)
 *  rather than throw a TypeError into a render path. */
export function isMotionLevel(value: unknown): value is MotionLevel {
  return typeof value === "string" && (MOTION_LEVELS as readonly string[]).includes(value)
}

/**
 * Read the user's current motion preference, falling back to
 * `DEFAULT_MOTION_LEVEL` when no preference is stored or the
 * stored value is not a recognised `MotionLevel`.
 *
 * `getUserPreference` already returns `null` on 404 / network
 * failure (see `lib/api.ts`), so this helper never throws — UI
 * code can call it without try/catch.
 */
export async function getMotionPreference(): Promise<MotionLevel> {
  const pref = await getUserPreference(MOTION_PREFERENCE_KEY)
  if (pref && isMotionLevel(pref.value)) return pref.value
  return DEFAULT_MOTION_LEVEL
}

/**
 * Persist the user's motion preference via the existing
 * user-preferences API. Errors propagate to the caller so the
 * Display Settings page (BS.3.6) can surface a retry / toast;
 * silent swallowing belongs at the call site, not here.
 *
 * Emits a same-tab `omnisight:motion-pref-changed` `CustomEvent`
 * after a successful write so subscribers (BS.3.5
 * `useEffectiveMotionLevel`) can refresh without a full remount.
 * Cross-tab propagation is out of scope (the user-preferences
 * API is server-of-record; another tab will pick up the change
 * on its next mount).
 */
export async function setMotionPreference(level: MotionLevel): Promise<void> {
  await setUserPreference(MOTION_PREFERENCE_KEY, level)
  if (typeof window !== "undefined") {
    window.dispatchEvent(
      new CustomEvent<MotionLevel>(MOTION_PREF_CHANGE_EVENT, { detail: level }),
    )
  }
}

/** Same-tab event name used to broadcast motion-preference writes
 *  to other components without a full remount. Exported for tests. */
export const MOTION_PREF_CHANGE_EVENT = "omnisight:motion-pref-changed"

/**
 * Subscribe to `setMotionPreference()` writes within the same tab.
 * Returns an unsubscribe function. SSR-safe: if `window` is not
 * available the subscription is a no-op (the `useEffect` site is
 * already gated, but this keeps the helper safe to call from
 * non-component code too).
 */
export function subscribeMotionPreference(
  callback: (level: MotionLevel) => void,
): () => void {
  if (typeof window === "undefined") return () => {}
  const handler = (event: Event) => {
    const detail = (event as CustomEvent<unknown>).detail
    if (isMotionLevel(detail)) callback(detail)
  }
  window.addEventListener(MOTION_PREF_CHANGE_EVENT, handler)
  return () => window.removeEventListener(MOTION_PREF_CHANGE_EVENT, handler)
}
