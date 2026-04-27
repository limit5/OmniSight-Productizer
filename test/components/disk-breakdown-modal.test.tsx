/**
 * BS.8.3 — Disk-breakdown modal contract tests.
 *
 * Locks the surface the platforms page wires up when the operator hits
 * the "Disk breakdown" button on the Installed tab:
 *
 *   1. ``open=false`` keeps the dialog closed — no portal mount.
 *   2. ``open=true`` opens the dialog and renders a per-family treemap.
 *   3. Family bucketing aggregates entries by ``family`` and sums the
 *      bytes; treemap cells appear in size-descending order.
 *   4. Each treemap cell carries the entry's bytes / share via
 *      ``data-entry-bytes`` / ``data-entry-share`` so visual contracts
 *      are introspectable.
 *   5. Entries with unknown disk usage are listed in the "Unknown size"
 *      footer rather than silently dropped.
 *   6. Empty entries / all-unknown entries surface the empty-state copy.
 *   7. Close button + Esc / overlay click invoke ``onClose``.
 *   8. ``computeFamilyTotals`` + ``groupByFamilyForTreemap`` +
 *      ``formatShare`` + ``entryDiskBytes`` are exported and behave
 *      deterministically.
 */

import * as React from "react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { cleanup, fireEvent, render, screen } from "@testing-library/react"

// Same ESM-cycle stub the cleanup-unused-modal test uses — the modal
// transitively imports `installed-tab.tsx` (for the `InstalledEntry`
// type re-export) which re-exports `CATALOG_FAMILIES` from
// `catalog-tab.tsx`, and `catalog-tab.tsx` imports `category-strip.tsx`.
// Stub `category-strip` to keep the test hermetic without forcing a
// refactor of the catalog-tab module graph.
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
  DiskBreakdownModal,
  computeFamilyTotals,
  groupByFamilyForTreemap,
  entryDiskBytes,
  formatShare,
} from "@/components/omnisight/disk-breakdown-modal"
import type { InstalledEntry } from "@/components/omnisight/installed-tab"

const MB = 1024 * 1024

const ENTRY_MOBILE_LARGE: InstalledEntry = {
  id: "neural-blur-sdk",
  displayName: "Neural Blur SDK",
  vendor: "Acme",
  family: "mobile",
  version: "1.2.3",
  diskUsageBytes: 800 * MB,
}

const ENTRY_MOBILE_SMALL: InstalledEntry = {
  id: "android-tools",
  displayName: "Android Tools",
  vendor: "Google",
  family: "mobile",
  version: "33",
  diskUsageBytes: 200 * MB,
}

const ENTRY_EMBEDDED: InstalledEntry = {
  id: "yocto-bsp",
  displayName: "Yocto BSP",
  vendor: "Yocto",
  family: "embedded",
  diskUsageBytes: 500 * MB,
}

const ENTRY_WEB: InstalledEntry = {
  id: "node-runtime",
  displayName: "Node Runtime",
  vendor: "Node",
  family: "web",
  diskUsageBytes: 100 * MB,
}

const ENTRY_UNKNOWN: InstalledEntry = {
  id: "legacy-thing",
  displayName: "Legacy Thing",
  vendor: "Legacy",
  family: "custom",
  // diskUsageBytes intentionally omitted to exercise the unknown bucket.
}

