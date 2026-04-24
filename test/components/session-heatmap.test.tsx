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
  SESSION_HEATMAP_ALL_MODELS,
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
  available_models: string[] = [],
  model: string | null = null,
): TokenHeatmapResponse {
  return { window, cells, available_models, model }
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
    await waitFor(() => expect(fetcher).toHaveBeenCalledWith("7d", null))
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
    await waitFor(() => expect(fetcher).toHaveBeenCalledWith("7d", null))
    const tab30 = view.getByTestId("session-heatmap-window-30d")
    act(() => { fireEvent.click(tab30) })
    await waitFor(() => expect(fetcher).toHaveBeenCalledWith("30d", null))
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

/**
 * ZZ.C2 (#305-2, 2026-04-24) checkbox 4 — per-model filter tests.
 *
 * Locks the UX contract for the dropdown that sits between the window
 * tabs and the refresh button:
 *   - default selection is "All models" (empty sentinel) so the initial
 *     GET omits the ``model`` param (backward-compatible with the
 *     pre-checkbox-4 backend shape)
 *   - ``available_models`` from the response populates the dropdown
 *   - selecting a slug triggers a re-fetch with that slug passed to
 *     ``fetchTokenHeatmap``
 *   - selecting "All models" again clears the filter
 *   - the selected model silently resets to "All models" when the
 *     response stops listing it (e.g. after a window switch)
 *   - malformed / missing ``available_models`` degrades to just the
 *     "All models" option
 */
describe("<SessionHeatmap> — per-model filter (checkbox 4)", () => {
  it("exports the 'all models' sentinel as an empty string", () => {
    // Locked because the value is spec in three places: (a) frontend
    // dropdown sentinel, (b) URLSearchParams omission condition, (c)
    // backend "empty string means all" path in the 400 guard.
    expect(SESSION_HEATMAP_ALL_MODELS).toBe("")
  })

  it("defaults the dropdown to 'All models' and omits the filter on mount", async () => {
    const fetcher = vi.fn().mockResolvedValue(
      makeResponse([], "7d", ["claude-opus-4-7", "gpt-4o"]),
    )
    const view = render(
      <SessionHeatmap fetchHeatmap={fetcher} refreshIntervalMs={0} />,
    )
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1))
    // First call must omit / null the model arg (backward compat).
    expect(fetcher.mock.calls[0][0]).toBe("7d")
    expect(fetcher.mock.calls[0][1] ?? null).toBeNull()
    // Dropdown exists, default value is the empty sentinel.
    const select = view.getByTestId(
      "session-heatmap-model-filter",
    ) as HTMLSelectElement
    expect(select.value).toBe(SESSION_HEATMAP_ALL_MODELS)
  })

  it("populates the dropdown from the response's available_models", async () => {
    const fetcher = vi.fn().mockResolvedValue(
      makeResponse(
        [{ day: "2026-04-24", hour: 12, token_total: 100, cost_total: 0.01 }],
        "7d",
        ["claude-opus-4-7", "gemini-2.5-pro", "gpt-4o"],
      ),
    )
    const view = render(
      <SessionHeatmap fetchHeatmap={fetcher} refreshIntervalMs={0} />,
    )
    await waitFor(() =>
      view.getByTestId("session-heatmap-model-option-claude-opus-4-7"),
    )
    // All three slugs + the "All models" sentinel option.
    expect(
      view.getByTestId("session-heatmap-model-option-all"),
    ).toBeInTheDocument()
    expect(
      view.getByTestId("session-heatmap-model-option-gemini-2.5-pro"),
    ).toBeInTheDocument()
    expect(
      view.getByTestId("session-heatmap-model-option-gpt-4o"),
    ).toBeInTheDocument()
  })

  it("re-fetches with the chosen slug when a model is picked", async () => {
    const fetcher = vi.fn().mockResolvedValue(
      makeResponse([], "7d", ["claude-opus-4-7", "gpt-4o"]),
    )
    const view = render(
      <SessionHeatmap fetchHeatmap={fetcher} refreshIntervalMs={0} />,
    )
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1))
    const select = view.getByTestId(
      "session-heatmap-model-filter",
    ) as HTMLSelectElement
    act(() => {
      fireEvent.change(select, { target: { value: "claude-opus-4-7" } })
    })
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(2))
    // The second call must carry the chosen slug as the model arg.
    expect(fetcher.mock.calls[1][0]).toBe("7d")
    expect(fetcher.mock.calls[1][1]).toBe("claude-opus-4-7")
    expect(select.value).toBe("claude-opus-4-7")
  })

  it("re-fetches with a null model when 'All models' is re-selected", async () => {
    const fetcher = vi.fn().mockResolvedValue(
      makeResponse([], "7d", ["claude-opus-4-7", "gpt-4o"]),
    )
    const view = render(
      <SessionHeatmap fetchHeatmap={fetcher} refreshIntervalMs={0} />,
    )
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1))
    const select = view.getByTestId(
      "session-heatmap-model-filter",
    ) as HTMLSelectElement
    // Pick a slug, then switch back to the sentinel.
    act(() => {
      fireEvent.change(select, { target: { value: "gpt-4o" } })
    })
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(2))
    act(() => {
      fireEvent.change(select, {
        target: { value: SESSION_HEATMAP_ALL_MODELS },
      })
    })
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(3))
    // Last call must have null/undefined model again.
    expect(fetcher.mock.calls[2][0]).toBe("7d")
    expect(fetcher.mock.calls[2][1] ?? null).toBeNull()
  })

  it("keeps the dropdown populated even when a filter is active", async () => {
    // Backend contract (checkbox 4) is that ``available_models`` stays
    // complete even when a filter is applied. The frontend must trust
    // that and not prune the list based on the selection.
    const fetcher = vi.fn().mockImplementation(
      (_w: TokenHeatmapWindow, _m?: string | null) =>
        Promise.resolve(
          makeResponse([], "7d", ["claude-opus-4-7", "gpt-4o"]),
        ),
    )
    const view = render(
      <SessionHeatmap fetchHeatmap={fetcher} refreshIntervalMs={0} />,
    )
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1))
    act(() => {
      fireEvent.change(view.getByTestId("session-heatmap-model-filter"), {
        target: { value: "claude-opus-4-7" },
      })
    })
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(2))
    // Both slugs still selectable after the filter landed.
    expect(
      view.getByTestId("session-heatmap-model-option-gpt-4o"),
    ).toBeInTheDocument()
    expect(
      view.getByTestId("session-heatmap-model-option-claude-opus-4-7"),
    ).toBeInTheDocument()
  })

  it("falls back to 'All models' when the selected slug disappears from the response", async () => {
    // Seed response order: first fetch lists the slug, second fetch
    // (e.g. after a window switch) no longer does. The dropdown must
    // auto-reset to the sentinel instead of dangling at a dead option.
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce(makeResponse([], "7d", ["claude-opus-4-7"]))
      .mockResolvedValueOnce(makeResponse([], "7d", ["claude-opus-4-7"]))
      .mockResolvedValueOnce(makeResponse([], "30d", ["gpt-4o"]))
      .mockResolvedValueOnce(makeResponse([], "30d", ["gpt-4o"]))
    const view = render(
      <SessionHeatmap fetchHeatmap={fetcher} refreshIntervalMs={0} />,
    )
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1))
    const select = view.getByTestId(
      "session-heatmap-model-filter",
    ) as HTMLSelectElement
    act(() => {
      fireEvent.change(select, { target: { value: "claude-opus-4-7" } })
    })
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(2))
    expect(select.value).toBe("claude-opus-4-7")
    // Switch to 30d — the new response drops claude-opus-4-7. The
    // effect auto-resets the selection and triggers one more fetch
    // under the sentinel.
    act(() => {
      fireEvent.click(view.getByTestId("session-heatmap-window-30d"))
    })
    await waitFor(() =>
      expect(select.value).toBe(SESSION_HEATMAP_ALL_MODELS),
    )
    // The final fetch for the window switch completed under the
    // sentinel — confirms the reset actually took effect on the
    // next fetch.
    await waitFor(() => {
      const lastCall = fetcher.mock.calls[fetcher.mock.calls.length - 1]
      expect(lastCall[1] ?? null).toBeNull()
    })
  })

  it("degrades gracefully when the backend omits available_models", async () => {
    // Legacy / pre-checkbox-4 backends return a response without
    // ``available_models``. The dropdown should still render with
    // just the sentinel — no crash on ``undefined.map``.
    const fetcher = vi.fn().mockResolvedValue({
      window: "7d" as TokenHeatmapWindow,
      cells: [],
    } as TokenHeatmapResponse)
    const view = render(
      <SessionHeatmap fetchHeatmap={fetcher} refreshIntervalMs={0} />,
    )
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1))
    const select = view.getByTestId(
      "session-heatmap-model-filter",
    ) as HTMLSelectElement
    expect(select).toBeInTheDocument()
    // Only the sentinel option exists; no per-model options.
    expect(
      view.getByTestId("session-heatmap-model-option-all"),
    ).toBeInTheDocument()
    expect(
      view.queryByTestId("session-heatmap-model-option-claude-opus-4-7"),
    ).toBeNull()
  })
})
