/**
 * V6 #4 (TODO row 1551 / issue #322) — DeviceGrid component tests.
 *
 * The grid is the shared viewing surface for:
 *   - V6 #5 agent visual-context injector (queries `data-device` and
 *     pulls the screenshot out of each cell per ReAct turn),
 *   - V7 mobile visual annotator (hit-tests inside the same cells),
 *   - the Mobile Workspace dashboard.
 *
 * The contract these consumers rely on:
 *   1. Default "all six presets, same screenshot" fan-out when the
 *      caller only supplies `screenshotUrl`.
 *   2. Per-cell overrides for screenshot / loading / empty / alt.
 *   3. Filtering by platform or form shrinks the grid deterministically.
 *   4. Unknown device ids + duplicates are silently dropped — agent
 *      data must never crash the view.
 *   5. Keyboard + click selection invoke `onSelectDevice` with the
 *      stable `DeviceProfileId` string (not the DOM node).
 *   6. The pure helpers (`normaliseDeviceGridItems`,
 *      `filterDeviceGridItems`, `buildDeviceGridStyle`,
 *      `nextSelectionIndex`) are deterministic for SSR-shell layout.
 */

import { describe, expect, it, vi } from "vitest"
import { render, screen, fireEvent, within } from "@testing-library/react"

import {
  DeviceGrid,
  DEFAULT_DEVICE_GRID_ITEMS,
  buildDeviceGridStyle,
  filterDeviceGridItems,
  nextSelectionIndex,
  normaliseDeviceGridItems,
  type DeviceGridItem,
} from "@/components/omnisight/device-grid"
import {
  DEVICE_PROFILE_IDS,
  type DeviceProfileId,
} from "@/components/omnisight/device-frame"

const ALL_IDS: DeviceProfileId[] = [
  "iphone-15",
  "iphone-se",
  "ipad",
  "pixel-8",
  "galaxy-fold",
  "galaxy-tab",
]

describe("DeviceGrid — defaults", () => {
  it("DEFAULT_DEVICE_GRID_ITEMS mirrors DEVICE_PROFILE_IDS in order", () => {
    expect(DEFAULT_DEVICE_GRID_ITEMS.map((i) => i.device)).toEqual(
      DEVICE_PROFILE_IDS,
    )
    expect(DEFAULT_DEVICE_GRID_ITEMS).toHaveLength(6)
  })

  it("DEFAULT_DEVICE_GRID_ITEMS is frozen", () => {
    expect(Object.isFrozen(DEFAULT_DEVICE_GRID_ITEMS)).toBe(true)
    expect(Object.isFrozen(DEFAULT_DEVICE_GRID_ITEMS[0])).toBe(true)
  })
})

describe("DeviceGrid — normaliseDeviceGridItems", () => {
  it("returns [] for undefined / empty input", () => {
    expect(normaliseDeviceGridItems(undefined)).toEqual([])
    expect(normaliseDeviceGridItems([])).toEqual([])
  })

  it("upgrades plain ids to items", () => {
    const out = normaliseDeviceGridItems(["iphone-15", "pixel-8"])
    expect(out).toEqual([{ device: "iphone-15" }, { device: "pixel-8" }])
  })

  it("preserves per-item overrides", () => {
    const out = normaliseDeviceGridItems([
      { device: "iphone-15", screenshotUrl: "a.png" },
      { device: "pixel-8", loading: true },
    ])
    expect(out).toEqual([
      { device: "iphone-15", screenshotUrl: "a.png" },
      { device: "pixel-8", loading: true },
    ])
  })

  it("drops unknown device ids silently", () => {
    const out = normaliseDeviceGridItems([
      "iphone-15",
      "not-a-device" as DeviceProfileId,
      "pixel-8",
    ])
    expect(out.map((i) => i.device)).toEqual(["iphone-15", "pixel-8"])
  })

  it("drops duplicates — first occurrence wins", () => {
    const out = normaliseDeviceGridItems([
      { device: "iphone-15", screenshotUrl: "first.png" },
      { device: "iphone-15", screenshotUrl: "second.png" },
    ])
    expect(out).toHaveLength(1)
    expect(out[0].screenshotUrl).toBe("first.png")
  })

  it("skips falsy / empty entries defensively", () => {
    const out = normaliseDeviceGridItems([
      null as unknown as DeviceProfileId,
      undefined as unknown as DeviceProfileId,
      { device: "" as DeviceProfileId },
      "iphone-15",
    ])
    expect(out).toEqual([{ device: "iphone-15" }])
  })
})

