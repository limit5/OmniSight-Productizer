/**
 * BS.5.5 — `<OrbitalDiagram />` contract tests.
 *
 * Six cases covering the orbital-diagram public surface that BS.5.2's
 * hero panel + future BS.6 catalog views consume:
 *
 *   1. `distributeEntries` — placement priority (failed → installing →
 *      healthy), outer-ring-first fill, overflow accounting.
 *   2. `defaultCatalogHref` — URL shape lock (deep-link contract).
 *   3. Render — placeholder dots when entries are empty + status
 *      classes/colours on entry dots.
 *   4. Hover → tooltip surfaces entry meta + ring orbit pauses.
 *   5. Click → `onEntryClick` callback fires with the right entry,
 *      bypassing the `useRouter` defensive branch.
 *   6. Overflow → "+N more" badge appears when entries > 30 capacity.
 *
 * `next/navigation` is stubbed because the orbital pulls `useRouter`
 * for its fallback click branch even though tests pass `onEntryClick`.
 */

import { describe, expect, it, vi } from "vitest"
import { fireEvent, render, screen } from "@testing-library/react"

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}))

import {
  ORBITAL_RING_SPECS,
  ORBITAL_TOTAL_CAPACITY,
  OrbitalDiagram,
  defaultCatalogHref,
  distributeEntries,
  type InstalledPlatformEntry,
} from "@/components/omnisight/orbital-diagram"

const entry = (
  id: string,
  status: InstalledPlatformEntry["status"],
  extras: Partial<InstalledPlatformEntry> = {},
): InstalledPlatformEntry => ({
  id,
  name: extras.name ?? id,
  status,
  ...extras,
})

// ─────────────────────────────────────────────────────────────────────
// 1. distributeEntries
// ─────────────────────────────────────────────────────────────────────

describe("distributeEntries", () => {
  it("places failed/installing entries on the outer ring first, healthy entries last, and tallies overflow", () => {
    // Sanity: spec contract is 8 + 10 + 12 = 30 slots.
    expect(ORBITAL_TOTAL_CAPACITY).toBe(30)
    const entries: InstalledPlatformEntry[] = [
      entry("h1", "healthy"),
      entry("h2", "healthy"),
      entry("f1", "failed"),
      entry("i1", "installing"),
    ]
    const result = distributeEntries(entries)
    expect(result.placedCount).toBe(4)
    expect(result.overflow).toBe(0)

    // Outer ring (capacity 12) fills first; first slot at 12 o'clock
    // belongs to the highest-priority entry — the failed one.
    const outer = result.ringSlots[2]
    expect(outer.length).toBe(12)
    expect(outer[0].entry?.id).toBe("f1") // failed wins priority
    expect(outer[1].entry?.id).toBe("i1") // installing next
    expect(outer[2].entry?.id).toBe("h1") // healthy by name (h1 < h2)
    expect(outer[3].entry?.id).toBe("h2")
    // Remaining outer slots are empty placeholders.
    expect(outer[4].entry).toBeNull()

    // Overflow accounting: 31 entries → 1 overflow.
    const big = Array.from({ length: 31 }, (_, i) =>
      entry(`e${i}`, "healthy", { name: `e${String(i).padStart(2, "0")}` }),
    )
    const overflow = distributeEntries(big)
    expect(overflow.placedCount).toBe(30)
    expect(overflow.overflow).toBe(1)
  })
})

// ─────────────────────────────────────────────────────────────────────
// 2. defaultCatalogHref
// ─────────────────────────────────────────────────────────────────────

describe("defaultCatalogHref", () => {
  it("URL-encodes the entry id into the catalog deep-link", () => {
    expect(defaultCatalogHref(entry("plain", "healthy"))).toBe(
      "/settings/platforms?tab=catalog&entry=plain",
    )
    expect(defaultCatalogHref(entry("acme/sdk@1.2", "installing"))).toBe(
      "/settings/platforms?tab=catalog&entry=acme%2Fsdk%401.2",
    )
  })
})

// ─────────────────────────────────────────────────────────────────────
// 3. Render — empty + status colours
// ─────────────────────────────────────────────────────────────────────

describe("<OrbitalDiagram /> render", () => {
  it("with no entries renders all 30 placeholder slots and no overflow badge", () => {
    render(<OrbitalDiagram />)
    const root = screen.getByTestId("orbital-diagram")
    expect(root.getAttribute("data-orbital-entries")).toBe("0")
    expect(root.getAttribute("data-orbital-overflow")).toBe("0")
    // 8 + 10 + 12 = 30 placeholder dots, one per slot.
    const placeholders = document.querySelectorAll('[data-slot-state="empty"]')
    expect(placeholders.length).toBe(ORBITAL_TOTAL_CAPACITY)
    // No tooltip + no overflow badge in the empty state.
    expect(screen.queryByTestId("orbital-diagram-tooltip")).toBeNull()
    expect(screen.queryByTestId("orbital-diagram-overflow-badge")).toBeNull()
    // Each ring's data-ring-filled is 0, data-ring-capacity matches spec.
    for (const ring of ORBITAL_RING_SPECS) {
      const ringEl = screen.getByTestId(`orbital-diagram-ring-${ring.index}`)
      expect(ringEl.getAttribute("data-ring-capacity")).toBe(String(ring.capacity))
      expect(ringEl.getAttribute("data-ring-filled")).toBe("0")
    }
  })

  it("renders entry dots with status-driven colour fills", () => {
    render(
      <OrbitalDiagram
        entries={[
          entry("h", "healthy"),
          entry("i", "installing"),
          entry("f", "failed"),
        ]}
      />,
    )
    const healthy = screen.getByTestId("orbital-diagram-dot-h")
    expect(healthy.getAttribute("data-entry-status")).toBe("healthy")
    // Visible (r=3.4) circle inside the dot group carries the fill colour.
    const healthyFill = healthy.querySelector('circle[r="3.4"]')
    expect(healthyFill?.getAttribute("fill")).toBe("#34d399")

    const installingFill = screen
      .getByTestId("orbital-diagram-dot-i")
      .querySelector('circle[r="3.4"]')
    expect(installingFill?.getAttribute("fill")).toBe("#fbbf24")

    const failedFill = screen
      .getByTestId("orbital-diagram-dot-f")
      .querySelector('circle[r="3.4"]')
    expect(failedFill?.getAttribute("fill")).toBe("#f43f5e")
  })
})

