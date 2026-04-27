/**
 * BS.8.2 — useInstalledEntries() hook contract.
 *
 * Verifies:
 *   1. Mount fires `listInstalledEntries` and surfaces a `loading=true`
 *      → `loading=false` transition once the GET resolves.
 *   2. Successful fetch maps the snake_case wire shape to the
 *      camelCase `InstalledEntry` shape consumed by `<InstalledTab />`.
 *   3. `installedEntryFromRow` is exported + pure (no closure state).
 *   4. Rejected fetch surfaces `error` and clears `loading`; the hook
 *      does not throw upward.
 *   5. `refresh()` triggers a fresh GET and returns the new entries
 *      array; tests can await on the promise without reading hook state.
 *   6. `refresh()` after an error clears the prior error message on a
 *      successful round-trip.
 *   7. `refresh()` returns `null` on failure (so callers can branch
 *      without re-throwing).
 *   8. `_coerceFamily` collapse: `rtos` / `cross-toolchain` → `embedded`
 *      (5-bucket UI palette); unknown → `custom`.
 *   9. Unknown `source` values become `undefined` (not the wire value).
 */

import { act, renderHook, waitFor } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

vi.mock("@/lib/api", () => ({
  listInstalledEntries: vi.fn(),
}))

import * as api from "@/lib/api"
import type { InstalledEntryRow } from "@/lib/api"
import {
  installedEntryFromRow,
  useInstalledEntries,
} from "@/hooks/use-installed-entries"

const SAMPLE_ROW: InstalledEntryRow = {
  entry_id: "neural-blur-sdk",
  display_name: "Neural Blur SDK",
  vendor: "Acme",
  family: "mobile",
  version: "1.2.3",
  description: "On-device blur kernel.",
  disk_usage_bytes: 12_345_678,
  used_by_workspace_count: 1,
  last_used_at: "2026-04-26T10:00:00Z",
  installed_at: "2026-04-25T08:00:00Z",
  update_available: false,
  available_version: null,
  source: "operator",
}

afterEach(() => {
  vi.clearAllMocks()
})

describe("BS.8.2 — useInstalledEntries hook", () => {
  it("starts loading=true and fires the REST fetch on mount", async () => {
    const fetcher = api.listInstalledEntries as ReturnType<typeof vi.fn>
    fetcher.mockResolvedValue({ items: [], count: 0 })
    const { result } = renderHook(() => useInstalledEntries())
    expect(result.current.loading).toBe(true)
    expect(result.current.entries).toEqual([])
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(fetcher).toHaveBeenCalledTimes(1)
  })

  it("maps the wire shape to the InstalledEntry UI shape after a successful fetch", async () => {
    const fetcher = api.listInstalledEntries as ReturnType<typeof vi.fn>
    fetcher.mockResolvedValue({ items: [SAMPLE_ROW], count: 1 })
    const { result } = renderHook(() => useInstalledEntries())
    await waitFor(() => expect(result.current.entries.length).toBe(1))
    const entry = result.current.entries[0]
    expect(entry.id).toBe("neural-blur-sdk")
    expect(entry.displayName).toBe("Neural Blur SDK")
    expect(entry.vendor).toBe("Acme")
    expect(entry.family).toBe("mobile")
    expect(entry.version).toBe("1.2.3")
    expect(entry.diskUsageBytes).toBe(12_345_678)
    expect(entry.usedByWorkspaceCount).toBe(1)
    expect(entry.lastUsedAt).toBe("2026-04-26T10:00:00Z")
    expect(entry.installedAt).toBe("2026-04-25T08:00:00Z")
    expect(entry.source).toBe("operator")
  })

  it("surfaces an error when the fetch rejects and clears loading", async () => {
    const fetcher = api.listInstalledEntries as ReturnType<typeof vi.fn>
    fetcher.mockRejectedValue(new Error("boom"))
    const { result } = renderHook(() => useInstalledEntries())
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.error).toBe("boom")
    expect(result.current.entries).toEqual([])
  })

  it("refresh() triggers a fresh GET and returns the new entries", async () => {
    const fetcher = api.listInstalledEntries as ReturnType<typeof vi.fn>
    fetcher.mockResolvedValueOnce({ items: [], count: 0 })
    const { result } = renderHook(() => useInstalledEntries())
    await waitFor(() => expect(result.current.loading).toBe(false))

    fetcher.mockResolvedValueOnce({ items: [SAMPLE_ROW], count: 1 })
    let returned: ReturnType<typeof installedEntryFromRow>[] | null = null
    await act(async () => {
      returned = await result.current.refresh()
    })
    expect(returned).not.toBeNull()
    expect(returned!.length).toBe(1)
    expect(returned![0].id).toBe("neural-blur-sdk")
    expect(fetcher).toHaveBeenCalledTimes(2)
    expect(result.current.entries.length).toBe(1)
  })

  it("refresh() clears prior error after a successful round-trip", async () => {
    const fetcher = api.listInstalledEntries as ReturnType<typeof vi.fn>
    fetcher.mockRejectedValueOnce(new Error("first failure"))
    const { result } = renderHook(() => useInstalledEntries())
    await waitFor(() => expect(result.current.error).toBe("first failure"))

    fetcher.mockResolvedValueOnce({ items: [], count: 0 })
    await act(async () => {
      await result.current.refresh()
    })
    expect(result.current.error).toBeNull()
  })

  it("refresh() returns null on failure (caller can branch without re-throwing)", async () => {
    const fetcher = api.listInstalledEntries as ReturnType<typeof vi.fn>
    fetcher.mockResolvedValueOnce({ items: [], count: 0 })
    const { result } = renderHook(() => useInstalledEntries())
    await waitFor(() => expect(result.current.loading).toBe(false))

    fetcher.mockRejectedValueOnce(new Error("retry failure"))
    let returned: unknown = "untouched"
    await act(async () => {
      returned = await result.current.refresh()
    })
    expect(returned).toBeNull()
    expect(result.current.error).toBe("retry failure")
  })

  it("installedEntryFromRow collapses rtos / cross-toolchain to embedded family", () => {
    const r = (family: string): InstalledEntryRow => ({ ...SAMPLE_ROW, family })
    expect(installedEntryFromRow(r("rtos")).family).toBe("embedded")
    expect(installedEntryFromRow(r("cross-toolchain")).family).toBe("embedded")
  })

  it("installedEntryFromRow falls back to custom for unknown family literals", () => {
    const r: InstalledEntryRow = { ...SAMPLE_ROW, family: "totally-new-bucket" }
    expect(installedEntryFromRow(r).family).toBe("custom")
  })

  it("installedEntryFromRow drops unknown source literals to undefined", () => {
    // ``subscription`` is a valid wire value but not a UI palette; the
    // tab today uses a 3-source palette (shipped/operator/override).
    const r: InstalledEntryRow = { ...SAMPLE_ROW, source: "subscription" }
    expect(installedEntryFromRow(r).source).toBeUndefined()
  })
})
