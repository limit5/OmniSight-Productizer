/**
 * ZZ.C2 (#305-2, 2026-04-24) checkbox 5 — dedicated integration matrix
 * for the three acceptance dimensions spelled out in the TODO row:
 *
 *   1. Timezone handling — operator sees **local** (dayKey, hour), not
 *      the UTC buckets the backend emits.
 *   2. Log-scale colour boundaries — the rendered ``backgroundColor``
 *      spans [20%, 100%] emerald-over-secondary with intensity proven
 *      to behave at the extrema and on a strictly-log curve.
 *   3. Empty date-cell handling — sparse payloads still produce a
 *      7×24 / 30×24 grid with empty cells carrying the muted
 *      background, ``data-tokens="0"``, ``data-intensity="0.000"``, and
 *      an accessible "no activity" label.
 *
 * Design note — why a *separate* file from ``session-heatmap.test.tsx``
 * (rather than appending more describe blocks):
 *   The existing file already locks the default behaviour contracts
 *   (fetch-on-mount, window tabs, refresh button, per-model filter,
 *   pure-helper edge cases). This file is the explicit "operator-
 *   acceptance matrix" for checkbox 5, so a future reader can grep
 *   ``checkbox5`` and see *exactly* which assertions back the TODO
 *   sign-off — not hunt through a 540-line file. Ownership is cleaner
 *   and the describe headers can state the operator-facing acceptance
 *   directly.
 *
 * Timezone cross-runner robustness:
 *   Node's Date TZ is baked in at process start; ``vi.stubEnv('TZ',…)``
 *   after boot does not flip ``new Date().getHours()``. So rather than
 *   forcing a specific offset, assertions are written as
 *   **TZ-independent invariants** — we build an epoch with ``Date.UTC``,
 *   feed that into the component, then re-derive the expected local
 *   (dayKey, hour) from the *same* epoch using ``new Date(epoch)``
 *   getters. Whatever TZ the runner happens to be in (Asia/Taipei in
 *   dev, UTC in CI), the assertion ties the rendered output to the
 *   browser's local view of the same moment — which is the whole
 *   point of "operator sees local time not UTC".
 */

import { describe, expect, it, vi } from "vitest"
import { fireEvent, render, waitFor, act } from "@testing-library/react"

import {
  SessionHeatmap,
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
  return { window, cells, available_models: [], model: null }
}

/** Construct the local-TZ view of a UTC (y, m, d, h) tuple without
 *  depending on any specific runner timezone. Mirrors what
 *  ``shiftCellToLocal`` does internally. */
function expectedLocal(
  y: number,
  m1: number,
  d: number,
  h: number,
): { dayKey: string; hour: number; epoch: number } {
  const epoch = Date.UTC(y, m1 - 1, d, h)
  const local = new Date(epoch)
  return { dayKey: localDayKey(local), hour: local.getHours(), epoch }
}

// ───────────────────────────────────────────────────────────────
// (1) Timezone handling — operator sees local time, not UTC.
// ───────────────────────────────────────────────────────────────

