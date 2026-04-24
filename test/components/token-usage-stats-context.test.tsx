/**
 * ZZ.A2 (#303-2) — context-window UI regression guards.
 *
 * Locks the frontend half of the ZZ.A2 spec that the earlier checkboxes
 * landed but never tested:
 *
 *   1. **Progress-bar colour × 4 bands.** The context-usage percentage
 *      colour follows the spec: ``<50`` green / ``50-75`` yellow /
 *      ``75-90`` orange / ``>=90`` red (+``animate-pulse``). Boundary
 *      semantics: strict ``<`` upper / inclusive ``>=`` lower so 50.0
 *      lands on yellow, 75.0 on orange, 90.0 on red — same "right
 *      edge moves up a band" reading as the ZZ.A1 cache bar.
 *   2. **NULL (unknown limit) degradation.** ``turn_metrics`` with
 *      ``context_limit=null`` (Ollama without env override / OpenRouter
 *      pass-through / unknown provider) must render ``"—"`` in
 *      muted-foreground with an empty rail — never masquerade as a
 *      real 0%. Same NULL-vs-genuine-zero contract ZZ.A1 established
 *      for cache fields, and the reason the backend carefully preserves
 *      ``None`` rather than coercing to 0.
 *   3. **Warning icon — trigger condition.** The card-top
 *      ``AlertTriangle`` only renders when at least one model has a
 *      recent ``context_usage_pct >= 90``. Sub-threshold rows (even
 *      89.99%) must not fire. NULL rows (unknown limit) must not fire
 *      either — "no data" never rings the alarm.
 *   4. **Warning icon — mixed state.** With one model at 92% and
 *      another at 45%, the icon fires and the tooltip names only the
 *      offending model so the operator knows which card to expand.
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

// CSS colour expectations — must match the four CSS variables / hex
// literals in `components/omnisight/token-usage-stats.tsx` Row 3a.
const GREEN_VAR = "var(--validation-emerald)"
const ORANGE_VAR = "var(--hardware-orange)"
const RED_VAR = "var(--critical-red)"
const MUTED_VAR = "var(--muted-foreground)"
const YELLOW_HEX = "rgb(234, 179, 8)" // jsdom normalises #eab308 → rgb(...)

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
  pct: number | null,
  limit: number | null,
  tokensUsed: number,
) {
  sse.emit({
    event: "turn_metrics",
    data: {
      provider: "anthropic",
      model,
      input_tokens: tokensUsed,
      output_tokens: 0,
      tokens_used: tokensUsed,
      context_limit: limit,
      context_usage_pct: pct,
      latency_ms: 100,
      cache_read_tokens: null,
      cache_create_tokens: null,
    },
  })
}

beforeEach(() => {
  ;(api.subscribeEvents as ReturnType<typeof vi.fn>).mockReset()
})

describe("TokenUsageStats — context-window bar colour bands", () => {
  it("colours the context-usage bar green / yellow / orange / red across the four bands", async () => {
    const rows: ModelTokenUsage[] = [
      makeRow({ model: "claude-opus-4-7", totalTokens: 4000 }),
      makeRow({ model: "gpt-4o", totalTokens: 3000 }),
      makeRow({ model: "gemini-2.5-pro", totalTokens: 2000 }),
      makeRow({ model: "grok-4", totalTokens: 1000 }),
    ]
    const sse = primeSSE(api)
    const { container } = render(<TokenUsageStats externalUsage={rows} />)

    // Push one snapshot per model in a distinct band:
    //   25%  → green   (< 50)
    //   60%  → yellow  (50-75)
    //   80%  → orange  (75-90)
    //   95%  → red + pulse (>= 90)
    await act(async () => {
      emitTurnMetrics(sse, "claude-opus-4-7", 25, 1_000_000, 250_000)
      emitTurnMetrics(sse, "gpt-4o", 60, 128_000, 76_800)
      emitTurnMetrics(sse, "gemini-2.5-pro", 80, 2_000_000, 1_600_000)
      emitTurnMetrics(sse, "grok-4", 95, 131_072, 124_518)
    })

    const pctNodes = container.querySelectorAll<HTMLElement>(
      "[data-testid='context-usage-pct']",
    )
    expect(pctNodes).toHaveLength(4)

    // Rows are sorted by totalTokens DESC, matching the makeRow order.
    expect(pctNodes[0].textContent).toBe("25%")
    expect(pctNodes[0].style.color).toBe(GREEN_VAR)

    expect(pctNodes[1].textContent).toBe("60%")
    expect(pctNodes[1].style.color).toBe(YELLOW_HEX)

    expect(pctNodes[2].textContent).toBe("80%")
    expect(pctNodes[2].style.color).toBe(ORANGE_VAR)

    expect(pctNodes[3].textContent).toBe("95%")
    expect(pctNodes[3].style.color).toBe(RED_VAR)

    // Bar colours mirror the percent label so a future refactor
    // can't silently drift the rail colour away from the pct colour.
    const bars = container.querySelectorAll<HTMLElement>(
      "[data-testid='context-usage-bar']",
    )
    expect(bars[0].style.backgroundColor).toBe(GREEN_VAR)
    expect(bars[1].style.backgroundColor).toBe(YELLOW_HEX)
    expect(bars[2].style.backgroundColor).toBe(ORANGE_VAR)
    expect(bars[3].style.backgroundColor).toBe(RED_VAR)

    // Only the red (>=90) bar gets the ``animate-pulse`` class —
    // green / yellow / orange must NOT pulse, else the alarm signal
    // loses its meaning.
    expect(bars[0].className).not.toContain("animate-pulse")
    expect(bars[1].className).not.toContain("animate-pulse")
    expect(bars[2].className).not.toContain("animate-pulse")
    expect(bars[3].className).toContain("animate-pulse")
  })

  it("boundary values 50 / 75 / 90 land on yellow / orange / red per strict-upper band semantics", async () => {
    const rows: ModelTokenUsage[] = [
      makeRow({ model: "claude-opus-4-7", totalTokens: 3000 }),
      makeRow({ model: "gpt-4o", totalTokens: 2000 }),
      makeRow({ model: "gemini-2.5-pro", totalTokens: 1000 }),
    ]
    const sse = primeSSE(api)
    const { container } = render(<TokenUsageStats externalUsage={rows} />)

    await act(async () => {
      emitTurnMetrics(sse, "claude-opus-4-7", 50, 1_000_000, 500_000)
      emitTurnMetrics(sse, "gpt-4o", 75, 128_000, 96_000)
      emitTurnMetrics(sse, "gemini-2.5-pro", 90, 2_000_000, 1_800_000)
    })

    const pctNodes = container.querySelectorAll<HTMLElement>(
      "[data-testid='context-usage-pct']",
    )
    // 50 → yellow (strict ``<`` upper on green means 50 is NOT green).
    expect(pctNodes[0].style.color).toBe(YELLOW_HEX)
    // 75 → orange (strict ``<`` upper on yellow means 75 is NOT yellow).
    expect(pctNodes[1].style.color).toBe(ORANGE_VAR)
    // 90 → red (strict ``<`` upper on orange means 90 is NOT orange).
    expect(pctNodes[2].style.color).toBe(RED_VAR)
  })
})

describe("TokenUsageStats — context-window NULL degradation", () => {
  it("renders '—' in muted colour with an empty rail when context_limit is null", async () => {
    const rows: ModelTokenUsage[] = [
      makeRow({ model: "llama3.1", totalTokens: 500 }),
    ]
    const sse = primeSSE(api)
    const { container } = render(<TokenUsageStats externalUsage={rows} />)

    // Ollama local without env override → backend emits context_limit=null
    // and context_usage_pct=null. UI must degrade to "—" + muted.
    await act(async () => {
      emitTurnMetrics(sse, "llama3.1", null, null, 4000)
    })

    const pct = container.querySelector<HTMLElement>(
      "[data-testid='context-usage-pct']",
    )
    expect(pct).not.toBeNull()
    expect(pct!.textContent).toBe("—")
    // Muted foreground — explicitly not any of the four band colours —
    // so "no data" never masquerades as a red (critical) turn.
    expect(pct!.style.color).toBe(MUTED_VAR)
    expect(pct!.style.color).not.toBe(GREEN_VAR)
    expect(pct!.style.color).not.toBe(YELLOW_HEX)
    expect(pct!.style.color).not.toBe(ORANGE_VAR)
    expect(pct!.style.color).not.toBe(RED_VAR)

    // Bar fill degrades to 0% width on an empty rail.
    const bar = container.querySelector<HTMLElement>(
      "[data-testid='context-usage-bar']",
    )
    expect(bar).not.toBeNull()
    expect(bar!.style.width).toBe("0%")
    // NULL rows must never pulse — pulse is the critical-context signal.
    expect(bar!.className).not.toContain("animate-pulse")

    // Tooltip on the rail surfaces the "limit unknown" hint so the
    // operator sees the reason, not an unexplained em-dash.
    const rail = container.querySelector<HTMLElement>(
      "[data-testid='context-usage-bar-rail']",
    )
    expect(rail).not.toBeNull()
    expect(rail!.getAttribute("title")).toContain("context limit unknown")
  })

  it("renders '—' when no turn_metrics SSE has landed yet (fresh card)", () => {
    const rows: ModelTokenUsage[] = [
      makeRow({ model: "claude-opus-4-7", totalTokens: 500 }),
    ]
    primeSSE(api)
    const { container } = render(<TokenUsageStats externalUsage={rows} />)

    const pct = container.querySelector<HTMLElement>(
      "[data-testid='context-usage-pct']",
    )
    expect(pct).not.toBeNull()
    expect(pct!.textContent).toBe("—")
    expect(pct!.style.color).toBe(MUTED_VAR)

    const rail = container.querySelector<HTMLElement>(
      "[data-testid='context-usage-bar-rail']",
    )
    expect(rail!.getAttribute("title")).toContain("no turn_metrics seen yet")
  })
})

describe("TokenUsageStats — card-top warning icon trigger condition", () => {
  it("does NOT render the warning icon when all recent turns are below 90%", async () => {
    const rows: ModelTokenUsage[] = [
      makeRow({ model: "claude-opus-4-7", totalTokens: 3000 }),
      makeRow({ model: "gpt-4o", totalTokens: 2000 }),
    ]
    const sse = primeSSE(api)
    const { container } = render(<TokenUsageStats externalUsage={rows} />)

    // 89.99% is the near-miss sentinel: inclusive ``>= 90`` means 89.99
    // must NOT fire. A sibling at 45% makes sure multiple sub-threshold
    // rows don't accidentally sum into the alarm.
    await act(async () => {
      emitTurnMetrics(sse, "claude-opus-4-7", 89.99, 1_000_000, 899_900)
      emitTurnMetrics(sse, "gpt-4o", 45, 128_000, 57_600)
    })

    expect(
      container.querySelector("[data-testid='context-critical-warning']"),
    ).toBeNull()
  })

  it("renders the warning icon when any recent turn is >= 90%", async () => {
    const rows: ModelTokenUsage[] = [
      makeRow({ model: "claude-opus-4-7", totalTokens: 3000 }),
      makeRow({ model: "gpt-4o", totalTokens: 2000 }),
    ]
    const sse = primeSSE(api)
    const { container } = render(<TokenUsageStats externalUsage={rows} />)

    // Mixed state: one model near-cap, one comfortable. The icon must
    // fire and the tooltip must name ONLY the offending model so the
    // operator knows which card to expand.
    await act(async () => {
      emitTurnMetrics(sse, "claude-opus-4-7", 92, 1_000_000, 920_000)
      emitTurnMetrics(sse, "gpt-4o", 45, 128_000, 57_600)
    })

    const warning = container.querySelector<HTMLElement>(
      "[data-testid='context-critical-warning']",
    )
    expect(warning).not.toBeNull()
    const title = warning!.getAttribute("title") ?? ""
    // Spec-mandated Chinese prefix, literal per checkbox 5 spec.
    expect(title).toContain("Context 接近上限，agent 可能 truncate")
    // Offending model appears in the tooltip …
    expect(title).toContain("claude-opus-4-7")
    // … the comfortable sibling does NOT (tooltip must not cry wolf
    // over a model that's actually fine).
    expect(title).not.toContain("gpt-4o")
  })

  it("does NOT render the warning icon when the offending model has NULL context_limit", async () => {
    const rows: ModelTokenUsage[] = [
      makeRow({ model: "llama3.1", totalTokens: 500 }),
    ]
    const sse = primeSSE(api)
    const { container } = render(<TokenUsageStats externalUsage={rows} />)

    // NULL degradation: the backend emits pct=null when the context
    // limit is unknown (Ollama without env override, OpenRouter pass-
    // through, unknown provider). Even though the raw token count is
    // huge, "no data" must never fire the alarm — it's the whole point
    // of the NULL-vs-genuine-zero contract.
    await act(async () => {
      emitTurnMetrics(sse, "llama3.1", null, null, 10_000_000)
    })

    expect(
      container.querySelector("[data-testid='context-critical-warning']"),
    ).toBeNull()
  })

  it("stops firing the warning once the latest turn drops back under 90%", async () => {
    const rows: ModelTokenUsage[] = [
      makeRow({ model: "claude-opus-4-7", totalTokens: 3000 }),
    ]
    const sse = primeSSE(api)
    const { container } = render(<TokenUsageStats externalUsage={rows} />)

    // First turn at 95% → icon fires.
    await act(async () => {
      emitTurnMetrics(sse, "claude-opus-4-7", 95, 1_000_000, 950_000)
    })
    expect(
      container.querySelector("[data-testid='context-critical-warning']"),
    ).not.toBeNull()

    // Operator resets the conversation; next turn is 30% → snapshot
    // updates (latest-turn semantics, not lifetime), icon clears.
    await act(async () => {
      emitTurnMetrics(sse, "claude-opus-4-7", 30, 1_000_000, 300_000)
    })
    expect(
      container.querySelector("[data-testid='context-critical-warning']"),
    ).toBeNull()
  })
})
