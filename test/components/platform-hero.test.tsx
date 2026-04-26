/**
 * BS.5.5 — `<PlatformHero />` contract tests.
 *
 * Locks the props/visual contract that BS.6 (catalog hook) and BS.7
 * (install pipeline) will plumb data into. The hero is a pure
 * props-driven render of (`PlatformCounters`, `entries`), so every
 * test mounts the component with explicit props and asserts on the
 * stable `data-testid` surface BS.5.2..BS.5.4 already shipped.
 *
 * Three layers under test:
 *   1. Disk-pressure helpers — `computeDiskPercent` clamps to [0,100],
 *      `classifyDiskPressure` flips at 80% / 95%.
 *   2. Hero render — counter strip prints localised numbers, disk-bar
 *      reflects the pressure band (fill-class + glow + ARIA values),
 *      orbital frame composes `<OrbitalDiagram />` with the right
 *      `testIdPrefix`.
 *   3. BS.5.4 motion contract — `data-motion-{level,float,reflect}`
 *      attributes mirror the resolver chain across the four motion
 *      levels (off / subtle / normal / dramatic).
 *
 * `useEffectiveMotionLevel` is mocked so each motion test drives a
 * specific level without standing up the full BS.3.5 chain (battery
 * API, prefers-reduced-motion, persisted user pref).
 */

import { afterEach, describe, expect, it } from "vitest"
import { render, screen } from "@testing-library/react"
import { vi } from "vitest"

import type { MotionLevel } from "@/lib/motion-preferences"

// Mutable mock state — set before render() in each motion test.
let mockLevel: MotionLevel = "dramatic"

vi.mock("@/hooks/use-effective-motion-level", () => ({
  useEffectiveMotionLevel: () => mockLevel,
  usePrefersReducedMotion: () => false,
}))

// `useGlassReflection` calls `useEffectiveMotionLevel` directly so the
// mock above already gates it. `useFloatingCard` does the same. The
// only extra guard we need is `next/navigation` for the orbital's
// fallback `useRouter()` defensive branch — the hero never triggers
// it (no clicks fire in these tests), but importing the orbital pulls
// the hook in regardless, so we stub it to keep the mount safe.
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}))

import {
  PlatformHero,
  classifyDiskPressure,
  computeDiskPercent,
  PLATFORM_COUNTERS_ZERO,
  type PlatformCounters,
} from "@/components/omnisight/platform-hero"

afterEach(() => {
  mockLevel = "dramatic"
})

const baseCounters = (overrides: Partial<PlatformCounters> = {}): PlatformCounters => ({
  installed: 4,
  available: 12,
  installing: 1,
  diskUsedGb: 80,
  diskTotalGb: 200,
  ...overrides,
})

// ─────────────────────────────────────────────────────────────────────
// 1. Disk-pressure helpers
// ─────────────────────────────────────────────────────────────────────

describe("computeDiskPercent", () => {
  it("clamps NaN/Infinity, divide-by-zero, and out-of-range values into [0,100]", () => {
    expect(computeDiskPercent(0, 0)).toBe(0)
    expect(computeDiskPercent(50, 100)).toBe(50)
    expect(computeDiskPercent(200, 100)).toBe(100)
    expect(computeDiskPercent(-5, 100)).toBe(0)
    expect(computeDiskPercent(Number.NaN, 100)).toBe(0)
    expect(computeDiskPercent(50, Number.NaN)).toBe(0)
    expect(computeDiskPercent(Number.POSITIVE_INFINITY, 100)).toBe(0)
    expect(computeDiskPercent(50, 0)).toBe(0)
    expect(computeDiskPercent(50, -10)).toBe(0)
  })
})

describe("classifyDiskPressure", () => {
  it("flips nominal → elevated at 80% and elevated → critical at 95% (boundary-inclusive)", () => {
    expect(classifyDiskPressure(0)).toBe("nominal")
    expect(classifyDiskPressure(79.99)).toBe("nominal")
    expect(classifyDiskPressure(80)).toBe("elevated")
    expect(classifyDiskPressure(94.99)).toBe("elevated")
    expect(classifyDiskPressure(95)).toBe("critical")
    expect(classifyDiskPressure(100)).toBe("critical")
  })
})

// ─────────────────────────────────────────────────────────────────────
// 2. Hero render — props → DOM contract
// ─────────────────────────────────────────────────────────────────────