describe("DeviceGrid — filterDeviceGridItems", () => {
  const items: DeviceGridItem[] = ALL_IDS.map((id) => ({ device: id }))

  it("returns a copy when no filters supplied", () => {
    const out = filterDeviceGridItems(items, {})
    expect(out).toEqual(items)
    expect(out).not.toBe(items)
  })

  it("filters by platform", () => {
    const out = filterDeviceGridItems(items, { platforms: ["ios"] })
    expect(out.map((i) => i.device)).toEqual(["iphone-15", "iphone-se", "ipad"])
  })

  it("filters by form", () => {
    const out = filterDeviceGridItems(items, { forms: ["tablet"] })
    expect(out.map((i) => i.device)).toEqual(["ipad", "galaxy-tab"])
  })

  it("filters by platform AND form (intersection)", () => {
    const out = filterDeviceGridItems(items, {
      platforms: ["android"],
      forms: ["foldable"],
    })
    expect(out.map((i) => i.device)).toEqual(["galaxy-fold"])
  })

  it("empty filter arrays mean 'no filter' (not 'exclude everything')", () => {
    const out = filterDeviceGridItems(items, { platforms: [], forms: [] })
    expect(out).toHaveLength(items.length)
  })
})

describe("DeviceGrid — buildDeviceGridStyle", () => {
  it("uses auto-fit by default", () => {
    const s = buildDeviceGridStyle({})
    expect(s.display).toBe("grid")
    expect(String(s.gridTemplateColumns)).toContain("auto-fit")
    expect(String(s.gridTemplateColumns)).toContain("minmax(")
    expect(s.gap).toBe(20)
  })

  it("explicit columns override auto-fit", () => {
    const s = buildDeviceGridStyle({ columns: 3 })
    expect(s.gridTemplateColumns).toBe("repeat(3, minmax(0, 1fr))")
  })

  it("rejects zero / negative / non-finite columns and falls back to auto-fit", () => {
    for (const c of [0, -3, Number.NaN, Number.POSITIVE_INFINITY]) {
      const s = buildDeviceGridStyle({ columns: c })
      expect(String(s.gridTemplateColumns)).toContain("auto-fit")
    }
  })

  it("respects custom minColumnWidth", () => {
    const s = buildDeviceGridStyle({ minColumnWidth: 400 })
    expect(String(s.gridTemplateColumns)).toContain("400px")
  })

  it("clamps tiny minColumnWidth to 48 (keeps grid usable)", () => {
    const s = buildDeviceGridStyle({ minColumnWidth: 10 })
    expect(String(s.gridTemplateColumns)).toContain("48px")
  })

  it("respects custom gap", () => {
    expect(buildDeviceGridStyle({ gap: 8 }).gap).toBe(8)
  })

  it("floors fractional columns so React's grid template stays integer", () => {
    const s = buildDeviceGridStyle({ columns: 2.9 })
    expect(s.gridTemplateColumns).toBe("repeat(2, minmax(0, 1fr))")
  })
})

describe("DeviceGrid — nextSelectionIndex", () => {
  it("ArrowLeft wraps around", () => {
    expect(nextSelectionIndex(0, 6, "ArrowLeft", 3)).toBe(5)
    expect(nextSelectionIndex(3, 6, "ArrowLeft", 3)).toBe(2)
  })

  it("ArrowRight wraps around", () => {
    expect(nextSelectionIndex(5, 6, "ArrowRight", 3)).toBe(0)
    expect(nextSelectionIndex(2, 6, "ArrowRight", 3)).toBe(3)
  })

  it("ArrowUp jumps by column count, clamps at row 0", () => {
    expect(nextSelectionIndex(4, 6, "ArrowUp", 3)).toBe(1)
    expect(nextSelectionIndex(1, 6, "ArrowUp", 3)).toBe(1) // clamped
  })

  it("ArrowDown jumps by column count, clamps at last row", () => {
    expect(nextSelectionIndex(1, 6, "ArrowDown", 3)).toBe(4)
    expect(nextSelectionIndex(4, 6, "ArrowDown", 3)).toBe(4) // clamped
  })

  it("Home / End go to first / last", () => {
    expect(nextSelectionIndex(3, 6, "Home", 3)).toBe(0)
    expect(nextSelectionIndex(3, 6, "End", 3)).toBe(5)
  })

  it("returns -1 when grid is empty", () => {
    expect(nextSelectionIndex(0, 0, "ArrowRight", 3)).toBe(-1)
  })

  it("treats current=-1 as 'start at 0' for arrow keys", () => {
    expect(nextSelectionIndex(-1, 6, "ArrowRight", 3)).toBe(1)
    expect(nextSelectionIndex(-1, 6, "ArrowLeft", 3)).toBe(5)
  })
})

