/**
 * BS.11.6 — Pure-helper contract tests for `lib/perf/fps-budget.ts`.
 *
 * The Playwright spec at `e2e/bs11-6-catalog-perf.spec.ts` injects an
 * `requestAnimationFrame` sampler into the page that returns an array
 * of `performance.now()` timestamps. The threshold math that turns
 * that array into pass/fail lives in `lib/perf/fps-budget.ts` —
 * unit-testing it here means a math regression red-fails CI without
 * spinning up a Chrome instance, and the Playwright spec only has to
 * cover the page integration.
 */

import { describe, expect, it } from "vitest"

import {
  BS11_6_DRAMATIC_MIN_MEAN_FPS,
  BS11_6_DRAMATIC_MIN_P10_FPS,
  BS11_6_OFF_MIN_MEAN_FPS,
  computeFpsStats,
  evaluateFpsVerdict,
  formatFpsSummary,
  quantileSorted,
} from "@/lib/perf/fps-budget"

/** Build a synthetic timestamp array at a constant frame interval. */
function constantTimestamps(frameIntervalMs: number, frameCount: number): number[] {
  const out: number[] = []
  for (let i = 0; i <= frameCount; i += 1) {
    out.push(i * frameIntervalMs)
  }
  return out
}

describe("BS.11.6 — fps-budget thresholds (literal SoT)", () => {
  it("exposes the BS.11.6 row's literal budget constants", () => {
    expect(BS11_6_DRAMATIC_MIN_MEAN_FPS).toBe(50)
    expect(BS11_6_DRAMATIC_MIN_P10_FPS).toBe(30)
    expect(BS11_6_OFF_MIN_MEAN_FPS).toBe(55)
  })
})

describe("BS.11.6 — quantileSorted", () => {
  it("returns 0 for empty input", () => {
    expect(quantileSorted([], 0.5)).toBe(0)
  })

  it("returns the only sample for length-1 input", () => {
    expect(quantileSorted([42], 0.9)).toBe(42)
  })

  it("returns the min at q=0", () => {
    expect(quantileSorted([10, 20, 30, 40, 50], 0)).toBe(10)
  })

  it("returns the max at q=1", () => {
    expect(quantileSorted([10, 20, 30, 40, 50], 1)).toBe(50)
  })

  it("interpolates linearly between adjacent samples", () => {
    // q=0.9 on length 5 → pos = 0.9*4 = 3.6 → between sortedAsc[3]=40 and [4]=50.
    // 40 + 0.6 * (50-40) = 46.
    expect(quantileSorted([10, 20, 30, 40, 50], 0.9)).toBeCloseTo(46, 6)
  })

  it("clamps q < 0 and q > 1", () => {
    expect(quantileSorted([10, 20, 30], -0.5)).toBe(10)
    expect(quantileSorted([10, 20, 30], 1.5)).toBe(30)
  })
})

describe("BS.11.6 — computeFpsStats", () => {
  it("returns zeroed stats for fewer than 2 timestamps", () => {
    const stats = computeFpsStats([100])
    expect(stats.frameCount).toBe(0)
    expect(stats.durationMs).toBe(0)
    expect(stats.meanFps).toBe(0)
    expect(stats.minFps).toBe(0)
    expect(stats.p10Fps).toBe(0)
    expect(stats.maxFrameIntervalMs).toBe(0)
  })

  it("computes 60fps on a perfect 16.667ms cadence", () => {
    const stats = computeFpsStats(constantTimestamps(1000 / 60, 60))
    expect(stats.frameCount).toBe(60)
    expect(stats.meanFps).toBeCloseTo(60, 4)
    expect(stats.minFps).toBeCloseTo(60, 4)
    expect(stats.p10Fps).toBeCloseTo(60, 4)
    expect(stats.maxFrameIntervalMs).toBeCloseTo(1000 / 60, 6)
  })

  it("computes 50fps mean when frames take 20ms each", () => {
    const stats = computeFpsStats(constantTimestamps(20, 100))
    expect(stats.meanFps).toBeCloseTo(50, 4)
    expect(stats.minFps).toBeCloseTo(50, 4)
    expect(stats.maxFrameIntervalMs).toBeCloseTo(20, 6)
  })

  it("reports the slowest single frame as minFps", () => {
    const ts = constantTimestamps(16, 30)
    // Append one slow frame: 100ms — should dominate maxFrameIntervalMs.
    ts.push(ts[ts.length - 1] + 100)
    const stats = computeFpsStats(ts)
    expect(stats.maxFrameIntervalMs).toBeCloseTo(100, 6)
    expect(stats.minFps).toBeCloseTo(10, 4)
    // Mean is still mostly fast frames.
    expect(stats.meanFps).toBeGreaterThan(50)
  })

  it("filters non-positive intervals (clock jitter, duplicate timestamps)", () => {
    // Some browsers can return identical timestamps in back-to-back rAF
    // ticks under heavy CPU pressure — the helper must not produce
    // Infinity fps from a 0ms interval.
    const stats = computeFpsStats([0, 16, 16, 32, 48])
    expect(stats.frameCount).toBe(3)
    expect(Number.isFinite(stats.meanFps)).toBe(true)
    expect(Number.isFinite(stats.minFps)).toBe(true)
  })

  it("computes p10 (90th-percentile slow) on a bimodal sample", () => {
    // 80 fast 16ms frames + 20 slow 50ms frames. The slow tail is
    // 20% so q=0.9 falls squarely inside the slow band. (At only 10%
    // slow tail the q=0.9 sample sits exactly on the fast/slow
    // boundary — useful as a reminder that p10 measures the slowest
    // 10%, not the worst single frame; that's `minFps`.)
    const ts = constantTimestamps(16, 80)
    let lastT = ts[ts.length - 1]
    for (let i = 0; i < 20; i += 1) {
      lastT += 50
      ts.push(lastT)
    }
    const stats = computeFpsStats(ts)
    expect(stats.p10Fps).toBeLessThan(40)
    expect(stats.p10Fps).toBeGreaterThan(10)
    expect(stats.meanFps).toBeGreaterThan(40)
  })
})

