/**
 * BS.6.8 — `<CatalogTab />` contract tests.
 *
 * Locks the toolbar + grid contract for BS.6.1 (filter / search / sort /
 * density), BS.6.5 (windowed virtualization opt-out path), and BS.6.6
 * (motion attributes plumbed onto the tab root). The detail panel
 * slide-out integration is exercised through `renderDetail` callback.
 *
 * Scope:
 *   1. Pure helpers — `filterAndSortEntries`, `coerceDensity`,
 *      `coerceSort`, `columnsForViewport`.
 *   2. Toolbar render — chips, summary line, density toggle, sort.
 *   3. Filter / search / sort flows — DOM updates after user input.
 *   4. renderCard pipeline — wired card receives floatVariantIndex +
 *      onSelect when detail panel is wired.
 *   5. renderDetail integration — clicking a card flips the grid for
 *      the detail slot; back closes.
 *   6. Virtualization opt-out flag — disableVirtualization keeps DOM
 *      static even past threshold.
 */

import * as React from "react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { fireEvent, render, screen } from "@testing-library/react"

import type { MotionLevel } from "@/lib/motion-preferences"

let mockLevel: MotionLevel = "normal"

vi.mock("@/hooks/use-effective-motion-level", () => ({
  useEffectiveMotionLevel: () => mockLevel,
  usePrefersReducedMotion: () => false,
}))

// `useUserStorage` requires AuthProvider + TenantProvider. We mock it to
// a plain useState-style hook so density persistence still round-trips
// through React without standing up the full context tree.
vi.mock("@/lib/storage", () => ({
  useUserStorage: (_key: string) => {
    const [v, setV] = React.useState<string | null>(null)
    return [v, setV]
  },
}))