describe("DeviceGrid — default rendering", () => {
  it("renders all six presets when no `devices` prop is supplied", () => {
    render(<DeviceGrid data-testid="grid" screenshotUrl="https://x/y.png" />)
    const gridSection = screen.getByTestId("grid")
    expect(gridSection.getAttribute("data-device-count")).toBe("6")
    for (const id of ALL_IDS) {
      const cell = screen.getByTestId(`grid-cell-${id}`)
      expect(cell).toBeInTheDocument()
      expect(cell.getAttribute("data-device")).toBe(id)
      // each cell inherits the grid-wide screenshot
      const img = within(cell).getByTestId(
        `grid-frame-${id}-screenshot`,
      ) as HTMLImageElement
      expect(img.getAttribute("src")).toBe("https://x/y.png")
    }
  })

  it("renders devices in DEVICE_PROFILE_IDS order by default", () => {
    render(<DeviceGrid data-testid="grid" />)
    // Scope to direct children — `data-device` also appears on the
    // inner DeviceFrame `<figure>` so a tree-wide query sees every id
    // twice and breaks the order assertion.
    const cells = screen
      .getByTestId("grid-grid")
      .querySelectorAll(":scope > [data-device]")
    const rendered = Array.from(cells).map((c) => c.getAttribute("data-device"))
    expect(rendered).toEqual(ALL_IDS)
  })

  it("aria-label defaults to 'Multi-device preview'", () => {
    render(<DeviceGrid data-testid="grid" />)
    expect(screen.getByTestId("grid").getAttribute("aria-label")).toBe(
      "Multi-device preview",
    )
  })
})

describe("DeviceGrid — custom devices + per-cell overrides", () => {
  it("honours a caller-supplied devices list", () => {
    render(
      <DeviceGrid
        data-testid="grid"
        devices={["iphone-15", "pixel-8"]}
        screenshotUrl="https://x/y.png"
      />,
    )
    expect(screen.getByTestId("grid-cell-iphone-15")).toBeInTheDocument()
    expect(screen.getByTestId("grid-cell-pixel-8")).toBeInTheDocument()
    expect(screen.queryByTestId("grid-cell-ipad")).not.toBeInTheDocument()
  })

  it("per-cell screenshotUrl beats the grid-wide screenshotUrl", () => {
    render(
      <DeviceGrid
        data-testid="grid"
        screenshotUrl="https://x/default.png"
        devices={[
          { device: "iphone-15", screenshotUrl: "https://x/custom.png" },
          { device: "pixel-8" },
        ]}
      />,
    )
    expect(
      (
        screen.getByTestId(
          "grid-frame-iphone-15-screenshot",
        ) as HTMLImageElement
      ).getAttribute("src"),
    ).toBe("https://x/custom.png")
    expect(
      (
        screen.getByTestId("grid-frame-pixel-8-screenshot") as HTMLImageElement
      ).getAttribute("src"),
    ).toBe("https://x/default.png")
  })

  it("per-cell loading overrides grid-wide screenshot (mixed state)", () => {
    render(
      <DeviceGrid
        data-testid="grid"
        screenshotUrl="https://x/y.png"
        devices={[
          { device: "iphone-15" },
          { device: "pixel-8", loading: true },
        ]}
      />,
    )
    // iphone-15 shows its image
    expect(
      screen.getByTestId("grid-frame-iphone-15-screenshot"),
    ).toBeInTheDocument()
    // pixel-8 shows a shimmer, NOT an image
    expect(
      screen.queryByTestId("grid-frame-pixel-8-screenshot"),
    ).not.toBeInTheDocument()
    expect(screen.getByRole("status", { name: /loading screenshot/i })).toBeInTheDocument()
  })

  it("grid-wide loading=true applies to every cell", () => {
    render(<DeviceGrid data-testid="grid" loading />)
    // six shimmers — each frame paints one
    expect(screen.getAllByRole("status", { name: /loading screenshot/i })).toHaveLength(
      6,
    )
  })

  it("per-cell empty shows 'no screenshot' placeholder", () => {
    render(
      <DeviceGrid
        data-testid="grid"
        devices={[{ device: "iphone-15", empty: true }]}
      />,
    )
    expect(screen.getByText("no screenshot")).toBeInTheDocument()
  })

  it("per-cell alt override reaches the <img>", () => {
    render(
      <DeviceGrid
        data-testid="grid"
        screenshotUrl="https://x/y.png"
        devices={[{ device: "iphone-15", alt: "login page on iPhone 15" }]}
      />,
    )
    expect(
      (
        screen.getByTestId(
          "grid-frame-iphone-15-screenshot",
        ) as HTMLImageElement
      ).getAttribute("alt"),
    ).toBe("login page on iPhone 15")
  })
})