const ENTRY_NEGATIVE: InstalledEntry = {
  id: "broken-row",
  displayName: "Broken Row",
  vendor: "?",
  family: "software",
  diskUsageBytes: -1,
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe("BS.8.3 — DiskBreakdownModal", () => {
  it("does not mount the dialog when open=false", () => {
    render(
      <DiskBreakdownModal
        open={false}
        entries={[ENTRY_MOBILE_LARGE]}
        onClose={vi.fn()}
      />,
    )
    expect(screen.queryByTestId("disk-breakdown-modal")).toBeNull()
  })

  it("opens with a header total badge totalling the known bytes", () => {
    render(
      <DiskBreakdownModal
        open
        entries={[ENTRY_MOBILE_LARGE, ENTRY_MOBILE_SMALL, ENTRY_EMBEDDED]}
        onClose={vi.fn()}
      />,
    )
    const modal = screen.getByTestId("disk-breakdown-modal")
    expect(modal.getAttribute("data-known-entry-count")).toBe("3")
    expect(modal.getAttribute("data-family-count")).toBe("2")
    // 800 MB + 200 MB + 500 MB = 1500 MiB → 1.5 GB
    expect(Number(modal.getAttribute("data-total-bytes"))).toBe(1500 * MB)
    expect(screen.getByTestId("disk-breakdown-modal-total").textContent).toMatch(/1\.5 GB/)
  })

  it("renders one treemap cell per known entry and skips unknown rows", () => {
    render(
      <DiskBreakdownModal
        open
        entries={[
          ENTRY_MOBILE_LARGE,
          ENTRY_MOBILE_SMALL,
          ENTRY_EMBEDDED,
          ENTRY_UNKNOWN,
          ENTRY_NEGATIVE,
        ]}
        onClose={vi.fn()}
      />,
    )
    const tree = screen.getByTestId("disk-breakdown-modal-treemap")
    expect(tree).toBeTruthy()
    // 3 known entries → 3 cells.
    expect(
      screen.queryByTestId(`disk-breakdown-modal-cell-${ENTRY_MOBILE_LARGE.id}`),
    ).not.toBeNull()
    expect(
      screen.queryByTestId(`disk-breakdown-modal-cell-${ENTRY_MOBILE_SMALL.id}`),
    ).not.toBeNull()
    expect(
      screen.queryByTestId(`disk-breakdown-modal-cell-${ENTRY_EMBEDDED.id}`),
    ).not.toBeNull()
    // Unknown + negative do not appear in the treemap.
    expect(
      screen.queryByTestId(`disk-breakdown-modal-cell-${ENTRY_UNKNOWN.id}`),
    ).toBeNull()
    expect(
      screen.queryByTestId(`disk-breakdown-modal-cell-${ENTRY_NEGATIVE.id}`),
    ).toBeNull()
  })

  it("orders families largest-first inside the treemap", () => {
    render(
      <DiskBreakdownModal
        open
        entries={[ENTRY_WEB, ENTRY_EMBEDDED, ENTRY_MOBILE_LARGE]}
        onClose={vi.fn()}
      />,
    )
    const tree = screen.getByTestId("disk-breakdown-modal-treemap")
    const familyChildren = Array.from(tree.children) as HTMLElement[]
    const ordered = familyChildren.map((el) =>
      el.getAttribute("data-testid")?.replace("disk-breakdown-modal-family-", ""),
    )
    // mobile (800 MB) > embedded (500 MB) > web (100 MB)
    expect(ordered).toEqual(["mobile", "embedded", "web"])
  })

  it("exposes per-cell bytes + share data attrs for visual contract", () => {
    render(
      <DiskBreakdownModal
        open
        entries={[ENTRY_MOBILE_LARGE, ENTRY_MOBILE_SMALL]}
        onClose={vi.fn()}
      />,
    )
    const big = screen.getByTestId(
      `disk-breakdown-modal-cell-${ENTRY_MOBILE_LARGE.id}`,
    )
    const small = screen.getByTestId(
      `disk-breakdown-modal-cell-${ENTRY_MOBILE_SMALL.id}`,
    )
    expect(big.getAttribute("data-entry-bytes")).toBe(String(800 * MB))
    expect(small.getAttribute("data-entry-bytes")).toBe(String(200 * MB))
    // 800/1000 = 80%, 200/1000 = 20%.
    expect(big.getAttribute("data-entry-share")).toBe("80%")
    expect(small.getAttribute("data-entry-share")).toBe("20%")
  })

  it("renders the unknown-size footer for entries with null/undefined/negative bytes", () => {
    render(
      <DiskBreakdownModal
        open
        entries={[ENTRY_MOBILE_LARGE, ENTRY_UNKNOWN, ENTRY_NEGATIVE]}
        onClose={vi.fn()}
      />,
    )
    const footer = screen.getByTestId("disk-breakdown-modal-unknown")
    expect(footer).toBeTruthy()
    expect(
      screen.getByTestId(`disk-breakdown-modal-unknown-item-${ENTRY_UNKNOWN.id}`),
    ).toBeTruthy()
    expect(
      screen.getByTestId(`disk-breakdown-modal-unknown-item-${ENTRY_NEGATIVE.id}`),
    ).toBeTruthy()
    const modal = screen.getByTestId("disk-breakdown-modal")
    expect(modal.getAttribute("data-unknown-entry-count")).toBe("2")
  })

  it("renders the empty-state when no entries report a known disk usage", () => {
    render(
      <DiskBreakdownModal
        open
        entries={[ENTRY_UNKNOWN, ENTRY_NEGATIVE]}
        onClose={vi.fn()}
      />,
    )
    expect(screen.getByTestId("disk-breakdown-modal-empty")).toBeTruthy()
    expect(screen.queryByTestId("disk-breakdown-modal-treemap")).toBeNull()
  })

  it("renders the empty-state for an empty entries array", () => {
    render(
      <DiskBreakdownModal open entries={[]} onClose={vi.fn()} />,
    )
    const empty = screen.getByTestId("disk-breakdown-modal-empty")
    expect(empty.textContent).toMatch(/install something/i)
  })

  it("renders the legend item for every family that contributes a row", () => {
    render(
      <DiskBreakdownModal
        open
        entries={[ENTRY_MOBILE_LARGE, ENTRY_EMBEDDED, ENTRY_WEB]}
        onClose={vi.fn()}
      />,
    )
    const legend = screen.getByTestId("disk-breakdown-modal-legend")
    expect(legend).toBeTruthy()
    expect(
      screen.getByTestId("disk-breakdown-modal-legend-item-mobile"),
    ).toBeTruthy()
    expect(
      screen.getByTestId("disk-breakdown-modal-legend-item-embedded"),
    ).toBeTruthy()
    expect(
      screen.getByTestId("disk-breakdown-modal-legend-item-web"),
    ).toBeTruthy()
    // No software entries → no software legend row.
    expect(
      screen.queryByTestId("disk-breakdown-modal-legend-item-software"),
    ).toBeNull()
  })

  it("Close button calls onClose", () => {
    const onClose = vi.fn()
    render(
      <DiskBreakdownModal
        open
        entries={[ENTRY_MOBILE_LARGE]}
        onClose={onClose}
      />,
    )
    fireEvent.click(screen.getByTestId("disk-breakdown-modal-close"))
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  describe("pure helpers", () => {
    it("entryDiskBytes returns finite non-negative bytes only", () => {
      expect(entryDiskBytes({ ...ENTRY_MOBILE_LARGE })).toBe(800 * MB)
      expect(entryDiskBytes({ ...ENTRY_UNKNOWN })).toBeNull()
      expect(entryDiskBytes({ ...ENTRY_NEGATIVE })).toBeNull()
      expect(
        entryDiskBytes({ ...ENTRY_MOBILE_LARGE, diskUsageBytes: NaN }),
      ).toBeNull()
    })

    it("computeFamilyTotals aggregates + filters + sorts", () => {
      const t = computeFamilyTotals([
        ENTRY_MOBILE_LARGE,
        ENTRY_MOBILE_SMALL,
        ENTRY_EMBEDDED,
        ENTRY_WEB,
        ENTRY_UNKNOWN,
      ])
      expect(t.totalBytes).toBe(1600 * MB)
      expect(t.byFamily.map((b) => b.family)).toEqual([
        "mobile",
        "embedded",
        "web",
      ])
      const mobile = t.byFamily[0]
      expect(mobile?.totalBytes).toBe(1000 * MB)
      // Largest entry inside a family bucket comes first.
      expect(mobile?.entries.map((e) => e.id)).toEqual([
        ENTRY_MOBILE_LARGE.id,
        ENTRY_MOBILE_SMALL.id,
      ])
      expect(t.unknownEntries.map((e) => e.id)).toEqual([ENTRY_UNKNOWN.id])
    })

    it("groupByFamilyForTreemap returns the same per-family list", () => {
      const a = computeFamilyTotals([ENTRY_MOBILE_LARGE, ENTRY_EMBEDDED])
      const b = groupByFamilyForTreemap([ENTRY_MOBILE_LARGE, ENTRY_EMBEDDED])
      expect(b).toEqual(a.byFamily)
    })

    it("formatShare rounds, clamps, and surfaces <1% for tiny shares", () => {
      expect(formatShare(0, 0)).toBe("0%")
      expect(formatShare(0, 100)).toBe("0%")
      expect(formatShare(50, 100)).toBe("50%")
      expect(formatShare(1, 100_000)).toBe("<1%")
      expect(formatShare(50, 99)).toBe("51%")
    })

    it("coerces unknown family strings into the custom bucket", () => {
      const weird: InstalledEntry = {
        id: "weird",
        displayName: "Weird",
        vendor: "?",
        // Cast through `unknown` so unit-tested input mirrors a wire-
        // payload the caller never explicitly typed.
        family: "rtos" as unknown as InstalledEntry["family"],
        diskUsageBytes: 100,
      }
      const t = computeFamilyTotals([weird])
      expect(t.byFamily[0]?.family).toBe("custom")
    })
  })
})