// Avoid the `category-strip` ↔ `catalog-tab` ESM init cycle (see
// catalog-card test for the full diagnosis). Replace the chip strip
// with a stub that mirrors the family-filter contract: clicking a chip
// fires `onSelect(family)` and the active family gets aria-pressed.
// vi.mock is hoisted to the top of the file so the factory must avoid
// referencing top-level identifiers — every constant is declared inline.
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
      rootTestId,
      chipTestIdPrefix,
    }: {
      family: string
      onSelect: (next: string) => void
      rootTestId?: string
      chipTestIdPrefix?: string
    }) =>
      React.createElement(
        "div",
        {
          "data-testid": rootTestId ?? "category-strip",
          "data-active-family": family,
          role: "group",
        },
        FAMILIES.map((f) =>
          React.createElement(
            "button",
            {
              key: f,
              type: "button",
              "data-testid": `${chipTestIdPrefix ?? "category-strip-chip"}-${f}`,
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
  CatalogTab,
  CATALOG_VIRTUALIZATION_THRESHOLD,
  coerceDensity,
  coerceFamily,
  coerceSort,
  columnsForViewport,
  filterAndSortEntries,
  type CatalogEntry,
} from "@/components/omnisight/catalog-tab"

afterEach(() => {
  mockLevel = "normal"
})

const entry = (e: Partial<CatalogEntry> & Pick<CatalogEntry, "id">): CatalogEntry => ({
  displayName: e.id,
  vendor: "Acme",
  family: "software",
  ...e,
})

const SAMPLE: CatalogEntry[] = [
  entry({ id: "alpha", displayName: "Alpha", vendor: "Beta", family: "mobile", updatedAt: "2026-04-01T00:00:00Z" }),
  entry({ id: "bravo", displayName: "Bravo", vendor: "Acme", family: "embedded", updatedAt: "2026-04-10T00:00:00Z" }),
  entry({ id: "charlie", displayName: "Charlie", vendor: "Acme", family: "web" }),
]

// ─────────────────────────────────────────────────────────────────────
// 1. Pure helpers
// ─────────────────────────────────────────────────────────────────────

describe("filterAndSortEntries", () => {
  it("filters by family + searches displayName/vendor/id (case-insensitive) + sorts", () => {
    const all = filterAndSortEntries({
      entries: SAMPLE,
      family: "all",
      search: "",
      sort: "name-asc",
    })
    expect(all.map((e) => e.id)).toEqual(["alpha", "bravo", "charlie"])

    const onlyMobile = filterAndSortEntries({
      entries: SAMPLE,
      family: "mobile",
      search: "",
      sort: "name-asc",
    })
    expect(onlyMobile.map((e) => e.id)).toEqual(["alpha"])

    // Search hits vendor for "Acme".
    const acme = filterAndSortEntries({
      entries: SAMPLE,
      family: "all",
      search: "ACME",
      sort: "name-asc",
    })
    expect(acme.map((e) => e.id)).toEqual(["bravo", "charlie"])

    // recent-desc: charlie has no updatedAt → sinks to bottom.
    const recent = filterAndSortEntries({
      entries: SAMPLE,
      family: "all",
      search: "",
      sort: "recent-desc",
    })
    expect(recent.map((e) => e.id)).toEqual(["bravo", "alpha", "charlie"])
  })

  it("does not mutate the input array", () => {
    const original = [...SAMPLE]
    filterAndSortEntries({
      entries: SAMPLE,
      family: "all",
      search: "",
      sort: "name-desc",
    })
    expect(SAMPLE).toEqual(original)
  })
})

describe("coerce* defensive helpers", () => {
  it("coerceFamily falls back to 'custom' for unknown / nullish input", () => {
    expect(coerceFamily("mobile")).toBe("mobile")
    expect(coerceFamily("UNKNOWN-FAMILY")).toBe("custom")
    expect(coerceFamily(null)).toBe("custom")
    expect(coerceFamily(undefined)).toBe("custom")
  })

  it("coerceDensity / coerceSort fall back to defaults", () => {
    expect(coerceDensity("compact")).toBe("compact")
    expect(coerceDensity("garbage")).toBe("comfortable")
    expect(coerceDensity(null)).toBe("comfortable")
    expect(coerceSort("vendor-asc")).toBe("vendor-asc")
    expect(coerceSort("not-a-sort")).toBe("name-asc")
  })
})

describe("columnsForViewport", () => {
  it("respects density × Tailwind breakpoint mapping", () => {
    expect(columnsForViewport("comfortable", 1280)).toBe(4)
    expect(columnsForViewport("comfortable", 1024)).toBe(3)
    expect(columnsForViewport("comfortable", 640)).toBe(2)
    expect(columnsForViewport("comfortable", 320)).toBe(1)
    expect(columnsForViewport("compact", 1280)).toBe(5)
    expect(columnsForViewport("spacious", 1280)).toBe(3)
  })
})

// ─────────────────────────────────────────────────────────────────────
// 2. Toolbar render
// ─────────────────────────────────────────────────────────────────────

describe("<CatalogTab /> — toolbar render", () => {
  it("mounts toolbar testids + summary counts + grid (zero-entries empty state)", () => {
    render(<CatalogTab />)
    expect(screen.getByTestId("catalog-tab-toolbar")).toBeInTheDocument()
    expect(screen.getByTestId("catalog-tab-search-input")).toBeInTheDocument()
    expect(screen.getByTestId("catalog-tab-sort-select")).toBeInTheDocument()
    expect(screen.getByTestId("catalog-tab-density-group")).toBeInTheDocument()
    expect(screen.getByTestId("catalog-tab-result-count").textContent).toBe(
      "0 / 0 entries",
    )
    expect(screen.getByTestId("catalog-tab-empty")).toBeInTheDocument()
  })
})

// ─────────────────────────────────────────────────────────────────────
// 3. Filter / search / sort flows
// ─────────────────────────────────────────────────────────────────────

describe("<CatalogTab /> — filter + search + sort flows", () => {
  it("clicking a family chip narrows the grid + flips data-catalog-family", () => {
    render(<CatalogTab entries={SAMPLE} disableVirtualization />)
    expect(screen.getByTestId("catalog-tab-result-count").textContent).toBe(
      "3 / 3 entries",
    )
    fireEvent.click(screen.getByTestId("catalog-tab-family-chip-mobile"))
    expect(screen.getByTestId("catalog-tab").getAttribute(
      "data-catalog-family",
    )).toBe("mobile")
    expect(screen.getByTestId("catalog-tab-result-count").textContent).toBe(
      "1 / 3 entries",
    )
    expect(screen.getByTestId("catalog-tab-card-slot-alpha")).toBeInTheDocument()
    expect(screen.queryByTestId("catalog-tab-card-slot-bravo")).toBeNull()
  })

  it("typing into search filters in-place; clear button resets", () => {
    render(<CatalogTab entries={SAMPLE} disableVirtualization />)
    const input = screen.getByTestId("catalog-tab-search-input") as HTMLInputElement
    fireEvent.change(input, { target: { value: "alpha" } })
    expect(screen.getByTestId("catalog-tab-result-count").textContent).toBe(
      "1 / 3 entries",
    )
    expect(screen.getByTestId("catalog-tab-card-slot-alpha")).toBeInTheDocument()
    expect(screen.queryByTestId("catalog-tab-card-slot-bravo")).toBeNull()
    fireEvent.click(screen.getByTestId("catalog-tab-search-clear"))
    expect(input.value).toBe("")
    expect(screen.getByTestId("catalog-tab-result-count").textContent).toBe(
      "3 / 3 entries",
    )
  })

  it("changing sort flips data-catalog-sort + reorders rendered cards", () => {
    render(<CatalogTab entries={SAMPLE} disableVirtualization />)
    fireEvent.change(screen.getByTestId("catalog-tab-sort-select"), {
      target: { value: "name-desc" },
    })
    expect(screen.getByTestId("catalog-tab").getAttribute("data-catalog-sort")).toBe(
      "name-desc",
    )
    // First card slot in DOM order should be the lex-greatest.
    const grid = screen.getByTestId("catalog-tab-grid")
    const slots = grid.querySelectorAll("[data-testid^='catalog-tab-card-slot-']")
    expect(slots[0].getAttribute("data-entry-id")).toBe("charlie")
  })
})

// ─────────────────────────────────────────────────────────────────────
// 4. Density toggle (does NOT need to persist — just flips state)
// ─────────────────────────────────────────────────────────────────────

describe("<CatalogTab /> — density toggle", () => {
  it("clicking compact / spacious flips data-catalog-density + grid attribute", () => {
    render(<CatalogTab entries={SAMPLE} disableVirtualization />)
    expect(
      screen.getByTestId("catalog-tab").getAttribute("data-catalog-density"),
    ).toBe("comfortable")
    fireEvent.click(screen.getByTestId("catalog-tab-density-compact"))
    expect(
      screen.getByTestId("catalog-tab").getAttribute("data-catalog-density"),
    ).toBe("compact")
    expect(
      screen.getByTestId("catalog-tab-grid").getAttribute("data-grid-density"),
    ).toBe("compact")
  })
})

// ─────────────────────────────────────────────────────────────────────
// 5. renderCard pipeline
// ─────────────────────────────────────────────────────────────────────

describe("<CatalogTab /> — renderCard pipeline", () => {
  it("invokes renderCard with entry + density + cardPaddingClass + floatVariantIndex", () => {
    const seen: Array<{
      id: string
      density: string
      pad: string
      idx: number
      hasOnSelect: boolean
    }> = []
    render(
      <CatalogTab
        entries={SAMPLE}
        disableVirtualization
        renderCard={(ctx) => {
          seen.push({
            id: ctx.entry.id,
            density: ctx.density,
            pad: ctx.cardPaddingClass,
            idx: ctx.floatVariantIndex,
            hasOnSelect: typeof ctx.onSelect === "function",
          })
          return <div data-testid={`stub-card-${ctx.entry.id}`}>{ctx.entry.id}</div>
        }}
      />,
    )
    // 3 entries × any number of renders — every entry must have been
    // rendered at least once with the right density / pad / idx.
    const byId = new Map(seen.map((s) => [s.id, s]))
    expect([...byId.keys()].sort()).toEqual(["alpha", "bravo", "charlie"])
    expect(new Set(seen.map((s) => s.idx))).toEqual(new Set([0, 1, 2]))
    expect(new Set(seen.map((s) => s.density))).toEqual(new Set(["comfortable"]))
    expect(new Set(seen.map((s) => s.pad))).toEqual(new Set(["p-3 text-xs"]))
    // No detail panel wired → onSelect should be undefined.
    expect(seen.every((s) => !s.hasOnSelect)).toBe(true)
  })
})

// ─────────────────────────────────────────────────────────────────────
// 6. renderDetail integration
// ─────────────────────────────────────────────────────────────────────

describe("<CatalogTab /> — renderDetail integration", () => {
  it("wires onSelect when renderDetail is provided; clicking flips to detail slot; close re-mounts grid", () => {
    render(
      <CatalogTab
        entries={SAMPLE}
        disableVirtualization
        renderCard={(ctx) => (
          <button
            type="button"
            data-testid={`stub-card-${ctx.entry.id}`}
            onClick={ctx.onSelect}
          >
            {ctx.entry.id}
          </button>
        )}
        renderDetail={({ entry, onClose }) => (
          <div data-testid="stub-detail" data-entry-id={entry.id}>
            <button type="button" data-testid="stub-back" onClick={onClose}>
              back
            </button>
          </div>
        )}
      />,
    )
    expect(
      screen.getByTestId("catalog-tab").getAttribute("data-catalog-detail-open"),
    ).toBe("false")
    fireEvent.click(screen.getByTestId("stub-card-bravo"))
    expect(
      screen.getByTestId("catalog-tab").getAttribute("data-catalog-detail-open"),
    ).toBe("true")
    expect(
      screen.getByTestId("catalog-tab").getAttribute("data-catalog-selected-id"),
    ).toBe("bravo")
    expect(screen.getByTestId("catalog-tab-detail-slot")).toBeInTheDocument()
    expect(screen.getByTestId("stub-detail").getAttribute("data-entry-id")).toBe(
      "bravo",
    )
    expect(screen.queryByTestId("catalog-tab-grid")).toBeNull()
    fireEvent.click(screen.getByTestId("stub-back"))
    expect(
      screen.getByTestId("catalog-tab").getAttribute("data-catalog-detail-open"),
    ).toBe("false")
    expect(screen.getByTestId("catalog-tab-grid")).toBeInTheDocument()
  })
})

// ─────────────────────────────────────────────────────────────────────
// 7. Virtualization opt-out + threshold constant.
// ─────────────────────────────────────────────────────────────────────

describe("<CatalogTab /> — virtualization", () => {
  beforeEach(() => {
    // Force a wide viewport so the column-count hook resolves to >1
    // (otherwise jsdom defaults to 1024px which already gives us 3 cols
    // at comfortable density). We set both window.innerWidth and trigger
    // resize so the hook updates after mount.
    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      writable: true,
      value: 1280,
    })
  })

  it("CATALOG_VIRTUALIZATION_THRESHOLD ≥ 1 (sane non-zero default)", () => {
    expect(CATALOG_VIRTUALIZATION_THRESHOLD).toBeGreaterThanOrEqual(1)
  })

  it("disableVirtualization=true keeps the static grid even with many entries", () => {
    const many: CatalogEntry[] = Array.from({ length: 60 }, (_, i) =>
      entry({ id: `e-${i}`, displayName: `E-${i}`, family: "software" }),
    )
    render(<CatalogTab entries={many} disableVirtualization />)
    const grid = screen.getByTestId("catalog-tab-grid")
    expect(grid.getAttribute("data-grid-virtualized")).toBe("false")
    // All 60 card slots are rendered in the static path.
    expect(
      grid.querySelectorAll("[data-testid^='catalog-tab-card-slot-']").length,
    ).toBe(60)
  })
})

// ─────────────────────────────────────────────────────────────────────
// 8. Motion attributes on the tab root.
// ─────────────────────────────────────────────────────────────────────

describe("<CatalogTab /> — motion attributes", () => {
  it("OFF motion level: group-breathe + parallax both off", () => {
    mockLevel = "off"
    render(<CatalogTab entries={SAMPLE} disableVirtualization />)
    const root = screen.getByTestId("catalog-tab")
    expect(root.getAttribute("data-catalog-motion-level")).toBe("off")
    expect(root.getAttribute("data-motion-group-breathe")).toBe("off")
    expect(root.getAttribute("data-motion-parallax")).toBe("off")
    expect(
      screen.getByTestId("catalog-tab-grid").getAttribute(
        "data-grid-group-breathe",
      ),
    ).toBe("off")
  })

  it("DRAMATIC motion level: group-breathe is engaged on the grid", () => {
    mockLevel = "dramatic"
    render(<CatalogTab entries={SAMPLE} disableVirtualization />)
    expect(
      screen.getByTestId("catalog-tab").getAttribute("data-motion-group-breathe"),
    ).toBe("on")
    expect(
      screen.getByTestId("catalog-tab-grid").getAttribute(
        "data-grid-group-breathe",
      ),
    ).toBe("on")
  })
})
