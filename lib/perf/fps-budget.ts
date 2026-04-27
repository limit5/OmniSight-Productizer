/**
 * BS.11.6 — Pure FPS-budget helpers for the Platforms catalog
 * performance-regression spec (`e2e/bs11-6-catalog-perf.spec.ts`).
 *
 * Why a separate module: the Playwright spec injects an
 * `requestAnimationFrame` sampler into the page that captures one
 * timestamp per browser frame. The math that turns that array of
 * `performance.now()` ticks into a verdict (mean fps, slowest frame,
 * 10th-percentile fps, pass/fail vs the budget) is pure and worth
 * unit-testing on its own — keeping it here lets `test/lib/fps-
 * budget.test.ts` exercise the threshold logic without spinning up a
 * full Playwright fixture.
 *
 * Module-global state audit (per docs/sop/implement_phase_step.md
 * Step 1): this module is dependency-free, exports only pure
 * functions and frozen const objects, and reads no globals. Cross-
 * worker derivation is trivially identical (Answer #1 in the SOP).
 */

export const BS11_6_DRAMATIC_MIN_MEAN_FPS = 50

export const BS11_6_OFF_MIN_MEAN_FPS = 55

export const BS11_6_DRAMATIC_MIN_P10_FPS = 30

export interface FpsStats {
  /** Number of frame timestamps observed. */
  frameCount: number
  /** End-to-end window duration in milliseconds (last - first). */
  durationMs: number
  /** frameCount / (durationMs / 1000) — overall throughput. */
  meanFps: number
  /** 1000 / (slowest frame interval) — worst single frame as fps. */
  minFps: number
  /** 1000 / (10th-percentile frame interval, slow side) — fps at the
   *  90th-percentile slowest frame. Uses linear interpolation between
   *  adjacent samples per the C8 quantile method. */
  p10Fps: number
  /** Slowest single frame interval in milliseconds. */
  maxFrameIntervalMs: number
}

/**
 * Compute throughput statistics from an ordered array of
 * `performance.now()` timestamps (ms). Each consecutive pair is
 * treated as one frame's wall-clock duration; frame count is the
 * number of intervals (timestamps - 1).
 *
 * Returns `frameCount: 0` and zeroed stats for fewer than 2
 * timestamps — the caller's pass/fail logic should treat that as a
 * sampling failure, not as a perf finding.
 */
export function computeFpsStats(timestamps: ReadonlyArray<number>): FpsStats {
  if (timestamps.length < 2) {
    return {
      frameCount: 0,
      durationMs: 0,
      meanFps: 0,
      minFps: 0,
      p10Fps: 0,
      maxFrameIntervalMs: 0,
    }
  }
  const intervals: number[] = []
  for (let i = 1; i < timestamps.length; i += 1) {
    const dt = timestamps[i] - timestamps[i - 1]
    if (Number.isFinite(dt) && dt > 0) {
      intervals.push(dt)
    }
  }
  if (intervals.length === 0) {
    return {
      frameCount: 0,
      durationMs: 0,
      meanFps: 0,
      minFps: 0,
      p10Fps: 0,
      maxFrameIntervalMs: 0,
    }
  }
  const durationMs = timestamps[timestamps.length - 1] - timestamps[0]
  const meanFps = durationMs > 0 ? (intervals.length * 1000) / durationMs : 0
  const sortedAsc = [...intervals].sort((a, b) => a - b)
  const maxFrameIntervalMs = sortedAsc[sortedAsc.length - 1]
  const minFps = maxFrameIntervalMs > 0 ? 1000 / maxFrameIntervalMs : 0
  const p10IntervalMs = quantileSorted(sortedAsc, 0.9)
  const p10Fps = p10IntervalMs > 0 ? 1000 / p10IntervalMs : 0
  return {
    frameCount: intervals.length,
    durationMs,
    meanFps,
    minFps,
    p10Fps,
    maxFrameIntervalMs,
  }
}

/**
 * Linear-interpolated quantile on a sorted-ascending array. q in
 * [0, 1]. For q=0.9 on a length-10 array returns the value at index
 * 8.1 — interpolated between sorted[8] and sorted[9].
 */
export function quantileSorted(sortedAsc: ReadonlyArray<number>, q: number): number {
  if (sortedAsc.length === 0) return 0
  if (sortedAsc.length === 1) return sortedAsc[0]
  const clamped = Math.max(0, Math.min(1, q))
  const pos = clamped * (sortedAsc.length - 1)
  const base = Math.floor(pos)
  const rest = pos - base
  if (base + 1 >= sortedAsc.length) return sortedAsc[sortedAsc.length - 1]
  return sortedAsc[base] + rest * (sortedAsc[base + 1] - sortedAsc[base])
}

export interface FpsVerdictInput {
  scenario: string
  minMeanFps: number
  minP10Fps?: number
  stats: FpsStats
}

export interface FpsVerdict {
  scenario: string
  passed: boolean
  reasons: ReadonlyArray<string>
  stats: FpsStats
  thresholds: {
    minMeanFps: number
    minP10Fps?: number
  }
}

/**
 * Apply the pass/fail rules:
 *   • `meanFps >= minMeanFps`
 *   • optional `p10Fps >= minP10Fps`
 *   • at least 30 frame intervals sampled (1.5 s at 20 fps floor) —
 *     anything shorter is a measurement failure not a perf signal.
 */
export function evaluateFpsVerdict(input: FpsVerdictInput): FpsVerdict {
  const reasons: string[] = []
  const { stats, minMeanFps, minP10Fps, scenario } = input
  if (stats.frameCount < 30) {
    reasons.push(
      `frameCount=${stats.frameCount} < 30 — sampling window too short or rAF starved`,
    )
  }
  if (stats.meanFps < minMeanFps) {
    reasons.push(
      `meanFps=${stats.meanFps.toFixed(2)} < minMeanFps=${minMeanFps}`,
    )
  }
  if (typeof minP10Fps === "number" && stats.p10Fps < minP10Fps) {
    reasons.push(
      `p10Fps=${stats.p10Fps.toFixed(2)} < minP10Fps=${minP10Fps}`,
    )
  }
  return {
    scenario,
    passed: reasons.length === 0,
    reasons,
    stats,
    thresholds: { minMeanFps, minP10Fps },
  }
}

/**
 * One-line summary readable in CI logs and pasted into HANDOFF
 * entries. Format:
 *
 *   [BS.11.6] dramatic-desktop  pass  mean=58.2fps p10=42.1fps min=28.0fps frames=174 over=3001ms
 */
export function formatFpsSummary(verdict: FpsVerdict): string {
  const status = verdict.passed ? "pass" : "FAIL"
  const s = verdict.stats
  return (
    `[BS.11.6] ${verdict.scenario.padEnd(22, " ")}` +
    ` ${status.padEnd(4, " ")}` +
    ` mean=${s.meanFps.toFixed(1)}fps` +
    ` p10=${s.p10Fps.toFixed(1)}fps` +
    ` min=${s.minFps.toFixed(1)}fps` +
    ` frames=${s.frameCount}` +
    ` over=${s.durationMs.toFixed(0)}ms`
  )
}
