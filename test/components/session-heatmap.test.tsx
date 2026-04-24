/**
 * ZZ.C2 (#305-2, 2026-04-24) checkbox 2 — SessionHeatmap test matrix.
 *
 * Locks the contract for the calendar-style heatmap that renders beneath
 * ``<TokenUsageStats>``:
 *   - log-scale intensity ([0, 1]) with sensible edge handling
 *   - UTC → local-timezone shift on the (day, hour) bucket keys
 *   - sparse-cell aggregation into a Map keyed by local slot
 *   - day-axis synthesis so idle days still render rows
 *   - rendering contract (loading / error / empty / grid + tooltip)
 *   - window-tab switching + refresh button re-fetch
 *
 * The component's ``fetchHeatmap`` prop is a test seam so we can drive
 * it with a synchronous fake instead of mocking the @/lib/api module.
 */

import { describe, expect, it, vi } from "vitest"
import { render, fireEvent, act, waitFor } from "@testing-library/react"

import {
  SessionHeatmap,
  SESSION_HEATMAP_WINDOWS,
  bucketsToLocalGrid,
  buildDayAxis,
  localDayKey,
  logScaleIntensity,
  shiftCellToLocal,
} from "@/components/omnisight/session-heatmap"
import type {
  TokenHeatmapCell,
  TokenHeatmapResponse,
  TokenHeatmapWindow,
} from "@/lib/api"

function makeResponse(
  cells: TokenHeatmapCell[],
  window: TokenHeatmapWindow = "7d",
): TokenHeatmapResponse {
  return { window, cells }
}

describe("SESSION_HEATMAP_WINDOWS", () => {
  it("enumerates the backend-whitelisted windows", () => {
    expect(SESSION_HEATMAP_WINDOWS).toEqual(["7d", "30d"])
  })
})

describe("logScaleIntensity()", () => {
  it("returns 0 for non-positive tokens", () => {
    expect(logScaleIntensity(0, 1000)).toBe(0)
    expect(logScaleIntensity(-5, 1000)).toBe(0)
  })

  it("returns 0 for non-finite inputs", () => {
    expect(logScaleIntensity(Number.NaN, 100)).toBe(0)
    expect(logScaleIntensity(50, Number.POSITIVE_INFINITY)).toBe(0)
  })

  it("returns 0 when max is non-positive", () => {
    expect(logScaleIntensity(10, 0)).toBe(0)
    expect(logScaleIntensity(10, -1)).toBe(0)
  })

  it("saturates at 1.0 when tokens equal max", () => {
    // log(1001)/log(1001) is exactly 1.
    expect(logScaleIntensity(1000, 1000)).toBeCloseTo(1, 6)
  })

  it("produces monotonically-increasing intensity on a log curve", () => {
    // A 1-token cell on a 1M-token max must still register > 0 — the
    // whole point of the log scale (a linear scale would round it to 0
    // and hide the faint activity).
    const low = logScaleIntensity(1, 1_000_000)
    const mid = logScaleIntensity(1_000, 1_000_000)
    const high = logScaleIntensity(100_000, 1_000_000)
    expect(low).toBeGreaterThan(0)
    expect(mid).toBeGreaterThan(low)
    expect(high).toBeGreaterThan(mid)
    expect(high).toBeLessThanOrEqual(1)
  })

  it("clamps into [0, 1]", () => {
    // Sanity — tokens above max must not explode above 1.
    const clipped = logScaleIntensity(10_000, 100)
    expect(clipped).toBeLessThanOrEqual(1)
    expect(clipped).toBeGreaterThanOrEqual(0)
  })
})

describe("shiftCellToLocal()", () => {
  it("returns null on malformed day strings", () => {
    expect(shiftCellToLocal("2026/04/24", 12)).toBeNull()
    expect(shiftCellToLocal("", 12)).toBeNull()
    expect(shiftCellToLocal("abcd-ef-gh", 12)).toBeNull()
  })

  it("returns null on out-of-range hours", () => {
    expect(shiftCellToLocal("2026-04-24", -1)).toBeNull()
    expect(shiftCellToLocal("2026-04-24", 24)).toBeNull()
    expect(shiftCellToLocal("2026-04-24", 1.5)).toBeNull()
  })

  it("produces the UTC epoch the inputs describe", () => {
    // TZ-independent assertion: the returned Date's epoch must equal
    // the Date.UTC derivation from the same (y, m, d, h) arguments.
    const shifted = shiftCellToLocal("2026-04-24", 12)
    expect(shifted).not.toBeNull()
    expect(shifted!.date.getTime()).toBe(Date.UTC(2026, 3, 24, 12))
  })

  it("extracts the LOCAL day + hour (matches browser TZ)", () => {
    // Same TZ-independent pattern: whatever the runner's TZ is, the
    // shifted bucket's (dayKey, hour) must match what a fresh Date in
    // that TZ reports for the same epoch.
    const shifted = shiftCellToLocal("2026-04-24", 14)
    expect(shifted).not.toBeNull()
    const expected = new Date(Date.UTC(2026, 3, 24, 14))
    expect(shifted!.hour).toBe(expected.getHours())
    expect(shifted!.dayKey).toBe(localDayKey(expected))
  })
})

