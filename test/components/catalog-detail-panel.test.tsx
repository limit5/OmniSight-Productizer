/**
 * BS.6.8 — `<CatalogDetailPanel />` contract tests.
 *
 * Locks the BS.6.3 inline-expand panel: header surface (back button +
 * family + state chip + vendor + version), energy orb visual state,
 * dependency / used-by / activity sections, and the BS.6.7 disabled
 * footer CTA tooltip wrapper. Pure helpers (`coerceActivityKind`,
 * `extractDependencies`, `extractUsedByWorkspaces`,
 * `extractActivityEvents`, `formatRelativeTime`, `buildEnergyOrbState`)
 * are tested directly so the BS.7 install pipeline can rely on the
 * defensive parse contracts without a DOM round-trip.
 */

import { afterEach, describe, expect, it, vi } from "vitest"
import { fireEvent, render, screen } from "@testing-library/react"

import type { MotionLevel } from "@/lib/motion-preferences"
import type { CatalogEntry } from "@/components/omnisight/catalog-tab"

let mockLevel: MotionLevel = "normal"

vi.mock("@/hooks/use-effective-motion-level", () => ({
  useEffectiveMotionLevel: () => mockLevel,
  usePrefersReducedMotion: () => false,
}))

// Break the catalog-tab ↔ category-strip ESM init cycle (see catalog-
// card test for the diagnosis). Detail panel never renders the chip
// strip so the stub is enough.
vi.mock("@/components/omnisight/category-strip", () => ({
  CategoryStrip: () => null,
  CATEGORY_STRIP_FAMILIES: [
    "all",
    "mobile",
    "embedded",
    "web",
    "software",
    "custom",
  ],
  getCategoryStripPalette: () => ({}),
}))

import {
  CatalogDetailPanel,
  CATALOG_ACTIVITY_KINDS,
  buildEnergyOrbState,
  coerceActivityKind,
  extractActivityEvents,
  extractDependencies,
  extractUsedByWorkspaces,
  formatRelativeTime,
} from "@/components/omnisight/catalog-detail-panel"
import { CATALOG_INSTALL_PENDING_TOOLTIP } from "@/components/omnisight/catalog-card"
import { TooltipProvider } from "@/components/ui/tooltip"

afterEach(() => {
  mockLevel = "normal"
})

const baseEntry = (overrides: Partial<CatalogEntry> = {}): CatalogEntry => ({
  id: "vision",
  displayName: "Vision SDK",
  vendor: "Acme",
  family: "software",
  version: "1.2.3",
  installState: "available",
  description: "Camera vision SDK for embedded boards.",
  ...overrides,
})

function renderPanel(props: Partial<Parameters<typeof CatalogDetailPanel>[0]> = {}) {
  return render(
    <TooltipProvider>
      <CatalogDetailPanel
        entry={props.entry ?? baseEntry()}
        onBack={props.onBack ?? vi.fn()}
        onInstall={props.onInstall}
        onRetry={props.onRetry}
        onViewLog={props.onViewLog}
        onSelectDependency={props.onSelectDependency}
        onSelectWorkspace={props.onSelectWorkspace}
        now={props.now}
        activityLimit={props.activityLimit}
      />
    </TooltipProvider>,
  )
}

// ─────────────────────────────────────────────────────────────────────
// 1. Pure helpers
// ─────────────────────────────────────────────────────────────────────

describe("coerceActivityKind", () => {
  it("maps known kinds through + collapses unknown / non-string to 'info'", () => {
    for (const kind of CATALOG_ACTIVITY_KINDS) {
      expect(coerceActivityKind(kind)).toBe(kind)
    }
    expect(coerceActivityKind("garbled-kind")).toBe("info")
    expect(coerceActivityKind(null)).toBe("info")
    expect(coerceActivityKind(123)).toBe("info")
    expect(coerceActivityKind(undefined)).toBe("info")
  })
})

describe("extractDependencies / extractUsedByWorkspaces", () => {
  it("reads PG-shaped JSONB and silently drops malformed rows", () => {
    const e = baseEntry({
      metadata: {
        depends_on: ["dep-1", "", 42, "dep-2"],
        depends_on_meta: {
          "dep-1": { displayName: "Dep One", state: "installed" },
          "dep-2": { state: "garbage-state" },
        },
        used_by_workspaces: [
          { id: "ws-1", name: "Web Studio", productLine: "web" },
          { id: "ws-2", name: "Mobile" }, // productLine omitted → "other"
          { name: "missing-id" }, // dropped
          { id: "ws-3" }, // dropped (no name)
          "junk", // dropped
        ],
      },
    })
    const deps = extractDependencies(e)
    expect(deps.map((d) => d.id)).toEqual(["dep-1", "dep-2"])
    expect(deps[0].displayName).toBe("Dep One")
    expect(deps[0].state).toBe("installed")
    // "garbage-state" coerces back to "available" via coerceInstallState.
    expect(deps[1].state).toBe("available")
    const ws = extractUsedByWorkspaces(e)
    expect(ws.map((w) => w.id)).toEqual(["ws-1", "ws-2"])
    expect(ws[1].productLine).toBe("other")
  })
})

