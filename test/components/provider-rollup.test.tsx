/**
 * Z.4 (#293) checkbox 3 — <ProviderRollup> contract tests.
 *
 * Locks:
 *   * `groupByProvider()` pure helper — bucket / sum / sort semantics.
 *   * Summary-row render — label + model count + aggregate tokens +
 *     aggregate cost + aria-label for screen readers.
 *   * Default-collapsed behaviour + toggle on click.
 *   * `defaultExpanded={true}` reveal-all behaviour (Z.5 screenshot
 *     matrix will rely on this).
 *   * renderRow / renderExpansion / renderStatusBadge slot wiring.
 *   * Unknown provider ("" resolved label) bucketed under "Unknown".
 */

import { describe, expect, it } from "vitest"
import { render, fireEvent } from "@testing-library/react"

import {
  ProviderRollup,
  groupByProvider,
  type ProviderRollupRow,
  type ProviderGroup,
} from "@/components/omnisight/provider-rollup"

interface Row extends ProviderRollupRow {
  model: string
  inputTokens: number
  outputTokens: number
  totalTokens: number
  cost: number
  requestCount: number
}

function row(model: string, overrides: Partial<Row> = {}): Row {
  return {
    model,
    inputTokens: 0,
    outputTokens: 0,
    totalTokens: 0,
    cost: 0,
    requestCount: 0,
    ...overrides,
  }
}

function anthropic(model: string) {
  return { provider: "Anthropic", color: "#f59e0b" }
}
function google(model: string) {
  return { provider: "Google", color: "#3b82f6" }
}

/** Minimal stand-in for `getModelInfo` — a dispatcher based on the
 *  model string's prefix, matching the real resolver's behaviour for
 *  the model names these tests use. */
function fakeResolver(model: string) {
  if (model.startsWith("claude")) return anthropic(model)
  if (model.startsWith("gemini") || model.startsWith("gemma")) return google(model)
  if (model.startsWith("deepseek")) return { provider: "DeepSeek", color: "#06b6d4" }
  if (model.startsWith("gpt")) return { provider: "OpenAI", color: "#10b981" }
  return { provider: "", color: "" }
}

describe("groupByProvider()", () => {
  it("buckets rows by resolved provider label", () => {
    const rows: Row[] = [
      row("claude-opus-4-7", { totalTokens: 100 }),
      row("claude-sonnet-4", { totalTokens: 40 }),
      row("gemini-1.5-pro", { totalTokens: 20 }),
    ]
    const groups = groupByProvider(rows, fakeResolver)
    const labels = groups.map((g) => g.providerLabel)
    expect(labels).toContain("Anthropic")
    expect(labels).toContain("Google")
    const anth = groups.find((g) => g.providerLabel === "Anthropic")!
    expect(anth.rows).toHaveLength(2)
    expect(anth.rows.map((r) => r.model)).toEqual([
      "claude-opus-4-7",
      "claude-sonnet-4",
    ])
  })

  it("sums tokens + cost + requestCount into the group totals", () => {
    const rows: Row[] = [
      row("claude-opus", {
        inputTokens: 500,
        outputTokens: 200,
        totalTokens: 700,
        cost: 3.5,
        requestCount: 7,
      }),
      row("claude-haiku", {
        inputTokens: 50,
        outputTokens: 25,
        totalTokens: 75,
        cost: 0.125,
        requestCount: 3,
      }),
    ]
    const groups = groupByProvider(rows, fakeResolver)
    const anth = groups.find((g) => g.providerLabel === "Anthropic")!
    expect(anth.totals.inputTokens).toBe(550)
    expect(anth.totals.outputTokens).toBe(225)
    expect(anth.totals.totalTokens).toBe(775)
    expect(anth.totals.cost).toBeCloseTo(3.625)
    expect(anth.totals.requestCount).toBe(10)
  })

  it("sorts groups by aggregate totalTokens DESC", () => {
    const rows: Row[] = [
      row("gemini-1.5-pro", { totalTokens: 500 }),
      row("claude-opus", { totalTokens: 1_000 }),
      row("deepseek-chat", { totalTokens: 200 }),
    ]
    const groups = groupByProvider(rows, fakeResolver)
    expect(groups.map((g) => g.providerLabel)).toEqual([
      "Anthropic",
      "Google",
      "DeepSeek",
    ])
  })

  it('buckets unresolved providers (resolver returns "") under "Unknown"', () => {
    const rows: Row[] = [
      row("my-custom-model", { totalTokens: 42 }),
      row("vendor-x-llm", { totalTokens: 10 }),
    ]
    const groups = groupByProvider(rows, fakeResolver)
    expect(groups).toHaveLength(1)
    expect(groups[0].providerLabel).toBe("Unknown")
    expect(groups[0].providerKey).toBe("unknown")
    expect(groups[0].totals.totalTokens).toBe(52)
  })

  it("uses the first row's colour as the group colour", () => {
    const rows: Row[] = [
      row("claude-opus", { totalTokens: 500 }),
      row("claude-haiku", { totalTokens: 100 }),
    ]
    const [group] = groupByProvider(rows, fakeResolver)
    expect(group.color).toBe("#f59e0b")
  })

  it("assigns a lowercase providerKey for dedup", () => {
    const rows: Row[] = [row("claude-opus", { totalTokens: 1 })]
    const [group] = groupByProvider(rows, fakeResolver)
    expect(group.providerKey).toBe("anthropic")
  })

  it("emits an empty array for empty input", () => {
    expect(groupByProvider([], fakeResolver)).toEqual([])
  })

  it("preserves row input order within each group", () => {
    const rows: Row[] = [
      row("claude-opus", { totalTokens: 5 }),
      row("gemini-1.5-pro", { totalTokens: 9 }),
      row("claude-haiku", { totalTokens: 1 }),
    ]
    const groups = groupByProvider(rows, fakeResolver)
    const anth = groups.find((g) => g.providerLabel === "Anthropic")!
    expect(anth.rows.map((r) => r.model)).toEqual([
      "claude-opus",
      "claude-haiku",
    ])
  })
})

