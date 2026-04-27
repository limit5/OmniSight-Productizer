/**
 * BS.8.6 — useCatalogEntries() hook contract.
 *
 * Verifies:
 *   1. Mount fires `listCatalogEntries` and surfaces loading transition.
 *   2. Successful fetch exposes the entries as-is.
 *   3. Rejected fetch surfaces `error` and clears loading (no throw).
 *   4. `refresh()` returns the fresh array and clears prior error.
 *   5. `refresh()` returns null on failure.
 *   6. Empty items resolves to empty array (defence against backend nulls).
 */

import { act, renderHook, waitFor } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

vi.mock("@/lib/api", () => ({
  listCatalogEntries: vi.fn(),
}))

import * as api from "@/lib/api"
import type { CatalogEntryDetail } from "@/lib/api"
import { useCatalogEntries } from "@/hooks/use-catalog-entries"

const SAMPLE: CatalogEntryDetail = {
  id: "vendor-sdk",
  source: "operator",
  schema_version: 1,
  tenant_id: "t-abc",
  vendor: "Acme",
  family: "embedded",
  display_name: "Acme SDK",
  version: "1.0.0",
  install_method: "shell_script",
  install_url: "https://x.test/sdk.tar.gz",
  sha256: "a".repeat(64),
  size_bytes: 1024,
  depends_on: [],
  metadata: {},
  hidden: false,
  created_at: "2026-04-27T10:00:00Z",
  updated_at: "2026-04-27T10:00:00Z",
}

const EMPTY_RES = { items: [], count: 0, total: 0, limit: 100, offset: 0 }

afterEach(() => {
  vi.clearAllMocks()
})

describe("BS.8.6 — useCatalogEntries hook", () => {
  it("starts loading=true and fires the REST fetch on mount", async () => {
    const fetcher = api.listCatalogEntries as ReturnType<typeof vi.fn>
    fetcher.mockResolvedValue(EMPTY_RES)
    const { result } = renderHook(() => useCatalogEntries())
    expect(result.current.loading).toBe(true)
    expect(result.current.entries).toEqual([])
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(fetcher).toHaveBeenCalledTimes(1)
  })

  it("returns the wire entries verbatim after a successful fetch", async () => {
    const fetcher = api.listCatalogEntries as ReturnType<typeof vi.fn>
    fetcher.mockResolvedValue({ ...EMPTY_RES, items: [SAMPLE], count: 1, total: 1 })
    const { result } = renderHook(() => useCatalogEntries())
    await waitFor(() => expect(result.current.entries.length).toBe(1))
    expect(result.current.entries[0]).toEqual(SAMPLE)
    expect(result.current.error).toBeNull()
  })

  it("surfaces an error when the fetch rejects and clears loading", async () => {
    const fetcher = api.listCatalogEntries as ReturnType<typeof vi.fn>
    fetcher.mockRejectedValue(new Error("boom"))
    const { result } = renderHook(() => useCatalogEntries())
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.error).toBe("boom")
    expect(result.current.entries).toEqual([])
  })

  it("refresh() triggers a fresh GET and returns the new array", async () => {
    const fetcher = api.listCatalogEntries as ReturnType<typeof vi.fn>
    fetcher.mockResolvedValueOnce(EMPTY_RES)
    const { result } = renderHook(() => useCatalogEntries())
    await waitFor(() => expect(result.current.loading).toBe(false))

    fetcher.mockResolvedValueOnce({ ...EMPTY_RES, items: [SAMPLE], count: 1, total: 1 })
    let refreshed: CatalogEntryDetail[] | null = null
    await act(async () => {
      refreshed = await result.current.refresh()
    })
    expect(refreshed).toEqual([SAMPLE])
    expect(fetcher).toHaveBeenCalledTimes(2)
    expect(result.current.entries).toEqual([SAMPLE])
  })

  it("refresh() clears a prior error on success", async () => {
    const fetcher = api.listCatalogEntries as ReturnType<typeof vi.fn>
    fetcher.mockRejectedValueOnce(new Error("first failed"))
    const { result } = renderHook(() => useCatalogEntries())
    await waitFor(() => expect(result.current.error).toBe("first failed"))

    fetcher.mockResolvedValueOnce({ ...EMPTY_RES, items: [SAMPLE], count: 1, total: 1 })
    await act(async () => {
      await result.current.refresh()
    })
    expect(result.current.error).toBeNull()
    expect(result.current.entries).toEqual([SAMPLE])
  })

  it("refresh() returns null when the fetch fails", async () => {
    const fetcher = api.listCatalogEntries as ReturnType<typeof vi.fn>
    fetcher.mockResolvedValueOnce(EMPTY_RES)
    const { result } = renderHook(() => useCatalogEntries())
    await waitFor(() => expect(result.current.loading).toBe(false))

    fetcher.mockRejectedValueOnce(new Error("net fail"))
    let refreshed: CatalogEntryDetail[] | null = SAMPLE as unknown as CatalogEntryDetail[] | null
    await act(async () => {
      refreshed = await result.current.refresh()
    })
    expect(refreshed).toBeNull()
    expect(result.current.error).toBe("net fail")
  })

  it("treats a missing items field as an empty array", async () => {
    const fetcher = api.listCatalogEntries as ReturnType<typeof vi.fn>
    fetcher.mockResolvedValue({ count: 0, total: 0, limit: 100, offset: 0 })
    const { result } = renderHook(() => useCatalogEntries())
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.entries).toEqual([])
  })
})