describe("<PlatformHero /> — counter strip", () => {
  it("renders the four counter tiles and toLocaleString-formats the values", () => {
    render(
      <PlatformHero
        counters={baseCounters({
          installed: 1234,
          available: 9876,
          installing: 7,
          diskUsedGb: 42.5,
          diskTotalGb: 500,
        })}
      />,
    )
    expect(
      screen.getByTestId("platform-hero-counter-installed-value").textContent,
    ).toBe((1234).toLocaleString())
    expect(
      screen.getByTestId("platform-hero-counter-available-value").textContent,
    ).toBe((9876).toLocaleString())
    expect(
      screen.getByTestId("platform-hero-counter-installing-value").textContent,
    ).toBe((7).toLocaleString())
    expect(
      screen.getByTestId("platform-hero-counter-disk-value").textContent,
    ).toBe("42.5 / 500 GB")
  })

  it("falls back to PLATFORM_COUNTERS_ZERO when counters prop is omitted", () => {
    render(<PlatformHero />)
    // All four numeric tiles read "0" when no counters arrive.
    expect(
      screen.getByTestId("platform-hero-counter-installed-value").textContent,
    ).toBe("0")
    expect(
      screen.getByTestId("platform-hero-counter-available-value").textContent,
    ).toBe("0")
    expect(
      screen.getByTestId("platform-hero-counter-installing-value").textContent,
    ).toBe("0")
    expect(
      screen.getByTestId("platform-hero-counter-disk-value").textContent,
    ).toBe("0.0 / 0 GB")
    // Defensive: PLATFORM_COUNTERS_ZERO truly is all zeros.
    expect(PLATFORM_COUNTERS_ZERO.diskUsedGb).toBe(0)
  })
})

describe("<PlatformHero /> — disk pressure bar", () => {
  it("flags 'nominal' (40%) with emerald fill + 0..100 progressbar ARIA values", () => {
    render(
      <PlatformHero counters={baseCounters({ diskUsedGb: 80, diskTotalGb: 200 })} />,
    )
    const hero = screen.getByTestId("platform-hero")
    expect(hero.getAttribute("data-disk-pressure")).toBe("nominal")
    const badge = screen.getByTestId("platform-hero-disk-pressure-badge")
    expect(badge.textContent).toBe("nominal")
    const bar = screen.getByTestId("platform-hero-disk-bar")
    expect(bar.getAttribute("role")).toBe("progressbar")
    expect(bar.getAttribute("aria-valuemin")).toBe("0")
    expect(bar.getAttribute("aria-valuemax")).toBe("100")
    expect(bar.getAttribute("aria-valuenow")).toBe("40")
    const fill = screen.getByTestId("platform-hero-disk-bar-fill")
    expect(fill.className).toMatch(/bg-emerald-500/)
    expect(fill.style.height).toBe("40%")
    expect(screen.getByTestId("platform-hero-disk-percent").textContent).toBe("40.0%")
  })

  it("flags 'elevated' (80%) with amber fill + matching badge + ARIA value", () => {
    render(
      <PlatformHero counters={baseCounters({ diskUsedGb: 160, diskTotalGb: 200 })} />,
    )
    expect(screen.getByTestId("platform-hero").getAttribute("data-disk-pressure")).toBe(
      "elevated",
    )
    expect(
      screen.getByTestId("platform-hero-disk-pressure-badge").textContent,
    ).toBe("elevated")
    expect(screen.getByTestId("platform-hero-disk-bar-fill").className).toMatch(
      /bg-amber-500/,
    )
    expect(screen.getByTestId("platform-hero-disk-bar").getAttribute("aria-valuenow")).toBe(
      "80",
    )
  })

  it("flags 'critical' (95%) with rose fill + critical badge styling", () => {
    render(
      <PlatformHero counters={baseCounters({ diskUsedGb: 190, diskTotalGb: 200 })} />,
    )
    expect(screen.getByTestId("platform-hero").getAttribute("data-disk-pressure")).toBe(
      "critical",
    )
    const badge = screen.getByTestId("platform-hero-disk-pressure-badge")
    expect(badge.textContent).toBe("critical")
    expect(badge.className).toMatch(/border-rose-500/)
    expect(screen.getByTestId("platform-hero-disk-bar-fill").className).toMatch(
      /bg-rose-500/,
    )
    expect(screen.getByTestId("platform-hero-disk-percent").textContent).toBe("95.0%")
  })

  it("clamps a runaway used > total ratio to 100% rather than overflow the bar", () => {
    render(
      <PlatformHero counters={baseCounters({ diskUsedGb: 999, diskTotalGb: 100 })} />,
    )
    expect(screen.getByTestId("platform-hero-disk-bar").getAttribute("aria-valuenow")).toBe(
      "100",
    )
    expect(screen.getByTestId("platform-hero-disk-bar-fill").style.height).toBe("100%")
    expect(screen.getByTestId("platform-hero").getAttribute("data-disk-pressure")).toBe(
      "critical",
    )
  })
})

