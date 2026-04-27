/**
 * BS.8.2 — Cleanup-unused modal contract tests.
 *
 * Locks the surface the platforms page wires up when the operator hits
 * the "Cleanup unused" button on the Installed tab:
 *
 *   1. ``open=false`` keeps the dialog closed — no portal mount.
 *   2. ``open=true`` opens the dialog and renders the candidate count.
 *   3. The component filters via :func:`isCleanupCandidate` — only
 *      30-day-idle entries with zero workspace dependants render.
 *   4. Empty candidate list shows the empty-state copy and disables
 *      the confirm button.
 *   5. Per-row checkbox toggles `data-selected` and updates the
 *      "Uninstall N selected" button label.
 *   6. "Select all" master toggle flips every visible row's checkbox.
 *   7. Confirm button calls `onUninstallSelected` with the exact set
 *      of selected ids and closes the operator's selection on success.
 *   8. After a successful confirm, the result banner shows X / Y.
 *   9. `onCompleted` fires AFTER the resolved promise so page wrapper
 *      can refresh its hook.
 *  10. Failure (rejected promise) surfaces an inline error and KEEPS
 *      the modal open so the operator can retry.
 *  11. `pickCleanupCandidates` is exported and matches the same filter
 *      used by the modal (toolbar count consistency).
 */

import * as React from "react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"

