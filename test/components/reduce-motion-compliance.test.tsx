/**
 * BS.11.1 — Reduce-motion 全鏈 contract verification.
 *
 * Locks the cross-surface a11y rule that BS.3 motion library set up:
 * when `useEffectiveMotionLevel()` resolves to `"off"` (driven by
 * any of the four input signals — OS `prefers-reduced-motion: reduce`
 * via R25.2 short-circuit, the user's app-level `motion: off` choice
 * via Settings → Display, the BS.3.4 critical-battery rule, or an
 * explicit user pref of `"off"`), every motion-bearing surface drops
 * its animation classes / hooks / transitions.
 *
 * Surfaces under contract per the BS.11.1 row in TODO.md:
 *
 *   1. Catalog tab                — `<CatalogTab />`
 *   2. Catalog detail panel       — `<CatalogDetailPanel />`
 *   3. Platform hero               — `<PlatformHero />`
 *   4. Install drawer              — `<InstallProgressDrawer />`
 *   5. Settings → Display page    — exposes the resolver chain via
 *                                    `data-motion-suppressed` on root
 *
 * The R25.2 fallback in `app/globals.css` neutralises CSS animations
 * for the OS flag, but the in-app `"off"` signal is JS-only and must
 * be wired explicitly per surface — these tests prove that wiring.
 *
 * Each test mocks `useEffectiveMotionLevel()` (and where applicable
 * `usePrefersReducedMotion()`) so the resolver is driven directly
 * without standing up the full BS.3.5 chain (battery API, MQL, J4
 * round-trip).
 */

import * as React from "react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { render, screen, waitFor } from "@testing-library/react"

import type { MotionLevel } from "@/lib/motion-preferences"
import type { CatalogEntry } from "@/components/omnisight/catalog-tab"
import type { InstallJob } from "@/lib/api"

// ─────────────────────────────────────────────────────────────────────
// Mock plumbing
// ─────────────────────────────────────────────────────────────────────

// Mutable bag flipped by each test. The same module is hoisted at the
// top of every file by Vitest, so writing `mockLevel = "off"` before
// `render()` propagates synchronously into every consumer.
let mockLevel: MotionLevel = "dramatic"
let mockOsReducedMotion = false

vi.mock("@/hooks/use-effective-motion-level", () => ({
  useEffectiveMotionLevel: () => mockLevel,
  usePrefersReducedMotion: () => mockOsReducedMotion,
}))

// BS.11.4 — density now flows through `useUserDensityPreference`
// (J4 user_preferences API). Stub the hook so the reduce-motion
// compliance tests don't have to mount the full Auth/Tenant/api
// chain just to render `<CatalogTab />`.
vi.mock("@/hooks/use-user-density-preference", () => ({
  useUserDensityPreference: () => {
    const [d, setD] = React.useState<"compact" | "comfortable" | "spacious">(
      "comfortable",
    )
    return {
      density: d,
      setDensity: async (next: "compact" | "comfortable" | "spacious") => {
        setD(next)
      },
      hydrated: true,
    }
  },
}))

// Break the catalog-tab ↔ category-strip ESM init cycle (see
// catalog-card test for the diagnosis). The detail panel + tab
// stubs share the contract: clicking a chip flips the active family.
vi.mock("@/components/omnisight/category-strip", () => {
  const FAMILIES = ["all", "mobile", "embedded", "web", "software", "custom"] as const
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
              "aria-pressed": f === family,
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

// `next/navigation` is pulled by the orbital surface inside the hero.
// Stub a no-op router so mounting works without a Next.js shell.
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
  usePathname: () => "/",
}))

// `lib/motion-preferences` is where the Display Settings page
// imports `getMotionPreference` / `setMotionPreference` from. We
// preserve every other export (the SoT type, MOTION_LEVELS,
// DEFAULT_MOTION_LEVEL, the event bus) and only stub the I/O so
// the page mounts past its initial fetch without a real HTTP call.
vi.mock("@/lib/motion-preferences", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/motion-preferences")>(
      "@/lib/motion-preferences",
    )
  return {
    ...actual,
    getMotionPreference: vi.fn(async () => "off" as const),
    setMotionPreference: vi.fn(async (_next: string) => {}),
  }
})

