"use client"

/**
 * BS.11.4 — Density preference hook (J4 round-trip).
 *
 * Loads the user's persisted catalog density preference once on
 * mount, returns it as React state, and keeps it in sync with
 * same-tab `setDensityPreference()` writes via the event bus
 * (`omnisight:density-pref-changed`). Cross-device sync flows
 * through the J4 backend router emitting `preferences.updated`
 * on the SSE bus — sibling devices pick the change up on their
 * next mount via `getDensityPreference()`.
 *
 * Mirrors the `useUserMotionPreference()` shape from
 * `hooks/use-effective-motion-level.ts` (BS.3.5):
 *
 *   - Returns `[density, setDensity, hydrated]` so consumers
 *     can render at the documented default before the first
 *     fetch resolves and surface "still loading" UI if they
 *     want to (the catalog tab keeps the toggle interactive
 *     and falls back to `CATALOG_DEFAULT_DENSITY` until then).
 *   - The `cancelled` flag guards against the classic
 *     stale-resolution race (component unmounts mid-fetch,
 *     fetch resolves, tries to setState on a dead component).
 *   - The setter wraps `setDensityPreference()` so the J4 PUT
 *     and the same-tab event dispatch are atomic from the
 *     caller's view; React state is updated optimistically
 *     before the await so the UI flips immediately and the
 *     PG write tracks behind. On API failure the optimistic
 *     update is rolled back to the prior persisted value.
 *
 * Module-global state audit (per implement_phase_step.md Step 1):
 *
 *   - All state is per-hook (`useState` / `useRef` / `useEffect`).
 *     `subscribeDensityPreference` is a `window` listener — per-tab,
 *     not cross-process. Cross-tab / cross-device propagation is
 *     the J4 router's responsibility.
 *   - Browser-only hook; the uvicorn `--workers N` model does not
 *     apply (answer #1: no shared state to synchronise).
 *
 * Read-after-write timing audit: the optimistic-then-persist
 * pattern means a same-tab observer (subscribed via
 * `subscribeDensityPreference`) sees the new density via two
 * independent paths — the local setState and the dispatched
 * `CustomEvent`. Both deliver the same value; a duplicate setState
 * is a no-op since React bails out on identical state. No
 * cross-process / cross-request ordering surfaces here.
 */

import { useCallback, useEffect, useRef, useState } from "react"

import {
  CATALOG_DEFAULT_DENSITY,
  type CatalogDensity,
} from "@/components/omnisight/catalog-tab"
import {
  getDensityPreference,
  setDensityPreference,
  subscribeDensityPreference,
} from "@/lib/density-preferences"

export interface UseUserDensityPreferenceResult {
  /** The current effective density. Until the first fetch resolves
   *  this is `CATALOG_DEFAULT_DENSITY` (comfortable). */
  density: CatalogDensity
  /** Persist a new density. Returns the promise so the caller can
   *  await / catch errors at the call site. The local state flips
   *  optimistically before the PG write completes; on PG failure
   *  the prior value is restored and the rejection propagates. */
  setDensity: (next: CatalogDensity) => Promise<void>
  /** True once the J4 fetch has resolved at least once (success
   *  or fallback). Lets the catalog tab distinguish "still loading"
   *  from "definitely the default". Optional for consumers — the
   *  catalog tab does not currently render a skeleton, but a
   *  future surface might. */
  hydrated: boolean
}

export function useUserDensityPreference(): UseUserDensityPreferenceResult {
  const [density, setLocalDensity] = useState<CatalogDensity>(
    CATALOG_DEFAULT_DENSITY,
  )
  const [hydrated, setHydrated] = useState(false)
  // Latest-ref pattern: the optimistic-rollback path needs the most
  // recent value at the moment the API rejects, but we must not
  // re-bind the setter callback every render. The ref tracks
  // the latest committed density without retriggering memoisation.
  const densityRef = useRef<CatalogDensity>(density)
  useEffect(() => {
    densityRef.current = density
  }, [density])

  useEffect(() => {
    let cancelled = false

    void getDensityPreference()
      .then((value) => {
        if (cancelled) return
        setLocalDensity(value)
        setHydrated(true)
      })
      .catch(() => {
        if (cancelled) return
        // `getDensityPreference` already swallows 404 / network
        // errors and falls back to the default, so a rejection
        // here is rare (e.g. an unexpected throw from `lib/api.ts`).
        // Mark hydrated so consumers don't spin forever and stay
        // on the documented default.
        setHydrated(true)
      })

    const unsubscribe = subscribeDensityPreference((next) => {
      if (!cancelled) setLocalDensity(next)
    })

    return () => {
      cancelled = true
      unsubscribe()
    }
  }, [])

  const setDensity = useCallback(
    async (next: CatalogDensity) => {
      const previous = densityRef.current
      if (next === previous) return
      // Optimistic update: flip the UI immediately so the toggle
      // feels responsive on slow networks / high latency. The PG
      // write tracks behind. Restoring the previous value on
      // rejection keeps the visible state consistent with what
      // the server actually has.
      setLocalDensity(next)
      try {
        await setDensityPreference(next)
      } catch (err) {
        setLocalDensity(previous)
        throw err
      }
    },
    [],
  )

  return { density, setDensity, hydrated }
}
