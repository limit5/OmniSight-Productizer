/**
 * ZZ.B3 (#304-3) checkbox 2 — Row 1 sparkline + current-rate badge.
 *
 * The four tests here lock the frontend half of the Wave B.3 spec:
 *
 * 1. **Mount fetch + badge renders "$0.12/hr".** On mount the component
 *    calls ``fetchTokenBurnRate("1h")`` and paints the latest bucket's
 *    ``cost_per_hour`` as the pinned current-rate badge next to Row 1.
 *    Formatting follows the cost helper convention: ``< $1`` → 3 d.p.
 *    (so the canonical spec example "$0.12/hr" comes out correct).
 * 2. **NULL degradation → "$—/hr".** When the endpoint returns an empty
 *    series (tenant hasn't emitted any turns yet, or the backend row is
 *    offline), the badge must not show "$0.000/hr" — that would
 *    masquerade as a real zero. The em-dash matches the NULL-vs-genuine-
 *    zero contract ZZ.A1 established for the cache fields.
 * 3. **Hover reveals 15m / 1h / 24h tabs.** The tab row is hidden until
 *    the operator hovers over the sparkline group. On hover, the three
 *    window tabs appear with the current selection (``1h`` by default)
 *    highlighted. Moving the mouse away hides the tabs again.
 * 4. **Tab click re-fetches for that window + updates selected state.**
 *    Clicking a non-default tab fires ``fetchTokenBurnRate("<window>")``
 *    and flips the ``aria-pressed`` highlight. Crucially, the click
 *    must NOT bubble up into the expand/collapse toggle — if it did,
 *    the operator would lose the TOKEN USAGE panel every time they
 *    switched windows.
 */

import { describe, expect, it, vi, beforeEach, afterEach } from "vitest"
import { render, act, fireEvent, waitFor } from "@testing-library/react"

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>()
  return {
    ...actual,
    subscribeEvents: vi.fn(() => ({ close: () => undefined, readyState: 1 })),
    fetchTokenBurnRate: vi.fn(),
  }
})

import * as api from "@/lib/api"
import {
  TokenUsageStats,
  type ModelTokenUsage,
} from "@/components/omnisight/token-usage-stats"