// ─────────────────────────────────────────────────────────────────────
// 4. Hover → tooltip + orbit pause
// ─────────────────────────────────────────────────────────────────────

describe("<OrbitalDiagram /> hover", () => {
  it("hovering an entry surfaces the tooltip with name/status/kind/version and pauses every ring", () => {
    render(
      <OrbitalDiagram
        entries={[
          entry("vision-sdk", "installing", {
            name: "Vision SDK",
            kind: "sdk",
            version: "1.2.0",
          }),
        ]}
      />,
    )
    // Initially the orbit is running (no hover).
    const ring = screen.getByTestId("orbital-diagram-ring-2")
    expect(ring.style.animationPlayState).toBe("running")
    expect(screen.queryByTestId("orbital-diagram-tooltip")).toBeNull()

    fireEvent.mouseEnter(screen.getByTestId("orbital-diagram-dot-vision-sdk"))

    // Tooltip rendered with structured fields.
    const tooltip = screen.getByTestId("orbital-diagram-tooltip")
    expect(tooltip.getAttribute("data-tooltip-entry-id")).toBe("vision-sdk")
    expect(screen.getByTestId("orbital-diagram-tooltip-name").textContent).toBe(
      "Vision SDK",
    )
    expect(screen.getByTestId("orbital-diagram-tooltip-status").textContent).toBe(
      "Installing",
    )
    expect(screen.getByTestId("orbital-diagram-tooltip-kind").textContent).toBe("sdk")
    expect(screen.getByTestId("orbital-diagram-tooltip-version").textContent).toBe(
      "v1.2.0",
    )
    // Hovered id surfaced as a data attribute on the root.
    expect(screen.getByTestId("orbital-diagram").getAttribute("data-orbital-hovered")).toBe(
      "vision-sdk",
    )
    // All three rings paused while hovered (so the operator can click steady).
    for (const spec of ORBITAL_RING_SPECS) {
      const r = screen.getByTestId(`orbital-diagram-ring-${spec.index}`)
      expect(r.style.animationPlayState).toBe("paused")
    }

    // Mouse-leave clears tooltip + resumes orbit.
    fireEvent.mouseLeave(screen.getByTestId("orbital-diagram-dot-vision-sdk"))
    expect(screen.queryByTestId("orbital-diagram-tooltip")).toBeNull()
    expect(ring.style.animationPlayState).toBe("running")
  })
})

// ─────────────────────────────────────────────────────────────────────
// 5. Click → onEntryClick + 6. Overflow badge
// ─────────────────────────────────────────────────────────────────────

describe("<OrbitalDiagram /> activation + overflow", () => {
  it("click + Enter key both fire onEntryClick with the matching entry, bypassing useRouter", () => {
    const onEntryClick = vi.fn()
    const e = entry("retail-vert", "healthy", { name: "Retail" })
    render(<OrbitalDiagram entries={[e]} onEntryClick={onEntryClick} />)
    const dot = screen.getByTestId("orbital-diagram-dot-retail-vert")
    fireEvent.click(dot)
    expect(onEntryClick).toHaveBeenCalledTimes(1)
    expect(onEntryClick).toHaveBeenLastCalledWith(e)
    // Keyboard activation (Enter) also fires.
    fireEvent.keyDown(dot, { key: "Enter" })
    expect(onEntryClick).toHaveBeenCalledTimes(2)
    // Space too.
    fireEvent.keyDown(dot, { key: " " })
    expect(onEntryClick).toHaveBeenCalledTimes(3)
    // ARIA contract: dot is exposed as a button with the status-bearing label.
    expect(dot.getAttribute("role")).toBe("button")
    expect(dot.getAttribute("aria-label")).toBe("Retail (Healthy)")
  })

  it("renders a '+N more' overflow badge when entries exceed the 30-slot capacity", () => {
    const entries = Array.from({ length: ORBITAL_TOTAL_CAPACITY + 5 }, (_, i) =>
      entry(`e${i}`, "healthy", { name: `e${String(i).padStart(2, "0")}` }),
    )
    render(<OrbitalDiagram entries={entries} />)
    const badge = screen.getByTestId("orbital-diagram-overflow-badge")
    expect(badge.textContent).toBe("+5 more")
    expect(
      screen.getByTestId("orbital-diagram").getAttribute("data-orbital-overflow"),
    ).toBe("5")
  })
})