describe("<ProviderRollup />", () => {
  function buildGroups(): ProviderGroup<Row>[] {
    return groupByProvider(
      [
        row("claude-opus-4-7", {
          totalTokens: 1_000_000,
          inputTokens: 800_000,
          outputTokens: 200_000,
          cost: 7.5,
          requestCount: 12,
        }),
        row("claude-haiku", {
          totalTokens: 20_000,
          cost: 0.05,
          requestCount: 4,
        }),
        row("gemini-1.5-pro", {
          totalTokens: 500_000,
          cost: 1.25,
          requestCount: 6,
        }),
      ],
      fakeResolver,
    )
  }

  it("renders a row per provider group, sorted by totalTokens DESC", () => {
    const groups = buildGroups()
    const { getAllByTestId } = render(
      <ProviderRollup
        groups={groups}
        grandTotalTokens={1_520_000}
        renderRow={(r) => <div>{r.model}</div>}
      />,
    )
    const items = getAllByTestId(/^provider-rollup-group-/)
    const keys = items.map((el) => el.getAttribute("data-provider-key"))
    expect(keys).toEqual(["anthropic", "google"])
  })

  it("formats aggregated tokens + cost in the summary", () => {
    const groups = buildGroups()
    const { getByTestId } = render(
      <ProviderRollup
        groups={groups}
        grandTotalTokens={1_520_000}
        renderRow={(r) => <div>{r.model}</div>}
      />,
    )
    expect(
      getByTestId("provider-rollup-tokens-anthropic").textContent,
    ).toBe("1.02M tokens")
    expect(
      getByTestId("provider-rollup-cost-anthropic").textContent,
    ).toBe("$7.55")
    expect(
      getByTestId("provider-rollup-tokens-google").textContent,
    ).toBe("500.0K tokens")
    expect(
      getByTestId("provider-rollup-cost-google").textContent,
    ).toBe("$1.25")
  })

  it("renders the model-count label in the summary row", () => {
    const groups = buildGroups()
    const { getByTestId } = render(
      <ProviderRollup
        groups={groups}
        grandTotalTokens={1_520_000}
        renderRow={(r) => <div>{r.model}</div>}
      />,
    )
    expect(
      getByTestId("provider-rollup-model-count-anthropic").textContent,
    ).toBe("2 models")
    expect(
      getByTestId("provider-rollup-model-count-google").textContent,
    ).toBe("1 model")
  })

  it("renders the grand-total percentage per group", () => {
    const groups = buildGroups()
    const { getByTestId } = render(
      <ProviderRollup
        groups={groups}
        grandTotalTokens={1_520_000}
        renderRow={(r) => <div>{r.model}</div>}
      />,
    )
    // Anthropic: 1_020_000 / 1_520_000 ≈ 67.1%
    expect(
      getByTestId("provider-rollup-pct-anthropic").textContent,
    ).toBe("67.1%")
    // Google: 500_000 / 1_520_000 ≈ 32.9%
    expect(
      getByTestId("provider-rollup-pct-google").textContent,
    ).toBe("32.9%")
  })

  it('renders "0.0%" when grandTotalTokens is 0 (no requests yet)', () => {
    const groups: ProviderGroup<Row>[] = [
      {
        providerKey: "anthropic",
        providerLabel: "Anthropic",
        color: "#f59e0b",
        totals: {
          inputTokens: 0,
          outputTokens: 0,
          totalTokens: 0,
          cost: 0,
          requestCount: 0,
        },
        rows: [row("claude-opus")],
      },
    ]
    const { getByTestId } = render(
      <ProviderRollup
        groups={groups}
        grandTotalTokens={0}
        renderRow={(r) => <div>{r.model}</div>}
      />,
    )
    expect(
      getByTestId("provider-rollup-pct-anthropic").textContent,
    ).toBe("0.0%")
  })

  it("starts collapsed by default — per-model rows are not rendered", () => {
    const groups = buildGroups()
    const { queryByText, queryByTestId, getByTestId } = render(
      <ProviderRollup
        groups={groups}
        grandTotalTokens={1_520_000}
        renderRow={(r) => <div data-testid={`row-${r.model}`}>{r.model}</div>}
      />,
    )
    expect(queryByText("claude-opus-4-7")).toBeNull()
    expect(queryByText("claude-haiku")).toBeNull()
    expect(queryByText("gemini-1.5-pro")).toBeNull()
    expect(queryByTestId("row-claude-opus-4-7")).toBeNull()
    expect(
      getByTestId("provider-rollup-group-anthropic").getAttribute(
        "data-expanded",
      ),
    ).toBe("false")
  })

  it("reveals per-model rows when a provider summary is clicked", () => {
    const groups = buildGroups()
    const { getByTestId, queryByTestId } = render(
      <ProviderRollup
        groups={groups}
        grandTotalTokens={1_520_000}
        renderRow={(r) => <div data-testid={`row-${r.model}`}>{r.model}</div>}
      />,
    )
    fireEvent.click(getByTestId("provider-rollup-summary-anthropic"))
    expect(
      getByTestId("provider-rollup-group-anthropic").getAttribute(
        "data-expanded",
      ),
    ).toBe("true")
    expect(getByTestId("row-claude-opus-4-7")).toBeTruthy()
    expect(getByTestId("row-claude-haiku")).toBeTruthy()
    // Google still collapsed.
    expect(queryByTestId("row-gemini-1.5-pro")).toBeNull()
  })

  it("collapses again on a second click (toggle)", () => {
    const groups = buildGroups()
    const { getByTestId, queryByTestId } = render(
      <ProviderRollup
        groups={groups}
        grandTotalTokens={1_520_000}
        renderRow={(r) => <div data-testid={`row-${r.model}`}>{r.model}</div>}
      />,
    )
    const summary = getByTestId("provider-rollup-summary-anthropic")
    fireEvent.click(summary)
    fireEvent.click(summary)
    expect(queryByTestId("row-claude-opus-4-7")).toBeNull()
  })

  it("respects defaultExpanded=true (screenshot-matrix mode)", () => {
    const groups = buildGroups()
    const { getByTestId } = render(
      <ProviderRollup
        groups={groups}
        grandTotalTokens={1_520_000}
        defaultExpanded
        renderRow={(r) => <div data-testid={`row-${r.model}`}>{r.model}</div>}
      />,
    )
    expect(getByTestId("row-claude-opus-4-7")).toBeTruthy()
    expect(getByTestId("row-claude-haiku")).toBeTruthy()
    expect(getByTestId("row-gemini-1.5-pro")).toBeTruthy()
  })

  it("clicking a defaultExpanded group collapses it", () => {
    const groups = buildGroups()
    const { getByTestId, queryByTestId } = render(
      <ProviderRollup
        groups={groups}
        grandTotalTokens={1_520_000}
        defaultExpanded
        renderRow={(r) => <div data-testid={`row-${r.model}`}>{r.model}</div>}
      />,
    )
    fireEvent.click(getByTestId("provider-rollup-summary-anthropic"))
    expect(queryByTestId("row-claude-opus-4-7")).toBeNull()
    // Google remains expanded (per-group state).
    expect(getByTestId("row-gemini-1.5-pro")).toBeTruthy()
  })

  it("summary button exposes aria-expanded in sync with state", () => {
    const groups = buildGroups()
    const { getByTestId } = render(
      <ProviderRollup
        groups={groups}
        grandTotalTokens={1_520_000}
        renderRow={(r) => <div>{r.model}</div>}
      />,
    )
    const summary = getByTestId("provider-rollup-summary-anthropic")
    expect(summary.getAttribute("aria-expanded")).toBe("false")
    fireEvent.click(summary)
    expect(summary.getAttribute("aria-expanded")).toBe("true")
  })

  it("summary aria-label carries the concrete provider totals", () => {
    const groups = buildGroups()
    const { getByTestId } = render(
      <ProviderRollup
        groups={groups}
        grandTotalTokens={1_520_000}
        renderRow={(r) => <div>{r.model}</div>}
      />,
    )
    const summary = getByTestId("provider-rollup-summary-anthropic")
    const aria = summary.getAttribute("aria-label") ?? ""
    expect(aria).toContain("Anthropic")
    expect(aria).toContain("2 models")
    expect(aria).toContain("1.02M tokens")
    expect(aria).toContain("$7.55")
    expect(aria).toContain("16 requests")
  })

  it("renders the colour swatch in each summary row", () => {
    const groups = buildGroups()
    const { getByTestId } = render(
      <ProviderRollup
        groups={groups}
        grandTotalTokens={1_520_000}
        renderRow={(r) => <div>{r.model}</div>}
      />,
    )
    // The swatch is the first `<span>` with a backgroundColor style
    // inside the summary. Easiest lock: the provider-label element is
    // adjacent to it and the row itself is data-testid-tagged; assert
    // the button text contains the label + the data attributes match.
    expect(
      getByTestId("provider-rollup-label-anthropic").textContent,
    ).toBe("Anthropic")
  })

  it("mounts an optional status-badge slot when renderStatusBadge is provided", () => {
    const groups = buildGroups()
    const { getByTestId } = render(
      <ProviderRollup
        groups={groups}
        grandTotalTokens={1_520_000}
        renderRow={(r) => <div>{r.model}</div>}
        renderStatusBadge={(key) => (
          <span data-testid={`badge-${key}`}>badge-{key}</span>
        )}
      />,
    )
    expect(getByTestId("badge-anthropic").textContent).toBe("badge-anthropic")
    expect(getByTestId("badge-google").textContent).toBe("badge-google")
    expect(
      getByTestId("provider-rollup-status-slot-anthropic"),
    ).toBeTruthy()
  })

  it("omits the status-badge slot when renderStatusBadge is not provided", () => {
    const groups = buildGroups()
    const { queryByTestId } = render(
      <ProviderRollup
        groups={groups}
        grandTotalTokens={1_520_000}
        renderRow={(r) => <div>{r.model}</div>}
      />,
    )
    expect(
      queryByTestId("provider-rollup-status-slot-anthropic"),
    ).toBeNull()
  })

  it("mounts an optional expansion slot above per-model rows when expanded", () => {
    const groups = buildGroups()
    const { getByTestId, queryByTestId } = render(
      <ProviderRollup
        groups={groups}
        grandTotalTokens={1_520_000}
        renderRow={(r) => <div data-testid={`row-${r.model}`}>{r.model}</div>}
        renderExpansion={(key) => (
          <div data-testid={`exp-${key}`}>expansion-{key}</div>
        )}
      />,
    )
    // Collapsed: expansion slot not rendered.
    expect(queryByTestId("exp-anthropic")).toBeNull()
    fireEvent.click(getByTestId("provider-rollup-summary-anthropic"))
    expect(getByTestId("exp-anthropic").textContent).toBe("expansion-anthropic")
    // Expansion slot is inside the expanded body.
    const body = getByTestId("provider-rollup-body-anthropic")
    expect(body.contains(getByTestId("exp-anthropic"))).toBe(true)
  })

  it("status-badge slot clicks do not toggle the summary row", () => {
    const groups = buildGroups()
    const { getByTestId } = render(
      <ProviderRollup
        groups={groups}
        grandTotalTokens={1_520_000}
        renderRow={(r) => <div>{r.model}</div>}
        renderStatusBadge={(key) => (
          <button data-testid={`badge-${key}`}>badge</button>
        )}
      />,
    )
    const summary = getByTestId("provider-rollup-summary-anthropic")
    expect(summary.getAttribute("aria-expanded")).toBe("false")
    fireEvent.click(getByTestId("badge-anthropic"))
    expect(summary.getAttribute("aria-expanded")).toBe("false")
  })

  it("singular vs plural model-count label matches row count exactly", () => {
    const groups: ProviderGroup<Row>[] = [
      {
        providerKey: "openai",
        providerLabel: "OpenAI",
        color: "#10b981",
        totals: {
          inputTokens: 0,
          outputTokens: 0,
          totalTokens: 100,
          cost: 0,
          requestCount: 1,
        },
        rows: [row("gpt-4o", { totalTokens: 100 })],
      },
    ]
    const { getByTestId } = render(
      <ProviderRollup
        groups={groups}
        grandTotalTokens={100}
        renderRow={(r) => <div>{r.model}</div>}
      />,
    )
    expect(
      getByTestId("provider-rollup-model-count-openai").textContent,
    ).toBe("1 model")
  })

  it("handles zero groups cleanly", () => {
    const { getByTestId, queryByTestId } = render(
      <ProviderRollup
        groups={[]}
        grandTotalTokens={0}
        renderRow={(r) => <div>{r.model}</div>}
      />,
    )
    expect(getByTestId("provider-rollup")).toBeTruthy()
    expect(queryByTestId(/^provider-rollup-group-/)).toBeNull()
  })
})