// Battery hook — drive a stable "plenty" tier so the resolver chain
// doesn't degrade on its own. `useBatteryAwareMotion` is what the
// Display page calls directly; we keep its signature.
vi.mock("@/lib/battery-aware-motion", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/battery-aware-motion")>(
      "@/lib/battery-aware-motion",
    )
  return {
    ...actual,
    useBatteryAwareMotion: (userPref: MotionLevel) => ({
      effective: userPref,
      tier: "plenty" as const,
      didDegrade: false,
      forceFullOverride: false,
      setForceFullOverride: vi.fn(),
      status: { level: 1, charging: true, unsupported: false, supported: true },
    }),
  }
})

// `<Toaster />` mounts a DOM root that's harmless in jsdom but
// lights up `act()` warnings; stub to a no-op span.
vi.mock("@/components/ui/toaster", () => ({
  Toaster: () => null,
}))

// `<MotionPreview />` runs its own internal motion hooks; stub to
// a transparent slot so the Display page test doesn't have to mock
// every nested motion surface.
vi.mock("@/components/omnisight/motion-preview", () => ({
  MotionPreview: () =>
    React.createElement("div", { "data-testid": "motion-preview-stub" }),
}))

// Imports must come AFTER the mocks above so Vitest's hoist order
// doesn't import the real modules first.
import {
  InstallProgressDrawer,
} from "@/components/omnisight/install-progress-drawer"
import { PlatformHero } from "@/components/omnisight/platform-hero"
import { CatalogTab } from "@/components/omnisight/catalog-tab"
import { CatalogDetailPanel } from "@/components/omnisight/catalog-detail-panel"
import DisplaySettingsPage from "@/app/settings/display/page"

afterEach(() => {
  mockLevel = "dramatic"
  mockOsReducedMotion = false
  vi.clearAllMocks()
})

// ─────────────────────────────────────────────────────────────────────
// Fixtures
// ─────────────────────────────────────────────────────────────────────

function mkJob(overrides: Partial<InstallJob> = {}): InstallJob {
  return {
    id: overrides.id ?? "job-bs111-1",
    tenant_id: "t-default",
    entry_id: overrides.entry_id ?? "android-sdk-platform-tools",
    state: overrides.state ?? "running",
    idempotency_key: "idem-bs111",
    sidecar_id: "sidecar-1",
    protocol_version: 1,
    bytes_done: overrides.bytes_done ?? 1024 * 1024,
    bytes_total: overrides.bytes_total ?? 4 * 1024 * 1024,
    eta_seconds: overrides.eta_seconds ?? 12,
    log_tail: overrides.log_tail ?? "",
    result_json: overrides.result_json ?? null,
    error_reason: null,
    pep_decision_id: null,
    requested_by: "u1",
    queued_at: "2026-04-27T00:00:00Z",
    claimed_at: null,
    started_at: null,
    completed_at: null,
    ...overrides,
  }
}

function mkCatalogEntry(overrides: Partial<CatalogEntry> = {}): CatalogEntry {
  return {
    id: overrides.id ?? "entry-bs111",
    displayName: overrides.displayName ?? "Android SDK Platform Tools",
    family: overrides.family ?? "embedded",
    installState: overrides.installState ?? "available",
    vendor: "Google",
    version: "34.0.0",
    metadata: {},
    ...overrides,
  } as CatalogEntry
}

// ─────────────────────────────────────────────────────────────────────
// 1. Install drawer (BS.7.3 — newly gated by BS.11.1)
// ─────────────────────────────────────────────────────────────────────