describe("extractActivityEvents", () => {
  it("drops malformed rows and sorts most-recent-first", () => {
    const e = baseEntry({
      metadata: {
        activity: [
          { id: "a", timestamp: "2026-01-01T00:00:00Z", message: "old" },
          { id: "b", timestamp: "2026-04-10T00:00:00Z", message: "new" },
          { id: "c", timestamp: "not-a-date", message: "broken" },
          { id: "d", timestamp: "2026-04-15T00:00:00Z" }, // no message → drop
          { timestamp: "2026-04-15T00:00:00Z", message: "no id" }, // drop
        ],
      },
    })
    const out = extractActivityEvents(e)
    expect(out.map((r) => r.id)).toEqual(["b", "a", "c"])
  })
})

describe("formatRelativeTime", () => {
  it("buckets into the right short label", () => {
    const now = Date.parse("2026-04-27T12:00:00Z")
    expect(formatRelativeTime("2026-04-27T11:59:30Z", now)).toBe("just now")
    expect(formatRelativeTime("2026-04-27T11:55:00Z", now)).toMatch(/^\d+m ago$/)
    expect(formatRelativeTime("2026-04-27T08:00:00Z", now)).toMatch(/^\d+h ago$/)
    expect(formatRelativeTime("2026-04-23T12:00:00Z", now)).toMatch(/^\d+d ago$/)
    expect(formatRelativeTime("2025-04-27T12:00:00Z", now)).toMatch(/^\d+y ago$/)
    expect(formatRelativeTime("not-a-date", now)).toBe("—")
  })
})

describe("buildEnergyOrbState", () => {
  it("returns per-state palette + label + ring-spin gating", () => {
    const a = buildEnergyOrbState("available", 0)
    expect(a.centerLabel).toBe("Ready")
    expect(a.innerRingFast).toBe(false)
    expect(a.rootAnimationClass).toBe("")
    const i = buildEnergyOrbState("installing", 73)
    expect(i.centerLabel).toBe("73%")
    expect(i.innerRingFast).toBe(true)
    const u = buildEnergyOrbState("update-available", 0)
    expect(u.rootAnimationClass).toBe("pulse-purple")
    const f = buildEnergyOrbState("failed", 0)
    expect(f.rootAnimationClass).toBe("force-turbo-armed")
    const ok = buildEnergyOrbState("installed", 0)
    expect(ok.centerLabel).toBe("OK")
  })
})

// ─────────────────────────────────────────────────────────────────────
// 2. Render contract
// ─────────────────────────────────────────────────────────────────────

describe("<CatalogDetailPanel /> — header surface", () => {
  it("renders back / family chip / state chip / vendor / version + counts", () => {
    const onBack = vi.fn()
    renderPanel({
      entry: baseEntry({
        installState: "update-available",
        metadata: {
          nextVersion: "2.0.0",
          depends_on: ["d1"],
          used_by_workspaces: [{ id: "w", name: "W", productLine: "web" }],
          activity: [
            { id: "a1", kind: "installed", timestamp: "2026-04-01T00:00:00Z", message: "yo" },
          ],
        },
      }),
      onBack,
    })
    fireEvent.click(screen.getByTestId("catalog-detail-panel-back"))
    expect(onBack).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId("catalog-detail-panel-family-chip").textContent).toBe(
      "Software",
    )
    expect(screen.getByTestId("catalog-detail-panel-state-chip").textContent).toBe(
      "Update available",
    )
    expect(screen.getByTestId("catalog-detail-panel-vendor").textContent).toBe(
      "Acme",
    )
    expect(screen.getByTestId("catalog-detail-panel-version").textContent).toBe(
      "v1.2.3",
    )
    // Aggregate data-* counts surface for BS.6.8 deep-link.
    const root = screen.getByTestId("catalog-detail-panel")
    expect(root.getAttribute("data-deps-count")).toBe("1")
    expect(root.getAttribute("data-usedby-count")).toBe("1")
    expect(root.getAttribute("data-activity-total")).toBe("1")
  })

  it("update-available state surfaces the next-version sub-line", () => {
    renderPanel({
      entry: baseEntry({
        installState: "update-available",
        metadata: { nextVersion: "9.9.9" },
      }),
    })
    expect(
      screen.getByTestId("catalog-detail-panel-update-version").textContent,
    ).toBe("→ v9.9.9")
  })
})

