/**
 * ZZ.A3 (#303-3) checkbox 5 — inter-turn gap, tool failure count,
 * and p95 outlier regression guards for the TokenUsageStats Row 3
 * mini-stats block.
 *
 * The cache UI (ZZ.A1) and context UI (ZZ.A2) already have sibling
 * test files; this one locks the three ZZ.A3 contracts the earlier
 * checkboxes landed but never tested:
 *
 * 1. **Gap formula correctness (incl. timezone consistency).**
 *    ``avg gap = mean(gap_i)`` where ``gap_i = (t[i].ts - t[i-1].ts)
 *    - t[i].latency_ms``. ``t[i].ts`` is parsed from the ISO-8601
 *    ``timestamp`` on each ``turn_metrics`` SSE event. Because
 *    ``Date.parse()`` normalises any offset to the same epoch ms,
 *    the same three absolute moments written in UTC (``+00:00``) vs
 *    Taipei local (``+08:00``) MUST yield the exact same avg gap —
 *    otherwise the dashboard would show different numbers depending
 *    on which worker's ``datetime.now(timezone.utc).isoformat()``
 *    chose to serialise (a real risk — the backend runs in UTC but
 *    operators are on their own machine time).
 * 2. **Tool failure count red badge.** "failed N" digit goes red +
 *    bold iff ``hasToolStats && failureCount > 0``. A ``0`` after a
 *    failing turn (pass-through zeroed snapshot emitted by the
 *    summarizer on a conversational turn) must CLEAR the stale red —
 *    otherwise the badge would stick across turn boundaries.
 *    "—" (no data) must not wear red either, else "no data" would
 *    masquerade as a genuine failing turn — same NULL-vs-genuine-
 *    zero contract ZZ.A1 / ZZ.A2 established elsewhere on this card.
 * 3. **p95 outlier judgment.** ``latest > p95(prior)`` with ≥4 prior
 *    POSITIVE latencies triggers the SLOW badge. Key invariants:
 *    - fires when latest exceeds p95,
 *    - does not fire when latest is within p95,
 *    - does not fire with fewer than 5 total turns (MIN_PRIOR=4),
 *    - zero-latency priors get filtered BEFORE the count check so a
 *      degenerate sample can't accidentally unlock the alarm with
 *      only 3 real priors.
 */

import { describe, expect, it, vi, beforeEach } from "vitest"
import { render, act } from "@testing-library/react"

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>()
  return {
    ...actual,
    subscribeEvents: vi.fn(),
  }
})

import * as api from "@/lib/api"
import {
  TokenUsageStats,
  type ModelTokenUsage,
} from "@/components/omnisight/token-usage-stats"
import { primeSSE } from "../helpers/sse"

function makeRow(overrides: Partial<ModelTokenUsage>): ModelTokenUsage {
  return {
    model: "claude-opus-4-7",
    inputTokens: 1000,
    outputTokens: 500,
    totalTokens: 1500,
    cost: 0.05,
    requestCount: 3,
    avgLatency: 120,
    lastUsed: "10:00:00",
    cacheReadTokens: null,
    cacheCreateTokens: null,
    cacheHitRatio: null,
    ...overrides,
  }
}

function emitTurnMetrics(
  sse: ReturnType<typeof primeSSE>,
  model: string,
  iso: string,
  latencyMs: number,
) {
  sse.emit({
    event: "turn_metrics",
    data: {
      provider: "anthropic",
      model,
      input_tokens: 1000,
      output_tokens: 200,
      tokens_used: 1200,
      context_limit: 1_000_000,
      context_usage_pct: 0.12,
      latency_ms: latencyMs,
      cache_read_tokens: null,
      cache_create_tokens: null,
      timestamp: iso,
    },
  })
}

function emitTurnToolStats(
  sse: ReturnType<typeof primeSSE>,
  callCount: number,
  failureCount: number,
  failedTools: string[],
) {
  sse.emit({
    event: "turn_tool_stats",
    data: {
      agent_type: "orchestrator",
      task_id: "task-42",
      tool_call_count: callCount,
      tool_failure_count: failureCount,
      failed_tools: failedTools,
      timestamp: "2026-04-24T12:00:00.000+00:00",
    },
  })
}

beforeEach(() => {
  ;(api.subscribeEvents as ReturnType<typeof vi.fn>).mockReset()
})

