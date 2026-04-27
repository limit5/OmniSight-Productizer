/**
 * BS.8.4 — Uninstall confirm modal contract tests.
 *
 * Locks the surface the platforms page wires up when the operator clicks
 * the per-row "Uninstall" overflow action on the Installed tab:
 *
 *   1. ``open=false`` keeps the dialog closed — no portal mount.
 *   2. ``open=true`` with ``entry=null`` keeps the dialog closed.
 *   3. On open, the modal calls `onFetchDependents` with the entry id
 *      exactly once.
 *   4. Empty dependents list shows the "safe to proceed" copy + the
 *      primary destructive button is the visible action.
 *   5. Non-empty dependents list shows the warning headline + per-row
 *      list and the primary action becomes the amber "I understand"
 *      acknowledgement button (gates the destructive submit).
 *   6. After acknowledging, the destructive submit button appears and
 *      can fire the uninstall.
 *   7. Submit calls `onUninstallConfirmed` with the entry id, and on
 *      success fires `onCompleted` + renders the result banner.
 *   8. Submit failure renders an inline error and keeps the modal open.
 *   9. Fetch failure renders an amber "could not load dependents" banner
 *      and surfaces the destructive submit (so the operator can still
 *      proceed if they accept the unknown).
 *  10. Cancel button calls `onClose` and resets transient state on
 *      next open.
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

// Same category-strip stub the cleanup-unused-modal test uses — the
// modal pulls `installed-tab.tsx` for `InstalledEntry` which transitively
// re-exports from `catalog-tab.tsx`. Replicating the stub keeps the
// uninstall-confirm test hermetic.
vi.mock("@/components/omnisight/category-strip", () => {
  const FAMILIES = ["all", "mobile", "embedded", "web", "software", "custom"] as const
  return {
    CategoryStrip: ({
      family,
      onSelect,
    }: {
      family: string
      onSelect: (next: string) => void
    }) =>
      React.createElement(
        "div",
        { "data-testid": "category-strip", "data-active-family": family },
        FAMILIES.map((f) =>
          React.createElement(
            "button",
            {
              key: f,
              type: "button",
              "aria-pressed": family === f,
              onClick: () => onSelect(f),
            },
            f,
          ),
        ),
      ),
    CATEGORY_STRIP_FAMILIES: FAMILIES,
    getCategoryStripPalette: () => ({}),
  }
})

import { UninstallConfirmModal } from "@/components/omnisight/uninstall-confirm-modal"
import type { InstalledEntry } from "@/components/omnisight/installed-tab"
import type {
  BulkUninstallResponse,
  InstalledEntryRow,
  ListEntryDependentsResponse,
} from "@/lib/api"

const TARGET: InstalledEntry = {
  id: "android-sdk-base",
  displayName: "Android SDK Base",
  vendor: "Google",
  family: "mobile",
  version: "34",
  diskUsageBytes: 256 * 1024 * 1024,
  usedByWorkspaceCount: 0,
}

const DEP_ROW_A: InstalledEntryRow = {
  entry_id: "neural-blur-sdk",
  display_name: "Neural Blur SDK",
  vendor: "Acme",
  family: "mobile",
  version: "1.2.3",
  description: null,
  disk_usage_bytes: 1024,
  used_by_workspace_count: 0,
  last_used_at: null,
  installed_at: "2026-04-25T08:00:00Z",
  update_available: false,
  available_version: null,
  source: "operator",
}

const DEP_ROW_B: InstalledEntryRow = {
  entry_id: "android-emulator-skin",
  display_name: "Android Emulator Skin",
  vendor: "Google",
  family: "mobile",
  version: "1.0",
  description: null,
  disk_usage_bytes: 4096,
  used_by_workspace_count: 0,
  last_used_at: null,
  installed_at: "2026-04-26T08:00:00Z",
  update_available: false,
  available_version: null,
  source: "shipped",
}

const EMPTY_DEPENDENTS: ListEntryDependentsResponse = {
  entry_id: TARGET.id,
  items: [],
  count: 0,
}

const TWO_DEPENDENTS: ListEntryDependentsResponse = {
  entry_id: TARGET.id,
  items: [DEP_ROW_A, DEP_ROW_B],
  count: 2,
}

const APPROVE_RESPONSE: BulkUninstallResponse = {
  items: [
    {
      entry_id: TARGET.id,
      job_id: "ij-aaaaaaaaaaaa",
      action: "approved",
      state: "completed",
      reason: null,
      pep_decision_id: "de-deadbeefcafe",
    },
  ],
  approved_count: 1,
  denied_count: 0,
  pep_decision_id: "de-deadbeefcafe",
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe("BS.8.4 — UninstallConfirmModal", () => {
  it("does not mount the dialog when open=false", () => {
    render(
      <UninstallConfirmModal
        open={false}
        entry={TARGET}
        onClose={vi.fn()}
        onFetchDependents={vi.fn()}
      />,
    )
    expect(screen.queryByTestId("uninstall-confirm-modal")).toBeNull()
  })

  it("does not mount the dialog when entry=null even if open=true", () => {
    render(
      <UninstallConfirmModal
        open
        entry={null}
        onClose={vi.fn()}
        onFetchDependents={vi.fn()}
      />,
    )
    expect(screen.queryByTestId("uninstall-confirm-modal")).toBeNull()
  })

  it("calls onFetchDependents exactly once with the entry id when opened", async () => {
    const onFetch = vi.fn().mockResolvedValue(EMPTY_DEPENDENTS)
    render(
      <UninstallConfirmModal
        open
        entry={TARGET}
        onClose={vi.fn()}
        onFetchDependents={onFetch}
      />,
    )
    await waitFor(() => expect(onFetch).toHaveBeenCalledTimes(1))
    expect(onFetch.mock.calls[0]![0]).toBe(TARGET.id)
  })

  it("renders the no-dependents banner + primary destructive submit when dependents=[]", async () => {
    const onFetch = vi.fn().mockResolvedValue(EMPTY_DEPENDENTS)
    render(
      <UninstallConfirmModal
        open
        entry={TARGET}
        onClose={vi.fn()}
        onFetchDependents={onFetch}
      />,
    )
    await waitFor(() =>
      expect(
        screen.queryByTestId("uninstall-confirm-modal-no-dependents"),
      ).not.toBeNull(),
    )
    const modal = screen.getByTestId("uninstall-confirm-modal")
    expect(modal.getAttribute("data-dependent-count")).toBe("0")
    expect(modal.getAttribute("data-data-state")).toBe("no-dependents")
    // The destructive submit is the primary action (no acknowledge gate).
    expect(
      screen.queryByTestId("uninstall-confirm-modal-confirm"),
    ).not.toBeNull()
    expect(
      screen.queryByTestId("uninstall-confirm-modal-acknowledge"),
    ).toBeNull()
  })

  it("renders the dependents warning + acknowledge button when dependents>0", async () => {
    const onFetch = vi.fn().mockResolvedValue(TWO_DEPENDENTS)
    render(
      <UninstallConfirmModal
        open
        entry={TARGET}
        onClose={vi.fn()}
        onFetchDependents={onFetch}
      />,
    )
    await waitFor(() =>
      expect(
        screen.queryByTestId("uninstall-confirm-modal-dependents-warning"),
      ).not.toBeNull(),
    )
    const headline = screen.getByTestId(
      "uninstall-confirm-modal-dependents-headline",
    )
    expect(headline.textContent).toContain("2 other installed")
    expect(headline.textContent).toContain("entries depend")
    // Per-row dependents are rendered with stable testids.
    expect(
      screen.queryByTestId(`uninstall-confirm-modal-dependent-${DEP_ROW_A.entry_id}`),
    ).not.toBeNull()
    expect(
      screen.queryByTestId(`uninstall-confirm-modal-dependent-${DEP_ROW_B.entry_id}`),
    ).not.toBeNull()
    // Acknowledge gate is the primary action; destructive submit is hidden.
    expect(
      screen.queryByTestId("uninstall-confirm-modal-acknowledge"),
    ).not.toBeNull()
    expect(
      screen.queryByTestId("uninstall-confirm-modal-confirm"),
    ).toBeNull()
  })

  it("acknowledging unlocks the destructive submit", async () => {
    const onFetch = vi.fn().mockResolvedValue(TWO_DEPENDENTS)
    render(
      <UninstallConfirmModal
        open
        entry={TARGET}
        onClose={vi.fn()}
        onFetchDependents={onFetch}
      />,
    )
    await waitFor(() =>
      expect(
        screen.queryByTestId("uninstall-confirm-modal-acknowledge"),
      ).not.toBeNull(),
    )
    fireEvent.click(screen.getByTestId("uninstall-confirm-modal-acknowledge"))
    // After acknowledging, acknowledge button gone, destructive shown.
    expect(
      screen.queryByTestId("uninstall-confirm-modal-acknowledge"),
    ).toBeNull()
    expect(
      screen.queryByTestId("uninstall-confirm-modal-confirm"),
    ).not.toBeNull()
    expect(
      screen.getByTestId("uninstall-confirm-modal").getAttribute("data-confirmed"),
    ).toBe("true")
  })

  it("submit calls onUninstallConfirmed with the entry id and onCompleted on success", async () => {
    const onFetch = vi.fn().mockResolvedValue(EMPTY_DEPENDENTS)
    const onUninstall = vi.fn().mockResolvedValue(APPROVE_RESPONSE)
    const onCompleted = vi.fn()
    const onClose = vi.fn()

    render(
      <UninstallConfirmModal
        open
        entry={TARGET}
        onClose={onClose}
        onFetchDependents={onFetch}
        onUninstallConfirmed={onUninstall}
        onCompleted={onCompleted}
      />,
    )
    await waitFor(() =>
      expect(
        screen.queryByTestId("uninstall-confirm-modal-no-dependents"),
      ).not.toBeNull(),
    )

    fireEvent.click(screen.getByTestId("uninstall-confirm-modal-confirm"))

    await waitFor(() => expect(onUninstall).toHaveBeenCalledTimes(1))
    expect(onUninstall.mock.calls[0]![0]).toBe(TARGET.id)
    expect(onCompleted).toHaveBeenCalledTimes(1)
    expect(onCompleted.mock.calls[0]![0]).toEqual(APPROVE_RESPONSE)

    await waitFor(() =>
      expect(
        screen.queryByTestId("uninstall-confirm-modal-result"),
      ).not.toBeNull(),
    )
    const banner = screen.getByTestId("uninstall-confirm-modal-result")
    expect(banner.textContent).toContain("Uninstalled 1")
    // Modal stays open until operator clicks Close.
    expect(onClose).not.toHaveBeenCalled()
  })

  it("renders an inline error and keeps the modal open on rejected uninstall", async () => {
    const onFetch = vi.fn().mockResolvedValue(EMPTY_DEPENDENTS)
    const onUninstall = vi
      .fn()
      .mockRejectedValue(new Error("pep_denied: tier_unlisted"))
    const onClose = vi.fn()

    render(
      <UninstallConfirmModal
        open
        entry={TARGET}
        onClose={onClose}
        onFetchDependents={onFetch}
        onUninstallConfirmed={onUninstall}
      />,
    )
    await waitFor(() =>
      expect(
        screen.queryByTestId("uninstall-confirm-modal-no-dependents"),
      ).not.toBeNull(),
    )
    fireEvent.click(screen.getByTestId("uninstall-confirm-modal-confirm"))

    await waitFor(() =>
      expect(
        screen.queryByTestId("uninstall-confirm-modal-error"),
      ).not.toBeNull(),
    )
    const err = screen.getByTestId("uninstall-confirm-modal-error")
    expect(err.textContent).toContain("pep_denied")
    expect(onClose).not.toHaveBeenCalled()
  })

  it("surfaces a fetch-error banner when the dependents fetch rejects", async () => {
    const onFetch = vi.fn().mockRejectedValue(new Error("network down"))
    render(
      <UninstallConfirmModal
        open
        entry={TARGET}
        onClose={vi.fn()}
        onFetchDependents={onFetch}
      />,
    )
    await waitFor(() =>
      expect(
        screen.queryByTestId("uninstall-confirm-modal-fetch-error"),
      ).not.toBeNull(),
    )
    const banner = screen.getByTestId("uninstall-confirm-modal-fetch-error")
    expect(banner.textContent).toContain("network down")
    // Destructive submit is still rendered so an admin can proceed.
    expect(
      screen.queryByTestId("uninstall-confirm-modal-confirm"),
    ).not.toBeNull()
  })

  it("clicking Cancel calls onClose and resets confirmed state on next open", async () => {
    const onFetch = vi.fn().mockResolvedValue(TWO_DEPENDENTS)
    const onClose = vi.fn()
    const { rerender } = render(
      <UninstallConfirmModal
        open
        entry={TARGET}
        onClose={onClose}
        onFetchDependents={onFetch}
      />,
    )
    await waitFor(() =>
      expect(
        screen.queryByTestId("uninstall-confirm-modal-acknowledge"),
      ).not.toBeNull(),
    )
    fireEvent.click(screen.getByTestId("uninstall-confirm-modal-acknowledge"))
    fireEvent.click(screen.getByTestId("uninstall-confirm-modal-close"))
    expect(onClose).toHaveBeenCalledTimes(1)

    // Re-open: the acknowledged-flag must be cleared so the warning gate
    // fires again on the next operator interaction.
    onFetch.mockResolvedValueOnce(TWO_DEPENDENTS)
    rerender(
      <UninstallConfirmModal
        open={false}
        entry={TARGET}
        onClose={onClose}
        onFetchDependents={onFetch}
      />,
    )
    rerender(
      <UninstallConfirmModal
        open
        entry={TARGET}
        onClose={onClose}
        onFetchDependents={onFetch}
      />,
    )
    await waitFor(() =>
      expect(
        screen.queryByTestId("uninstall-confirm-modal-acknowledge"),
      ).not.toBeNull(),
    )
    expect(
      screen.getByTestId("uninstall-confirm-modal").getAttribute("data-confirmed"),
    ).toBe("false")
  })
})
