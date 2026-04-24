/**
 * ZZ.C2 (#305-2, 2026-04-24) checkbox 3 — Session Heatmap section mount.
 *
 * Locks the three UX guarantees the spec requires when the heatmap is
 * hung at the bottom of ``<TokenUsageStats>``:
 *
 *   1. **Default collapsed** — the spec says "預設折起避免佔版面"
 *      (default folded so it doesn't eat vertical space). On mount the
 *      section header + chevron are visible, but the ``<SessionHeatmap>``
 *      grid itself must NOT be in the DOM. A test that only checked for
 *      ``data-testid='session-heatmap-section'`` visibility would miss a
 *      ``display:none`` impl; we rely on absence of the inner
 *      ``data-testid='session-heatmap'`` element.
 *   2. **Click to expand** — clicking the toggle header flips
 *      ``aria-expanded`` from ``false`` → ``true`` and mounts the
 *      ``<SessionHeatmap>`` component. A second click re-collapses it
 *      (unmounts the inner component) — toggle behaviour, not
 *      single-shot.
 *   3. **Lazy fetch gating** — the heatmap endpoint
 *      (``fetchTokenHeatmap``) must NOT be called until the operator
 *      expands the section. Default-collapsed keeps the panel cheap
 *      for operators who never open it (avoids an extra 60s polling
 *      loop per mount). The fetch fires on the first expand and the
 *      component's own polling kicks in from there.
 *   4. **Parent-collapse hides the section** — when the outer
 *      TokenUsageStats panel itself is collapsed (the Row 1 toggle
 *      flipped), the heatmap section wrapper must also disappear.
 *      Prevents a stray "SESSION HEATMAP" toggle row hanging under a
 *      folded-up panel, which would defeat the space-saving intent.
 */

import { describe, expect, it, vi, beforeEach, afterEach } from "vitest"
import { render, act, fireEvent, waitFor } from "@testing-library/react"

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
    fetchTokenHeatmap: vi.fn().mockResolvedValue({
      window: "7d",
      cells: [],
    }),
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
  ;(api.fetchTokenBurnRate as ReturnType<typeof vi.fn>).mockClear()
  ;(api.fetchTokenHeatmap as ReturnType<typeof vi.fn>).mockClear()
})

afterEach(() => {
  vi.useRealTimers()
})

describe("TokenUsageStats — Session Heatmap section (ZZ.C2 checkbox 3)", () => {
  it("renders the toggle header by default but keeps the SessionHeatmap unmounted", async () => {
    const { container } = render(
      <TokenUsageStats externalUsage={[makeRow()]} />,
    )

    // Wait for the burn-rate mount fetch so later assertions are stable.
    await waitFor(() => {
      expect(api.fetchTokenBurnRate).toHaveBeenCalled()
    })

    // Section wrapper + toggle button are in the DOM.
    const section = container.querySelector(
      "[data-testid='session-heatmap-section']",
    )
    expect(section).not.toBeNull()

    const toggle = container.querySelector<HTMLButtonElement>(
      "[data-testid='session-heatmap-section-toggle']",
    )
    expect(toggle).not.toBeNull()
    expect(toggle!.getAttribute("aria-expanded")).toBe("false")
    expect(toggle!.textContent).toContain("SESSION HEATMAP")

    // The inner SessionHeatmap component is NOT mounted yet.
    expect(
      container.querySelector("[data-testid='session-heatmap']"),
    ).toBeNull()

    // Grid / tabs / refresh — none of them should exist prior to expand.
    expect(
      container.querySelector("[data-testid='session-heatmap-grid']"),
    ).toBeNull()
    expect(
      container.querySelector("[data-testid='session-heatmap-window-tabs']"),
    ).toBeNull()

    // CRUCIAL: the heatmap endpoint must NOT have been hit on mount.
    expect(api.fetchTokenHeatmap).not.toHaveBeenCalled()
  })

  it("clicking the toggle expands and mounts the SessionHeatmap, fires the endpoint", async () => {
    const { container } = render(
      <TokenUsageStats externalUsage={[makeRow()]} />,
    )
    await waitFor(() => {
      expect(api.fetchTokenBurnRate).toHaveBeenCalled()
    })

    const toggle = container.querySelector<HTMLButtonElement>(
      "[data-testid='session-heatmap-section-toggle']",
    )
    expect(toggle).not.toBeNull()

    act(() => {
      fireEvent.click(toggle!)
    })

    // aria-expanded flips true.
    expect(toggle!.getAttribute("aria-expanded")).toBe("true")

    // SessionHeatmap mounts — grid wrapper appears once the initial
    // fetch resolves.
    await waitFor(() => {
      expect(
        container.querySelector("[data-testid='session-heatmap']"),
      ).not.toBeNull()
    })

    // Heatmap endpoint fired exactly once, and for the default "7d"
    // window. The second positional arg is the checkbox-4 per-model
    // filter — ``null`` means "All models" and is the default on
    // mount so pre-checkbox-4 behaviour is preserved.
    expect(api.fetchTokenHeatmap).toHaveBeenCalledTimes(1)
    expect(api.fetchTokenHeatmap).toHaveBeenCalledWith("7d", null)
  })

  it("clicking the toggle a second time collapses + unmounts the SessionHeatmap", async () => {
    const { container } = render(
      <TokenUsageStats externalUsage={[makeRow()]} />,
    )
    await waitFor(() => {
      expect(api.fetchTokenBurnRate).toHaveBeenCalled()
    })

    const toggle = container.querySelector<HTMLButtonElement>(
      "[data-testid='session-heatmap-section-toggle']",
    )!

    act(() => {
      fireEvent.click(toggle)
    })
    await waitFor(() => {
      expect(
        container.querySelector("[data-testid='session-heatmap']"),
      ).not.toBeNull()
    })

    act(() => {
      fireEvent.click(toggle)
    })

    expect(toggle.getAttribute("aria-expanded")).toBe("false")
    // Component must unmount (not just hide) — we rely on absence of
    // the inner data-testid rather than a display:none check so a
    // future refactor can't silently swap behaviour.
    expect(
      container.querySelector("[data-testid='session-heatmap']"),
    ).toBeNull()
  })

  it("hides the entire heatmap section when the outer TokenUsageStats panel is collapsed", async () => {
    const { container } = render(
      <TokenUsageStats externalUsage={[makeRow()]} />,
    )
    await waitFor(() => {
      expect(api.fetchTokenBurnRate).toHaveBeenCalled()
    })

    // Heatmap section exists while the outer panel is expanded (default).
    expect(
      container.querySelector("[data-testid='session-heatmap-section']"),
    ).not.toBeNull()

    // Collapse the outer panel via its Row 1 expand toggle — the same
    // toggle the operator clicks to compact the card.
    const outerToggle = container.querySelector<HTMLButtonElement>(
      "[data-testid='token-usage-expand-toggle']",
    )
    expect(outerToggle).not.toBeNull()
    act(() => {
      fireEvent.click(outerToggle!)
    })

    // Section wrapper + its toggle must both be gone so a folded panel
    // stays compact. This is the "避免佔版面" contract — a stray
    // SESSION HEATMAP row under a collapsed panel would defeat the
    // whole reason the section is foldable.
    expect(
      container.querySelector("[data-testid='session-heatmap-section']"),
    ).toBeNull()
    expect(
      container.querySelector("[data-testid='session-heatmap-section-toggle']"),
    ).toBeNull()
  })
})