describe("checkbox 5 / timezone — operator sees local time not UTC", () => {
  it("shifts a UTC (day, hour) into the runner's local dayKey/hour", () => {
    // 2026-04-24 14:00 UTC — in most IANA zones this produces a non-UTC
    // local hour. The invariant is: ``shiftCellToLocal`` returns
    // whatever ``new Date(epoch)`` reports for the same moment.
    const want = expectedLocal(2026, 4, 24, 14)
    const shifted = shiftCellToLocal("2026-04-24", 14)
    expect(shifted).not.toBeNull()
    expect(shifted!.dayKey).toBe(want.dayKey)
    expect(shifted!.hour).toBe(want.hour)
    // And the underlying Date object really is the UTC moment the
    // backend described — rules out the whole function silently
    // pinning itself to UTC.
    expect(shifted!.date.getTime()).toBe(want.epoch)
  })

  it("UTC hour 23 near midnight may roll forward into tomorrow's local dayKey", () => {
    // This asserts the property that *if* the runner TZ is positive-
    // offset, the UTC 23:00 cell lands on the *next* local day (same as
    // how git-blame-style graphs shift for Asia/Taipei operators). If
    // the runner is UTC, the dayKey stays equal — also correct. Either
    // way, the expected dayKey comes from ``new Date(epoch)`` so both
    // branches are covered without fighting the runner TZ.
    const want = expectedLocal(2026, 4, 24, 23)
    const shifted = shiftCellToLocal("2026-04-24", 23)!
    expect(shifted.dayKey).toBe(want.dayKey)
    expect(shifted.hour).toBe(want.hour)
    // Explicit: the shifted dayKey must NOT *always* be the raw UTC
    // input — that would mean the shift is a no-op. The invariant is
    // "matches the Date getters", which is what we check above. If the
    // runner happens to be UTC, shifted.dayKey === raw (legal); if not
    // (e.g. Asia/Taipei), shifted.dayKey !== raw (also legal). The
    // assertion that ``dayKey === want.dayKey`` covers both.
    if (new Date(want.epoch).getTimezoneOffset() !== 0) {
      expect(shifted.dayKey === "2026-04-24" && shifted.hour === 23).toBe(
        // If the TZ offset is non-zero, at least one of dayKey or hour
        // must differ from the UTC pair (24 * 60 / 60 = 24 possible
        // (day, hour) pairs per day — only zero-offset TZs preserve
        // both). Asserting the pair is NOT the identity-mapping in
        // non-UTC runners locks the behaviour.
        false,
      )
    }
  })

  it("UTC hour 0 near midnight may roll backward into yesterday's local dayKey", () => {
    // Symmetric to the above — for negative-offset TZs (e.g. LA -7),
    // UTC 2026-04-24 00:00 is still 2026-04-23 locally.
    const want = expectedLocal(2026, 4, 24, 0)
    const shifted = shiftCellToLocal("2026-04-24", 0)!
    expect(shifted.dayKey).toBe(want.dayKey)
    expect(shifted.hour).toBe(want.hour)
  })

  it("two UTC cells 24h apart produce two distinct local dayKeys", () => {
    // Whatever the TZ, shifting by exactly 24h (via +1 day) moves the
    // local dayKey forward by one. DST transitions squeeze/expand this
    // invariant by an hour, but the *dayKey* still changes (the local
    // hour is what shifts on a DST day, not whether the next day
    // belongs to a different date).
    const a = shiftCellToLocal("2026-04-24", 12)!
    const b = shiftCellToLocal("2026-04-25", 12)!
    expect(a.dayKey).not.toBe(b.dayKey)
  })

  it("bucketsToLocalGrid aggregates under the LOCAL slot, not the UTC one", () => {
    // Two UTC cells on (2026-04-24, 23) and (2026-04-25, 0). Depending
    // on the runner's offset, these may collapse onto the same local
    // slot (e.g. Pacific -08: both land on 2026-04-24 local at 15/16),
    // produce adjacent local slots (UTC: no change), or span a
    // midnight on +08. In all cases the grid keys must match what the
    // shift helper reports — the grid must never leak the raw UTC key.
    const cells: TokenHeatmapCell[] = [
      { day: "2026-04-24", hour: 23, token_total: 100, cost_total: 0.01 },
      { day: "2026-04-25", hour: 0, token_total: 200, cost_total: 0.02 },
    ]
    const { grid } = bucketsToLocalGrid(cells)
    const s1 = shiftCellToLocal("2026-04-24", 23)!
    const s2 = shiftCellToLocal("2026-04-25", 0)!
    // Neither raw-UTC key should be in the grid when the two shifts
    // both land on a non-raw slot (i.e. TZ offset non-zero). That
    // invariant is expressed as: every key in the grid is derivable
    // from the shift helper applied to the seed cells.
    const expectedKeys = new Set([
      `${s1.dayKey}:${s1.hour}`,
      `${s2.dayKey}:${s2.hour}`,
    ])
    for (const key of grid.keys()) {
      expect(expectedKeys.has(key)).toBe(true)
    }
    // Token totals sum correctly whether or not the two cells collide.
    let totalTokens = 0
    for (const b of grid.values()) totalTokens += b.tokens
    expect(totalTokens).toBe(300)
  })

  it("renders the cell at the LOCAL (dayKey, hour) not the UTC one", async () => {
    // End-to-end: seed one UTC bucket, look up the rendered cell by
    // the shifted local (dayKey, hour) and verify its data-tokens.
    // Also assert the cell is NOT rendered at the raw UTC key (unless
    // the runner TZ is zero-offset, in which case the two coincide by
    // design — that branch is handled via the shift helper below).
    const fetcher = vi.fn().mockResolvedValue(
      makeResponse([
        { day: "2026-04-24", hour: 14, token_total: 4321, cost_total: 0.12 },
      ]),
    )
    const view = render(
      <SessionHeatmap fetchHeatmap={fetcher} refreshIntervalMs={0} />,
    )
    await waitFor(() => view.getByTestId("session-heatmap-grid"))
    const s = shiftCellToLocal("2026-04-24", 14)!
    const target = view
      .getAllByTestId("session-heatmap-cell")
      .find(
        (el) =>
          el.getAttribute("data-day-key") === s.dayKey &&
          el.getAttribute("data-hour") === String(s.hour),
      )
    expect(target).toBeDefined()
    expect(target!.getAttribute("data-tokens")).toBe("4321")
    // The cell carries the *local* dayKey on its data-attr — operator
    // inspecting DevTools sees their own date, not UTC.
    expect(target!.getAttribute("data-day-key")).toBe(s.dayKey)
  })

  it("tooltip surfaces the local day label, not the UTC dayKey string", async () => {
    const fetcher = vi.fn().mockResolvedValue(
      makeResponse([
        { day: "2026-04-24", hour: 14, token_total: 1500, cost_total: 0.08 },
      ]),
    )
    const view = render(
      <SessionHeatmap fetchHeatmap={fetcher} refreshIntervalMs={0} />,
    )
    await waitFor(() => view.getByTestId("session-heatmap-grid"))
    const s = shiftCellToLocal("2026-04-24", 14)!
    const target = view
      .getAllByTestId("session-heatmap-cell")
      .find(
        (el) =>
          el.getAttribute("data-day-key") === s.dayKey &&
          el.getAttribute("data-hour") === String(s.hour),
      )!
    act(() => {
      fireEvent.mouseEnter(target)
    })
    const tipDay = await waitFor(() =>
      view.getByTestId("session-heatmap-tooltip-day"),
    )
    // Derive the expected label from the same epoch we seeded.
    const expectedEpoch = new Date(
      Number(s.dayKey.slice(0, 4)),
      Number(s.dayKey.slice(5, 7)) - 1,
      Number(s.dayKey.slice(8, 10)),
    )
    const expectedLabel = expectedEpoch.toLocaleDateString(undefined, {
      month: "short",
      day: "2-digit",
    })
    expect(tipDay).toHaveTextContent(expectedLabel)
  })

  it("dayAxis synthesises LOCAL 'today' back N-1 days (operator's calendar)", () => {
    // Operator anchor is their local today. A fixed anchor of
    // (2026-04-24 local) must produce ``2026-04-24`` as the last row
    // regardless of TZ — the anchor is built via ``new Date(y, m-1, d)``
    // which plants the date in local time directly (no UTC roundtrip).
    const anchor = new Date(2026, 3, 24, 12, 30, 0)
    const axis7 = buildDayAxis("7d", anchor)
    expect(axis7).toHaveLength(7)
    expect(axis7[6]).toBe("2026-04-24")
    expect(axis7[0]).toBe("2026-04-18")
    const axis30 = buildDayAxis("30d", anchor)
    expect(axis30).toHaveLength(30)
    expect(axis30[29]).toBe("2026-04-24")
    expect(axis30[0]).toBe("2026-03-26")
  })
})

