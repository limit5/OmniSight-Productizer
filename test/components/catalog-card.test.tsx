/**
 * BS.6.8 — `<CatalogCard />` contract tests.
 *
 * Locks the props/visual contract for the 5-state catalog card shipped
 * by BS.6.2 + the 8-layer motion library wired by BS.6.6 + the disabled
 * tooltip affordance owned by BS.6.7. The card is a stateless props-
 * driven render so tests mount it with explicit props and assert on the
 * stable `data-testid` surface plus the exported pure helpers
 * (`clampInstallProgress`, `coerceInstallState`,
 * `buildInstallProgressGradient`, `pickCatalogCardFloatVariant`,
 * `CATALOG_INSTALL_PENDING_TOOLTIP`).
 *
 * Layers under test:
 *   1. Pure helpers — defensive parse contracts.
 *   2. Per-state visuals — 5 install states render the right testid +
 *      data-state + footer affordance.
 *   3. BS.6.7 disabled tooltip — pending wrapper mounts with title +
 *      tab-stop when no handler; collapses when handler present.
 *   4. BS.6.6 motion layering — `data-motion-{level,float,...}` reflect
 *      the resolver chain across motion levels.
 *
 * `useEffectiveMotionLevel` is mocked so each motion test drives a
 * specific level without standing up the full BS.3.5 chain (battery,
 * prefers-reduced-motion, persisted user pref).
 */

import { afterEach, describe, expect, it, vi } from "vitest"
import { fireEvent, render, screen } from "@testing-library/react"

import type { MotionLevel } from "@/lib/motion-preferences"
import type { CatalogEntry } from "@/components/omnisight/catalog-tab"

// Mutable mock state — set before render() in each motion test.
let mockLevel: MotionLevel = "normal"

vi.mock("@/hooks/use-effective-motion-level", () => ({
  useEffectiveMotionLevel: () => mockLevel,
  usePrefersReducedMotion: () => false,
}))

// Vitest's ESM transform evaluates side-effects of `@/components/omnisight/
// category-strip` before `catalog-tab` finishes initialising — the
// category strip's `["all", ...CATALOG_FAMILIES]` spread reads `undefined`
// and throws `not iterable`. The card under test never renders the chip
// strip, so we stub it with a no-op component to break the cycle.
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
  CatalogCard,
  CATALOG_CARD_FLOAT_VARIANTS,
  CATALOG_INSTALL_PENDING_TOOLTIP,
  buildInstallProgressGradient,
  clampInstallProgress,
  coerceInstallState,
  pickCatalogCardFloatVariant,
} from "@/components/omnisight/catalog-card"
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
  description: "A short tagline.",
  ...overrides,
})

function renderCard(props: Partial<Parameters<typeof CatalogCard>[0]> = {}) {
  return render(
    <TooltipProvider>
      <CatalogCard
        entry={props.entry ?? baseEntry()}
        density={props.density ?? "comfortable"}
        cardPaddingClass={props.cardPaddingClass ?? "p-3 text-xs"}
        installProgressPercent={props.installProgressPercent}
        onSelect={props.onSelect}
        onInstall={props.onInstall}
        onRetry={props.onRetry}
        onViewLog={props.onViewLog}
        floatVariantIndex={props.floatVariantIndex}
        className={props.className}
      />
    </TooltipProvider>,
  )
}

// ─────────────────────────────────────────────────────────────────────
// 1. Pure helpers — exported so tests lock contract directly.
// ─────────────────────────────────────────────────────────────────────

describe("clampInstallProgress", () => {
  it("collapses non-finite / non-numeric / out-of-range to [0,100]", () => {
    expect(clampInstallProgress(0)).toBe(0)
    expect(clampInstallProgress(50)).toBe(50)
    expect(clampInstallProgress(100)).toBe(100)
    expect(clampInstallProgress(-5)).toBe(0)
    expect(clampInstallProgress(101)).toBe(100)
    expect(clampInstallProgress(Number.NaN)).toBe(0)
    expect(clampInstallProgress(Number.POSITIVE_INFINITY)).toBe(0)
    expect(clampInstallProgress(undefined)).toBe(0)
    expect(clampInstallProgress(null)).toBe(0)
    expect(clampInstallProgress("42")).toBe(42)
    expect(clampInstallProgress("not-a-number")).toBe(0)
  })
})

describe("coerceInstallState", () => {
  it("maps unknown / falsy values back to 'available' (forward-compat)", () => {
    expect(coerceInstallState("available")).toBe("available")
    expect(coerceInstallState("installed")).toBe("installed")
    expect(coerceInstallState("installing")).toBe("installing")
    expect(coerceInstallState("update-available")).toBe("update-available")
    expect(coerceInstallState("failed")).toBe("failed")
    expect(coerceInstallState("queued")).toBe("available")
    expect(coerceInstallState(null)).toBe("available")
    expect(coerceInstallState(undefined)).toBe("available")
  })
})