describe("DeviceGrid — filtering", () => {
  it("platforms=['ios'] hides Android devices", () => {
    render(<DeviceGrid data-testid="grid" platforms={["ios"]} />)
    expect(screen.getByTestId("grid-cell-iphone-15")).toBeInTheDocument()
    expect(screen.getByTestId("grid-cell-ipad")).toBeInTheDocument()
    expect(screen.queryByTestId("grid-cell-pixel-8")).not.toBeInTheDocument()
  })

  it("forms=['phone'] hides tablets and foldables", () => {
    render(<DeviceGrid data-testid="grid" forms={["phone"]} />)
    expect(screen.queryByTestId("grid-cell-ipad")).not.toBeInTheDocument()
    expect(screen.queryByTestId("grid-cell-galaxy-fold")).not.toBeInTheDocument()
    expect(screen.queryByTestId("grid-cell-galaxy-tab")).not.toBeInTheDocument()
    expect(screen.getByTestId("grid-cell-iphone-15")).toBeInTheDocument()
    expect(screen.getByTestId("grid-cell-iphone-se")).toBeInTheDocument()
    expect(screen.getByTestId("grid-cell-pixel-8")).toBeInTheDocument()
  })

  it("filter combinations that resolve to zero show the empty placeholder", () => {
    render(
      <DeviceGrid
        data-testid="grid"
        platforms={["ios"]}
        forms={["foldable"]}
      />,
    )
    expect(screen.getByTestId("grid-empty")).toBeInTheDocument()
    expect(screen.queryByTestId("grid-grid")).not.toBeInTheDocument()
  })
})