// ───── Gap formula correctness ─────────────────────────────────────
describe("TokenUsageStats — inter-turn gap formula", () => {
  it("avg gap = mean((delta_ts - latency)) rounded to the nearest ms", async () => {
    const rows: ModelTokenUsage[] = [makeRow({ model: "claude-opus-4-7" })]
    const sse = primeSSE(api)
    const { container } = render(<TokenUsageStats externalUsage={rows} />)

    // t=0,   latency=100 (no gap — no prior turn)
    // t=+1s, latency=200 → gap_1 = 1000 - 200 = 800ms
    // t=+2.5s, latency=300 → gap_2 = 1500 - 300 = 1200ms
    // avg = (800 + 1200) / 2 = 1000ms → "avg gap 1000ms"
    await act(async () => {
      emitTurnMetrics(sse, "claude-opus-4-7", "2026-04-24T12:00:00.000+00:00", 100)
      emitTurnMetrics(sse, "claude-opus-4-7", "2026-04-24T12:00:01.000+00:00", 200)
      emitTurnMetrics(sse, "claude-opus-4-7", "2026-04-24T12:00:02.500+00:00", 300)
    })

    const gap = container.querySelector<HTMLElement>(
      "[data-testid='turn-avg-gap']",
    )
    expect(gap).not.toBeNull()
    expect(gap!.textContent).toBe("avg gap 1000ms")
  })

  it("avg gap is invariant under ISO-8601 timezone offset", async () => {
    const rows: ModelTokenUsage[] = [makeRow({ model: "claude-opus-4-7" })]
    const sse = primeSSE(api)
    const { container } = render(<TokenUsageStats externalUsage={rows} />)

    // Same three absolute moments as the prior test, but written in
    // Taipei local time (+08:00). ``Date.parse`` normalises any offset
    // to epoch ms so the UTC form and the +08:00 form yield the exact
    // same ``ts`` values → same gaps → same avg.
    //   12:00:00.000Z         ≡ 20:00:00.000+08:00
    //   12:00:01.000Z         ≡ 20:00:01.000+08:00
    //   12:00:02.500Z         ≡ 20:00:02.500+08:00
    await act(async () => {
      emitTurnMetrics(sse, "claude-opus-4-7", "2026-04-24T20:00:00.000+08:00", 100)
      emitTurnMetrics(sse, "claude-opus-4-7", "2026-04-24T20:00:01.000+08:00", 200)
      emitTurnMetrics(sse, "claude-opus-4-7", "2026-04-24T20:00:02.500+08:00", 300)
    })

    const gap = container.querySelector<HTMLElement>(
      "[data-testid='turn-avg-gap']",
    )
    // If the component computed gaps in local-time-aware Date objects
    // rather than epoch-ms, the +08:00 emission would look 8h away
    // from the next one and the gap would land in the billions of ms.
    // This assertion pins the epoch-ms normalisation as the contract.
    expect(gap!.textContent).toBe("avg gap 1000ms")
  })

  it("renders '—' for avg gap when fewer than 2 turns have landed", async () => {
    const rows: ModelTokenUsage[] = [makeRow({ model: "claude-opus-4-7" })]
    const sse = primeSSE(api)
    const { container } = render(<TokenUsageStats externalUsage={rows} />)

    await act(async () => {
      emitTurnMetrics(sse, "claude-opus-4-7", "2026-04-24T12:00:00.000+00:00", 100)
    })

    const gap = container.querySelector<HTMLElement>(
      "[data-testid='turn-avg-gap']",
    )
    // Single turn → no consecutive pair → nothing to average → "—".
    expect(gap!.textContent).toBe("avg gap —")
  })
})

