/**
 * BS.11.3 — Screen-reader label + aria-live announcement contract tests.
 *
 * Locks the screen-reader-facing affordances introduced for the
 * Platforms catalog surface:
 *
 *   1. `<CatalogCard />` aria-label — rich phrasing including family,
 *      vendor, version, and a state-aware status that surfaces
 *      installing percent / update next-version / failure reason.
 *   2. `<CatalogTab />` aria-live — polite live region that announces
 *      install-state transitions when the BS.7 install pipeline flips
 *      `entries[i].installState` (available → installing → installed,
 *      etc.). The first render does not emit announcements.
 *   3. `<CatalogDetailPanel />` aria-label + panel-local aria-live —
 *      mirrors the card phrasing for the larger detail surface and
 *      announces transitions while the panel is open.
 *   4. `buildCatalogStateAnnouncement` / `buildCatalogCardAriaLabel` /
 *      `buildCatalogDetailPanelAriaLabel` pure helpers — every branch.
 */

import * as React from "react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { act, render, screen } from "@testing-library/react"

import type { MotionLevel } from "@/lib/motion-preferences"

let mockLevel: MotionLevel = "normal"

vi.mock("@/hooks/use-effective-motion-level", () => ({
  useEffectiveMotionLevel: () => mockLevel,
  usePrefersReducedMotion: () => false,
}))

vi.mock("@/lib/storage", () => ({
  useUserStorage: (_key: string) => {
    const [v, setV] = React.useState<string | null>(null)
    return [v, setV]
  },
}))