// Avoid the ``category-strip`` ↔ ``catalog-tab`` ESM init cycle that
// trips when modules transitively import ``catalog-tab.tsx`` (the
// modal pulls ``installed-tab.tsx`` for ``InstalledEntry`` /
// ``formatRelativeDuration``, which re-exports ``CATALOG_FAMILIES``
// from ``catalog-tab``). The catalog-tab unit test file uses the same
// stub for the same reason; replicating it here keeps the modal test
// hermetic without forcing a refactor of the catalog-tab module
// graph (out of BS.8.2 scope).
vi.mock("@/components/omnisight/category-strip", () => {
  const FAMILIES = [
    "all",
    "mobile",
    "embedded",
    "web",
    "software",
    "custom",
  ] as const
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

import {
  CleanupUnusedModal,
  pickCleanupCandidates,
} from "@/components/omnisight/cleanup-unused-modal"
import type { InstalledEntry } from "@/components/omnisight/installed-tab"
import type { BulkUninstallResponse } from "@/lib/api"

const NOW = new Date("2026-04-27T12:00:00Z")
const idle = (deltaDays: number): string =>
  new Date(NOW.getTime() - deltaDays * 24 * 60 * 60 * 1000).toISOString()

const IDLE_ENTRY: InstalledEntry = {
  id: "neural-blur-sdk",
  displayName: "Neural Blur SDK",
  vendor: "Acme",
  family: "mobile",
  version: "1.2.3",
  diskUsageBytes: 1_048_576,
  usedByWorkspaceCount: 0,
  lastUsedAt: idle(45),
  installedAt: idle(60),
}

const RECENT_ENTRY: InstalledEntry = {
  id: "fresh-runtime",
  displayName: "Fresh Runtime",
  vendor: "Acme",
  family: "software",
  version: "2.0.0",
  diskUsageBytes: 2 * 1024 * 1024,
  usedByWorkspaceCount: 0,
  lastUsedAt: idle(3),
  installedAt: idle(10),
}

const ACTIVE_ENTRY: InstalledEntry = {
  id: "in-use-toolchain",
  displayName: "In-Use Toolchain",
  vendor: "Vendor",
  family: "embedded",
  diskUsageBytes: 100,
  usedByWorkspaceCount: 5,
  lastUsedAt: idle(60),  // idle but actively depended on → excluded
  installedAt: idle(100),
}

const ANOTHER_IDLE: InstalledEntry = {
  id: "old-android-sdk",
  displayName: "Old Android SDK",
  vendor: "Google",
  family: "mobile",
  version: "30",
  diskUsageBytes: 4 * 1024 * 1024,
  usedByWorkspaceCount: 0,
  lastUsedAt: idle(90),
  installedAt: idle(180),
}

const SAMPLE_RESPONSE: BulkUninstallResponse = {
  items: [
    {
      entry_id: "neural-blur-sdk",
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

describe("BS.8.2 — CleanupUnusedModal", () => {
  it("does not mount the dialog when open=false", () => {
    const onClose = vi.fn()
    render(
      <CleanupUnusedModal
        open={false}
        entries={[IDLE_ENTRY]}
        now={NOW}
        onClose={onClose}
      />,
    )
    expect(screen.queryByTestId("cleanup-unused-modal")).toBeNull()
  })

  it("opens with the candidate count badge when open=true", () => {
    render(
      <CleanupUnusedModal
        open
        entries={[IDLE_ENTRY, RECENT_ENTRY, ACTIVE_ENTRY, ANOTHER_IDLE]}
        now={NOW}
        onClose={vi.fn()}
      />,
    )
    const modal = screen.getByTestId("cleanup-unused-modal")
    expect(modal).toBeTruthy()
    expect(modal.getAttribute("data-candidate-count")).toBe("2")
    const count = screen.getByTestId("cleanup-unused-modal-count")
    expect(count.textContent).toContain("2 idle")
  })

  it("filters out non-candidate rows (recent + active)", () => {
    render(
      <CleanupUnusedModal
        open
        entries={[IDLE_ENTRY, RECENT_ENTRY, ACTIVE_ENTRY, ANOTHER_IDLE]}
        now={NOW}
        onClose={vi.fn()}
      />,
    )
    expect(
      screen.queryByTestId(`cleanup-unused-modal-row-${IDLE_ENTRY.id}`),
    ).not.toBeNull()
    expect(
      screen.queryByTestId(`cleanup-unused-modal-row-${ANOTHER_IDLE.id}`),
    ).not.toBeNull()
    expect(
      screen.queryByTestId(`cleanup-unused-modal-row-${RECENT_ENTRY.id}`),
    ).toBeNull()
    expect(
      screen.queryByTestId(`cleanup-unused-modal-row-${ACTIVE_ENTRY.id}`),
    ).toBeNull()
  })

  it("shows the empty-state copy + disables confirm when no candidates", () => {
    render(
      <CleanupUnusedModal
        open
        entries={[RECENT_ENTRY, ACTIVE_ENTRY]}
        now={NOW}
        onClose={vi.fn()}
      />,
    )
    expect(screen.getByTestId("cleanup-unused-modal-empty")).toBeTruthy()
    const confirm = screen.getByTestId("cleanup-unused-modal-confirm") as HTMLButtonElement
    expect(confirm.disabled).toBe(true)
  })

  it("toggles a single row checkbox + updates the confirm button label", () => {
    render(
      <CleanupUnusedModal
        open
        entries={[IDLE_ENTRY, ANOTHER_IDLE]}
        now={NOW}
        onClose={vi.fn()}
      />,
    )
    const checkbox = screen.getByTestId(
      `cleanup-unused-modal-checkbox-${IDLE_ENTRY.id}`,
    )
    const confirm = screen.getByTestId("cleanup-unused-modal-confirm") as HTMLButtonElement
    expect(confirm.disabled).toBe(true)
    expect(confirm.textContent).toContain("Uninstall selected")

    fireEvent.click(checkbox)
    expect(checkbox.getAttribute("aria-pressed")).toBe("true")
    expect(confirm.disabled).toBe(false)
    expect(confirm.textContent).toContain("1 selected")

    fireEvent.click(checkbox)  // un-toggle
    expect(checkbox.getAttribute("aria-pressed")).toBe("false")
    expect(confirm.disabled).toBe(true)
  })

  it("toggle-all selects every visible candidate (and de-selects when all selected)", () => {
    render(
      <CleanupUnusedModal
        open
        entries={[IDLE_ENTRY, ANOTHER_IDLE, RECENT_ENTRY]}
        now={NOW}
        onClose={vi.fn()}
      />,
    )
    const toggleAll = screen.getByTestId("cleanup-unused-modal-toggle-all")
    fireEvent.click(toggleAll)
    expect(
      screen
        .getByTestId(`cleanup-unused-modal-checkbox-${IDLE_ENTRY.id}`)
        .getAttribute("aria-pressed"),
    ).toBe("true")
    expect(
      screen
        .getByTestId(`cleanup-unused-modal-checkbox-${ANOTHER_IDLE.id}`)
        .getAttribute("aria-pressed"),
    ).toBe("true")
    const confirm = screen.getByTestId("cleanup-unused-modal-confirm")
    expect(confirm.textContent).toContain("2 selected")

    fireEvent.click(toggleAll)
    expect(
      screen
        .getByTestId(`cleanup-unused-modal-checkbox-${IDLE_ENTRY.id}`)
        .getAttribute("aria-pressed"),
    ).toBe("false")
  })

  it("confirm calls onUninstallSelected with the selected ids and onCompleted on success", async () => {
    const onUninstall = vi.fn().mockResolvedValue(SAMPLE_RESPONSE)
    const onCompleted = vi.fn()
    const onClose = vi.fn()

    render(
      <CleanupUnusedModal
        open
        entries={[IDLE_ENTRY, ANOTHER_IDLE]}
        now={NOW}
        onClose={onClose}
        onUninstallSelected={onUninstall}
        onCompleted={onCompleted}
      />,
    )

    fireEvent.click(
      screen.getByTestId(`cleanup-unused-modal-checkbox-${IDLE_ENTRY.id}`),
    )
    fireEvent.click(screen.getByTestId("cleanup-unused-modal-confirm"))

    await waitFor(() => expect(onUninstall).toHaveBeenCalledTimes(1))
    expect(onUninstall.mock.calls[0]![0]).toEqual([IDLE_ENTRY.id])
    expect(onCompleted).toHaveBeenCalledTimes(1)
    expect(onCompleted.mock.calls[0]![0]).toEqual(SAMPLE_RESPONSE)

    // Selection cleared + result banner rendered.
    await waitFor(() =>
      expect(screen.queryByTestId("cleanup-unused-modal-result")).not.toBeNull(),
    )
    const banner = screen.getByTestId("cleanup-unused-modal-result")
    expect(banner.textContent).toContain("Uninstalled 1")
    expect(banner.textContent).toContain("rejected 0")
    // The modal stays open so the operator can confirm what happened;
    // the page wrapper closes via the Close button (not auto-close).
    expect(onClose).not.toHaveBeenCalled()
  })

  it("renders an inline error banner on rejected uninstall and stays open", async () => {
    const onUninstall = vi.fn().mockRejectedValue(new Error("pep_denied: tier_unlisted"))
    const onClose = vi.fn()

    render(
      <CleanupUnusedModal
        open
        entries={[IDLE_ENTRY]}
        now={NOW}
        onClose={onClose}
        onUninstallSelected={onUninstall}
      />,
    )
    fireEvent.click(
      screen.getByTestId(`cleanup-unused-modal-checkbox-${IDLE_ENTRY.id}`),
    )
    fireEvent.click(screen.getByTestId("cleanup-unused-modal-confirm"))

    await waitFor(() =>
      expect(screen.queryByTestId("cleanup-unused-modal-error")).not.toBeNull(),
    )
    const err = screen.getByTestId("cleanup-unused-modal-error")
    expect(err.textContent).toContain("pep_denied")
    expect(onClose).not.toHaveBeenCalled()
    // The selection survives so the operator can re-trigger after
    // approving the next coaching card.
    expect(
      screen
        .getByTestId(`cleanup-unused-modal-checkbox-${IDLE_ENTRY.id}`)
        .getAttribute("aria-pressed"),
    ).toBe("true")
  })

  it("clicking Close calls onClose and resets transient state on next open", () => {
    const onClose = vi.fn()
    const { rerender } = render(
      <CleanupUnusedModal
        open
        entries={[IDLE_ENTRY]}
        now={NOW}
        onClose={onClose}
      />,
    )
    fireEvent.click(
      screen.getByTestId(`cleanup-unused-modal-checkbox-${IDLE_ENTRY.id}`),
    )
    fireEvent.click(screen.getByTestId("cleanup-unused-modal-close"))
    expect(onClose).toHaveBeenCalledTimes(1)
    rerender(
      <CleanupUnusedModal
        open
        entries={[IDLE_ENTRY]}
        now={NOW}
        onClose={onClose}
      />,
    )
    // Selection cleared after close (resetting transient state).
    expect(
      screen
        .getByTestId(`cleanup-unused-modal-checkbox-${IDLE_ENTRY.id}`)
        .getAttribute("aria-pressed"),
    ).toBe("false")
  })

  it("pickCleanupCandidates matches the modal's filter exactly (toolbar count consistency)", () => {
    const result = pickCleanupCandidates(
      [IDLE_ENTRY, RECENT_ENTRY, ACTIVE_ENTRY, ANOTHER_IDLE],
      NOW,
    )
    expect(result.map((e) => e.id)).toEqual([IDLE_ENTRY.id, ANOTHER_IDLE.id])
  })
})