function makeRow(overrides: Partial<ModelTokenUsage> = {}): ModelTokenUsage {
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

beforeEach(() => {
  ;(api.fetchTokenBurnRate as ReturnType<typeof vi.fn>).mockReset()
})

afterEach(() => {
  vi.useRealTimers()
})

describe("TokenUsageStats — Row 1 burn-rate sparkline + badge", () => {
  it("fetches 1h window on mount and renders the '$0.12/hr' badge from the latest bucket", async () => {
    // Spec canonical: ``cost_per_hour: 0.12`` → "$0.12/hr". Two buckets
    // drive the MetricSparkline polyline (>=2 points) so the sparkline
    // swaps from the "—" empty-state placeholder to real SVG.
    ;(api.fetchTokenBurnRate as ReturnType<typeof vi.fn>).mockResolvedValue({
      window: "1h",
      bucket_seconds: 60,
      points: [
        { timestamp: "2026-04-24T12:00:00Z", tokens_per_hour: 120_000, cost_per_hour: 0.08 },
        { timestamp: "2026-04-24T12:01:00Z", tokens_per_hour: 180_000, cost_per_hour: 0.12 },
      ],
    })

    const { container } = render(
      <TokenUsageStats externalUsage={[makeRow()]} />,
    )

    // Give the mount-time effect a tick to resolve.
    await waitFor(() => {
      const badge = container.querySelector<HTMLElement>(
        "[data-testid='burn-rate-badge']",
      )
      expect(badge?.textContent).toBe("$0.120/hr")
    })

    // 1h is the default window — the fetch should have been called
    // exactly once with that window.
    expect(api.fetchTokenBurnRate).toHaveBeenCalledTimes(1)
    expect(api.fetchTokenBurnRate).toHaveBeenCalledWith("1h")

    // Sparkline materialises — MetricSparkline with 2 points renders
    // an SVG (not the "—" placeholder div used at < 2 points).
    const sparkline = container.querySelector<SVGElement>(
      "[data-testid='burn-rate-sparkline']",
    )
    expect(sparkline?.tagName.toLowerCase()).toBe("svg")
    expect(sparkline?.getAttribute("data-points")).toBe("2")
  })

  it("renders '$—/hr' when the endpoint returns an empty series", async () => {
    // NULL-vs-genuine-zero contract: empty series must NOT render
    // "$0.000/hr" — operators read that as a real zero.
    ;(api.fetchTokenBurnRate as ReturnType<typeof vi.fn>).mockResolvedValue({
      window: "1h",
      bucket_seconds: 60,
      points: [],
    })

    const { container } = render(
      <TokenUsageStats externalUsage={[makeRow()]} />,
    )

    await waitFor(() => {
      const badge = container.querySelector<HTMLElement>(
        "[data-testid='burn-rate-badge']",
      )
      expect(badge?.textContent).toBe("$—/hr")
    })

    // MetricSparkline renders its <2-point empty state — a muted
    // <div data-empty="true"> rather than an SVG. This is the signal
    // that "no data" stays visually distinct from a genuine 0 rate.
    const empty = container.querySelector<HTMLElement>(
      "[data-testid='burn-rate-sparkline']",
    )
    expect(empty?.getAttribute("data-empty")).toBe("true")
  })

  it("hover surfaces the 15m / 1h / 24h tab row with 1h highlighted", async () => {
    ;(api.fetchTokenBurnRate as ReturnType<typeof vi.fn>).mockResolvedValue({
      window: "1h",
      bucket_seconds: 60,
      points: [],
    })
    const { container } = render(
      <TokenUsageStats externalUsage={[makeRow()]} />,
    )
    await waitFor(() => {
      expect(api.fetchTokenBurnRate).toHaveBeenCalled()
    })

    const group = container.querySelector<HTMLElement>(
      "[data-testid='burn-rate-group']",
    )
    expect(group).not.toBeNull()

    // Before hover: tabs are NOT in the DOM (unmounted, not just
    // hidden — a display:none impl would still pass a
    // ``queryByTestId`` check, so we rely on absence).
    expect(
      container.querySelector("[data-testid='burn-rate-tabs']"),
    ).toBeNull()

    act(() => {
      fireEvent.mouseEnter(group!)
    })

    const tabs = container.querySelector<HTMLElement>(
      "[data-testid='burn-rate-tabs']",
    )
    expect(tabs).not.toBeNull()

    // The three window tabs exist and the current window (1h) is
    // aria-pressed="true" — other two are "false".
    const tab15 = container.querySelector<HTMLElement>("[data-testid='burn-rate-tab-15m']")
    const tab1 = container.querySelector<HTMLElement>("[data-testid='burn-rate-tab-1h']")
    const tab24 = container.querySelector<HTMLElement>("[data-testid='burn-rate-tab-24h']")
    expect(tab15?.getAttribute("aria-pressed")).toBe("false")
    expect(tab1?.getAttribute("aria-pressed")).toBe("true")
    expect(tab24?.getAttribute("aria-pressed")).toBe("false")

    act(() => {
      fireEvent.mouseLeave(group!)
    })
    expect(
      container.querySelector("[data-testid='burn-rate-tabs']"),
    ).toBeNull()
  })

  it("clicking a tab re-fetches that window, updates aria-pressed, and does NOT collapse the panel", async () => {
    // Two resolutions: first call (mount, 1h) → points including $0.12,
    // second call (tab click, 24h) → points including $0.340 so the
    // badge visibly updates.
    const mock = api.fetchTokenBurnRate as ReturnType<typeof vi.fn>
    mock.mockImplementation(async (window: unknown) => {
      if (window === "24h") {
        return {
          window: "24h",
          bucket_seconds: 60,
          points: [
            { timestamp: "2026-04-24T12:00:00Z", tokens_per_hour: 500_000, cost_per_hour: 0.34 },
            { timestamp: "2026-04-24T12:01:00Z", tokens_per_hour: 520_000, cost_per_hour: 0.35 },
          ],
        }
      }
      return {
        window: "1h",
        bucket_seconds: 60,
        points: [
          { timestamp: "2026-04-24T12:00:00Z", tokens_per_hour: 120_000, cost_per_hour: 0.08 },
          { timestamp: "2026-04-24T12:01:00Z", tokens_per_hour: 180_000, cost_per_hour: 0.12 },
        ],
      }
    })

    const { container } = render(
      <TokenUsageStats externalUsage={[makeRow()]} />,
    )
    await waitFor(() => {
      const badge = container.querySelector<HTMLElement>(
        "[data-testid='burn-rate-badge']",
      )
      expect(badge?.textContent).toBe("$0.120/hr")
    })

    // Panel starts expanded → per-model card for claude-opus-4-7 is
    // visible in the DOM. The aria-expanded state on the left
    // toggle is the canonical "panel expanded?" signal.
    const toggle = container.querySelector<HTMLElement>(
      "[data-testid='token-usage-expand-toggle']",
    )
    expect(toggle?.getAttribute("aria-expanded")).toBe("true")

    // Hover to reveal tabs, then click 24h.
    const group = container.querySelector<HTMLElement>(
      "[data-testid='burn-rate-group']",
    )
    act(() => {
      fireEvent.mouseEnter(group!)
    })
    const tab24 = container.querySelector<HTMLElement>(
      "[data-testid='burn-rate-tab-24h']",
    )
    expect(tab24).not.toBeNull()
    act(() => {
      fireEvent.click(tab24!)
    })

    // The badge should flip to the new window's latest bucket
    // ($0.35/hr formatted with 3 d.p. since < $1).
    await waitFor(() => {
      const badge = container.querySelector<HTMLElement>(
        "[data-testid='burn-rate-badge']",
      )
      expect(badge?.textContent).toBe("$0.350/hr")
    })

    // Fetch called twice total: once on mount (1h) and once on tab
    // click (24h). The second call carried the new window arg.
    expect(mock).toHaveBeenCalledTimes(2)
    expect(mock.mock.calls[0][0]).toBe("1h")
    expect(mock.mock.calls[1][0]).toBe("24h")

    // aria-pressed flipped — 24h is now selected, 1h is not.
    // (Re-query because hover may still be on, keeping tabs in DOM.)
    const tab1After = container.querySelector<HTMLElement>(
      "[data-testid='burn-rate-tab-1h']",
    )
    const tab24After = container.querySelector<HTMLElement>(
      "[data-testid='burn-rate-tab-24h']",
    )
    expect(tab1After?.getAttribute("aria-pressed")).toBe("false")
    expect(tab24After?.getAttribute("aria-pressed")).toBe("true")

    // Crucially: the panel is still expanded. If tab clicks bubbled
    // up into the outer header and fired the expand toggle, the
    // aria-expanded attribute on the left toggle would have flipped
    // to "false". This is the regression guard for nesting buttons /
    // bubbling — operators losing their panel every time they
    // switched windows would be a paper cut.
    const toggleAfter = container.querySelector<HTMLElement>(
      "[data-testid='token-usage-expand-toggle']",
    )
    expect(toggleAfter?.getAttribute("aria-expanded")).toBe("true")
  })
})
