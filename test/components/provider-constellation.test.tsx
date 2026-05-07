/**
 * MP.W4.8 / OP-44 — provider constellation interaction, accessibility,
 * and responsive-layout contract tests.
 *
 * This suite composes the existing MP provider primitives instead of
 * introducing a new product component: ProviderRollup owns the provider
 * constellation shell, while ProviderStatusBadge and ProviderCardExpansion
 * fill the summary and detail slots.
 */

import { render, screen, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { describe, expect, it } from "vitest"

import {
  ProviderRollup,
  type ProviderGroup,
  type ProviderRollupRow,
} from "@/components/omnisight/provider-rollup"
import { ProviderCardExpansion } from "@/components/omnisight/provider-card-expansion"
import { ProviderStatusBadge } from "@/components/omnisight/provider-status-badge"

interface Row extends ProviderRollupRow {
  model: string
}

const NOW = 1_700_000_000

function row(
  model: string,
  overrides: Partial<ProviderRollupRow> = {},
): Row {
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

function group(
  providerKey: string,
  providerLabel: string,
  color: string,
  rows: Row[],
): ProviderGroup<Row> {
  return {
    providerKey,
    providerLabel,
    color,
    rows,
    totals: rows.reduce(
      (totals, current) => ({
        inputTokens: totals.inputTokens + current.inputTokens,
        outputTokens: totals.outputTokens + current.outputTokens,
        totalTokens: totals.totalTokens + current.totalTokens,
        cost: totals.cost + current.cost,
        requestCount: totals.requestCount + current.requestCount,
      }),
      {
        inputTokens: 0,
        outputTokens: 0,
        totalTokens: 0,
        cost: 0,
        requestCount: 0,
      },
    ),
  }
}

function providerGroups(): ProviderGroup<Row>[] {
  return [
    group("anthropic", "Anthropic", "#f59e0b", [
      row("claude-opus-4-7", {
        inputTokens: 800_000,
        outputTokens: 200_000,
        totalTokens: 1_000_000,
        cost: 7.5,
        requestCount: 12,
      }),
      row("claude-haiku", {
        totalTokens: 20_000,
        cost: 0.05,
        requestCount: 4,
      }),
    ]),
    group("openrouter", "OpenRouter", "#a855f7", [
      row("anthropic/claude-sonnet-4", {
        totalTokens: 500_000,
        cost: 1.25,
        requestCount: 6,
      }),
    ]),
    group("deepseek", "DeepSeek", "#06b6d4", [
      row("deepseek-chat", {
        totalTokens: 80_000,
        cost: 0.45,
        requestCount: 2,
      }),
    ]),
  ]
}

function renderConstellation(
  groups: ProviderGroup<Row>[] = providerGroups(),
) {
  return render(
    <ProviderRollup
      groups={groups}
      grandTotalTokens={groups.reduce(
        (total, current) => total + current.totals.totalTokens,
        0,
      )}
      renderRow={(modelRow) => (
        <div data-testid={`constellation-model-${modelRow.model}`}>
          {modelRow.model}
        </div>
      )}
      renderStatusBadge={(providerKey) => (
        <ProviderStatusBadge
          provider={providerKey}
          status={providerKey === "openrouter" ? "unsupported" : "ok"}
          reason={
            providerKey === "openrouter"
              ? "OpenRouter usage endpoint is dashboard-only for this tenant."
              : undefined
          }
          balanceRemaining={providerKey === "deepseek" ? 2 : 80}
          grantedTotal={100}
          currency={providerKey === "deepseek" ? "CNY" : "USD"}
          rateLimitRemainingRequests={providerKey === "deepseek" ? 0 : 900}
          rateLimitLimitRequests={1_000}
        />
      )}
      renderExpansion={(providerKey) => (
        <ProviderCardExpansion
          provider={providerKey}
          status={providerKey === "openrouter" ? "unsupported" : "ok"}
          reason={
            providerKey === "openrouter"
              ? "OpenRouter usage endpoint is dashboard-only for this tenant."
              : undefined
          }
          balanceRemaining={providerKey === "deepseek" ? 2 : 80}
          grantedTotal={100}
          currency={providerKey === "deepseek" ? "CNY" : "USD"}
          rateLimitRemainingRequests={providerKey === "deepseek" ? 0 : 900}
          rateLimitRemainingTokens={providerKey === "deepseek" ? 0 : 199_876}
          resetAtTs={NOW + 42}
          lastRefreshedAt={NOW - 125}
          nowTs={NOW}
        />
      )}
    />,
  )
}

describe("provider constellation rendering", () => {
  it("renders one summary button per provider in constellation order", () => {
    renderConstellation()

    expect(screen.getAllByTestId(/^provider-rollup-group-/)).toHaveLength(3)
    expect(screen.getByTestId("provider-rollup-label-anthropic")).toHaveTextContent("Anthropic")
    expect(screen.getByTestId("provider-rollup-label-openrouter")).toHaveTextContent("OpenRouter")
    expect(screen.getByTestId("provider-rollup-label-deepseek")).toHaveTextContent("DeepSeek")
  })

  it("keeps every provider collapsed on first render", () => {
    renderConstellation()

    expect(screen.getByTestId("provider-rollup-group-anthropic")).toHaveAttribute("data-expanded", "false")
    expect(screen.getByTestId("provider-rollup-group-openrouter")).toHaveAttribute("data-expanded", "false")
    expect(screen.queryByTestId("constellation-model-claude-opus-4-7")).toBeNull()
  })

  it("shows aggregate usage, cost, and share for each provider", () => {
    renderConstellation()

    expect(screen.getByTestId("provider-rollup-tokens-anthropic")).toHaveTextContent("1.02M tokens")
    expect(screen.getByTestId("provider-rollup-cost-anthropic")).toHaveTextContent("$7.55")
    expect(screen.getByTestId("provider-rollup-pct-anthropic")).toHaveTextContent("63.7%")
    expect(screen.getByTestId("provider-rollup-tokens-openrouter")).toHaveTextContent("500.0K tokens")
  })

  it("renders status badges in the summary slot for every provider", () => {
    renderConstellation()

    expect(screen.getByTestId("provider-rollup-status-slot-anthropic")).toContainElement(
      within(screen.getByTestId("provider-rollup-status-slot-anthropic")).getByTestId("provider-status-badge"),
    )
    expect(screen.getByTestId("provider-rollup-status-slot-openrouter")).toContainElement(
      within(screen.getByTestId("provider-rollup-status-slot-openrouter")).getByTestId("provider-status-badge"),
    )
  })
})

describe("provider constellation interaction", () => {
  it("expands only the clicked provider and renders its detail card plus rows", async () => {
    const user = userEvent.setup()
    renderConstellation()

    await user.click(screen.getByTestId("provider-rollup-summary-anthropic"))

    expect(screen.getByTestId("provider-rollup-group-anthropic")).toHaveAttribute("data-expanded", "true")
    expect(screen.getByTestId("provider-card-expansion")).toHaveAttribute("data-provider", "anthropic")
    expect(screen.getByTestId("constellation-model-claude-opus-4-7")).toBeInTheDocument()
    expect(screen.queryByTestId("constellation-model-anthropic/claude-sonnet-4")).toBeNull()
  })

  it("collapses a provider on the second click", async () => {
    const user = userEvent.setup()
    renderConstellation()
    const summary = screen.getByTestId("provider-rollup-summary-anthropic")

    await user.click(summary)
    await user.click(summary)

    expect(screen.getByTestId("provider-rollup-group-anthropic")).toHaveAttribute("data-expanded", "false")
    expect(screen.queryByTestId("constellation-model-claude-opus-4-7")).toBeNull()
  })

  it("expands with keyboard Enter when the summary button has focus", async () => {
    const user = userEvent.setup()
    renderConstellation()

    await user.tab()
    expect(screen.getByTestId("provider-rollup-summary-anthropic")).toHaveFocus()
    await user.keyboard("{Enter}")

    expect(screen.getByTestId("provider-rollup-summary-anthropic")).toHaveAttribute("aria-expanded", "true")
  })

  it("toggles with keyboard Space without moving focus off the provider", async () => {
    const user = userEvent.setup()
    renderConstellation()
    const summary = screen.getByTestId("provider-rollup-summary-anthropic")

    summary.focus()
    await user.keyboard(" ")

    expect(summary).toHaveFocus()
    expect(summary).toHaveAttribute("aria-expanded", "true")
  })

  it("does not expand the provider when the nested status badge is clicked", async () => {
    const user = userEvent.setup()
    renderConstellation()

    await user.click(
      within(screen.getByTestId("provider-rollup-status-slot-anthropic")).getByTestId("provider-status-badge"),
    )

    expect(screen.getByTestId("provider-rollup-summary-anthropic")).toHaveAttribute("aria-expanded", "false")
  })
})

describe("provider constellation accessibility", () => {
  it("exposes each provider summary as a named button", () => {
    renderConstellation()

    expect(screen.getByRole("button", { name: /Anthropic: 2 models, 1\.02M tokens, \$7\.55, 16 requests/ })).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /OpenRouter: 1 model, 500\.0K tokens, \$1\.25, 6 requests/ })).toBeInTheDocument()
  })

  it("keeps aria-expanded synchronized with click state", async () => {
    const user = userEvent.setup()
    renderConstellation()
    const summary = screen.getByTestId("provider-rollup-summary-deepseek")

    expect(summary).toHaveAttribute("aria-expanded", "false")
    await user.click(summary)
    expect(summary).toHaveAttribute("aria-expanded", "true")
  })

  it("status-badge aria-label includes concrete balance and rate-limit numbers", () => {
    renderConstellation()
    const badge = within(screen.getByTestId("provider-rollup-status-slot-anthropic")).getByTestId("provider-status-badge")

    expect(badge).toHaveAttribute("aria-label", expect.stringContaining("$80.00"))
    expect(badge).toHaveAttribute("aria-label", expect.stringContaining("$100.00"))
    expect(badge).toHaveAttribute("aria-label", expect.stringContaining("900 req remaining"))
  })

  it("expanded detail values expose balance, rate-limit, and sync labels", async () => {
    const user = userEvent.setup()
    renderConstellation()

    await user.click(screen.getByTestId("provider-rollup-summary-anthropic"))

    expect(screen.getByTestId("provider-card-expansion-balance-value")).toHaveAttribute("aria-label", "Balance: $80.00 / $100.00")
    expect(screen.getByTestId("provider-card-expansion-rate-limit-value")).toHaveAttribute(
      "aria-label",
      "Rate-limit: 900 req remaining / 199,876 tokens remaining (reset in 42s)",
    )
    expect(screen.getByTestId("provider-card-expansion-last-synced-value")).toHaveAttribute("aria-label", "Last synced: 2:05 ago")
  })
})

