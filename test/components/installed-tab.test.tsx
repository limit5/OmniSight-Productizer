/**
 * BS.8.7 — InstalledTab contract tests.
 *
 * Locks the surface BS.8.1 ships and the platforms page wires up:
 *   1. Empty list renders the "No installed entries yet" hint.
 *   2. Toolbar surfaces the count + aggregate disk total.
 *   3. Disk total flips to "—" when no entry has a known size.
 *   4. Sort dropdown changes the rendered row order (size-desc).
 *   5. last-used-desc sort puts most-recent first; never-used last.
 *   6. coerceInstalledTabSort tolerates unknown values.
 *   7. sortInstalledEntries returns a fresh array (no in-place mutation).
 *   8. formatRelativeDuration covers "never", seconds, hours, days.
 *   9. Per-row metric column shows disk / workspace count / last-used,
 *      with "1 workspace" vs "N workspaces" pluralisation.
 *  10. Update-available chip renders only when updateAvailable=true.
 *  11. Family chip uses the catalog-tab vocabulary palette.
 *  12. Per-row dropdown menu fires the matching action callback when
 *      the operator picks an item (opened via pointer events).
 *  13. Update menu item disabled when updateAvailable=false even if
 *      onUpdate is supplied.
 *  14. Uninstall menu item disabled when onUninstall is omitted.
 */

import * as React from "react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react"

// Same ESM init-cycle stub the cleanup-unused-modal test uses — installed-tab
// imports CATALOG_FAMILIES from catalog-tab, which transitively imports
// category-strip, which re-imports CATALOG_FAMILIES at module-init time.
// Stubbing category-strip breaks the cycle without forcing a refactor.
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
  InstalledTab,
  coerceInstalledTabSort,
  formatRelativeDuration,
  sortInstalledEntries,
  type InstalledEntry,
} from "@/components/omnisight/installed-tab"

const NOW = new Date("2026-04-27T12:00:00Z")

const ALPHA: InstalledEntry = {
  id: "alpha-sdk",
  displayName: "Alpha SDK",
  vendor: "Acme",
  family: "embedded",
  version: "1.0.0",
  diskUsageBytes: 5 * 1024 * 1024 * 1024, // 5 GiB
  usedByWorkspaceCount: 3,
  lastUsedAt: "2026-04-27T10:00:00Z", // 2h ago
  installedAt: "2026-04-01T10:00:00Z",
  updateAvailable: false,
}

const BRAVO: InstalledEntry = {
  id: "bravo-runtime",
  displayName: "Bravo Runtime",
  vendor: "Acme",
  family: "software",
  version: "2.0.0",
  diskUsageBytes: 100 * 1024 * 1024, // 100 MiB
  usedByWorkspaceCount: 1,
  lastUsedAt: "2026-04-26T12:00:00Z", // 1d ago
  installedAt: "2026-04-15T10:00:00Z",
  updateAvailable: true,
  availableVersion: "2.1.0",
}

const CHARLIE: InstalledEntry = {
  id: "charlie-tools",
  displayName: "Charlie Tools",
  vendor: "Vendor",
  family: "custom",
  version: "0.5.0",
  diskUsageBytes: null, // unknown size
  usedByWorkspaceCount: 0,
  lastUsedAt: null, // never used
  installedAt: "2026-03-10T10:00:00Z",
}

/**
 * Open a Radix DropdownMenu trigger inside jsdom. Plain `fireEvent.click`
 * is not enough — Radix listens on pointer events, so the trigger only
 * commits the open state when pointerDown + pointerUp + click are dispatched
 * in sequence. Wrapping in `act` keeps React 18 happy.
 */
