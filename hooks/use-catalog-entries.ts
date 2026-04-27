"use client"

/**
 * BS.8.6 — `useCatalogEntries()` hook.
 *
 * Fetches the per-tenant catalog entry list from `GET /catalog/entries`
 * for the admin-only `<CustomEntryForm />` tab. The list includes
 * `shipped` rows (global), `operator` / `override` rows scoped to the
 * caller's tenant, and any `subscription` rows the feed worker has
 * pulled in. The form filters the snapshot client-side: the table
 * shows only `operator` / `override` rows (the "custom" set), while
 * the depends_on multi-select can pick from any visible row.
 *
 * REST-only (deliberate scope limit)
 * ──────────────────────────────────
 * Catalog rows mutate only on admin intent (create / patch / delete)
 * or feed-worker tick (subscription rows), so a caller-driven
 * `refresh()` after each action keeps the snapshot fresh without an
 * SSE subscription. If a future row needs cross-tab live updates,
 * an SSE listener can layer in then.
 *
 * Module-global state audit (SOP Step 1)
 * ──────────────────────────────────────
 * Pure per-instance React state:
 *   - `entries` — current snapshot (useState array)
 *   - `loading` / `error` — UI status flags (useState scalars)
 *   - `mountedRef` — guards setState after unmount (useRef)
 * No module-level mutable state. Each tab fetches its own snapshot;
 * cross-worker safety comes from the backend reading from PG.
 *
 * Read-after-write timing audit
 * ─────────────────────────────
 * After `createCatalogEntry` / `patchCatalogEntry` / `deleteCatalogEntry`
 * resolve, the backend has already committed; the caller fires
 * `refresh()` and the next GET sees the post-commit state via PG MVCC.
 */

import { useCallback, useEffect, useRef, useState } from "react"

import {
  listCatalogEntries,
  type CatalogEntryDetail,
  type ListCatalogEntriesOptions,
} from "@/lib/api"

export interface UseCatalogEntriesResult {
  /** The current snapshot. */
  entries: CatalogEntryDetail[]
  /** True from mount through the first successful or failed fetch,
   *  and while a manual `refresh()` round-trip is in flight. */
  loading: boolean
  /** Last error message, or `null` after a successful fetch. */
  error: string | null
  /** Manual refresh trigger. Returns the fresh array (or `null` if
   *  the fetch failed). */
  refresh: () => Promise<CatalogEntryDetail[] | null>
}

const DEFAULT_OPTIONS: ListCatalogEntriesOptions = {
  // Pull a generous limit so the depends_on multi-select can search
  // across the full visible catalog without per-keystroke round-trips.
  // Backend caps at 500 (`Limit(default=100, max_cap=500)`).
  limit: 500,
  sort: "display_name",
  order: "asc",
}

export function useCatalogEntries(
  options: ListCatalogEntriesOptions = DEFAULT_OPTIONS,
): UseCatalogEntriesResult {
  const [entries, setEntries] = useState<CatalogEntryDetail[]>([])
  const [loading, setLoading] = useState<boolean>(true)
  const [error, setError] = useState<string | null>(null)
  const mountedRef = useRef(true)

  // Stash options in a ref so the refresh callback's identity is stable
  // even when the caller passes an options object literal each render.
  // The ref is updated in an effect rather than during render so React
  // refs aren't mutated mid-render (lint rule react-hooks/refs).
  const optsRef = useRef(options)
  useEffect(() => {
    optsRef.current = options
  }, [options])

  const refresh = useCallback(async (): Promise<CatalogEntryDetail[] | null> => {
    if (mountedRef.current) {
      setLoading(true)
      setError(null)
    }
    try {
      const res = await listCatalogEntries(optsRef.current)
      const next = res.items ?? []
      if (mountedRef.current) {
        setEntries(next)
        setLoading(false)
        setError(null)
      }
      return next
    } catch (err) {
      const message =
        err instanceof Error
          ? err.message
          : typeof err === "string"
            ? err
            : "failed to load catalog entries"
      if (mountedRef.current) {
        setError(message)
        setLoading(false)
      }
      return null
    }
  }, [])

  useEffect(() => {
    mountedRef.current = true
    void refresh() // eslint-disable-line react-hooks/set-state-in-effect -- fetch-on-mount populates state from network
    return () => {
      mountedRef.current = false
    }
  }, [refresh])

  return { entries, loading, error, refresh }
}