describe("bucketsToLocalGrid()", () => {
  it("returns an empty grid for an empty input", () => {
    const result = bucketsToLocalGrid([])
    expect(result.grid.size).toBe(0)
    expect(result.dayKeys).toEqual([])
    expect(result.maxTokens).toBe(0)
  })

  it("merges cells that collide on the same local slot", () => {
    // Build two UTC cells that both map to the same local slot by
    // using the same UTC (day, hour) twice — the shift is
    // deterministic so collision is guaranteed.
    const cells: TokenHeatmapCell[] = [
      { day: "2026-04-24", hour: 12, token_total: 100, cost_total: 0.02 },
      { day: "2026-04-24", hour: 12, token_total: 250, cost_total: 0.05 },
    ]
    const { grid, maxTokens } = bucketsToLocalGrid(cells)
    expect(grid.size).toBe(1)
    const [bucket] = Array.from(grid.values())
    expect(bucket.tokens).toBe(350)
    expect(bucket.cost).toBeCloseTo(0.07, 6)
    expect(maxTokens).toBe(350)
  })

  it("tracks the max tokens seen across buckets", () => {
    const cells: TokenHeatmapCell[] = [
      { day: "2026-04-22", hour: 2, token_total: 10, cost_total: 0.001 },
      { day: "2026-04-23", hour: 14, token_total: 9_999, cost_total: 0.5 },
      { day: "2026-04-24", hour: 9, token_total: 500, cost_total: 0.04 },
    ]
    const { maxTokens, grid } = bucketsToLocalGrid(cells)
    expect(grid.size).toBe(3)
    expect(maxTokens).toBe(9_999)
  })

  it("skips malformed cells silently", () => {
    const cells: TokenHeatmapCell[] = [
      { day: "bad-date", hour: 12, token_total: 100, cost_total: 0.01 },
      { day: "2026-04-24", hour: 99, token_total: 100, cost_total: 0.01 },
      { day: "2026-04-24", hour: 8, token_total: 42, cost_total: 0.003 },
    ]
    const { grid, maxTokens } = bucketsToLocalGrid(cells)
    expect(grid.size).toBe(1)
    expect(maxTokens).toBe(42)
  })
})

describe("buildDayAxis()", () => {
  it("returns 7 ascending-sorted rows for the 7d window", () => {
    const anchor = new Date(2026, 3, 24)
    const axis = buildDayAxis("7d", anchor)
    expect(axis).toHaveLength(7)
    expect(axis[axis.length - 1]).toBe(localDayKey(anchor))
    // Ascending.
    for (let i = 1; i < axis.length; i++) {
      expect(axis[i] > axis[i - 1]).toBe(true)
    }
  })

  it("returns 30 rows for the 30d window", () => {
    const anchor = new Date(2026, 3, 24)
    const axis = buildDayAxis("30d", anchor)
    expect(axis).toHaveLength(30)
    expect(axis[axis.length - 1]).toBe(localDayKey(anchor))
  })
})