describe("DeviceGrid — selection", () => {
  it("applies the selected ring + aria-selected=true on the active cell", () => {
    const onSelectDevice = vi.fn()
    render(
      <DeviceGrid
        data-testid="grid"
        selectedDevice="pixel-8"
        onSelectDevice={onSelectDevice}
      />,
    )
    const cell = screen.getByTestId("grid-cell-pixel-8")
    expect(cell.getAttribute("aria-selected")).toBe("true")
    // other cells are aria-selected="false"
    expect(screen.getByTestId("grid-cell-iphone-15").getAttribute("aria-selected")).toBe(
      "false",
    )
  })

  it("click on a cell's frame invokes onSelectDevice with the device id", () => {
    const onSelectDevice = vi.fn()
    render(
      <DeviceGrid
        data-testid="grid"
        onSelectDevice={onSelectDevice}
      />,
    )
    fireEvent.click(screen.getByTestId("grid-frame-pixel-8"))
    expect(onSelectDevice).toHaveBeenCalledWith("pixel-8")
  })

  it("without onSelectDevice, cells don't carry aria-selected", () => {
    render(<DeviceGrid data-testid="grid" />)
    expect(
      screen.getByTestId("grid-cell-iphone-15").getAttribute("aria-selected"),
    ).toBeNull()
  })

  it("ArrowRight advances selection to the next device", () => {
    const onSelectDevice = vi.fn()
    render(
      <DeviceGrid
        data-testid="grid"
        selectedDevice="iphone-15"
        onSelectDevice={onSelectDevice}
        columns={3}
      />,
    )
    fireEvent.keyDown(screen.getByTestId("grid-grid"), { key: "ArrowRight" })
    expect(onSelectDevice).toHaveBeenCalledWith("iphone-se")
  })

  it("ArrowDown jumps a whole row (columns=3 → index 0 → 3)", () => {
    const onSelectDevice = vi.fn()
    render(
      <DeviceGrid
        data-testid="grid"
        selectedDevice="iphone-15"
        onSelectDevice={onSelectDevice}
        columns={3}
      />,
    )
    fireEvent.keyDown(screen.getByTestId("grid-grid"), { key: "ArrowDown" })
    expect(onSelectDevice).toHaveBeenCalledWith("pixel-8")
  })

  it("Home / End jump to the first / last device", () => {
    const onSelectDevice = vi.fn()
    render(
      <DeviceGrid
        data-testid="grid"
        selectedDevice="ipad"
        onSelectDevice={onSelectDevice}
        columns={3}
      />,
    )
    fireEvent.keyDown(screen.getByTestId("grid-grid"), { key: "End" })
    expect(onSelectDevice).toHaveBeenLastCalledWith("galaxy-tab")
    fireEvent.keyDown(screen.getByTestId("grid-grid"), { key: "Home" })
    expect(onSelectDevice).toHaveBeenLastCalledWith("iphone-15")
  })

  it("arrow keys without onSelectDevice are a no-op (silent)", () => {
    render(<DeviceGrid data-testid="grid" />)
    fireEvent.keyDown(screen.getByTestId("grid-grid"), { key: "ArrowRight" })
    // no error; grid just stays as-is.
    expect(screen.getByTestId("grid")).toBeInTheDocument()
  })

  it("unrelated keys don't trigger selection", () => {
    const onSelectDevice = vi.fn()
    render(
      <DeviceGrid
        data-testid="grid"
        selectedDevice="iphone-15"
        onSelectDevice={onSelectDevice}
        columns={3}
      />,
    )
    fireEvent.keyDown(screen.getByTestId("grid-grid"), { key: "Tab" })
    fireEvent.keyDown(screen.getByTestId("grid-grid"), { key: "a" })
    expect(onSelectDevice).not.toHaveBeenCalled()
  })
})

describe("DeviceGrid — empty state", () => {
  it("renders a placeholder when `devices` is empty", () => {
    render(<DeviceGrid data-testid="grid" devices={[]} />)
    expect(screen.getByTestId("grid-empty")).toBeInTheDocument()
    expect(screen.queryByTestId("grid-grid")).not.toBeInTheDocument()
    expect(screen.getByTestId("grid").getAttribute("data-device-count")).toBe(
      "0",
    )
  })

  it("uses a caller-supplied emptyLabel", () => {
    render(
      <DeviceGrid
        data-testid="grid"
        devices={[]}
        emptyLabel="nothing captured yet"
      />,
    )
    expect(screen.getByText("nothing captured yet")).toBeInTheDocument()
  })

  it("drops unknown ids — 'only unknowns' resolves to the empty state", () => {
    render(
      <DeviceGrid
        data-testid="grid"
        devices={["mystery" as DeviceProfileId, "nope" as DeviceProfileId]}
      />,
    )
    expect(screen.getByTestId("grid-empty")).toBeInTheDocument()
  })
})

describe("DeviceGrid — header + layout props", () => {
  it("renders title + description when provided", () => {
    render(
      <DeviceGrid
        data-testid="grid"
        title="Mobile preview"
        description="Same login page across six targets"
      />,
    )
    const header = screen.getByTestId("grid-header")
    expect(within(header).getByText("Mobile preview")).toBeInTheDocument()
    expect(
      within(header).getByText("Same login page across six targets"),
    ).toBeInTheDocument()
  })

  it("omits the header when no title / description is given", () => {
    render(<DeviceGrid data-testid="grid" />)
    expect(screen.queryByTestId("grid-header")).not.toBeInTheDocument()
  })

  it("merges a custom className with the base", () => {
    render(<DeviceGrid data-testid="grid" className="my-card" />)
    const section = screen.getByTestId("grid")
    expect(section.className).toContain("omnisight-device-grid")
    expect(section.className).toContain("my-card")
  })

  it("frame width + column count are applied to the grid style", () => {
    render(
      <DeviceGrid
        data-testid="grid"
        frameWidth={200}
        columns={2}
        gap={12}
      />,
    )
    const inner = screen.getByTestId("grid-grid")
    expect(inner.style.gridTemplateColumns).toBe("repeat(2, minmax(0, 1fr))")
    expect(inner.style.gap).toBe("12px")
  })
})
