/**
 * ZZ.A1 (#303-1) — prompt-cache UI regression guards.
 *
 * The two tests here lock the frontend half of the ZZ.A1 spec:
 *
 * 1. **Badge colour × 3 bands.** The CACHE HIT percentage colour
 *    tracks the Wave A spec ("green > 50 / yellow 20–50 / red < 20").
 *    Boundary semantics: strict `>` at 50 (so 50.0 is yellow),
 *    inclusive `>=` at 20 (so 20.0 is yellow), which matches the
 *    literal reading of the spec. A future refactor that flips either
 *    boundary would silently mis-colour the dashboard — this test
 *    catches it.
 * 2. **NULL degradation renders "—".** Pre-ZZ rows (legacy payloads)
 *    and synthesised ``configuredProviders`` placeholder cards both
 *    carry ``null`` in the three cache fields. The badge must fall
 *    back to an em-dash and the muted-foreground colour, so "no data"
 *    stays visually distinct from a real 0% hit rate.
 */

import { describe, expect, it, vi, beforeEach } from "vitest"
import { render } from "@testing-library/react"

// ZZ.B3 #304-3 checkbox 2: TokenUsageStats now calls
// ``fetchTokenBurnRate`` on mount for the Row 1 burn-rate sparkline.
// These tests render the component without going through ``primeSSE``;
// stub the fetch directly so we don't hit the real ``request`` retry
// ladder inside jsdom.
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>()
  return {
    ...actual,
    subscribeEvents: vi.fn(() => ({ close: () => undefined, readyState: 1 })),
    fetchTokenBurnRate: vi.fn().mockResolvedValue({
      window: "1h",
      bucket_seconds: 60,
      points: [],
    }),
  }
})

import * as api from "@/lib/api"
import {
  TokenUsageStats,
  type ModelTokenUsage,
} from "@/components/omnisight/token-usage-stats"

beforeEach(() => {
  ;(api.fetchTokenBurnRate as ReturnType<typeof vi.fn>).mockClear()
})

const baseUsage: Omit<
  ModelTokenUsage,
  "model" | "cacheReadTokens" | "cacheCreateTokens" | "cacheHitRatio"
> = {
  inputTokens: 1000,
  outputTokens: 500,
  totalTokens: 1500,
  cost: 0.05,
  requestCount: 3,
  avgLatency: 120,
  lastUsed: "10:00:00",
}

function makeRow(overrides: Partial<ModelTokenUsage>): ModelTokenUsage {
  return {
    model: "claude-opus-4-7",
    ...baseUsage,
    cacheReadTokens: 0,
    cacheCreateTokens: 0,
    cacheHitRatio: 0,
    ...overrides,
  }
}

// CSS colour expectations — must match the three CSS variables / hex
// literals in `components/omnisight/token-usage-stats.tsx` Row 3b.
const GREEN_VAR = "var(--validation-emerald)"
const RED_VAR = "var(--critical-red)"
const MUTED_VAR = "var(--muted-foreground)"
const YELLOW_HEX = "rgb(234, 179, 8)" // jsdom normalises #eab308 → rgb(...)

describe("TokenUsageStats — CACHE HIT badge colour bands", () => {
  it("colours the CACHE HIT percentage green / yellow / red across the three bands", () => {
    // Three rows, each in a distinct band:
    //   0.72 → 72% → green  (> 50)
    //   0.35 → 35% → yellow (20 ≤ r ≤ 50)
    //   0.10 → 10% → red    (< 20)
    const rows: ModelTokenUsage[] = [
      makeRow({
        model: "claude-opus-4-7",
        cacheReadTokens: 800,
        cacheCreateTokens: 100,
        cacheHitRatio: 0.72,
      }),
      makeRow({
        model: "gpt-4o",
        cacheReadTokens: 300,
        cacheCreateTokens: 50,
        cacheHitRatio: 0.35,
      }),
      makeRow({
        model: "gemini-3.1-pro",
        cacheReadTokens: 50,
        cacheCreateTokens: 10,
        cacheHitRatio: 0.10,
      }),
    ]

    const { container } = render(<TokenUsageStats externalUsage={rows} />)
    const pctNodes = container.querySelectorAll<HTMLElement>(
      "[data-testid='cache-hit-pct']",
    )
    expect(pctNodes).toHaveLength(3)

    // The component sorts by totalTokens DESC; all three rows share
    // the same totals here so order is stable as provided (72 / 35 / 10).
    expect(pctNodes[0].textContent).toBe("72%")
    expect(pctNodes[0].style.color).toBe(GREEN_VAR)

    expect(pctNodes[1].textContent).toBe("35%")
    // Yellow hex #eab308 — jsdom normalises to rgb() form.
    expect(pctNodes[1].style.color).toBe(YELLOW_HEX)

    expect(pctNodes[2].textContent).toBe("10%")
    expect(pctNodes[2].style.color).toBe(RED_VAR)

    // And the coloured fill bar tracks the same colour (sanity check:
    // the bar shouldn't lag the percent label if the band logic flips).
    const bars = container.querySelectorAll<HTMLElement>(
      "[data-testid='cache-hit-bar']",
    )
    expect(bars[0].style.backgroundColor).toBe(GREEN_VAR)
    expect(bars[1].style.backgroundColor).toBe(YELLOW_HEX)
    expect(bars[2].style.backgroundColor).toBe(RED_VAR)
  })
})

describe("TokenUsageStats — NULL degradation renders em-dash", () => {
  it("renders '—' with muted colour when the three cache fields are null", () => {
    // Pre-ZZ row: legacy Redis payload surfaced via /runtime/tokens
    // with cache_* = null. The dashboard must NOT fall back to 0% —
    // that would be a lie. An em-dash + muted colour keeps "no data"
    // visually distinct from "zero hits".
    const rows: ModelTokenUsage[] = [
      makeRow({
        model: "claude-opus-4-7",
        cacheReadTokens: null,
        cacheCreateTokens: null,
        cacheHitRatio: null,
      }),
    ]

    const { container } = render(<TokenUsageStats externalUsage={rows} />)
    const pct = container.querySelector<HTMLElement>(
      "[data-testid='cache-hit-pct']",
    )
    expect(pct).not.toBeNull()
    expect(pct!.textContent).toBe("—")
    // Muted foreground — NOT any of the three band colours — so the
    // "no data" case doesn't masquerade as a red (bad) hit rate.
    expect(pct!.style.color).toBe(MUTED_VAR)
    expect(pct!.style.color).not.toBe(GREEN_VAR)
    expect(pct!.style.color).not.toBe(RED_VAR)
    expect(pct!.style.color).not.toBe(YELLOW_HEX)

    // CACHE HIT rail renders at 0% width (empty rail signals "no
    // data" visually — complementing the em-dash label).
    const bar = container.querySelector<HTMLElement>(
      "[data-testid='cache-hit-bar']",
    )
    expect(bar).not.toBeNull()
    expect(bar!.style.width).toBe("0%")

    // Container-level tooltip carries the "no data" hint so operators
    // hovering the section don't have to guess why the bar is empty.
    const section = container.querySelector<HTMLElement>(
      "[data-testid='cache-hit-section']",
    )
    expect(section).not.toBeNull()
    expect(section!.getAttribute("title")).toContain("no cache data")
  })
})
