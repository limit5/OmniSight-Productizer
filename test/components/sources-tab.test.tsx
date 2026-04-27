/**
 * BS.8.5 — SourcesTab contract tests.
 *
 * Locks the surface the platforms page wires up:
 *   1. Empty list renders the "No catalog feed subscriptions yet" hint.
 *   2. Toolbar count reflects the current rows.
 *   3. Sorting puts the newest-created row first.
 *   4. Per-row Sync now button calls `onSync` with the source.
 *   5. Per-row Remove → confirm overlay → Confirm calls `onRemove`.
 *   6. Per-row Remove → Cancel does not call `onRemove`.
 *   7. Add-source toolbar button opens an inline form.
 *   8. Form submit calls `onAdd` with the form payload.
 *   9. Form submit failure surfaces an inline form-error banner.
 *  10. URL validation rejects an obviously-bad URL before submitting.
 *  11. Sync failure surfaces an inline error banner above the list.
 *  12. Pure helpers `formatRefreshInterval` + `formatLastSyncedRelative`
 *      hit the obvious cases.
 *  13. `validateSourcesTabForm` returns null only on a valid payload.
 *  14. Snapshot fetch error renders the retry button when `onRetry`
 *      is supplied.
 */

import * as React from "react"
import { afterEach, describe, expect, it, vi } from "vitest"
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react"

import {
  SourcesTab,
  formatLastSyncedRelative,
  formatRefreshInterval,
  validateSourcesTabForm,
} from "@/components/omnisight/sources-tab"
import type { CatalogSource } from "@/lib/api"

const SOURCE_OLDER: CatalogSource = {
  id: "sub-aaaa1111aaaa1111",
  tenant_id: "t-abc",
  feed_url: "https://feeds.example.com/older.json",
  auth_method: "none",
  auth_secret_ref: null,
  refresh_interval_s: 3600,
  last_synced_at: "2026-04-26T10:00:00Z",
  last_sync_status: "ok",
  enabled: true,
  created_at: "2026-04-26T10:00:00Z",
  updated_at: "2026-04-26T10:00:00Z",
}