// ───── Tool failure count red badge ────────────────────────────────
describe("TokenUsageStats — tool failure count red badge", () => {
  it("renders the failed count in critical-red + semibold when failureCount > 0", async () => {
    const rows: ModelTokenUsage[] = [makeRow({ model: "claude-opus-4-7" })]
    const sse = primeSSE(api)
    const { container } = render(<TokenUsageStats externalUsage={rows} />)

    // ``turn_metrics`` first so the component's lastMetricsModelRef
    // points at this model — otherwise turn_tool_stats lands
    // unattributed and never reaches the card.
    await act(async () => {
      emitTurnMetrics(sse, "claude-opus-4-7", "2026-04-24T12:00:00.000+00:00", 100)
      emitTurnToolStats(sse, 5, 1, ["run_bash"])
    })

    const count = container.querySelector<HTMLElement>(
      "[data-testid='turn-tool-count']",
    )
    expect(count).not.toBeNull()
    expect(count!.textContent).toContain("tools 5")
    expect(count!.textContent).toContain("failed 1")

    const failed = container.querySelector<HTMLElement>(
      "[data-testid='turn-failed-count']",
    )
    expect(failed).not.toBeNull()
    expect(failed!.textContent).toBe("1")
    // Spec: 紅字 badge 若 failed > 0 — critical-red + font-semibold.
    expect(failed!.className).toContain("text-[var(--critical-red)]")
    expect(failed!.className).toContain("font-semibold")

    // Tooltip surfaces which tool failed so the operator doesn't have
    // to expand the card to find out WHICH retry-loop is running.
    expect(count!.getAttribute("title")).toContain("run_bash")
  })

  it("clears the red badge when the next turn emits a zeroed snapshot", async () => {
    const rows: ModelTokenUsage[] = [makeRow({ model: "claude-opus-4-7" })]
    const sse = primeSSE(api)
    const { container } = render(<TokenUsageStats externalUsage={rows} />)

    // First turn: tool failure → red.
    await act(async () => {
      emitTurnMetrics(sse, "claude-opus-4-7", "2026-04-24T12:00:00.000+00:00", 100)
      emitTurnToolStats(sse, 3, 2, ["run_bash", "edit_file"])
    })
    let failed = container.querySelector<HTMLElement>(
      "[data-testid='turn-failed-count']",
    )
    expect(failed!.className).toContain("text-[var(--critical-red)]")

    // Next conversational turn: summarizer emits a zeroed snapshot
    // (tool_call_count=0, tool_failure_count=0). ZZ.A3 checkbox 2
    // carefully emits this BEFORE the pass-through early-return so
    // the UI can clear the stale red — this test locks that invariant
    // on the frontend side.
    await act(async () => {
      emitTurnMetrics(sse, "claude-opus-4-7", "2026-04-24T12:00:01.000+00:00", 100)
      emitTurnToolStats(sse, 0, 0, [])
    })
    failed = container.querySelector<HTMLElement>(
      "[data-testid='turn-failed-count']",
    )
    expect(failed!.textContent).toBe("0")
    expect(failed!.className).not.toContain("text-[var(--critical-red)]")
  })

  it("renders 'tools — / failed —' when no turn_tool_stats has landed yet", async () => {
    const rows: ModelTokenUsage[] = [makeRow({ model: "claude-opus-4-7" })]
    const sse = primeSSE(api)
    const { container } = render(<TokenUsageStats externalUsage={rows} />)

    // A turn_metrics lands (to prove the card is wired) but NO
    // turn_tool_stats yet. The mini-stats must NOT fabricate a zero —
    // "no data" → em-dash, distinct from a real 0/0 turn.
    await act(async () => {
      emitTurnMetrics(sse, "claude-opus-4-7", "2026-04-24T12:00:00.000+00:00", 100)
    })

    const count = container.querySelector<HTMLElement>(
      "[data-testid='turn-tool-count']",
    )
    expect(count!.textContent).toContain("tools —")
    expect(count!.textContent).toContain("failed —")
    expect(count!.getAttribute("title")).toContain(
      "no turn_tool_stats seen for this model yet",
    )

    // "—" (no data) must NOT fire the red badge — else a legacy row
    // would masquerade as a critical failing turn.
    const failed = container.querySelector<HTMLElement>(
      "[data-testid='turn-failed-count']",
    )
    expect(failed!.textContent).toBe("—")
    expect(failed!.className).not.toContain("text-[var(--critical-red)]")
  })
})

