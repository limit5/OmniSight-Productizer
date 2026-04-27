"use client"

/**
 * BS.11.5 — e2e fixture page for the Platforms catalog visual-regression
 * matrix (5 critical viewports × 3 motion levels).
 *
 * Mirrors the BS.5/BS.6 surface that operators actually see at
 * `/settings/platforms?tab=catalog`, but rendered with deterministic
 * data so the screenshots stay byte-stable across runs:
 *
 *   • `<PlatformHero counters={DETERMINISTIC_COUNTERS} entries={…} />`
 *     drives the orbital diagram + counter strip + ENERGY CORE bar with
 *     fixed values (no `useHostMetricsTick()` tick that would shift each
 *     run).
 *   • `<CatalogTab entries={DETERMINISTIC_ENTRIES} renderCard={…} />`
 *     drives the toolbar + grid with a hand-crafted 6-entry list that
 *     covers every BS.6.2 visual variant: each of the 5 install states
 *     (`available` / `installing` / `installed` / `update-available` /
 *     `failed`) plus a second `available` entry so the grid renders ≥ 2
 *     columns at desktop widths.
 *   • `<CatalogDetailPanel entry={…} />` is rendered inline through the
 *     tab's `renderDetail` slot when the spec passes `?view=detail` —
 *     BS.6.3 swaps the grid for the detail panel without remounting.
 *
 * Importantly: this page does NOT call `useEngine`, `useAuth`, or the
 * BS.5.x `useHostMetricsTick()` hook. The visual spec stubs
 * `/api/v1/auth/whoami` + `/api/v1/auth/tenants` + `/api/v1/events` and
 * the user-preferences fetches at the browser layer (see
 * `e2e/bs11-5-catalog-visual.spec.ts`); everything else is pure render.
 *
 * The spec injects motion level via `?motion=`:
 *   • `off`      — also drives `page.emulateMedia({ reducedMotion:
 *                  "reduce" })` so `useEffectiveMotionLevel()`'s OS
 *                  short-circuit fires.
 *   • `normal`   — `motion_level` user-pref stub returns "normal".
 *   • `dramatic` — `motion_level` user-pref stub returns "dramatic".
 *
 * Density is held at `comfortable` (the BS.6.5 default) so width-based
 * grid layouts are the only variable across viewports.
 *
 * If the BS.6.x card / hero contracts ever shift the deterministic
 * fixtures here MUST be updated alongside — the spec is otherwise
 * faithful to whatever the source-of-truth components render today.
 */

import { Suspense, useCallback, useMemo } from "react"
import { useSearchParams } from "next/navigation"

import { CatalogCard } from "@/components/omnisight/catalog-card"
import { CatalogDetailPanel } from "@/components/omnisight/catalog-detail-panel"
import {
  CatalogTab,
  type CatalogEntry,
} from "@/components/omnisight/catalog-tab"
import {
  PLATFORM_COUNTERS_ZERO,
  PlatformHero,
  type PlatformCounters,
} from "@/components/omnisight/platform-hero"
import type { InstalledPlatformEntry } from "@/components/omnisight/orbital-diagram"

// ─────────────────────────────────────────────────────────────────────
// Deterministic fixtures.
// ─────────────────────────────────────────────────────────────────────

const FIXTURE_COUNTERS: PlatformCounters = {
  ...PLATFORM_COUNTERS_ZERO,
  installed: 4,
  available: 22,
  installing: 1,
  diskUsedGb: 18.5,
  diskTotalGb: 64.0,
}

/** Six catalog entries chosen to cover every BS.6.2 visual variant
 *  exactly once + one filler `available` row so the grid lays out ≥ 2
 *  columns at desktop widths.
 *
 *  Slugs / vendors are stable strings rather than realistic copies so a
 *  rename in the live catalog feed never silently invalidates the
 *  screenshots — the spec is testing the rendering pipeline, not the
 *  catalog data itself. */
const FIXTURE_CATALOG_ENTRIES: ReadonlyArray<CatalogEntry> = [
  {
    id: "fixture-android-sdk",
    displayName: "Android SDK",
    vendor: "Google",
    family: "mobile",
    version: "34.0.0",
    installState: "installed",
    description: "Reference Android SDK with platform tools and build-tools.",
    updatedAt: "2026-04-20T08:00:00Z",
    source: "shipped",
  },
  {
    id: "fixture-esp-idf",
    displayName: "ESP-IDF",
    vendor: "Espressif",
    family: "embedded",
    version: "5.2.1",
    installState: "installing",
    description: "Espressif IoT development framework toolchain.",
    updatedAt: "2026-04-22T08:00:00Z",
    source: "shipped",
    metadata: {
      // BS.6.2 progress hint — the spec also injects an
      // `installProgressPercent` via `renderCard` so the conic-gradient
      // ring lands on a stable angle.
    },
  },
  {
    id: "fixture-yocto-meta",
    displayName: "Yocto meta-omnisight",
    vendor: "Yocto Project",
    family: "embedded",
    version: "kirkstone-4.0.18",
    installState: "update-available",
    description: "Embedded Linux meta-layer with the OmniSight overlay.",
    updatedAt: "2026-04-25T08:00:00Z",
    source: "shipped",
    metadata: { nextVersion: "scarthgap-5.0.4" },
  },
  {
    id: "fixture-rk-bsp",
    displayName: "Rockchip RK3588 BSP",
    vendor: "Rockchip",
    family: "embedded",
    version: "1.4.0",
    installState: "failed",
    description: "Vendor BSP for the RK3588 SoC family.",
    updatedAt: "2026-04-19T08:00:00Z",
    source: "shipped",
    metadata: {
      failureReason: "checksum mismatch on toolchain tarball",
    },
  },
  {
    id: "fixture-web-vite",
    displayName: "Vite + React Workspace",
    vendor: "OmniSight",
    family: "web",
    version: "1.0.0",
    installState: "available",
    description: "Web workspace template with Tailwind and Playwright.",
    updatedAt: "2026-04-15T08:00:00Z",
    source: "shipped",
  },
  {
    id: "fixture-py-runtime",
    displayName: "Python 3.12 Runtime",
    vendor: "Python Software Foundation",
    family: "software",
    version: "3.12.4",
    installState: "available",
    description: "Embedded CPython runtime with platform wheels prebuilt.",
    updatedAt: "2026-04-10T08:00:00Z",
    source: "shipped",
  },
]