// Stub out CategoryStrip to avoid the ESM init cycle the BS.6.8 test
// suites already document.
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
        },
        FAMILIES.map((f) =>
          React.createElement(
            "button",
            {
              key: f,
              type: "button",
              "data-testid": `${chipTestIdPrefix ?? "category-strip-chip"}-${f}`,
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
  CatalogCard,
  buildCatalogCardAriaLabel,
} from "@/components/omnisight/catalog-card"
import {
  CatalogTab,
  type CatalogEntry,
  buildCatalogStateAnnouncement,
} from "@/components/omnisight/catalog-tab"
import {
  CatalogDetailPanel,
  buildCatalogDetailPanelAriaLabel,
} from "@/components/omnisight/catalog-detail-panel"
import { TooltipProvider } from "@/components/ui/tooltip"

afterEach(() => {
  mockLevel = "normal"
})

const baseEntry = (overrides: Partial<CatalogEntry> = {}): CatalogEntry => ({
  id: "ent-1",
  displayName: "Vision SDK",
  vendor: "Acme",
  family: "software",
  version: "1.2.3",
  installState: "available",
  ...overrides,
})

// ─────────────────────────────────────────────────────────────────────
// 1. Pure helpers — buildCatalogCardAriaLabel branches
// ─────────────────────────────────────────────────────────────────────

describe("BS.11.3 — buildCatalogCardAriaLabel", () => {
  it("includes displayName, family, vendor, version, and the available status", () => {
    const label = buildCatalogCardAriaLabel(
      baseEntry({ family: "mobile" }),
      "available",
      0,
    )
    expect(label).toContain("Vision SDK")
    expect(label).toContain("Mobile catalog entry")
    expect(label).toContain("vendor Acme")
    expect(label).toContain("version 1.2.3")
    expect(label).toContain("Available to install")
  })

  it("omits the version segment when entry has no version", () => {
    const label = buildCatalogCardAriaLabel(
      baseEntry({ version: undefined }),
      "available",
      0,
    )
    expect(label).not.toContain("version")
  })

  it("installing state surfaces rounded percent in the status phrase", () => {
    const label = buildCatalogCardAriaLabel(
      baseEntry({ installState: "installing" }),
      "installing",
      42.7,
    )
    expect(label).toContain("Installing — 43 percent complete")
  })

  it("installing state clamps an out-of-range progress to 0..100", () => {
    expect(
      buildCatalogCardAriaLabel(baseEntry(), "installing", -50),
    ).toContain("0 percent complete")
    expect(
      buildCatalogCardAriaLabel(baseEntry(), "installing", 9999),
    ).toContain("100 percent complete")
  })

  it("update-available with metadata.nextVersion surfaces the next version", () => {
    const label = buildCatalogCardAriaLabel(
      baseEntry({
        installState: "update-available",
        metadata: { nextVersion: "2.0.0" },
      }),
      "update-available",
      0,
    )
    expect(label).toContain("Update available — version 2.0.0")
  })

  it("update-available without metadata falls back to the generic chip text", () => {
    const label = buildCatalogCardAriaLabel(
      baseEntry({ installState: "update-available" }),
      "update-available",
      0,
    )
    expect(label).toContain("Update available")
    expect(label).not.toContain("version 2")
  })

  it("failed state surfaces the failureReason hint", () => {
    const label = buildCatalogCardAriaLabel(
      baseEntry({
        installState: "failed",
        metadata: { failureReason: "shasum mismatch" },
      }),
      "failed",
      0,
    )
    expect(label).toContain("Install failed — shasum mismatch")
  })
})

// ─────────────────────────────────────────────────────────────────────
// 2. <CatalogCard /> root aria-label — DOM contract
// ─────────────────────────────────────────────────────────────────────

describe("BS.11.3 — <CatalogCard /> aria-label DOM contract", () => {
  it("renders the rich aria-label on the card root for the available state", () => {
    render(
      <TooltipProvider>
        <CatalogCard
          entry={baseEntry({ family: "embedded" })}
          density="comfortable"
          cardPaddingClass="p-3 text-xs"
        />
      </TooltipProvider>,
    )
    const root = screen.getByTestId("catalog-card-ent-1")
    const label = root.getAttribute("aria-label") ?? ""
    expect(label).toContain("Vision SDK")
    expect(label).toContain("Embedded catalog entry")
    expect(label).toContain("vendor Acme")
    expect(label).toContain("version 1.2.3")
    expect(label).toContain("Available to install")
  })

  it("installing card surfaces the percent in the aria-label (fed via prop override)", () => {
    render(
      <TooltipProvider>
        <CatalogCard
          entry={baseEntry({ installState: "installing" })}
          density="comfortable"
          cardPaddingClass="p-3 text-xs"
          installProgressPercent={73}
        />
      </TooltipProvider>,
    )
    const root = screen.getByTestId("catalog-card-ent-1")
    expect(root.getAttribute("aria-label")).toContain("73 percent complete")
  })
})

// ─────────────────────────────────────────────────────────────────────
// 3. Pure helpers — buildCatalogStateAnnouncement branches
// ─────────────────────────────────────────────────────────────────────

describe("BS.11.3 — buildCatalogStateAnnouncement", () => {
  const e = baseEntry({ displayName: "ESP-IDF" })

  it("returns null when prev === next (no transition)", () => {
    expect(buildCatalogStateAnnouncement(e, "available", "available")).toBeNull()
    expect(buildCatalogStateAnnouncement(e, "installed", "installed")).toBeNull()
  })

  it("returns null when transitioning back to the available baseline", () => {
    expect(buildCatalogStateAnnouncement(e, "installed", "available")).toBeNull()
    expect(buildCatalogStateAnnouncement(e, "failed", "available")).toBeNull()
  })

  it("available → installing announces install started", () => {
    expect(buildCatalogStateAnnouncement(e, "available", "installing")).toBe(
      "ESP-IDF install started",
    )
  })

  it("failed → installing announces install retry started", () => {
    expect(buildCatalogStateAnnouncement(e, "failed", "installing")).toBe(
      "ESP-IDF install retry started",
    )
  })

  it("update-available → installing announces update started", () => {
    expect(
      buildCatalogStateAnnouncement(e, "update-available", "installing"),
    ).toBe("ESP-IDF update started")
  })

  it("installing → installed announces installed successfully", () => {
    expect(buildCatalogStateAnnouncement(e, "installing", "installed")).toBe(
      "ESP-IDF installed successfully",
    )
  })

  it("installing → failed announces failure with failureReason hint", () => {
    const failed = baseEntry({
      displayName: "ESP-IDF",
      metadata: { failureReason: "network unreachable" },
    })
    expect(buildCatalogStateAnnouncement(failed, "installing", "failed")).toBe(
      "ESP-IDF install failed — network unreachable",
    )
  })

  it("installing → failed without failureReason falls back to plain failure phrase", () => {
    expect(buildCatalogStateAnnouncement(e, "installing", "failed")).toBe(
      "ESP-IDF install failed",
    )
  })

  it("any → update-available with nextVersion surfaces the version", () => {
    const upd = baseEntry({
      displayName: "ESP-IDF",
      metadata: { nextVersion: "5.4.1" },
    })
    expect(
      buildCatalogStateAnnouncement(upd, "installed", "update-available"),
    ).toBe("Update available for ESP-IDF — version 5.4.1")
  })

  it("any → update-available without metadata falls back to the generic phrase", () => {
    expect(
      buildCatalogStateAnnouncement(e, "installed", "update-available"),
    ).toBe("Update available for ESP-IDF")
  })
})

// ─────────────────────────────────────────────────────────────────────
// 4. <CatalogTab /> aria-live region — DOM contract
// ─────────────────────────────────────────────────────────────────────

const SAMPLE: CatalogEntry[] = [
  baseEntry({ id: "alpha", displayName: "Alpha", installState: "available" }),
  baseEntry({ id: "bravo", displayName: "Bravo", installState: "available" }),
]

describe("BS.11.3 — <CatalogTab /> aria-live announcer", () => {
  it("mounts a polite live region with role=status + sr-only + aria-atomic=true", () => {
    render(
      <TooltipProvider>
        <CatalogTab entries={SAMPLE} disableVirtualization />
      </TooltipProvider>,
    )
    const region = screen.getByTestId("catalog-tab-announcer")
    expect(region.getAttribute("role")).toBe("status")
    expect(region.getAttribute("aria-live")).toBe("polite")
    expect(region.getAttribute("aria-atomic")).toBe("true")
    expect(region.className).toContain("sr-only")
    // First render seeds prev-state map; no announcement yet.
    expect(region.textContent).toBe("")
  })

  it("announces a transition when an entry's install-state flips between renders", () => {
    function Host({ data }: { data: CatalogEntry[] }) {
      return (
        <TooltipProvider>
          <CatalogTab entries={data} disableVirtualization />
        </TooltipProvider>
      )
    }
    const { rerender } = render(<Host data={SAMPLE} />)
    expect(screen.getByTestId("catalog-tab-announcer").textContent).toBe("")

    rerender(
      <Host
        data={[
          { ...SAMPLE[0], installState: "installing" },
          SAMPLE[1],
        ]}
      />,
    )
    expect(screen.getByTestId("catalog-tab-announcer").textContent).toBe(
      "Alpha install started",
    )

    rerender(
      <Host
        data={[
          { ...SAMPLE[0], installState: "installed" },
          SAMPLE[1],
        ]}
      />,
    )
    expect(screen.getByTestId("catalog-tab-announcer").textContent).toBe(
      "Alpha installed successfully",
    )
  })

  it("data-catalog-announcement attribute mirrors the live region for tooling", () => {
    function Host({ data }: { data: CatalogEntry[] }) {
      return (
        <TooltipProvider>
          <CatalogTab entries={data} disableVirtualization />
        </TooltipProvider>
      )
    }
    const { rerender } = render(<Host data={SAMPLE} />)
    rerender(
      <Host
        data={[
          { ...SAMPLE[0], installState: "installing" },
          SAMPLE[1],
        ]}
      />,
    )
    expect(
      screen.getByTestId("catalog-tab").getAttribute("data-catalog-announcement"),
    ).toBe("Alpha install started")
  })

  it("multiple entries flipping in one render — most recent transition wins", () => {
    function Host({ data }: { data: CatalogEntry[] }) {
      return (
        <TooltipProvider>
          <CatalogTab entries={data} disableVirtualization />
        </TooltipProvider>
      )
    }
    const { rerender } = render(<Host data={SAMPLE} />)
    rerender(
      <Host
        data={[
          { ...SAMPLE[0], installState: "installing" },
          { ...SAMPLE[1], installState: "installing" },
        ]}
      />,
    )
    // Bravo is later in the entries list → its message wins.
    expect(screen.getByTestId("catalog-tab-announcer").textContent).toBe(
      "Bravo install started",
    )
  })
})

// ─────────────────────────────────────────────────────────────────────
// 5. <CatalogDetailPanel /> aria-label + announcer
// ─────────────────────────────────────────────────────────────────────

describe("BS.11.3 — buildCatalogDetailPanelAriaLabel", () => {
  it("renders the rich detail-panel phrasing for the installed state", () => {
    const label = buildCatalogDetailPanelAriaLabel(
      baseEntry({ family: "embedded", installState: "installed" }),
      "installed",
      0,
    )
    expect(label).toContain("Vision SDK detail")
    expect(label).toContain("Embedded catalog entry")
    expect(label).toContain("vendor Acme")
    expect(label).toContain("version 1.2.3")
    expect(label).toContain("Installed and healthy")
  })

  it("installing state surfaces the percent", () => {
    const label = buildCatalogDetailPanelAriaLabel(
      baseEntry({ installState: "installing" }),
      "installing",
      55,
    )
    expect(label).toContain("Installing — 55 percent complete")
  })
})

describe("BS.11.3 — <CatalogDetailPanel /> aria-label + announcer DOM", () => {
  it("renders rich aria-label + sr-only announcer with role=status", () => {
    render(
      <TooltipProvider>
        <CatalogDetailPanel
          entry={baseEntry({ family: "mobile" })}
          onBack={() => {}}
        />
      </TooltipProvider>,
    )
    const panel = screen.getByTestId("catalog-detail-panel")
    const label = panel.getAttribute("aria-label") ?? ""
    expect(label).toContain("Vision SDK detail")
    expect(label).toContain("Mobile catalog entry")
    const announcer = screen.getByTestId("catalog-detail-panel-announcer")
    expect(announcer.getAttribute("role")).toBe("status")
    expect(announcer.getAttribute("aria-live")).toBe("polite")
    expect(announcer.getAttribute("aria-atomic")).toBe("true")
    expect(announcer.className).toContain("sr-only")
    // First mount — no transition yet.
    expect(announcer.textContent).toBe("")
  })

  it("announces install-state transitions while the panel stays mounted on the same entry", () => {
    function Host({ entry }: { entry: CatalogEntry }) {
      return (
        <TooltipProvider>
          <CatalogDetailPanel entry={entry} onBack={() => {}} />
        </TooltipProvider>
      )
    }
    const { rerender } = render(<Host entry={baseEntry()} />)
    expect(
      screen.getByTestId("catalog-detail-panel-announcer").textContent,
    ).toBe("")
    act(() => {
      rerender(<Host entry={baseEntry({ installState: "installing" })} />)
    })
    expect(
      screen.getByTestId("catalog-detail-panel-announcer").textContent,
    ).toBe("Vision SDK install started")
    act(() => {
      rerender(<Host entry={baseEntry({ installState: "installed" })} />)
    })
    expect(
      screen.getByTestId("catalog-detail-panel-announcer").textContent,
    ).toBe("Vision SDK installed successfully")
  })

  it("switching to a different entry id resets the announcer (no replay)", () => {
    function Host({ entry }: { entry: CatalogEntry }) {
      return (
        <TooltipProvider>
          <CatalogDetailPanel entry={entry} onBack={() => {}} />
        </TooltipProvider>
      )
    }
    const { rerender } = render(
      <Host entry={baseEntry({ id: "ent-1", installState: "available" })} />,
    )
    act(() => {
      rerender(
        <Host
          entry={baseEntry({ id: "ent-1", installState: "installed" })}
        />,
      )
    })
    expect(
      screen.getByTestId("catalog-detail-panel-announcer").textContent,
    ).toBe("Vision SDK installed successfully")
    // Operator clicks a different card → new entry id flows in.
    act(() => {
      rerender(
        <Host
          entry={baseEntry({
            id: "ent-2",
            displayName: "Other SDK",
            installState: "available",
          })}
        />,
      )
    })
    expect(
      screen.getByTestId("catalog-detail-panel-announcer").textContent,
    ).toBe("")
  })
})