describe("BS.11.1 — InstallProgressDrawer reduce-motion compliance", () => {
  it("drops `animate-spin` from chip Loader2 when motion === 'off'", () => {
    mockLevel = "off"
    render(<InstallProgressDrawer jobs={[mkJob()]} initialOpen={false} />)
    const chip = screen.getByTestId("install-drawer-chip")
    expect(chip.getAttribute("data-motion-spin")).toBe("off")
    // The Loader2 is the first child icon — its className should not
    // contain `animate-spin` once motion is off.
    const loader = chip.querySelector("svg")
    expect(loader).not.toBeNull()
    expect(loader!.getAttribute("class") ?? "").not.toMatch(/animate-spin/)
  })

  it("keeps `animate-spin` on chip Loader2 when motion === 'dramatic'", () => {
    mockLevel = "dramatic"
    render(<InstallProgressDrawer jobs={[mkJob()]} initialOpen={false} />)
    const chip = screen.getByTestId("install-drawer-chip")
    expect(chip.getAttribute("data-motion-spin")).toBe("on")
    const loader = chip.querySelector("svg")
    expect(loader!.getAttribute("class") ?? "").toMatch(/animate-spin/)
  })

  it("drops the running-row spinner + bar transition when motion === 'off'", () => {
    mockLevel = "off"
    const job = mkJob({ state: "running" })
    render(<InstallProgressDrawer jobs={[job]} initialOpen={true} />)
    const panel = screen.getByTestId("install-drawer-panel")
    expect(panel.getAttribute("data-motion-spin")).toBe("off")
    const bar = screen.getByTestId(`install-drawer-bar-${job.id}`)
    expect(bar.getAttribute("data-motion-spin")).toBe("off")
    expect(bar.innerHTML).not.toMatch(/transition-\[width\]/)
  })

  it("drops indeterminate `animate-pulse` bar when motion === 'off' and bytes_total unknown", () => {
    mockLevel = "off"
    const job = mkJob({ bytes_total: 0 })
    render(<InstallProgressDrawer jobs={[job]} initialOpen={true} />)
    const bar = screen.getByTestId(`install-drawer-bar-${job.id}`)
    expect(bar.getAttribute("data-progress-known")).toBe("false")
    expect(bar.innerHTML).not.toMatch(/animate-pulse/)
  })

  it("keeps indeterminate `animate-pulse` bar when motion === 'normal'", () => {
    mockLevel = "normal"
    const job = mkJob({ bytes_total: 0 })
    render(<InstallProgressDrawer jobs={[job]} initialOpen={true} />)
    const bar = screen.getByTestId(`install-drawer-bar-${job.id}`)
    expect(bar.innerHTML).toMatch(/animate-pulse/)
  })

  it("exposes `data-motion-level` on the drawer root for both chip + panel", () => {
    mockLevel = "subtle"
    const job = mkJob()
    const { container, rerender } = render(
      <InstallProgressDrawer jobs={[job]} initialOpen={false} />,
    )
    // Collapsed → chip wrapper carries the attr.
    expect(
      (container.firstChild as HTMLElement).getAttribute("data-motion-level"),
    ).toBe("subtle")
    rerender(<InstallProgressDrawer jobs={[job]} initialOpen={true} />)
    expect(
      (container.firstChild as HTMLElement).getAttribute("data-motion-level"),
    ).toBe("subtle")
  })
})

// ─────────────────────────────────────────────────────────────────────
// 2. Catalog tab (BS.6.1/6.6 — slide → fade swap when reduced)
// ─────────────────────────────────────────────────────────────────────

describe("BS.11.1 — CatalogTab reduce-motion compliance", () => {
  it("surfaces motion === 'off' on the tab root for downstream contract checks", () => {
    mockLevel = "off"
    render(<CatalogTab entries={[mkCatalogEntry()]} />)
    const tab = screen.getByTestId("catalog-tab")
    expect(tab.getAttribute("data-catalog-motion-level")).toBe("off")
    // Group-breathe + scroll-parallax both gate off at level === "off".
    expect(tab.getAttribute("data-motion-group-breathe")).toBe("off")
    expect(tab.getAttribute("data-motion-parallax")).toBe("off")
  })

  it("re-enables motion-bearing layers when level flips to 'dramatic'", () => {
    mockLevel = "dramatic"
    render(<CatalogTab entries={[mkCatalogEntry()]} />)
    const tab = screen.getByTestId("catalog-tab")
    expect(tab.getAttribute("data-catalog-motion-level")).toBe("dramatic")
    expect(tab.getAttribute("data-motion-group-breathe")).toBe("on")
  })
})