// ───── p95 outlier judgment ────────────────────────────────────────
describe("TokenUsageStats — p95 outlier SLOW badge", () => {
  it("fires when latest latency exceeds p95 of prior turns", async () => {
    const rows: ModelTokenUsage[] = [makeRow({ model: "claude-opus-4-7" })]
    const sse = primeSSE(api)
    const { container } = render(<TokenUsageStats externalUsage={rows} />)

    // 4 prior turns at 100 / 200 / 300 / 400 ms, latest at 1000 ms.
    //   sorted(prior) = [100, 200, 300, 400], n = 4
    //   idx = min(n-1, max(0, ceil(n * 0.95) - 1))
    //       = min(3,  max(0, ceil(3.8)     - 1))
    //       = min(3,  max(0, 3))
    //       = 3
    //   p95 = sorted[3] = 400
    //   latest (1000) > p95 (400) → OUTLIER → SLOW badge renders.
    await act(async () => {
      emitTurnMetrics(sse, "claude-opus-4-7", "2026-04-24T12:00:00.000+00:00", 100)
      emitTurnMetrics(sse, "claude-opus-4-7", "2026-04-24T12:00:01.000+00:00", 200)
      emitTurnMetrics(sse, "claude-opus-4-7", "2026-04-24T12:00:02.000+00:00", 300)
      emitTurnMetrics(sse, "claude-opus-4-7", "2026-04-24T12:00:03.000+00:00", 400)
      emitTurnMetrics(sse, "claude-opus-4-7", "2026-04-24T12:00:04.000+00:00", 1000)
    })

    const badge = container.querySelector<HTMLElement>(
      "[data-testid='turn-p95-outlier']",
    )
    expect(badge).not.toBeNull()
    expect(badge!.textContent).toContain("SLOW")

    // Tooltip encodes the exact latency + p95 driving the alarm so
    // operators see "why" on hover.
    const title = badge!.getAttribute("title") ?? ""
    expect(title).toContain("1000ms")
    expect(title).toContain("(400ms)")

    // SLOW uses hardware-orange (intentionally NOT critical-red, which
    // is reserved for the ZZ.A2 context-critical warning — keeping the
    // two alarms visually distinct).
    expect(badge!.className).toContain("text-[var(--hardware-orange)]")
    expect(badge!.className).toContain("animate-pulse")
  })

  it("does NOT fire when the latest turn is within p95 of prior turns", async () => {
    const rows: ModelTokenUsage[] = [makeRow({ model: "claude-opus-4-7" })]
    const sse = primeSSE(api)
    const { container } = render(<TokenUsageStats externalUsage={rows} />)

    // Same 4 priors, latest at 350ms (below p95=400) — no outlier.
    // Strict ``>`` not ``>=``: a latest that equals the max of the
    // priors is NOT an outlier.
    await act(async () => {
      emitTurnMetrics(sse, "claude-opus-4-7", "2026-04-24T12:00:00.000+00:00", 100)
      emitTurnMetrics(sse, "claude-opus-4-7", "2026-04-24T12:00:01.000+00:00", 200)
      emitTurnMetrics(sse, "claude-opus-4-7", "2026-04-24T12:00:02.000+00:00", 300)
      emitTurnMetrics(sse, "claude-opus-4-7", "2026-04-24T12:00:03.000+00:00", 400)
      emitTurnMetrics(sse, "claude-opus-4-7", "2026-04-24T12:00:04.000+00:00", 350)
    })

    expect(
      container.querySelector("[data-testid='turn-p95-outlier']"),
    ).toBeNull()
  })

  it("does NOT fire with fewer than 5 total turns (MIN_PRIOR=4)", async () => {
    const rows: ModelTokenUsage[] = [makeRow({ model: "claude-opus-4-7" })]
    const sse = primeSSE(api)
    const { container } = render(<TokenUsageStats externalUsage={rows} />)

    // 4 turns total: 3 prior + 1 latest. priorLatencies.length = 3 <
    // MIN_PRIOR=4 → baseline is too small to compute a meaningful p95,
    // so the badge refuses to fire even on a clearly-outlying latest.
    // Documents the "don't cry wolf on a cold card" contract.
    await act(async () => {
      emitTurnMetrics(sse, "claude-opus-4-7", "2026-04-24T12:00:00.000+00:00", 100)
      emitTurnMetrics(sse, "claude-opus-4-7", "2026-04-24T12:00:01.000+00:00", 200)
      emitTurnMetrics(sse, "claude-opus-4-7", "2026-04-24T12:00:02.000+00:00", 300)
      emitTurnMetrics(sse, "claude-opus-4-7", "2026-04-24T12:00:03.000+00:00", 10_000)
    })

    expect(
      container.querySelector("[data-testid='turn-p95-outlier']"),
    ).toBeNull()
  })

  it("filters zero-latency prior samples before the MIN_PRIOR check", async () => {
    const rows: ModelTokenUsage[] = [makeRow({ model: "claude-opus-4-7" })]
    const sse = primeSSE(api)
    const { container } = render(<TokenUsageStats externalUsage={rows} />)

    // 5 total turns (meets the raw count), but the first prior is a
    // degenerate 0ms sample (backend stub race / callback ordering
    // bug). After filtering for ``l > 0`` we're left with 3 real
    // priors (< MIN_PRIOR=4) → badge must NOT fire, even though the
    // latest (10000ms) would be an outlier if we naïvely let the 0
    // through. This locks the "filter-then-count" ordering — flipping
    // it would silently re-introduce the regression the positive-
    // latency filter was added to prevent.
    await act(async () => {
      emitTurnMetrics(sse, "claude-opus-4-7", "2026-04-24T12:00:00.000+00:00", 0)
      emitTurnMetrics(sse, "claude-opus-4-7", "2026-04-24T12:00:01.000+00:00", 100)
      emitTurnMetrics(sse, "claude-opus-4-7", "2026-04-24T12:00:02.000+00:00", 200)
      emitTurnMetrics(sse, "claude-opus-4-7", "2026-04-24T12:00:03.000+00:00", 300)
      emitTurnMetrics(sse, "claude-opus-4-7", "2026-04-24T12:00:04.000+00:00", 10_000)
    })

    expect(
      container.querySelector("[data-testid='turn-p95-outlier']"),
    ).toBeNull()
  })
})
