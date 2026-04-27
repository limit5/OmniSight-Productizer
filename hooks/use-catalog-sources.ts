"use client"

/**
 * BS.8.5 — `useCatalogSources()` hook.
 *
 * Fetches the per-tenant catalog feed subscriptions list from
 * `GET /catalog/sources` for the admin-only `<SourcesTab />`. The hook
 * exposes the raw `CatalogSource[]` shape (snake_case wire format) since
 * the Sources tab consumes it directly — there is only one consumer, so
 * there is no need to fork into a camelCase UI shape the way
 * `useInstalledEntries()` does for two consumers.
 *
 * REST-only (deliberate scope limit)
 * ──────────────────────────────────
 * Sources mutate only on operator intent (add / patch / delete / sync),
 * so a caller-driven `refresh()` after each action keeps the list fresh
 * without an SSE subscription. If a future row needs cross-tab live
 * updates (one admin adds a source while another is on the page), an
 * SSE listener can be layered in then.
 *
 * Module-global state audit (SOP Step 1)
 * ──────────────────────────────────────
 * Pure per-instance React state:
 *   - `sources` — current snapshot (useState array)
 *   - `loading` / `error` — UI status flags (useState scalars)
 *   - `mountedRef` — guards setState after unmount (useRef)
 * No module-level mutable state, no in-memory cache, no thread-locals.
 * Each tab fetches its own snapshot; cross-worker safety comes from the
 * backend reading from the shared PG state.
 *
 * Read-after-write timing audit
 * ─────────────────────────────
 * After `createCatalogSource` / `patchCatalogSource` / `deleteCatalogSource`
 * / `syncCatalogSource` resolve, the backend has already committed the
 * row; the caller fires `refresh()` and the next GET sees the post-commit
 * state via PG MVCC. No shared in-memory cache to lag.
 */

import { useCallback, useEffect, useRef, useState } from "react"

import {
  listCatalogSources,
  type CatalogSource,
} from "@/lib/api"

export interface UseCatalogSourcesResult {
  /** The current snapshot. The hook returns a fresh array on each
   *  fetch so React's referential equality re-render path fires. */
  sources: CatalogSource[]
  /** True from mount through the first successful or failed fetch, and
   *  while a manual `refresh()` round-trip is in flight. */
  loading: boolean
  /** Last error message, or `null` after a successful fetch. The hook
   *  does NOT throw — `<ApiErrorToastCenter />` surfaces the error
   *  globally; surfacing it here is for components that want to render
   *  an inline retry CTA. */
  error: string | null
  /** Manual refresh trigger. Returns the fresh array (or `null` if the
   *  fetch failed). Tests use the return value to await without
   *  reaching into the hook's internal state. */
  refresh: () => Promise<CatalogSource[] | null>
}

export function useCatalogSources(): UseCatalogSourcesResult {
  const [sources, setSources] = useState<CatalogSource[]>([])
  const [loading, setLoading] = useState<boolean>(true)
  const [error, setError] = useState<string | null>(null)
  const mountedRef = useRef(true)

  const refresh = useCallback(async (): Promise<CatalogSource[] | null> => {
    if (mountedRef.current) {
      setLoading(true)
      setError(null)
    }
    try {
      const res = await listCatalogSources()
      const next = res.items ?? []
      if (mountedRef.current) {
        setSources(next)
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
            : "failed to load catalog sources"
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

  return { sources, loading, error, refresh }
}