function openDropdown(trigger: HTMLElement) {
  act(() => {
    fireEvent.pointerDown(trigger, { pointerType: "mouse", button: 0 })
    fireEvent.pointerUp(trigger, { pointerType: "mouse", button: 0 })
    fireEvent.click(trigger)
  })
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe("BS.8.7 — InstalledTab", () => {
  it("renders the empty hint when no entries are installed", () => {
    render(<InstalledTab entries={[]} />)
    expect(screen.getByTestId("installed-tab-empty")).toBeTruthy()
    expect(screen.queryByTestId("installed-tab-list")).toBeNull()
    const count = screen.getByTestId("installed-tab-result-count")
    expect(count.textContent).toMatch(/0 \/ 0 installed/)
  })

  it("toolbar surfaces the visible/total count and the aggregate disk total", () => {
    render(<InstalledTab entries={[ALPHA, BRAVO]} now={NOW} />)
    expect(screen.getByTestId("installed-tab-result-count").textContent).toMatch(
      /2 \/ 2 installed/,
    )
    const diskTotal = screen.getByTestId("installed-tab-disk-total")
    expect(diskTotal.getAttribute("data-disk-usage-known")).toBe("true")
    // 5 GiB + 100 MiB ⇒ formatInstallBytes picks the largest whole unit.
    expect(diskTotal.textContent).toMatch(/disk · /)
    expect(diskTotal.textContent).not.toContain("—")
  })

  it("flips disk total to em-dash when no row reports a finite size", () => {
    render(<InstalledTab entries={[CHARLIE]} now={NOW} />)
    const diskTotal = screen.getByTestId("installed-tab-disk-total")
    expect(diskTotal.getAttribute("data-disk-usage-known")).toBe("false")
    expect(diskTotal.textContent).toContain("—")
  })

  it("sort=size-desc orders rows by disk usage (large first); unknown sizes last", () => {
    render(<InstalledTab entries={[BRAVO, CHARLIE, ALPHA]} now={NOW} />)
    const select = screen.getByTestId(
      "installed-tab-sort-select",
    ) as HTMLSelectElement
    fireEvent.change(select, { target: { value: "size-desc" } })
    expect(select.value).toBe("size-desc")
    const list = screen.getByTestId("installed-tab-list")
    const rows = list.querySelectorAll("li[data-entry-id]")
    expect(rows.length).toBe(3)
    expect((rows[0] as HTMLElement).dataset.entryId).toBe(ALPHA.id)
    expect((rows[1] as HTMLElement).dataset.entryId).toBe(BRAVO.id)
    expect((rows[2] as HTMLElement).dataset.entryId).toBe(CHARLIE.id)
  })

  it("sort=last-used-desc puts most-recent first; never-used row last", () => {
    render(<InstalledTab entries={[CHARLIE, BRAVO, ALPHA]} now={NOW} />)
    const select = screen.getByTestId(
      "installed-tab-sort-select",
    ) as HTMLSelectElement
    fireEvent.change(select, { target: { value: "last-used-desc" } })
    const list = screen.getByTestId("installed-tab-list")
    const rows = list.querySelectorAll("li[data-entry-id]")
    expect((rows[0] as HTMLElement).dataset.entryId).toBe(ALPHA.id) // 2h ago
    expect((rows[1] as HTMLElement).dataset.entryId).toBe(BRAVO.id) // 1d ago
    expect((rows[2] as HTMLElement).dataset.entryId).toBe(CHARLIE.id) // never
  })

  it("coerceInstalledTabSort tolerates unknown / null inputs", () => {
    expect(coerceInstalledTabSort(null)).toBe("name-asc")
    expect(coerceInstalledTabSort(undefined)).toBe("name-asc")
    expect(coerceInstalledTabSort("garbage")).toBe("name-asc")
    expect(coerceInstalledTabSort("size-desc")).toBe("size-desc")
    expect(coerceInstalledTabSort("name-desc")).toBe("name-desc")
  })

  it("sortInstalledEntries returns a fresh array (no in-place mutation)", () => {
    const input: ReadonlyArray<InstalledEntry> = [BRAVO, ALPHA]
    const out = sortInstalledEntries(input, "name-asc")
    expect(out).not.toBe(input)
    // Source array unchanged.
    expect(input[0]!.id).toBe(BRAVO.id)
    expect(input[1]!.id).toBe(ALPHA.id)
    // Output sorted A→Z.
    expect(out[0]!.id).toBe(ALPHA.id)
    expect(out[1]!.id).toBe(BRAVO.id)
  })

  it("formatRelativeDuration reads 'never', seconds, hours, days correctly", () => {
    expect(formatRelativeDuration(null, NOW)).toBe("never")
    expect(formatRelativeDuration(undefined, NOW)).toBe("never")
    expect(formatRelativeDuration("not-a-date", NOW)).toBe("never")
    expect(formatRelativeDuration(NOW.toISOString(), NOW)).toBe("0s ago")
    expect(formatRelativeDuration("2026-04-27T11:30:00Z", NOW)).toBe("30m ago")
    expect(formatRelativeDuration("2026-04-27T10:00:00Z", NOW)).toBe("2h ago")
    expect(formatRelativeDuration("2026-04-25T12:00:00Z", NOW)).toBe("2d ago")
    expect(formatRelativeDuration("2026-02-01T12:00:00Z", NOW)).toBe("2mo ago")
  })

  it("per-row metric column shows disk / workspace count / last-used", () => {
    render(<InstalledTab entries={[ALPHA, BRAVO]} now={NOW} />)
    // ALPHA: 3 workspaces, 2h ago.
    const usedAlpha = screen.getByTestId(`installed-tab-row-usedby-${ALPHA.id}`)
    expect(usedAlpha.textContent).toMatch(/3 workspaces/)
    const lastAlpha = screen.getByTestId(`installed-tab-row-lastused-${ALPHA.id}`)
    expect(lastAlpha.textContent).toMatch(/2h ago/)
    // BRAVO: 1 workspace (singular), 1d ago.
    const usedBravo = screen.getByTestId(`installed-tab-row-usedby-${BRAVO.id}`)
    expect(usedBravo.textContent).toMatch(/1 workspace$/)
    const lastBravo = screen.getByTestId(`installed-tab-row-lastused-${BRAVO.id}`)
    expect(lastBravo.textContent).toMatch(/1d ago/)
  })

  it("update-available chip renders only when updateAvailable=true", () => {
    render(<InstalledTab entries={[ALPHA, BRAVO, CHARLIE]} now={NOW} />)
    // BRAVO has updateAvailable=true with availableVersion=2.1.0 → chip shown.
    const chip = screen.getByTestId(`installed-tab-row-update-chip-${BRAVO.id}`)
    expect(chip.textContent).toMatch(/v2\.1\.0/)
    // ALPHA / CHARLIE no chip.
    expect(
      screen.queryByTestId(`installed-tab-row-update-chip-${ALPHA.id}`),
    ).toBeNull()
    expect(
      screen.queryByTestId(`installed-tab-row-update-chip-${CHARLIE.id}`),
    ).toBeNull()
    // The row also flips its data-update-available attr.
    const bravoRow = screen.getByTestId(`installed-tab-row-${BRAVO.id}`)
    expect(bravoRow.getAttribute("data-update-available")).toBe("true")
    const alphaRow = screen.getByTestId(`installed-tab-row-${ALPHA.id}`)
    expect(alphaRow.getAttribute("data-update-available")).toBe("false")
  })

  it("family chip uses the catalog-tab vocabulary palette", () => {
    render(<InstalledTab entries={[ALPHA, CHARLIE]} now={NOW} />)
    const alphaFamily = screen.getByTestId(
      `installed-tab-row-family-${ALPHA.id}`,
    )
    expect(alphaFamily.textContent).toBe("Embedded")
    const charlieFamily = screen.getByTestId(
      `installed-tab-row-family-${CHARLIE.id}`,
    )
    expect(charlieFamily.textContent).toBe("Custom")
    // Row data-attr matches.
    const alphaRow = screen.getByTestId(`installed-tab-row-${ALPHA.id}`)
    expect(alphaRow.getAttribute("data-entry-family")).toBe("embedded")
  })

  it("opens the per-row ⋮ menu and fires the matching callback on each action", () => {
    const onViewLog = vi.fn()
    const onReinstall = vi.fn()
    render(
      <InstalledTab
        entries={[ALPHA]}
        onViewLog={onViewLog}
        onReinstall={onReinstall}
        now={NOW}
      />,
    )
    const trigger = screen.getByTestId(`installed-tab-row-actions-${ALPHA.id}`)
    openDropdown(trigger)
    // After the dropdown is open the portal-content menu items are queryable.
    const viewLog = screen.getByTestId(
      `installed-tab-row-action-viewlog-${ALPHA.id}`,
    )
    expect(viewLog).toBeTruthy()
    act(() => {
      fireEvent.click(viewLog)
    })
    expect(onViewLog).toHaveBeenCalledTimes(1)
    expect(onViewLog.mock.calls[0]![0]).toEqual(ALPHA)
    // Re-open the menu for the second action — first click closed the menu
    // because a Radix DropdownMenuItem auto-dismisses on select.
    openDropdown(trigger)
    const reinstall = screen.getByTestId(
      `installed-tab-row-action-reinstall-${ALPHA.id}`,
    )
    act(() => {
      fireEvent.click(reinstall)
    })
    expect(onReinstall).toHaveBeenCalledTimes(1)
    expect(onReinstall.mock.calls[0]![0]).toEqual(ALPHA)
  })

  it("Update menu item is disabled when updateAvailable=false (even with onUpdate)", () => {
    const onUpdate = vi.fn()
    render(
      <InstalledTab entries={[ALPHA]} onUpdate={onUpdate} now={NOW} />,
    )
    const trigger = screen.getByTestId(`installed-tab-row-actions-${ALPHA.id}`)
    openDropdown(trigger)
    const update = screen.getByTestId(
      `installed-tab-row-action-update-${ALPHA.id}`,
    ) as HTMLElement
    // Radix flips data-disabled / aria-disabled — assert via attribute.
    expect(
      update.getAttribute("data-disabled") !== null ||
        update.getAttribute("aria-disabled") === "true",
    ).toBe(true)
    // Even forcing a click doesn't fire onUpdate.
    act(() => {
      fireEvent.click(update)
    })
    expect(onUpdate).not.toHaveBeenCalled()
  })

  it("Uninstall menu item is disabled when onUninstall is omitted", () => {
    render(<InstalledTab entries={[ALPHA]} now={NOW} />)
    const trigger = screen.getByTestId(`installed-tab-row-actions-${ALPHA.id}`)
    openDropdown(trigger)
    const uninstall = screen.getByTestId(
      `installed-tab-row-action-uninstall-${ALPHA.id}`,
    ) as HTMLElement
    expect(
      uninstall.getAttribute("data-disabled") !== null ||
        uninstall.getAttribute("aria-disabled") === "true",
    ).toBe(true)
  })
})