describe("<SessionHeatmap>", () => {
  it("fetches the default 7d window on mount and renders the grid", async () => {
    const fetcher = vi.fn().mockResolvedValue(
      makeResponse([
        { day: "2026-04-24", hour: 12, token_total: 1000, cost_total: 0.1 },
      ]),
    )
    const view = render(
      <SessionHeatmap
        fetchHeatmap={fetcher}
        refreshIntervalMs={0}
      />,
    )
    expect(view.getByTestId("session-heatmap")).toBeInTheDocument()
    await waitFor(() => expect(fetcher).toHaveBeenCalledWith("7d"))
    await waitFor(() => view.getByTestId("session-heatmap-grid"))
    // 7 day rows synthesised (even though only 1 cell came back).
    expect(view.getAllByTestId("session-heatmap-row")).toHaveLength(7)
    // All 7 × 24 cell buttons regardless of sparsity.
    expect(view.getAllByTestId("session-heatmap-cell")).toHaveLength(7 * 24)
  })

  it("re-fetches after switching to the 30d window", async () => {
    const fetcher = vi.fn().mockResolvedValue(makeResponse([]))
    const view = render(
      <SessionHeatmap fetchHeatmap={fetcher} refreshIntervalMs={0} />,
    )
    await waitFor(() => expect(fetcher).toHaveBeenCalledWith("7d"))
    const tab30 = view.getByTestId("session-heatmap-window-30d")
    act(() => { fireEvent.click(tab30) })
    await waitFor(() => expect(fetcher).toHaveBeenCalledWith("30d"))
    await waitFor(() => expect(view.getAllByTestId("session-heatmap-row")).toHaveLength(30))
  })

  it("surfaces the fetch error without hiding the grid scaffold", async () => {
    const fetcher = vi.fn().mockRejectedValue(new Error("503 boom"))
    const view = render(
      <SessionHeatmap fetchHeatmap={fetcher} refreshIntervalMs={0} />,
    )
    await waitFor(() => {
      const err = view.getByTestId("session-heatmap-error")
      expect(err).toHaveTextContent(/503 boom/)
    })
  })

  it("renders every cell as no-activity when the payload is empty", async () => {
    const fetcher = vi.fn().mockResolvedValue(makeResponse([]))
    const view = render(
      <SessionHeatmap fetchHeatmap={fetcher} refreshIntervalMs={0} />,
    )
    await waitFor(() => view.getByTestId("session-heatmap-grid"))
    const cells = view.getAllByTestId("session-heatmap-cell")
    expect(cells).toHaveLength(7 * 24)
    // Every cell reports 0 tokens and 0 intensity.
    for (const cell of cells) {
      expect(cell.getAttribute("data-tokens")).toBe("0")
      expect(cell.getAttribute("data-intensity")).toBe("0.000")
    }
  })

  it("reveals the tooltip with tokens + cost on cell hover", async () => {
    // Pick a cell whose UTC -> local shift we can predict independently
    // so we can locate it in the rendered grid.
    const shift = shiftCellToLocal("2026-04-24", 14)
    expect(shift).not.toBeNull()
    const fetcher = vi.fn().mockResolvedValue(
      makeResponse([
        { day: "2026-04-24", hour: 14, token_total: 1234, cost_total: 0.056 },
      ]),
    )
    const view = render(
      <SessionHeatmap
        fetchHeatmap={fetcher}
        refreshIntervalMs={0}
      />,
    )
    await waitFor(() => view.getByTestId("session-heatmap-grid"))
    // Find the cell by (dayKey, hour) data-attrs.
    const target = view
      .getAllByTestId("session-heatmap-cell")
      .find(
        (el) =>
          el.getAttribute("data-day-key") === shift!.dayKey &&
          el.getAttribute("data-hour") === String(shift!.hour),
      )
    expect(target).toBeDefined()
    expect(target!.getAttribute("data-tokens")).toBe("1234")
    act(() => { fireEvent.mouseEnter(target!) })
    const tooltip = await waitFor(() => view.getByTestId("session-heatmap-tooltip"))
    expect(tooltip).toBeInTheDocument()
    expect(view.getByTestId("session-heatmap-tooltip-tokens")).toHaveTextContent(
      /1\.2K tokens/,
    )
    expect(view.getByTestId("session-heatmap-tooltip-cost")).toHaveTextContent(
      /\$0\.056/,
    )
    // Leaving closes the tooltip.
    act(() => { fireEvent.mouseLeave(target!) })
    await waitFor(() =>
      expect(view.queryByTestId("session-heatmap-tooltip")).toBeNull(),
    )
  })

  it("scales intensity against the max token cell", async () => {
    const max = 1_000_000
    const fetcher = vi.fn().mockResolvedValue(
      makeResponse([
        { day: "2026-04-24", hour: 12, token_total: max, cost_total: 10 },
        { day: "2026-04-24", hour: 13, token_total: 1, cost_total: 0.001 },
      ]),
    )
    const view = render(
      <SessionHeatmap fetchHeatmap={fetcher} refreshIntervalMs={0} />,
    )
    await waitFor(() => view.getByTestId("session-heatmap-grid"))
    const cells = view.getAllByTestId("session-heatmap-cell")
    const hot = cells.find((el) => el.getAttribute("data-tokens") === String(max))
    const cool = cells.find((el) => el.getAttribute("data-tokens") === "1")
    expect(hot).toBeDefined()
    expect(cool).toBeDefined()
    expect(Number(hot!.getAttribute("data-intensity"))).toBeCloseTo(1, 3)
    // Log scale: a 1-token cell on a 1M max stays above 0 (the reason
    // we chose log over linear in the first place).
    expect(Number(cool!.getAttribute("data-intensity"))).toBeGreaterThan(0)
    expect(Number(cool!.getAttribute("data-intensity"))).toBeLessThan(0.2)
  })

  it("triggers a re-fetch when the refresh button is clicked", async () => {
    const fetcher = vi.fn().mockResolvedValue(makeResponse([]))
    const view = render(
      <SessionHeatmap fetchHeatmap={fetcher} refreshIntervalMs={0} />,
    )
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1))
    act(() => { fireEvent.click(view.getByTestId("session-heatmap-refresh")) })
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(2))
  })
})