describe("provider constellation responsive layout contract", () => {
  it("uses a two-row summary header so identity and metrics can wrap independently", () => {
    renderConstellation()
    const summary = screen.getByTestId("provider-rollup-summary-anthropic")

    expect(summary).toHaveClass("flex", "flex-col", "gap-1.5")
    expect(summary.querySelectorAll("div")).toHaveLength(2)
  })

  it("allows the metrics strip to wrap under narrow widths", () => {
    renderConstellation()
    const summary = screen.getByTestId("provider-rollup-summary-anthropic")
    const metrics = summary.querySelectorAll("div")[1]

    expect(metrics).toHaveClass("flex-wrap")
    expect(metrics).toHaveClass("gap-y-1")
  })

  it("keeps long provider labels readable instead of truncating them", () => {
    renderConstellation([
      group("verylong", "VeryLongProviderNameWithoutSpacesForWrapChecks", "#737373", [
        row("verylong-model", { totalTokens: 100, requestCount: 1 }),
      ]),
    ])
    const label = screen.getByTestId("provider-rollup-label-verylong")

    expect(label).toHaveTextContent("VeryLongProviderNameWithoutSpacesForWrapChecks")
    expect(label).toHaveClass("break-words")
    expect(label).not.toHaveClass("truncate")
  })
})