const FIXTURE_ORBITAL_ENTRIES: ReadonlyArray<InstalledPlatformEntry> = [
  { id: "fixture-android-sdk", name: "Android SDK", status: "healthy", kind: "sdk", version: "34.0.0" },
  { id: "fixture-esp-idf", name: "ESP-IDF", status: "installing", kind: "sdk", version: "5.2.1" },
  { id: "fixture-yocto-meta", name: "Yocto meta-omnisight", status: "healthy", kind: "bsp", version: "kirkstone-4.0.18" },
  { id: "fixture-rk-bsp", name: "Rockchip RK3588 BSP", status: "failed", kind: "bsp", version: "1.4.0" },
]

// ─────────────────────────────────────────────────────────────────────
// Inner page — wrapped in Suspense per Next.js 15 / React 19 rule
// that `useSearchParams()` must be inside a Suspense boundary.
// ─────────────────────────────────────────────────────────────────────

type FixtureView = "grid" | "detail"

function Inner() {
  const params = useSearchParams()
  const view: FixtureView = params.get("view") === "detail" ? "detail" : "grid"
  const motion = params.get("motion") || "default"
  // Pin the entry surfaced by the detail panel so the screenshot for
  // `view=detail` is independent of the spec's grid scroll position.
  const detailEntry = useMemo(
    () =>
      FIXTURE_CATALOG_ENTRIES.find((e) => e.id === "fixture-android-sdk")
        ?? FIXTURE_CATALOG_ENTRIES[0],
    [],
  )

  // BS.6.2 — the installing card paints a conic-gradient ring that snaps
  // to the supplied progress percentage. Pinning to 65 gives the
  // screenshot a recognisable angle that is neither full nor empty.
  const renderCard = useCallback(
    (ctx: {
      entry: CatalogEntry
      density: "compact" | "comfortable" | "spacious"
      cardPaddingClass: string
      floatVariantIndex: number
      tabIndex?: number
      onSelect?: () => void
    }) => {
      const installProgressPercent = ctx.entry.installState === "installing" ? 65 : undefined
      return (
        <CatalogCard
          entry={ctx.entry}
          density={ctx.density}
          cardPaddingClass={ctx.cardPaddingClass}
          floatVariantIndex={ctx.floatVariantIndex}
          tabIndex={ctx.tabIndex}
          installProgressPercent={installProgressPercent}
        />
      )
    },
    [],
  )

  return (
    <main
      className="min-h-screen bg-[var(--background)] text-[var(--foreground)] p-6 md:p-10"
      data-testid="bs11-5-fixture-root"
      data-fixture-view={view}
      data-fixture-motion-input={motion}
    >
      <div className="mx-auto max-w-6xl">
        <header className="mb-6">
          <h1 className="text-xl font-semibold">Platforms (fixture)</h1>
          <p className="mt-1 font-mono text-[10px] text-[var(--muted-foreground)]">
            BS.11.5 visual-diff fixture · view={view} · motion-input={motion}
          </p>
        </header>

        <div className="mb-6">
          <PlatformHero counters={FIXTURE_COUNTERS} entries={FIXTURE_ORBITAL_ENTRIES} />
        </div>

        {view === "detail" ? (
          <section
            data-testid="bs11-5-fixture-detail-section"
            className="rounded-lg border border-[var(--border)] bg-[var(--card)] p-6"
          >
            <CatalogDetailPanel entry={detailEntry} onBack={() => undefined} />
          </section>
        ) : (
          <section
            data-testid="bs11-5-fixture-grid-section"
            className="rounded-lg border border-[var(--border)] bg-[var(--card)] p-6"
          >
            <CatalogTab
              entries={FIXTURE_CATALOG_ENTRIES}
              renderCard={renderCard}
              // jsdom's missing layout engine has no effect in real
              // Chromium, but disabling virtualization keeps every card
              // in the rendered DOM regardless of viewport height — the
              // screenshot must capture the full grid surface, not just
              // the visible window. BS.6.5's `disableVirtualization`
              // opt-out exists for exactly this scenario.
              disableVirtualization
            />
          </section>
        )}
      </div>
    </main>
  )
}

export default function CatalogVisualFixturePage() {
  return (
    <Suspense fallback={<div data-testid="bs11-5-fixture-loading">loading…</div>}>
      <Inner />
    </Suspense>
  )
}