const SOURCE_NEWER: CatalogSource = {
  id: "sub-bbbb2222bbbb2222",
  tenant_id: "t-abc",
  feed_url: "https://feeds.example.com/newer.json",
  auth_method: "bearer",
  auth_secret_ref: "tenant_token_b",
  refresh_interval_s: 86400,
  last_synced_at: null,
  last_sync_status: null,
  enabled: false,
  created_at: "2026-04-27T10:00:00Z",
  updated_at: "2026-04-27T10:00:00Z",
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe("BS.8.5 — SourcesTab", () => {
  it("renders the empty hint when no sources are supplied", () => {
    render(<SourcesTab sources={[]} />)
    expect(screen.getByTestId("sources-tab-empty")).toBeTruthy()
    expect(screen.getByTestId("sources-tab-count").textContent).toMatch(
      /0 sources/,
    )
  })

  it("renders one row per source with the toolbar count and sorts newest first", () => {
    render(<SourcesTab sources={[SOURCE_OLDER, SOURCE_NEWER]} />)
    expect(screen.getByTestId("sources-tab-count").textContent).toMatch(
      /2 sources/,
    )
    const list = screen.getByTestId("sources-tab-list")
    const rows = list.querySelectorAll("li[data-source-id]")
    expect(rows.length).toBe(2)
    // Newest (SOURCE_NEWER, created 2026-04-27) sorted first.
    expect((rows[0] as HTMLElement).dataset.sourceId).toBe(SOURCE_NEWER.id)
    expect((rows[1] as HTMLElement).dataset.sourceId).toBe(SOURCE_OLDER.id)
  })

  it("invokes onSync with the source row when the per-row Sync now button is clicked", async () => {
    const onSync = vi.fn().mockResolvedValue(SOURCE_OLDER)
    render(<SourcesTab sources={[SOURCE_OLDER]} onSync={onSync} />)
    fireEvent.click(screen.getByTestId(`sources-tab-row-sync-${SOURCE_OLDER.id}`))
    await waitFor(() => expect(onSync).toHaveBeenCalledTimes(1))
    expect(onSync.mock.calls[0]![0]).toEqual(SOURCE_OLDER)
  })

  it("requires a confirm click before invoking onRemove", async () => {
    const onRemove = vi.fn().mockResolvedValue(undefined)
    render(<SourcesTab sources={[SOURCE_OLDER]} onRemove={onRemove} />)
    // Click Remove → confirm overlay appears, but onRemove is NOT called.
    fireEvent.click(
      screen.getByTestId(`sources-tab-row-remove-${SOURCE_OLDER.id}`),
    )
    expect(
      screen.getByTestId(`sources-tab-row-confirm-${SOURCE_OLDER.id}`),
    ).toBeTruthy()
    expect(onRemove).not.toHaveBeenCalled()
    // Click confirm → onRemove fires with the source row.
    fireEvent.click(
      screen.getByTestId(`sources-tab-row-confirm-delete-${SOURCE_OLDER.id}`),
    )
    await waitFor(() => expect(onRemove).toHaveBeenCalledTimes(1))
    expect(onRemove.mock.calls[0]![0]).toEqual(SOURCE_OLDER)
  })

  it("Cancel on the confirm overlay does not invoke onRemove", () => {
    const onRemove = vi.fn().mockResolvedValue(undefined)
    render(<SourcesTab sources={[SOURCE_OLDER]} onRemove={onRemove} />)
    fireEvent.click(
      screen.getByTestId(`sources-tab-row-remove-${SOURCE_OLDER.id}`),
    )
    fireEvent.click(
      screen.getByTestId(`sources-tab-row-confirm-cancel-${SOURCE_OLDER.id}`),
    )
    // Confirm overlay is gone.
    expect(
      screen.queryByTestId(`sources-tab-row-confirm-${SOURCE_OLDER.id}`),
    ).toBeNull()
    expect(onRemove).not.toHaveBeenCalled()
  })

  it("Add source button opens an inline form with the default refresh interval", () => {
    render(<SourcesTab sources={[]} />)
    fireEvent.click(screen.getByTestId("sources-tab-add-button"))
    const form = screen.getByTestId("sources-tab-form")
    expect(form).toBeTruthy()
    const interval = screen.getByTestId(
      "sources-tab-form-refresh-interval",
    ) as HTMLInputElement
    expect(interval.value).toBe("86400")
  })

  it("submitting the add-source form invokes onAdd with the form payload", async () => {
    const onAdd = vi
      .fn<(payload: { feedUrl: string }) => Promise<CatalogSource>>()
      .mockResolvedValue(SOURCE_NEWER)
    render(<SourcesTab sources={[]} onAdd={onAdd} />)
    fireEvent.click(screen.getByTestId("sources-tab-add-button"))
    fireEvent.change(screen.getByTestId("sources-tab-form-feed-url"), {
      target: { value: "https://feeds.example.com/x.json" },
    })
    fireEvent.click(screen.getByTestId("sources-tab-form-submit"))
    await waitFor(() => expect(onAdd).toHaveBeenCalledTimes(1))
    expect(onAdd.mock.calls[0]![0]).toEqual({
      feedUrl: "https://feeds.example.com/x.json",
      authMethod: "none",
      authSecretRef: null,
      refreshIntervalS: 86400,
    })
  })

  it("renders an inline form-error banner when onAdd throws", async () => {
    const onAdd = vi.fn().mockRejectedValue(new Error("dup feed_url"))
    render(<SourcesTab sources={[]} onAdd={onAdd} />)
    fireEvent.click(screen.getByTestId("sources-tab-add-button"))
    fireEvent.change(screen.getByTestId("sources-tab-form-feed-url"), {
      target: { value: "https://feeds.example.com/x.json" },
    })
    fireEvent.click(screen.getByTestId("sources-tab-form-submit"))
    await waitFor(() =>
      expect(screen.getByTestId("sources-tab-form-error").textContent).toMatch(
        /dup feed_url/,
      ),
    )
  })

  it("rejects an obviously-bad URL before calling onAdd", async () => {
    const onAdd = vi.fn().mockResolvedValue(SOURCE_NEWER)
    render(<SourcesTab sources={[]} onAdd={onAdd} />)
    fireEvent.click(screen.getByTestId("sources-tab-add-button"))
    fireEvent.change(screen.getByTestId("sources-tab-form-feed-url"), {
      target: { value: "ftp://x.test/feed" },
    })
    fireEvent.click(screen.getByTestId("sources-tab-form-submit"))
    await waitFor(() =>
      expect(screen.getByTestId("sources-tab-form-error")).toBeTruthy(),
    )
    expect(onAdd).not.toHaveBeenCalled()
  })

  it("renders the retry button when fetchError is supplied alongside onRetry", () => {
    const onRetry = vi.fn()
    render(
      <SourcesTab
        sources={[]}
        fetchError="boom"
        onRetry={onRetry}
      />,
    )
    expect(screen.getByTestId("sources-tab-fetch-error").textContent).toMatch(
      /boom/,
    )
    fireEvent.click(screen.getByTestId("sources-tab-fetch-retry"))
    expect(onRetry).toHaveBeenCalledTimes(1)
  })

  it("formatRefreshInterval picks the largest whole-unit fit", () => {
    expect(formatRefreshInterval(60)).toBe("1m")
    expect(formatRefreshInterval(3600)).toBe("1h")
    expect(formatRefreshInterval(86400)).toBe("1d")
    expect(formatRefreshInterval(45)).toBe("45s")
    expect(formatRefreshInterval(0)).toBe("—")
  })

  it("formatLastSyncedRelative returns 'never' for a missing timestamp", () => {
    const now = new Date("2026-04-27T10:00:00Z")
    expect(formatLastSyncedRelative(null, now)).toBe("never")
    expect(formatLastSyncedRelative(undefined, now)).toBe("never")
    expect(
      formatLastSyncedRelative("2026-04-27T08:00:00Z", now),
    ).toBe("2h ago")
    expect(
      formatLastSyncedRelative("2026-04-25T10:00:00Z", now),
    ).toBe("2d ago")
  })

  it("validateSourcesTabForm returns null only when every field is valid", () => {
    expect(
      validateSourcesTabForm({
        feedUrl: "https://x.test/a",
        authMethod: "none",
        authSecretRef: "",
        refreshIntervalS: 86400,
      }),
    ).toBeNull()
    // Missing URL.
    expect(
      validateSourcesTabForm({
        feedUrl: "",
        authMethod: "none",
        authSecretRef: "",
        refreshIntervalS: 86400,
      }),
    ).not.toBeNull()
    // Auth method needs a secret-ref.
    expect(
      validateSourcesTabForm({
        feedUrl: "https://x.test/a",
        authMethod: "bearer",
        authSecretRef: "",
        refreshIntervalS: 86400,
      }),
    ).not.toBeNull()
    // Whitespace in secret-ref.
    expect(
      validateSourcesTabForm({
        feedUrl: "https://x.test/a",
        authMethod: "bearer",
        authSecretRef: "has spaces",
        refreshIntervalS: 86400,
      }),
    ).not.toBeNull()
    // Out-of-range refresh interval.
    expect(
      validateSourcesTabForm({
        feedUrl: "https://x.test/a",
        authMethod: "none",
        authSecretRef: "",
        refreshIntervalS: 30,
      }),
    ).not.toBeNull()
  })
})

describe("BS.8.7 — SourcesTab supplementary contract", () => {
  it("toolbar count is singular for 1 source, plural for 2+", () => {
    const { rerender } = render(<SourcesTab sources={[SOURCE_OLDER]} />)
    expect(screen.getByTestId("sources-tab-count").textContent).toMatch(
      /^1 source$/,
    )
    rerender(<SourcesTab sources={[SOURCE_OLDER, SOURCE_NEWER]} />)
    expect(screen.getByTestId("sources-tab-count").textContent).toMatch(
      /^2 sources$/,
    )
  })

  it("renders the loading copy in the toolbar count when loading=true", () => {
    render(<SourcesTab sources={[]} loading />)
    expect(screen.getByTestId("sources-tab-count").textContent).toMatch(
      /Loading sources/,
    )
  })

  it("status chip surfaces the last_sync_status with a data-status attr", () => {
    render(<SourcesTab sources={[SOURCE_OLDER, SOURCE_NEWER]} />)
    const okChip = screen.getByTestId(`sources-tab-row-status-${SOURCE_OLDER.id}`)
    expect(okChip.getAttribute("data-status")).toBe("ok")
    expect(okChip.textContent).toContain("ok")
    // SOURCE_NEWER has last_sync_status=null → "never synced" copy + data-status=none.
    const neverChip = screen.getByTestId(
      `sources-tab-row-status-${SOURCE_NEWER.id}`,
    )
    expect(neverChip.getAttribute("data-status")).toBe("none")
    expect(neverChip.textContent).toMatch(/never synced/)
  })

  it("disabled subscriptions render a 'disabled' chip in the metric row", () => {
    render(<SourcesTab sources={[SOURCE_OLDER, SOURCE_NEWER]} />)
    // SOURCE_NEWER has enabled=false, SOURCE_OLDER has enabled=true.
    expect(
      screen.getByTestId(`sources-tab-row-disabled-${SOURCE_NEWER.id}`),
    ).toBeTruthy()
    expect(
      screen.queryByTestId(`sources-tab-row-disabled-${SOURCE_OLDER.id}`),
    ).toBeNull()
    const newerRow = screen.getByTestId(`sources-tab-row-${SOURCE_NEWER.id}`)
    expect(newerRow.getAttribute("data-enabled")).toBe("false")
  })

  it("Sync button is disabled when onSync is omitted (no in-context handler)", () => {
    render(<SourcesTab sources={[SOURCE_OLDER]} />)
    const sync = screen.getByTestId(
      `sources-tab-row-sync-${SOURCE_OLDER.id}`,
    ) as HTMLButtonElement
    expect(sync.disabled).toBe(true)
  })

  it("sync failure surfaces an inline error banner above the list", async () => {
    const onSync = vi.fn().mockRejectedValue(new Error("503 upstream"))
    render(<SourcesTab sources={[SOURCE_OLDER]} onSync={onSync} />)
    fireEvent.click(
      screen.getByTestId(`sources-tab-row-sync-${SOURCE_OLDER.id}`),
    )
    await waitFor(() =>
      expect(screen.getByTestId("sources-tab-error").textContent).toMatch(
        /Sync failed/,
      ),
    )
    expect(screen.getByTestId("sources-tab-error").textContent).toMatch(
      /503 upstream/,
    )
  })

  it("preset dropdown updates the refresh-interval input on change", () => {
    render(<SourcesTab sources={[]} />)
    fireEvent.click(screen.getByTestId("sources-tab-add-button"))
    const interval = screen.getByTestId(
      "sources-tab-form-refresh-interval",
    ) as HTMLInputElement
    expect(interval.value).toBe("86400")
    const preset = screen.getByTestId(
      "sources-tab-form-refresh-preset",
    ) as HTMLSelectElement
    fireEvent.change(preset, { target: { value: "3600" } })
    expect(interval.value).toBe("3600")
  })
})
