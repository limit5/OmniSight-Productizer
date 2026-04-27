/**
 * BS.8.5 — useCatalogSources() hook contract.
 *
 * Verifies:
 *   1. Mount fires `listCatalogSources` and surfaces a `loading=true` →
 *      `loading=false` transition once the GET resolves.
 *   2. Successful fetch surfaces the wire `CatalogSource[]` array as-is.
 *   3. Rejected fetch surfaces `error` and clears `loading` (no throw).
 *   4. `refresh()` triggers a fresh GET and returns the new array; tests
 *      can await on the promise without reading hook state.
 *   5. `refresh()` after an error clears the prior error message on a
 *      successful round-trip.
 *   6. `refresh()` returns `null` on failure so callers can branch
 *      without re-throwing.
 *   7. Empty `items` is treated as an empty array (defence against the
 *      backend ever returning `null`).
 */

import { act, renderHook, waitFor } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

vi.mock("@/lib/api", () => ({
  listCatalogSources: vi.fn(),
}))

import * as api from "@/lib/api"
import type { CatalogSource } from "@/lib/api"
import { useCatalogSources } from "@/hooks/use-catalog-sources"

const SAMPLE: CatalogSource = {
  id: "sub-deadbeef01234567",
  tenant_id: "t-abc",
  feed_url: "https://feeds.example.com/catalog.json",
  auth_method: "bearer",
  auth_secret_ref: "tenant_token_a",
  refresh_interval_s: 86400,
  last_synced_at: null,
  last_sync_status: null,
  enabled: true,
  created_at: "2026-04-27T10:00:00Z",
  updated_at: "2026-04-27T10:00:00Z",
}

afterEach(() => {
  vi.clearAllMocks()
})

describe("BS.8.5 — useCatalogSources hook", () => {
  it("starts loading=true and fires the REST fetch on mount", async () => {
    const fetcher = api.listCatalogSources as ReturnType<typeof vi.fn>
    fetcher.mockResolvedValue({ items: [], count: 0 })
    const { result } = renderHook(() => useCatalogSources())
    expect(result.current.loading).toBe(true)
    expect(result.current.sources).toEqual([])
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(fetcher).toHaveBeenCalledTimes(1)
  })

  it("returns the wire CatalogSource array verbatim after a successful fetch", async () => {
    const fetcher = api.listCatalogSources as ReturnType<typeof vi.fn>
    fetcher.mockResolvedValue({ items: [SAMPLE], count: 1 })
    const { result } = renderHook(() => useCatalogSources())
    await waitFor(() => expect(result.current.sources.length).toBe(1))
    expect(result.current.sources[0]).toEqual(SAMPLE)
    expect(result.current.error).toBeNull()
  })

  it("surfaces an error when the fetch rejects and clears loading", async () => {
    const fetcher = api.listCatalogSources as ReturnType<typeof vi.fn>
    fetcher.mockRejectedValue(new Error("boom"))
    const { result } = renderHook(() => useCatalogSources())
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.error).toBe("boom")
    expect(result.current.sources).toEqual([])
  })

  it("refresh() triggers a fresh GET and returns the new array", async () => {
    const fetcher = api.listCatalogSources as ReturnType<typeof vi.fn>
    fetcher.mockResolvedValueOnce({ items: [], count: 0 })
    const { result } = renderHook(() => useCatalogSources())
    await waitFor(() => expect(result.current.loading).toBe(false))

    fetcher.mockResolvedValueOnce({ items: [SAMPLE], count: 1 })
    let refreshed: CatalogSource[] | null = null
    await act(async () => {
      refreshed = await result.current.refresh()
    })
    expect(refreshed).toEqual([SAMPLE])
    expect(fetcher).toHaveBeenCalledTimes(2)
    expect(result.current.sources).toEqual([SAMPLE])
  })

  it("refresh() clears a prior error on success", async () => {
    const fetcher = api.listCatalogSources as ReturnType<typeof vi.fn>
    fetcher.mockRejectedValueOnce(new Error("first failed"))
    const { result } = renderHook(() => useCatalogSources())
    await waitFor(() => expect(result.current.error).toBe("first failed"))

    fetcher.mockResolvedValueOnce({ items: [SAMPLE], count: 1 })
    await act(async () => {
      await result.current.refresh()
    })
    expect(result.current.error).toBeNull()
    expect(result.current.sources).toEqual([SAMPLE])
  })

  it("refresh() returns null on failure (no re-throw)", async () => {
    const fetcher = api.listCatalogSources as ReturnType<typeof vi.fn>
    fetcher.mockResolvedValueOnce({ items: [], count: 0 })
    const { result } = renderHook(() => useCatalogSources())
    await waitFor(() => expect(result.current.loading).toBe(false))

    fetcher.mockRejectedValueOnce(new Error("bang"))
    let refreshed: CatalogSource[] | null = []
    await act(async () => {
      refreshed = await result.current.refresh()
    })
    expect(refreshed).toBeNull()
    expect(result.current.error).toBe("bang")
  })

  it("treats missing items as an empty array", async () => {
    const fetcher = api.listCatalogSources as ReturnType<typeof vi.fn>
    // Intentionally skip the items field — the hook should default it.
    fetcher.mockResolvedValue({ count: 0 })
    const { result } = renderHook(() => useCatalogSources())
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.sources).toEqual([])
  })
})