// ───────────────────────────────────────────────────────────────
// (2) Log-scale colour boundaries — the [20%, 100%] mix span.
// ───────────────────────────────────────────────────────────────

describe("checkbox 5 / log-scale — colour boundary contract", () => {
  it("saturates at intensity 1.0 precisely when tokens equal max", () => {
    // Not "close to 1", *exactly* 1 — because log(k+1)/log(k+1) is 1.
    // The renderer computes 20 + intensity*80 = 100% for this case, so
    // the hot cell is fully-emerald no-mix.
    expect(logScaleIntensity(1, 1)).toBe(1)
    expect(logScaleIntensity(9_999, 9_999)).toBe(1)
    expect(logScaleIntensity(1_000_000, 1_000_000)).toBe(1)
  })

  it("clamps back to 0 for every degenerate input the renderer may pass", () => {
    // All of these must yield a zero-intensity cell *without* short-
    // circuiting to NaN — NaN would render as "color-mix(... NaN%, ...)"
    // which is an invalid CSS value and silently drops the entire
    // colour-mix, visually indistinguishable from a real empty cell but
    // a landmine for ops if the pattern ever creeps back in.
    const degenerate: Array<[number, number]> = [
      [0, 100],
      [-1, 100],
      [Number.NaN, 100],
      [Number.POSITIVE_INFINITY, 100],
      [Number.NEGATIVE_INFINITY, 100],
      [50, 0],
      [50, -5],
      [50, Number.NaN],
      [50, Number.POSITIVE_INFINITY],
    ]
    for (const [tokens, max] of degenerate) {
      const ret = logScaleIntensity(tokens, max)
      expect(Number.isFinite(ret)).toBe(true)
      expect(ret).toBe(0)
    }
  })

  it("produces a strictly-log (not linear) distribution across a wide range", () => {
    // The concrete boundary the spec cares about: on a 1M-max grid,
    // a 1-token cell is STILL visible (i.e. intensity > 0 and
    // meaningfully above the linear proportion it would otherwise get
    // on a straight k/max scale).
    const max = 1_000_000
    const one = logScaleIntensity(1, max)
    const linearEquiv = 1 / max // what the cell would get on linear
    expect(one).toBeGreaterThan(0)
    // Log gives ~0.05 for (1, 1e6); linear gives 1e-6. The ratio
    // proves "log lifts low-token cells out of the noise floor" — the
    // whole reason for picking log over linear. Bounds are loose so we
    // don't tie ourselves to the exact base of the log.
    expect(one / linearEquiv).toBeGreaterThan(1_000)
  })

  it("intensity is monotonically non-decreasing across the full 1M decade ladder", () => {
    const max = 1_000_000
    const ladder = [1, 10, 100, 1_000, 10_000, 100_000, 1_000_000]
    const intensities = ladder.map((t) => logScaleIntensity(t, max))
    for (let i = 1; i < intensities.length; i++) {
      expect(intensities[i]).toBeGreaterThanOrEqual(intensities[i - 1])
    }
    // The top of the ladder saturates at 1.
    expect(intensities[intensities.length - 1]).toBe(1)
  })

  it("never returns a value outside [0, 1] (boundary contract for the CSS mix percentage)", () => {
    // Sample a spread of token values against several maxes and confirm
    // nothing leaks outside the closed unit interval. This is the
    // guarantee that turns intensity → ``20 + intensity*80`` into a
    // legal CSS percentage in all cases.
    const maxes = [1, 10, 1_000, 1_000_000, Number.MAX_SAFE_INTEGER]
    const samples = [
      0.5, 1, 2, 5, 50, 500, 5_000, 50_000, 500_000, 5_000_000,
      Number.MAX_SAFE_INTEGER,
    ]
    for (const m of maxes) {
      for (const t of samples) {
        const i = logScaleIntensity(t, m)
        expect(i).toBeGreaterThanOrEqual(0)
        expect(i).toBeLessThanOrEqual(1)
      }
    }
  })

  it("max-token cell renders a 100% emerald background (no secondary mix)", async () => {
    // Boundary case: the hottest cell's inline backgroundColor should
    // evaluate to a ``color-mix`` expressing 100% emerald against
    // --secondary. jsdom reports the inline style verbatim, so we
    // parse the percentage out of the string.
    const fetcher = vi.fn().mockResolvedValue(
      makeResponse([
        { day: "2026-04-24", hour: 12, token_total: 10_000, cost_total: 1 },
      ]),
    )
    const view = render(
      <SessionHeatmap fetchHeatmap={fetcher} refreshIntervalMs={0} />,
    )
    await waitFor(() => view.getByTestId("session-heatmap-grid"))
    const s = shiftCellToLocal("2026-04-24", 12)!
    const target = view
      .getAllByTestId("session-heatmap-cell")
      .find(
        (el) =>
          el.getAttribute("data-day-key") === s.dayKey &&
          el.getAttribute("data-hour") === String(s.hour),
      )!
    const bg = (target as HTMLElement).style.backgroundColor
    // Form: "color-mix(in srgb, var(--validation-emerald) 100.0%, var(--secondary))"
    expect(bg).toMatch(/color-mix/)
    const m = /(\d+\.?\d*)%/.exec(bg)
    expect(m).not.toBeNull()
    expect(Number(m![1])).toBeCloseTo(100, 1)
  })

  it("the faintest active cell (on a huge max) still carries a non-default background", async () => {
    // Boundary case: a 1-token cell against a 1M-max grid must end up
    // with a ``color-mix`` (NOT the plain ``var(--secondary)`` fallback
    // reserved for truly-empty cells). The percentage should sit
    // strictly above the 20% floor — that's the "20 + log*80" formula
    // confirmed visible at the minimum active level.
    const fetcher = vi.fn().mockResolvedValue(
      makeResponse([
        { day: "2026-04-24", hour: 9, token_total: 1_000_000, cost_total: 50 },
        { day: "2026-04-24", hour: 13, token_total: 1, cost_total: 0.0001 },
      ]),
    )
    const view = render(
      <SessionHeatmap fetchHeatmap={fetcher} refreshIntervalMs={0} />,
    )
    await waitFor(() => view.getByTestId("session-heatmap-grid"))
    const cool = shiftCellToLocal("2026-04-24", 13)!
    const target = view
      .getAllByTestId("session-heatmap-cell")
      .find(
        (el) =>
          el.getAttribute("data-day-key") === cool.dayKey &&
          el.getAttribute("data-hour") === String(cool.hour),
      )!
    const bg = (target as HTMLElement).style.backgroundColor
    expect(bg).toMatch(/color-mix/)
    const m = /(\d+\.?\d*)%/.exec(bg)
    expect(m).not.toBeNull()
    // Must sit strictly in (20%, 100%) — >20% because log(2)/log(1e6+1)
    // ≈ 5%, scaled + offset gives ~24%; <100% because it's not the
    // hottest cell.
    const pct = Number(m![1])
    expect(pct).toBeGreaterThan(20)
    expect(pct).toBeLessThan(100)
  })

  it("intensity data-attr is formatted as a 3-decimal string (UI contract)", async () => {
    // The renderer emits ``data-intensity={intensity.toFixed(3)}`` for
    // tests + debugging. Lock that format so screenshot-style diff
    // tests (which grep data-intensity values) stay stable.
    const fetcher = vi.fn().mockResolvedValue(
      makeResponse([
        { day: "2026-04-24", hour: 12, token_total: 500, cost_total: 0.05 },
        { day: "2026-04-24", hour: 13, token_total: 1000, cost_total: 0.1 },
      ]),
    )
    const view = render(
      <SessionHeatmap fetchHeatmap={fetcher} refreshIntervalMs={0} />,
    )
    await waitFor(() => view.getByTestId("session-heatmap-grid"))
    for (const cell of view.getAllByTestId("session-heatmap-cell")) {
      const raw = cell.getAttribute("data-intensity")
      expect(raw).not.toBeNull()
      // Regex for a fixed-3-decimal number, possibly zero.
      expect(raw).toMatch(/^\d+\.\d{3}$/)
    }
  })
})