describe("BS.11.6 — evaluateFpsVerdict", () => {
  it("passes a healthy 60fps sample against the 50fps budget", () => {
    const stats = computeFpsStats(constantTimestamps(1000 / 60, 200))
    const v = evaluateFpsVerdict({
      scenario: "dramatic-desktop",
      minMeanFps: BS11_6_DRAMATIC_MIN_MEAN_FPS,
      minP10Fps: BS11_6_DRAMATIC_MIN_P10_FPS,
      stats,
    })
    expect(v.passed).toBe(true)
    expect(v.reasons).toEqual([])
    expect(v.scenario).toBe("dramatic-desktop")
    expect(v.thresholds.minMeanFps).toBe(50)
    expect(v.thresholds.minP10Fps).toBe(30)
  })

  it("fails with a sub-50fps mean", () => {
    // 25fps cadence — squarely below the budget.
    const stats = computeFpsStats(constantTimestamps(40, 200))
    const v = evaluateFpsVerdict({
      scenario: "dramatic-desktop",
      minMeanFps: BS11_6_DRAMATIC_MIN_MEAN_FPS,
      minP10Fps: BS11_6_DRAMATIC_MIN_P10_FPS,
      stats,
    })
    expect(v.passed).toBe(false)
    expect(v.reasons.some((r) => r.includes("meanFps="))).toBe(true)
    expect(v.reasons.some((r) => r.includes("p10Fps="))).toBe(true)
  })

  it("fails with a sub-30fps p10 even when mean clears the budget", () => {
    // 150 fast frames at 8ms + 50 slow 50ms frames. Mean ≈ 54fps
    // (clears 50fps budget); p10 ≈ 20fps (well below 30fps p10
    // budget). The slow tail must be ≥ 11% of intervals for q=0.9
    // to land inside it — see the bimodal test above.
    const ts = constantTimestamps(8, 150)
    let last = ts[ts.length - 1]
    for (let i = 0; i < 50; i += 1) {
      last += 50
      ts.push(last)
    }
    const stats = computeFpsStats(ts)
    const v = evaluateFpsVerdict({
      scenario: "dramatic-desktop",
      minMeanFps: BS11_6_DRAMATIC_MIN_MEAN_FPS,
      minP10Fps: BS11_6_DRAMATIC_MIN_P10_FPS,
      stats,
    })
    expect(stats.meanFps).toBeGreaterThan(BS11_6_DRAMATIC_MIN_MEAN_FPS)
    expect(stats.p10Fps).toBeLessThan(BS11_6_DRAMATIC_MIN_P10_FPS)
    expect(v.passed).toBe(false)
    expect(v.reasons.some((r) => r.includes("p10Fps="))).toBe(true)
  })

  it("fails on too-short sample window even at high fps", () => {
    // Only 10 frame intervals — below the 30-frame floor.
    const stats = computeFpsStats(constantTimestamps(16, 10))
    const v = evaluateFpsVerdict({
      scenario: "dramatic-desktop",
      minMeanFps: BS11_6_DRAMATIC_MIN_MEAN_FPS,
      minP10Fps: BS11_6_DRAMATIC_MIN_P10_FPS,
      stats,
    })
    expect(v.passed).toBe(false)
    expect(v.reasons.some((r) => r.includes("frameCount="))).toBe(true)
  })

  it("respects optional minP10Fps absence (off-control scenario)", () => {
    const stats = computeFpsStats(constantTimestamps(1000 / 60, 200))
    const v = evaluateFpsVerdict({
      scenario: "off-control",
      minMeanFps: BS11_6_OFF_MIN_MEAN_FPS,
      stats,
    })
    expect(v.passed).toBe(true)
    expect(v.thresholds.minP10Fps).toBeUndefined()
  })
})

describe("BS.11.6 — formatFpsSummary", () => {
  it("emits a fixed-shape one-liner readable in CI logs", () => {
    const stats = computeFpsStats(constantTimestamps(1000 / 60, 60))
    const v = evaluateFpsVerdict({
      scenario: "dramatic-desktop",
      minMeanFps: BS11_6_DRAMATIC_MIN_MEAN_FPS,
      minP10Fps: BS11_6_DRAMATIC_MIN_P10_FPS,
      stats,
    })
    const line = formatFpsSummary(v)
    expect(line).toMatch(/^\[BS\.11\.6\] dramatic-desktop\s+pass/)
    expect(line).toContain("mean=")
    expect(line).toContain("p10=")
    expect(line).toContain("min=")
    expect(line).toContain("frames=")
    expect(line).toContain("over=")
  })

  it("flags FAIL prominently when verdict failed", () => {
    const stats = computeFpsStats(constantTimestamps(40, 200))
    const v = evaluateFpsVerdict({
      scenario: "dramatic-desktop",
      minMeanFps: BS11_6_DRAMATIC_MIN_MEAN_FPS,
      stats,
    })
    const line = formatFpsSummary(v)
    expect(line).toContain("FAIL")
  })
})