describe("<CatalogDetailPanel /> — energy orb", () => {
  it("flips data-orb-state + data-orb-progress for installing state", () => {
    renderPanel({
      entry: baseEntry({
        installState: "installing",
        metadata: { progressPercent: 42 },
      }),
    })
    const orb = screen.getByTestId("catalog-detail-panel-energy-orb")
    expect(orb.getAttribute("data-orb-state")).toBe("installing")
    expect(orb.getAttribute("data-orb-progress")).toBe("42.00")
    expect(orb.getAttribute("data-orb-inner-fast")).toBe("true")
    expect(
      screen.getByTestId("catalog-detail-panel-energy-orb-progress-ring"),
    ).toBeInTheDocument()
    expect(
      screen.getByTestId("catalog-detail-panel-energy-orb-label").textContent,
    ).toBe("42%")
  })
})

describe("<CatalogDetailPanel /> — sections", () => {
  it("dependencies + used-by render lists; clicking dep button fires onSelectDependency", () => {
    const onSelectDependency = vi.fn()
    const onSelectWorkspace = vi.fn()
    renderPanel({
      entry: baseEntry({
        metadata: {
          depends_on: ["dep-a"],
          depends_on_meta: { "dep-a": { displayName: "A", state: "installed" } },
          used_by_workspaces: [{ id: "ws-1", name: "Studio", productLine: "web" }],
        },
      }),
      onSelectDependency,
      onSelectWorkspace,
    })
    const depList = screen.getByTestId("catalog-detail-panel-deps-list")
    expect(depList.querySelectorAll("li").length).toBe(1)
    const depBtn = depList.querySelector(
      "[data-testid='catalog-detail-panel-dep-dep-a'] button",
    ) as HTMLButtonElement
    expect(depBtn).toBeTruthy()
    fireEvent.click(depBtn)
    expect(onSelectDependency).toHaveBeenCalledWith("dep-a")
    const usedByBtn = screen
      .getByTestId("catalog-detail-panel-usedby-ws-1")
      .querySelector("button") as HTMLButtonElement
    fireEvent.click(usedByBtn)
    expect(onSelectWorkspace).toHaveBeenCalledTimes(1)
    expect(onSelectWorkspace.mock.calls[0][0]?.id).toBe("ws-1")
  })

  it("activity timeline respects activityLimit + folds the rest", () => {
    const events = Array.from({ length: 9 }, (_, i) => ({
      id: `e${i}`,
      kind: "info",
      timestamp: `2026-04-${String(20 - i).padStart(2, "0")}T00:00:00Z`,
      message: `event ${i}`,
    }))
    renderPanel({
      entry: baseEntry({ metadata: { activity: events } }),
      activityLimit: 4,
      now: () => Date.parse("2026-04-25T00:00:00Z"),
    })
    const root = screen.getByTestId("catalog-detail-panel")
    expect(root.getAttribute("data-activity-total")).toBe("9")
    expect(root.getAttribute("data-activity-visible")).toBe("4")
    expect(
      screen.getByTestId("catalog-detail-panel-activity-more").textContent,
    ).toMatch(/\+ 5 more/)
  })
})

// ─────────────────────────────────────────────────────────────────────
// 3. Footer CTA — BS.6.7 disabled tooltip + handler wiring
// ─────────────────────────────────────────────────────────────────────

describe("<CatalogDetailPanel /> — footer CTA", () => {
  it("available state shows Install button wrapped in pending tooltip when handler absent", () => {
    renderPanel({ entry: baseEntry({ installState: "available" }) })
    expect(
      screen.getByTestId(
        "catalog-detail-panel-action-install-pending-tooltip",
      ),
    ).toBeInTheDocument()
    const btn = screen.getByTestId(
      "catalog-detail-panel-action-install",
    ) as HTMLButtonElement
    expect(btn.disabled).toBe(true)
    expect(btn.getAttribute("title")).toBe(CATALOG_INSTALL_PENDING_TOOLTIP)
  })

  it("wired onInstall enables the button + collapses pending wrapper", () => {
    const onInstall = vi.fn()
    renderPanel({
      entry: baseEntry({ installState: "available" }),
      onInstall,
    })
    expect(
      screen.queryByTestId(
        "catalog-detail-panel-action-install-pending-tooltip",
      ),
    ).toBeNull()
    const btn = screen.getByTestId(
      "catalog-detail-panel-action-install",
    ) as HTMLButtonElement
    expect(btn.disabled).toBe(false)
    fireEvent.click(btn)
    expect(onInstall).toHaveBeenCalledTimes(1)
    expect(onInstall.mock.calls[0][0]?.id).toBe("vision")
  })

  it("failed state renders Retry + View log, both pending when no handlers", () => {
    renderPanel({ entry: baseEntry({ installState: "failed" }) })
    const retry = screen.getByTestId(
      "catalog-detail-panel-action-retry",
    ) as HTMLButtonElement
    const log = screen.getByTestId(
      "catalog-detail-panel-action-view-log",
    ) as HTMLButtonElement
    expect(retry.disabled).toBe(true)
    expect(log.disabled).toBe(true)
    expect(retry.getAttribute("title")).toBe(CATALOG_INSTALL_PENDING_TOOLTIP)
    expect(log.getAttribute("title")).toBe(CATALOG_INSTALL_PENDING_TOOLTIP)
  })
})