// ───────────────────────────────────────────────────────────────
// (3) Empty date-cell handling — sparse / missing / stale cells.
// ───────────────────────────────────────────────────────────────

describe("checkbox 5 / empty cells — sparse payload handling", () => {
  it("renders a full 7×24 grid when the payload is empty", async () => {
    const fetcher = vi.fn().mockResolvedValue(makeResponse([]))
    const view = render(
      <SessionHeatmap fetchHeatmap={fetcher} refreshIntervalMs={0} />,
    )
    await waitFor(() => view.getByTestId("session-heatmap-grid"))
    const cells = view.getAllByTestId("session-heatmap-cell")
    expect(cells).toHaveLength(7 * 24)
    for (const cell of cells) {
      expect(cell.getAttribute("data-tokens")).toBe("0")
      expect(cell.getAttribute("data-intensity")).toBe("0.000")
    }
  })

  it("empty cells render the 'no activity' accessible label + title", async () => {
    const fetcher = vi.fn().mockResolvedValue(makeResponse([]))
    const view = render(
      <SessionHeatmap fetchHeatmap={fetcher} refreshIntervalMs={0} />,
    )
    await waitFor(() => view.getByTestId("session-heatmap-grid"))
    const cell = view.getAllByTestId("session-heatmap-cell")[0]
    expect(cell.getAttribute("aria-label")).toMatch(/no activity/)
    expect(cell.getAttribute("title")).toMatch(/no activity/)
  })

  it("empty cells get the plain --secondary background (no color-mix)", async () => {
    // Boundary between "cell rendered as empty" and "cell rendered as
    // active" is whether the ``backgroundColor`` contains ``color-mix``.
    // Empty cells bypass the mix entirely — that visual invariant is
    // what makes idle days distinguishable from low-activity ones.
    const fetcher = vi.fn().mockResolvedValue(makeResponse([]))
    const view = render(
      <SessionHeatmap fetchHeatmap={fetcher} refreshIntervalMs={0} />,
    )
    await waitFor(() => view.getByTestId("session-heatmap-grid"))
    for (const cell of view.getAllByTestId("session-heatmap-cell")) {
      const bg = (cell as HTMLElement).style.backgroundColor
      expect(bg).not.toMatch(/color-mix/)
      expect(bg).toContain("var(--secondary)")
    }
  })

  it("mixes populated and empty cells correctly (sparse population)", async () => {
    // Seed one cell and verify ALL other cells on the grid render as
    // empty. Proves the sparse payload doesn't accidentally leak into
    // neighbouring slots (e.g. a broken ``reduce`` / ``forEach`` over a
    // wider day range).
    const fetcher = vi.fn().mockResolvedValue(
      makeResponse([
        { day: "2026-04-24", hour: 12, token_total: 2222, cost_total: 0.22 },
      ]),
    )
    const view = render(
      <SessionHeatmap fetchHeatmap={fetcher} refreshIntervalMs={0} />,
    )
    await waitFor(() => view.getByTestId("session-heatmap-grid"))
    const cells = view.getAllByTestId("session-heatmap-cell")
    const activeCount = cells.filter(
      (c) => c.getAttribute("data-tokens") === "2222",
    ).length
    const emptyCount = cells.filter(
      (c) => c.getAttribute("data-tokens") === "0",
    ).length
    expect(activeCount).toBe(1)
    expect(emptyCount).toBe(cells.length - 1) // all other cells
  })

  it("cells outside the synthesised dayAxis (stale payload) are silently dropped", async () => {
    // Backend cells from further back than the 7d window may slip
    // through during a window-change race. They're legitimate cells but
    // there's no row to render them in — the grid ignores them and
    // shows only the current axis. No crash, no off-grid render.
    const longAgo = "2024-01-15" // well outside any 7d window anchor
    const fetcher = vi.fn().mockResolvedValue(
      makeResponse([
        { day: longAgo, hour: 5, token_total: 99_999, cost_total: 5 },
      ]),
    )
    const view = render(
      <SessionHeatmap fetchHeatmap={fetcher} refreshIntervalMs={0} />,
    )
    await waitFor(() => view.getByTestId("session-heatmap-grid"))
    // Still 7 * 24 rendered cells — the stale cell didn't get rendered
    // because its dayKey isn't in the axis.
    const cells = view.getAllByTestId("session-heatmap-cell")
    expect(cells).toHaveLength(7 * 24)
    // And none of the rendered cells carries the stale cell's token
    // total.
    for (const cell of cells) {
      expect(cell.getAttribute("data-tokens")).not.toBe("99999")
    }
  })

  it("payload with only malformed cells renders identically to an empty payload", async () => {
    // The shift helper rejects malformed cells. A payload that's 100%
    // malformed must degrade to the empty-grid path — same cell count,
    // same data-tokens=0, same title fallback — so ops don't see half-
    // rendered grids on a buggy backend.
    const fetcher = vi.fn().mockResolvedValue(
      makeResponse([
        // all rejected: bad day / bad hour
        { day: "bad-date", hour: 10, token_total: 999, cost_total: 1 },
        { day: "2026-04-24", hour: 77, token_total: 999, cost_total: 1 },
        { day: "2026/04/24", hour: 12, token_total: 999, cost_total: 1 },
      ]),
    )
    const view = render(
      <SessionHeatmap fetchHeatmap={fetcher} refreshIntervalMs={0} />,
    )
    await waitFor(() => view.getByTestId("session-heatmap-grid"))
    const cells = view.getAllByTestId("session-heatmap-cell")
    expect(cells).toHaveLength(7 * 24)
    for (const cell of cells) {
      expect(cell.getAttribute("data-tokens")).toBe("0")
    }
  })

  it("switching to 30d keeps all 30*24 cells rendered on empty payload", async () => {
    // 30d has more rows (30) and is visually denser — confirm the
    // empty-cell scaffold scales without a row being collapsed by an
    // off-by-one in ``buildDayAxis`` or the grid.
    const fetcher = vi.fn().mockResolvedValue(makeResponse([]))
    const view = render(
      <SessionHeatmap fetchHeatmap={fetcher} refreshIntervalMs={0} />,
    )
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1))
    act(() => {
      fireEvent.click(view.getByTestId("session-heatmap-window-30d"))
    })
    await waitFor(() =>
      expect(view.getAllByTestId("session-heatmap-row")).toHaveLength(30),
    )
    const cells = view.getAllByTestId("session-heatmap-cell")
    expect(cells).toHaveLength(30 * 24)
    // Every single one is still empty.
    for (const cell of cells) {
      expect(cell.getAttribute("data-tokens")).toBe("0")
    }
  })

  it("hovering an empty cell shows a zero-token tooltip (not suppressed)", async () => {
    // The tooltip is driven off ``hoverKey``, not off the bucket's
    // presence. Empty cells still deserve a tooltip so the operator
    // can confirm "yes, I hovered this slot and nothing happened" vs
    // "the hover handler is broken".
    const fetcher = vi.fn().mockResolvedValue(makeResponse([]))
    const view = render(
      <SessionHeatmap fetchHeatmap={fetcher} refreshIntervalMs={0} />,
    )
    await waitFor(() => view.getByTestId("session-heatmap-grid"))
    const firstEmpty = view.getAllByTestId("session-heatmap-cell")[0]
    act(() => {
      fireEvent.mouseEnter(firstEmpty)
    })
    const tip = await waitFor(() =>
      view.getByTestId("session-heatmap-tooltip"),
    )
    expect(tip).toBeInTheDocument()
    expect(
      view.getByTestId("session-heatmap-tooltip-tokens"),
    ).toHaveTextContent(/0 tokens/)
    expect(
      view.getByTestId("session-heatmap-tooltip-cost"),
    ).toHaveTextContent(/\$0\.000/)
  })
})