describe("<PlatformHero /> — orbital composition", () => {
  it("mounts <OrbitalDiagram /> with the platform-hero testIdPrefix and entries", () => {
    render(
      <PlatformHero
        counters={baseCounters()}
        entries={[
          { id: "vert-retail", name: "Retail Loss-Prevention", status: "healthy", kind: "vertical" },
          { id: "sdk-vision", name: "Vision SDK", status: "installing", kind: "sdk" },
        ]}
      />,
    )
    // Orbital uses the BS.5.2 testid contract — `platform-hero-orbital-*`.
    const orbital = screen.getByTestId("platform-hero-orbital")
    expect(orbital.getAttribute("data-orbital-entries")).toBe("2")
    expect(screen.getByTestId("platform-hero-orbital-svg")).toBeInTheDocument()
    // Each entry surfaces its own dot.
    expect(screen.getByTestId("platform-hero-orbital-dot-vert-retail")).toBeInTheDocument()
    expect(screen.getByTestId("platform-hero-orbital-dot-sdk-vision")).toBeInTheDocument()
  })
})

// ─────────────────────────────────────────────────────────────────────
// 3. BS.5.4 motion-layer contract
// ─────────────────────────────────────────────────────────────────────

describe("<PlatformHero /> — BS.5.4 motion layering", () => {
  it("OFF level: tilt + reflect + float all gated to 'off' on the data attributes", () => {
    mockLevel = "off"
    render(<PlatformHero counters={baseCounters()} />)
    const hero = screen.getByTestId("platform-hero")
    expect(hero.getAttribute("data-motion-level")).toBe("off")
    expect(screen.getByTestId("platform-hero-glass").getAttribute("data-motion-reflect")).toBe(
      "off",
    )
    expect(
      screen.getByTestId("platform-hero-orbital-frame").getAttribute("data-motion-float"),
    ).toBe("off")
  })

  it("SUBTLE level: float ON (variant 'a'), tilt OFF, reflect OFF (per ADR §5.7)", () => {
    mockLevel = "subtle"
    render(<PlatformHero counters={baseCounters()} />)
    expect(screen.getByTestId("platform-hero").getAttribute("data-motion-level")).toBe(
      "subtle",
    )
    // Float is enabled at every non-off level → orbital frame carries the variant class.
    expect(
      screen.getByTestId("platform-hero-orbital-frame").getAttribute("data-motion-float"),
    ).toBe("float-card-a")
    // Tilt + reflect are still gated off.
    expect(screen.getByTestId("platform-hero-glass").getAttribute("data-motion-reflect")).toBe(
      "off",
    )
  })

  it("DRAMATIC level: float + tilt + reflect all engaged", () => {
    mockLevel = "dramatic"
    render(<PlatformHero counters={baseCounters()} />)
    const hero = screen.getByTestId("platform-hero")
    expect(hero.getAttribute("data-motion-level")).toBe("dramatic")
    // Reflection wrapper toggles to "on" + the holo-reflect-glass class lands on it.
    const glass = screen.getByTestId("platform-hero-glass")
    expect(glass.getAttribute("data-motion-reflect")).toBe("on")
    expect(glass.className).toMatch(/holo-reflect-glass/)
    // Float remains on at variant 'a'.
    expect(
      screen.getByTestId("platform-hero-orbital-frame").getAttribute("data-motion-float"),
    ).toBe("float-card-a")
    // Tilt writes a perspective transform inline on the section.
    expect(hero.style.transform).toMatch(/perspective\(800px\)/)
  })
})