// ─────────────────────────────────────────────────────────────────────
// 3. Catalog detail panel (BS.6.3 — slide-in vs fade-only)
// ─────────────────────────────────────────────────────────────────────

describe("BS.11.1 — CatalogDetailPanel reduce-motion compliance", () => {
  it("flips `data-reduced-motion` true and uses fade-only on level === 'off'", () => {
    mockLevel = "off"
    render(
      <CatalogDetailPanel
        entry={mkCatalogEntry({ id: "detail-off" })}
        onBack={() => {}}
      />,
    )
    const panel = screen.getByTestId("catalog-detail-panel")
    expect(panel.getAttribute("data-motion-level")).toBe("off")
    expect(panel.getAttribute("data-reduced-motion")).toBe("true")
    expect(panel.className).not.toMatch(/slide-in-from-right/)
    expect(panel.className).toMatch(/fade-in-0/)
  })

  it("flips `data-reduced-motion` true on level === 'subtle' (per BS ADR §6 — subtle joins reduced)", () => {
    mockLevel = "subtle"
    render(
      <CatalogDetailPanel
        entry={mkCatalogEntry({ id: "detail-subtle" })}
        onBack={() => {}}
      />,
    )
    const panel = screen.getByTestId("catalog-detail-panel")
    expect(panel.getAttribute("data-reduced-motion")).toBe("true")
    expect(panel.className).not.toMatch(/slide-in-from-right/)
  })

  it("uses slide-in animation on level === 'dramatic'", () => {
    mockLevel = "dramatic"
    render(
      <CatalogDetailPanel
        entry={mkCatalogEntry({ id: "detail-dramatic" })}
        onBack={() => {}}
      />,
    )
    const panel = screen.getByTestId("catalog-detail-panel")
    expect(panel.getAttribute("data-reduced-motion")).toBe("false")
    expect(panel.className).toMatch(/slide-in-from-right/)
  })

  it("strips orb ring spin classes on level === 'off' (energy orb static)", () => {
    mockLevel = "off"
    render(
      <CatalogDetailPanel
        entry={mkCatalogEntry({ id: "detail-orb-off", installState: "installing" })}
        onBack={() => {}}
      />,
    )
    const orb = screen.getByTestId("catalog-detail-panel-energy-orb")
    expect(orb.className).not.toMatch(/orbital-rotate/)
    expect(orb.className).not.toMatch(/ring-spin/)
  })
})

// ─────────────────────────────────────────────────────────────────────
// 4. Platform hero (BS.5.4 — three layers all gate on level === 'off')
// ─────────────────────────────────────────────────────────────────────

describe("BS.11.1 — PlatformHero reduce-motion compliance", () => {
  it("gates float / tilt / reflect all to 'off' on level === 'off'", () => {
    mockLevel = "off"
    render(<PlatformHero />)
    const hero = screen.getByTestId("platform-hero")
    expect(hero.getAttribute("data-motion-level")).toBe("off")
    expect(
      screen.getByTestId("platform-hero-glass").getAttribute("data-motion-reflect"),
    ).toBe("off")
    expect(
      screen.getByTestId("platform-hero-orbital-frame").getAttribute("data-motion-float"),
    ).toBe("off")
  })

  it("re-engages the full layer stack on level === 'dramatic'", () => {
    mockLevel = "dramatic"
    render(<PlatformHero />)
    const hero = screen.getByTestId("platform-hero")
    expect(hero.getAttribute("data-motion-level")).toBe("dramatic")
    expect(
      screen.getByTestId("platform-hero-glass").getAttribute("data-motion-reflect"),
    ).toBe("on")
    expect(
      screen.getByTestId("platform-hero-orbital-frame").getAttribute("data-motion-float"),
    ).toBe("float-card-a")
  })
})

