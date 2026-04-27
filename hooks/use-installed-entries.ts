"use client"

/**
 * BS.8.2 — `useInstalledEntries()` hook.
 *
 * Fetches the list of currently-installed catalog entries from
 * `GET /installer/installed` and exposes them in the camelCase shape
 * `<InstalledTab />` consumes (`InstalledEntry` interface from
 * `components/omnisight/installed-tab.tsx`).
 *
 * Why a dedicated hook
 * ────────────────────
 * BS.8.1 shipped `<InstalledTab />` as a purely presentational component
 * that takes `entries` as a prop and the page wrapper fed it an empty
 * placeholder. This row replaces the placeholder with a real REST poll
 * so the Installed tab actually shows something.
 *
 * REST-only (deliberate scope limit)
 * ──────────────────────────────────
 * BS.7.4's `useInstallJobs()` is SSE-driven because install lifecycle
 * ticks need sub-second freshness. The Installed list, in contrast,
 * only changes when the operator approves an install or a cleanup-
 * unused uninstall — both events the operator just initiated, so a
 * caller-driven `refresh()` after action is enough. We avoid wiring
 * yet another SSE subscription per tab; if BS.8 follow-ups need live
 * "another tenant just installed" signalling we can add an SSE listener
 * in a separate row.
 *
 * Module-global state audit (SOP Step 1)
 * ──────────────────────────────────────
 * Pure per-instance React state:
 *   - `entries` (useState array) — the rendered snapshot
 *   - `loading` / `error` (useState scalars) — UI status flags
 *   - `mountedRef` (useRef) — guards setState after unmount
 * No module-level mutable state, no in-memory cache, no thread-locals.
 * Each tab fetches its own snapshot; cross-tab consistency is the
 * backend's job (every tab is reading the same PG state). Multi-worker
 * safe because the GET is read-only and PG MVCC handles the rest.
 *
 * Read-after-write timing audit
 * ─────────────────────────────
 * Caller writes (`createInstallJob`, `bulkUninstallEntries`) commit on
 * the backend before returning; the subsequent `refresh()` issues a
 * fresh GET that PG MVCC honours (the commit happened before the GET
 * starts), so the new list reflects the just-completed write. There is
 * no shared in-memory cache to lag.
 */

import { useCallback, useEffect, useRef, useState } from "react"

import {
  listInstalledEntries,
  type InstalledEntryRow,
} from "@/lib/api"
import { type InstalledEntry } from "@/components/omnisight/installed-tab"
import { type CatalogFamily } from "@/components/omnisight/catalog-tab"

/** Map a backend family literal to the BS.8.1 5-bucket UI vocabulary.
 *  `rtos` / `cross-toolchain` collapse into `embedded` since the
 *  `InstalledTab` palette only has five chips; the catalog detail page
 *  surfaces the precise variant when needed. */
function _coerceFamily(family: string | null | undefined): CatalogFamily {
  switch (family) {
    case "mobile":
    case "embedded":
    case "web":
    case "software":
    case "custom":
      return family
    case "rtos":
    case "cross-toolchain":
      return "embedded"
    default:
      return "custom"
  }
}

/** Snake_case wire shape → camelCase UI shape. Exported so tests can
 *  exercise the marshalling without spinning up the full hook. */
export function installedEntryFromRow(row: InstalledEntryRow): InstalledEntry {
  const sourceRaw = row.source
  const source =
    sourceRaw === "shipped" || sourceRaw === "operator" || sourceRaw === "override"
      ? sourceRaw
      : undefined
  return {
    id: row.entry_id,
    displayName: row.display_name,
    vendor: row.vendor,
    family: _coerceFamily(row.family),
    version: row.version ?? undefined,
    description: row.description ?? undefined,
    diskUsageBytes: row.disk_usage_bytes,
    usedByWorkspaceCount: row.used_by_workspace_count,
    lastUsedAt: row.last_used_at,
    installedAt: row.installed_at,
    updateAvailable: row.update_available,
    availableVersion: row.available_version ?? undefined,
    source,
  }
}

export interface UseInstalledEntriesResult {
  /** The current snapshot. Stable identity within a render — when the
   *  hook re-fetches we return a fresh array so React's referential
   *  equality re-render path fires. */
  entries: InstalledEntry[]
  /** True from mount through the first successful or failed fetch, and
   *  while a manual `refresh()` round-trip is in flight. */
  loading: boolean
  /** Last error message, or `null` after a successful fetch. The hook
   *  does NOT throw — `<ApiErrorToastCenter />` already surfaces the
   *  error globally; surfacing it here is for components that want to
   *  render an inline retry CTA. */
  error: string | null
  /** Manual refresh trigger. Returns the fresh entries array (or `null`
   *  if the fetch failed). Tests use the return value to await without
   *  reaching into the hook's internal state. */
  refresh: () => Promise<InstalledEntry[] | null>
}

/** Subscribe-once-on-mount + manual `refresh()` semantics. Mirrors the
 *  `useInstallJobs()` mountedRef pattern so React StrictMode's
 *  double-mount doesn't leak setState calls. */
export function useInstalledEntries(): UseInstalledEntriesResult {
  const [entries, setEntries] = useState<InstalledEntry[]>([])
  const [loading, setLoading] = useState<boolean>(true)
  const [error, setError] = useState<string | null>(null)
  const mountedRef = useRef(true)

  const refresh = useCallback(async (): Promise<InstalledEntry[] | null> => {
    if (mountedRef.current) {
      setLoading(true)
      setError(null)
    }
    try {
      const res = await listInstalledEntries()
      const next = (res.items ?? []).map(installedEntryFromRow)
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
            : "failed to load installed entries"
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
    // refresh is stable (useCallback with empty deps) so listing it as
    // a dep is consistent with React's exhaustive-deps lint.
  }, [refresh])

  return { entries, loading, error, refresh }
}