describe("buildInstallProgressGradient", () => {
  it("emits a conic-gradient string starting at 12 o'clock with the requested stop", () => {
    const g0 = buildInstallProgressGradient(0)
    const g50 = buildInstallProgressGradient(50)
    const g100 = buildInstallProgressGradient(100)
    expect(g0).toMatch(/^conic-gradient\(from -90deg/)
    expect(g50).toContain("0%")
    expect(g50).toContain("50%")
    expect(g100).toContain("100%")
    // Values outside [0,100] are clamped.
    expect(buildInstallProgressGradient(-10)).toContain("0%")
    expect(buildInstallProgressGradient(250)).toContain("100%")
  })
})

describe("pickCatalogCardFloatVariant", () => {
  it("returns one of a/b/c/d for any seed (numeric or string)", () => {
    const variants = new Set<string>()
    variants.add(pickCatalogCardFloatVariant(0))
    variants.add(pickCatalogCardFloatVariant(1))
    variants.add(pickCatalogCardFloatVariant(2))
    variants.add(pickCatalogCardFloatVariant(3))
    expect([...variants].sort()).toEqual(["a", "b", "c", "d"])
    // String seeds are deterministic — same id maps to same variant.
    const v1 = pickCatalogCardFloatVariant("entry-X")
    const v2 = pickCatalogCardFloatVariant("entry-X")
    expect(v1).toBe(v2)
    expect(CATALOG_CARD_FLOAT_VARIANTS).toContain(v1)
    // Non-finite numeric seeds collapse to variant 'a' (idx 0).
    expect(pickCatalogCardFloatVariant(Number.NaN)).toBe("a")
    expect(pickCatalogCardFloatVariant(Number.POSITIVE_INFINITY)).toBe("a")
    // Negative seeds wrap via Math.abs so they still pick a real bucket.
    expect(CATALOG_CARD_FLOAT_VARIANTS).toContain(
      pickCatalogCardFloatVariant(-7),
    )
  })
})

// ─────────────────────────────────────────────────────────────────────
// 2. Per-state visuals — 5 install states.
// ─────────────────────────────────────────────────────────────────────

describe("<CatalogCard /> — available state", () => {
  it("renders Install CTA wrapped in pending tooltip + sets data-state=available", () => {
    renderCard({ entry: baseEntry({ installState: "available" }) })
    const root = screen.getByTestId("catalog-card-ent-1")
    expect(root.getAttribute("data-state")).toBe("available")
    expect(root.getAttribute("data-entry-family")).toBe("software")
    expect(screen.getByTestId("catalog-card-state-chip").textContent).toContain(
      "Available",
    )
    // Pending tooltip wrapper is mounted because no onInstall handler.
    const wrapper = screen.getByTestId(
      "catalog-card-action-install-pending-tooltip",
    )
    expect(wrapper.getAttribute("data-pending-install-tooltip")).toBe("true")
    expect(wrapper.getAttribute("aria-label")).toBe(
      CATALOG_INSTALL_PENDING_TOOLTIP,
    )
    // Button is disabled with the same title fallback.
    const btn = screen.getByTestId(
      "catalog-card-action-install",
    ) as HTMLButtonElement
    expect(btn.disabled).toBe(true)
    expect(btn.getAttribute("title")).toBe(CATALOG_INSTALL_PENDING_TOOLTIP)
  })
})

describe("<CatalogCard /> — installed state", () => {
  it("renders the static badge instead of a button + emerald state chip", () => {
    renderCard({ entry: baseEntry({ installState: "installed" }) })
    expect(screen.getByTestId("catalog-card-ent-1").getAttribute("data-state")).toBe(
      "installed",
    )
    expect(
      screen.getByTestId("catalog-card-action-installed-badge"),
    ).toBeInTheDocument()
    expect(screen.queryByTestId("catalog-card-action-install")).toBeNull()
    expect(screen.queryByTestId("catalog-card-progress-block")).toBeNull()
  })
})

describe("<CatalogCard /> — installing state", () => {
  it("conic-gradient shell + hazard overlay + live progress readout", () => {
    renderCard({
      entry: baseEntry({
        installState: "installing",
        metadata: { progressPercent: 73 },
      }),
    })
    const root = screen.getByTestId("catalog-card-ent-1")
    expect(root.getAttribute("data-state")).toBe("installing")
    expect(root.getAttribute("data-progress")).toBe("73.00")
    expect(screen.getByTestId("catalog-card-hazard-overlay")).toBeInTheDocument()
    expect(screen.getByTestId("catalog-card-progress-ring")).toBeInTheDocument()
    expect(
      screen.getByTestId("catalog-card-progress-value").textContent,
    ).toBe("73%")
    expect(
      screen.getByTestId("catalog-card-action-installing-label"),
    ).toBeInTheDocument()
  })

  it("clamps a runaway 150% progressPercent to 100", () => {
    renderCard({
      entry: baseEntry({
        installState: "installing",
        metadata: { progressPercent: 150 },
      }),
    })
    expect(
      screen.getByTestId("catalog-card-ent-1").getAttribute("data-progress"),
    ).toBe("100.00")
    expect(screen.getByTestId("catalog-card-progress-value").textContent).toBe(
      "100%",
    )
  })

  it("installProgressPercent prop overrides metadata.progressPercent", () => {
    renderCard({
      entry: baseEntry({
        installState: "installing",
        metadata: { progressPercent: 10 },
      }),
      installProgressPercent: 88,
    })
    expect(
      screen.getByTestId("catalog-card-ent-1").getAttribute("data-progress"),
    ).toBe("88.00")
  })
})

describe("<CatalogCard /> — update-available state", () => {
  it("renders Update CTA + nextVersion sub-line when metadata present", () => {
    renderCard({
      entry: baseEntry({
        installState: "update-available",
        metadata: { nextVersion: "2.0.0" },
      }),
    })
    expect(screen.getByTestId("catalog-card-ent-1").getAttribute("data-state")).toBe(
      "update-available",
    )
    expect(screen.getByTestId("catalog-card-update-version").textContent).toBe(
      "→ v2.0.0",
    )
    expect(screen.getByTestId("catalog-card-action-update")).toBeInTheDocument()
    // The chip carries the pulse-purple class for the breathing accent.
    expect(screen.getByTestId("catalog-card-state-chip").className).toMatch(
      /pulse-purple/,
    )
  })

  it("omits the next-version sub-line when metadata.nextVersion is missing", () => {
    renderCard({
      entry: baseEntry({ installState: "update-available", metadata: {} }),
    })
    expect(screen.queryByTestId("catalog-card-update-version")).toBeNull()
  })
})

describe("<CatalogCard /> — failed state", () => {
  it("renders Retry + log buttons + failure reason from metadata", () => {
    renderCard({
      entry: baseEntry({
        installState: "failed",
        metadata: { failureReason: "shasum mismatch" },
      }),
    })
    expect(screen.getByTestId("catalog-card-ent-1").getAttribute("data-state")).toBe(
      "failed",
    )
    expect(screen.getByTestId("catalog-card-action-retry")).toBeInTheDocument()
    expect(screen.getByTestId("catalog-card-action-view-log")).toBeInTheDocument()
    expect(screen.getByTestId("catalog-card-error-message").textContent).toBe(
      "shasum mismatch",
    )
  })
})

// ─────────────────────────────────────────────────────────────────────
// 3. Density toggling — compact hides description + footer.
// ─────────────────────────────────────────────────────────────────────

describe("<CatalogCard /> — density-aware layout", () => {
  it("compact density hides description + footer", () => {
    renderCard({
      density: "compact",
      cardPaddingClass: "p-2 text-[11px]",
      entry: baseEntry({
        description: "should be hidden",
        installState: "available",
      }),
    })
    expect(screen.queryByTestId("catalog-card-description")).toBeNull()
    expect(screen.queryByTestId("catalog-card-footer")).toBeNull()
  })

  it("comfortable density shows description + footer", () => {
    renderCard({
      density: "comfortable",
      entry: baseEntry({ description: "must show" }),
    })
    expect(screen.getByTestId("catalog-card-description")).toBeInTheDocument()
    expect(screen.getByTestId("catalog-card-footer")).toBeInTheDocument()
  })
})

// ─────────────────────────────────────────────────────────────────────
// 4. onSelect interaction.
// ─────────────────────────────────────────────────────────────────────

describe("<CatalogCard /> — onSelect interaction", () => {
  it("fires onSelect on click + Enter/Space when handler is wired", () => {
    const onSelect = vi.fn()
    renderCard({ onSelect, entry: baseEntry() })
    const root = screen.getByTestId("catalog-card-ent-1")
    expect(root.getAttribute("role")).toBe("button")
    expect(root.getAttribute("tabindex")).toBe("0")
    fireEvent.click(root)
    fireEvent.keyDown(root, { key: "Enter" })
    fireEvent.keyDown(root, { key: " " })
    expect(onSelect).toHaveBeenCalledTimes(3)
  })

  it("is non-interactive when onSelect is omitted", () => {
    renderCard({ entry: baseEntry() })
    const root = screen.getByTestId("catalog-card-ent-1")
    expect(root.getAttribute("role")).toBeNull()
    expect(root.getAttribute("tabindex")).toBeNull()
  })

  it("fires onInstall + stops propagation so the card click doesn't double-fire", () => {
    const onSelect = vi.fn()
    const onInstall = vi.fn()
    renderCard({ onSelect, onInstall, entry: baseEntry() })
    const btn = screen.getByTestId(
      "catalog-card-action-install",
    ) as HTMLButtonElement
    expect(btn.disabled).toBe(false)
    fireEvent.click(btn)
    expect(onInstall).toHaveBeenCalledTimes(1)
    expect(onSelect).not.toHaveBeenCalled()
  })
})

// ─────────────────────────────────────────────────────────────────────
// 5. BS.6.7 — disabled tooltip wrapper contract.
// ─────────────────────────────────────────────────────────────────────

describe("<CatalogCard /> — BS.6.7 pending tooltip wrapper", () => {
  it("collapses to passthrough when an onInstall handler is wired", () => {
    renderCard({ onInstall: vi.fn(), entry: baseEntry() })
    expect(
      screen.queryByTestId("catalog-card-action-install-pending-tooltip"),
    ).toBeNull()
    const btn = screen.getByTestId(
      "catalog-card-action-install",
    ) as HTMLButtonElement
    expect(btn.disabled).toBe(false)
    expect(btn.getAttribute("title")).toBeNull()
  })

  it("failed state — retry + log each get their own pending wrapper when handlers absent", () => {
    renderCard({
      entry: baseEntry({ installState: "failed" }),
    })
    expect(
      screen.getByTestId("catalog-card-action-retry-pending-tooltip"),
    ).toBeInTheDocument()
    expect(
      screen.getByTestId("catalog-card-action-view-log-pending-tooltip"),
    ).toBeInTheDocument()
    const retryBtn = screen.getByTestId(
      "catalog-card-action-retry",
    ) as HTMLButtonElement
    const logBtn = screen.getByTestId(
      "catalog-card-action-view-log",
    ) as HTMLButtonElement
    expect(retryBtn.disabled).toBe(true)
    expect(logBtn.disabled).toBe(true)
    expect(retryBtn.getAttribute("title")).toBe(CATALOG_INSTALL_PENDING_TOOLTIP)
    expect(logBtn.getAttribute("title")).toBe(CATALOG_INSTALL_PENDING_TOOLTIP)
  })
})

// ─────────────────────────────────────────────────────────────────────
// 6. BS.6.6 — motion layering.
// ─────────────────────────────────────────────────────────────────────

describe("<CatalogCard /> — BS.6.6 motion layering", () => {
  it("OFF level: every motion-* attribute reads 'off'", () => {
    mockLevel = "off"
    renderCard({ entry: baseEntry() })
    const root = screen.getByTestId("catalog-card-ent-1")
    expect(root.getAttribute("data-motion-level")).toBe("off")
    expect(root.getAttribute("data-motion-float")).toBe("off")
    expect(root.getAttribute("data-motion-tilt")).toBe("off")
    expect(root.getAttribute("data-motion-reflect")).toBe("off")
    expect(root.getAttribute("data-motion-glow")).toBe("off")
    // Available-state Sparkles icon orbital-rotate is also off.
    expect(
      screen.getByTestId("catalog-card-state-icon").getAttribute(
        "data-state-icon-orbital",
      ),
    ).toBe("off")
  })

  it("DRAMATIC level: float + reflect + glow all engage; available icon orbits", () => {
    mockLevel = "dramatic"
    renderCard({ entry: baseEntry({ installState: "available" }) })
    const root = screen.getByTestId("catalog-card-ent-1")
    expect(root.getAttribute("data-motion-level")).toBe("dramatic")
    expect(root.getAttribute("data-motion-float")).toBe("on")
    expect(root.getAttribute("data-motion-reflect")).toBe("on")
    expect(root.getAttribute("data-motion-glow")).toBe("on")
    expect(
      screen.getByTestId("catalog-card-state-icon").getAttribute(
        "data-state-icon-orbital",
      ),
    ).toBe("on")
  })

  it("explicit floatVariantIndex cycles a/b/c/d deterministically", () => {
    mockLevel = "normal"
    const { rerender } = renderCard({ floatVariantIndex: 0, entry: baseEntry() })
    expect(
      screen.getByTestId("catalog-card-ent-1").getAttribute(
        "data-motion-float-variant",
      ),
    ).toBe("a")
    rerender(
      <TooltipProvider>
        <CatalogCard
          entry={baseEntry()}
          density="comfortable"
          cardPaddingClass="p-3 text-xs"
          floatVariantIndex={3}
        />
      </TooltipProvider>,
    )
    expect(
      screen.getByTestId("catalog-card-ent-1").getAttribute(
        "data-motion-float-variant",
      ),
    ).toBe("d")
  })
})
