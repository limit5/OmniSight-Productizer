"use client"

/**
 * BS.11.4 — Catalog density preference SoT (J4 round-trip).
 *
 * Owns the persisted side of the catalog density setting:
 *
 *   - The `DENSITY_PREFERENCE_KEY` used against the J4
 *     user-preferences API (mirrors `motion_level` for motion).
 *   - `getDensityPreference` / `setDensityPreference` async helpers
 *     that wrap `getUserPreference` / `setUserPreference` from
 *     `lib/api.ts`, following the same shape as
 *     `lib/motion-preferences.ts` (BS.3.3 reference pattern).
 *   - `subscribeDensityPreference` same-tab event bus so any in-tab
 *     consumer (e.g. a future second density toggle in another panel)
 *     refreshes without a full remount on a `setDensityPreference()`
 *     write — cross-tab / cross-device sync flows through the existing
 *     `preferences.updated` SSE channel emitted by the J4 backend
 *     router (`backend/routers/preferences.py::set_preference`).
 *
 * The density-level type (`CatalogDensity`) intentionally lives in
 * `components/omnisight/catalog-tab.tsx` next to the grid/CSS lookup
 * tables it parameterises — this module imports from there rather
 * than redefining the union, the same way `lib/motion-preferences.ts`
 * is referenced from `hooks/use-zero-g.ts`. Keeping the SoT split
 * (type at the consumer, persistence here) avoids a circular import
 * between the lib and the component module.
 *
 * Module-global state audit (per implement_phase_step.md Step 1):
 *
 *   - No module-level mutable state. The same-tab event bus is a
 *     `window.addEventListener('omnisight:density-pref-changed', …)`
 *     subscription — per-tab, not cross-process. Cross-tab / cross-
 *     device propagation is the J4 router's responsibility (PG row
 *     is server-of-record + `preferences.updated` SSE event).
 *   - Browser-only module; the uvicorn `--workers N` model does not
 *     apply (answer #1 in the SOP — every tab derives the same value
 *     from the same persisted PG row at load time).
 *
 * Read-after-write timing audit: N/A — pure browser DOM API + a
 * single PUT followed by a `dispatchEvent`; no cross-process or
 * cross-request ordering. Errors from `setUserPreference` propagate
 * to the caller so the catalog tab can surface a retry / toast at
 * the call site instead of swallowing silently.
 */

import { getUserPreference, setUserPreference } from "@/lib/api"
import {
  CATALOG_DEFAULT_DENSITY,
  CATALOG_DENSITIES,
  type CatalogDensity,
} from "@/components/omnisight/catalog-tab"

/** User-preferences API key. Stored verbatim in the J4
 *  `user_preferences` table keyed by `(tenant_id, user_id)`.
 *  Renaming requires a data migration — the value is part of the
 *  persisted contract, not a private constant. Mirrors
 *  `MOTION_PREFERENCE_KEY = "motion_level"`. */
export const DENSITY_PREFERENCE_KEY = "catalog_density"

/** Type guard for raw values coming back from the user-preferences
 *  API. Lets `getDensityPreference()` degrade to the default on
 *  malformed values (legacy entry, hand-edited row, schema drift)
 *  rather than throw a TypeError into a render path. */
export function isCatalogDensity(value: unknown): value is CatalogDensity {
  return (
    typeof value === "string" &&
    (CATALOG_DENSITIES as readonly string[]).includes(value)
  )
}

/**
 * Read the user's current catalog density, falling back to
 * `CATALOG_DEFAULT_DENSITY` when no preference is stored or the
 * stored value is not a recognised `CatalogDensity`.
 *
 * `getUserPreference` already returns `null` on 404 / network
 * failure (see `lib/api.ts`), so this helper never throws — the
 * catalog tab can call it without try/catch.
 */
export async function getDensityPreference(): Promise<CatalogDensity> {
  const pref = await getUserPreference(DENSITY_PREFERENCE_KEY)
  if (pref && isCatalogDensity(pref.value)) return pref.value
  return CATALOG_DEFAULT_DENSITY
}

/**
 * Persist the user's density preference via the existing
 * user-preferences API. Errors propagate to the caller so the
 * catalog tab can surface a retry / toast; silent swallowing
 * belongs at the call site, not here.
 *
 * Emits a same-tab `omnisight:density-pref-changed` `CustomEvent`
 * after a successful write so subscribers (the catalog tab's own
 * `useUserDensityPreference` hook, plus any future in-tab consumer)
 * refresh without a full remount. Cross-device sync is handled by
 * the J4 backend router emitting `preferences.updated` on the SSE
 * bus — sibling devices pick the change up on their next mount.
 */
export async function setDensityPreference(
  density: CatalogDensity,
): Promise<void> {
  await setUserPreference(DENSITY_PREFERENCE_KEY, density)
  if (typeof window !== "undefined") {
    window.dispatchEvent(
      new CustomEvent<CatalogDensity>(DENSITY_PREF_CHANGE_EVENT, {
        detail: density,
      }),
    )
  }
}

/** Same-tab event name used to broadcast density-preference writes
 *  to other components without a full remount. Exported for tests. */
export const DENSITY_PREF_CHANGE_EVENT = "omnisight:density-pref-changed"

/**
 * Subscribe to `setDensityPreference()` writes within the same tab.
 * Returns an unsubscribe function. SSR-safe: if `window` is not
 * available the subscription is a no-op (the `useEffect` site is
 * already gated, but this keeps the helper safe to call from
 * non-component code too).
 */
export function subscribeDensityPreference(
  callback: (density: CatalogDensity) => void,
): () => void {
  if (typeof window === "undefined") return () => {}
  const handler = (event: Event) => {
    const detail = (event as CustomEvent<unknown>).detail
    if (isCatalogDensity(detail)) callback(detail)
  }
  window.addEventListener(DENSITY_PREF_CHANGE_EVENT, handler)
  return () => window.removeEventListener(DENSITY_PREF_CHANGE_EVENT, handler)
}