// ─────────────────────────────────────────────────────────────────────
// 5. Settings → Display page (BS.3.6 — surfaces resolver chain)
// ─────────────────────────────────────────────────────────────────────

describe("BS.11.1 — Display Settings page reduce-motion compliance", () => {
  it("surfaces `data-motion-suppressed=true` when OS prefers-reduced-motion is on", async () => {
    mockOsReducedMotion = true
    render(<DisplaySettingsPage />)
    // Wait one frame for the initial getMotionPreference() promise to
    // resolve (mocked to "off").
    await screen.findByTestId("display-settings-page")
    const root = screen.getByTestId("display-settings-page")
    expect(root.getAttribute("data-os-reduced-motion")).toBe("true")
    expect(root.getAttribute("data-motion-suppressed")).toBe("true")
    // The OS-flag indicator panel labels the live state.
    expect(
      screen.getByTestId("reduced-motion-state").getAttribute("data-reduced"),
    ).toBe("true")
  })

  it("surfaces `data-motion-suppressed=true` after user picks 'off' (JS-only signal)", async () => {
    mockOsReducedMotion = false
    render(<DisplaySettingsPage />)
    // Initial fetch resolves to "off" (mocked) so the page lands at
    // `userPref === "off"` after the promise resolves and React
    // commits the state update. `waitFor` polls until the
    // optimistic-state commit has flushed — without it we read the
    // initial DEFAULT_MOTION_LEVEL ("dramatic") and false-fail.
    await waitFor(() => {
      const root = screen.getByTestId("display-settings-page")
      expect(root.getAttribute("data-user-pref")).toBe("off")
    })
    const root = screen.getByTestId("display-settings-page")
    expect(root.getAttribute("data-motion-suppressed")).toBe("true")
  })

  it("`reduced-motion-state` reflects no-preference when OS does not request reduce", async () => {
    mockOsReducedMotion = false
    render(<DisplaySettingsPage />)
    await screen.findByTestId("display-settings-page")
    expect(
      screen.getByTestId("reduced-motion-state").getAttribute("data-reduced"),
    ).toBe("false")
    expect(screen.getByTestId("reduced-motion-state").textContent).toMatch(/no-preference/)
  })
})

// ─────────────────────────────────────────────────────────────────────
// 6. Cross-surface drift guard
// ─────────────────────────────────────────────────────────────────────

describe("BS.11.1 — cross-surface drift guard", () => {
  it("every BS.11.1 surface exposes a `data-motion-level` (or equivalent) attribute when motion='off'", () => {
    mockLevel = "off"

    // Catalog tab
    const { unmount: unmountTab } = render(<CatalogTab entries={[mkCatalogEntry()]} />)
    expect(screen.getByTestId("catalog-tab").getAttribute("data-catalog-motion-level"))
      .toBe("off")
    unmountTab()

    // Catalog detail panel
    const { unmount: unmountDetail } = render(
      <CatalogDetailPanel
        entry={mkCatalogEntry({ id: "drift-detail" })}
        onBack={() => {}}
      />,
    )
    expect(screen.getByTestId("catalog-detail-panel").getAttribute("data-motion-level"))
      .toBe("off")
    unmountDetail()

    // Hero
    const { unmount: unmountHero } = render(<PlatformHero />)
    expect(screen.getByTestId("platform-hero").getAttribute("data-motion-level"))
      .toBe("off")
    unmountHero()

    // Install drawer (collapsed → root chip wrapper carries the attr)
    const { container: drawerContainer, unmount: unmountDrawer } = render(
      <InstallProgressDrawer jobs={[mkJob()]} initialOpen={false} />,
    )
    expect(
      (drawerContainer.firstChild as HTMLElement).getAttribute("data-motion-level"),
    ).toBe("off")
    unmountDrawer()
  })
})
