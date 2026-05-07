/**
 * OP-44 / MP.W4.8 - provider constellation interaction, a11y,
 * and responsive contract tests.
 *
 * This suite treats the provider roll-up, status badge, and expansion
 * slot as the operator-facing "constellation" surface. The component
 * pieces are already tested individually; these tests lock their
 * integration behavior without changing production code.
 */

import { describe, expect, it } from "vitest"
import { fireEvent, render } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

import {
  ProviderRollup,
  groupByProvider,
  openRouterAwareResolver,
  type ProviderGroup,
  type ProviderRollupRow,
} from "@/components/omnisight/provider-rollup"
import { ProviderStatusBadge } from "@/components/omnisight/provider-status-badge"
import { ProviderCardExpansion } from "@/components/omnisight/provider-card-expansion"

interface Row extends ProviderRollupRow {
  latencyMs: number
}

type BadgeState = React.ComponentProps<typeof ProviderStatusBadge>
type ExpansionState = React.ComponentProps<typeof ProviderCardExpansion>

const NOW = 1_700_000_000

const BADGE_STATE_BY_PROVIDER: Record<string, Omit<BadgeState, "provider">> = {
  anthropic: {
    status: "ok",
    rateLimitRemainingRequests: 420,
    rateLimitLimitRequests: 500,
    rateLimitRemainingTokens: 180_000,
    rateLimitLimitTokens: 200_000,
  },
  openai: {
    status: "ok",
    balanceRemaining: 4,
    grantedTotal: 100,
    currency: "USD",
    rateLimitRemainingRequests: 0,
    rateLimitLimitRequests: 100,
  },
  google: {
    status: "unsupported",
    reason: "Google usage is checked in AI Studio.",
    balanceRemaining: 25,
    grantedTotal: 100,
    currency: "USD",
  },
  openrouter: {
    status: "ok",
    balanceRemaining: 37.42,
    grantedTotal: null,
    currency: "USD",
  },
}

const EXPANSION_STATE_BY_PROVIDER: Record<string, Omit<ExpansionState, "provider">> = {
  anthropic: {
    status: "ok",
    rateLimitRemainingRequests: 420,
    rateLimitRemainingTokens: 180_000,
    resetAtTs: NOW + 45,
    lastRefreshedAt: NOW - 65,
    nowTs: NOW,
  },
  openai: {
    status: "error",
    balanceRemaining: 4,
    grantedTotal: 100,
    currency: "USD",
    rateLimitRemainingRequests: 0,
    resetAtTs: NOW - 1,
    lastRefreshedAt: NOW - 600,
    errorMessage: "OpenAI quota refresh failed",
    nowTs: NOW,
  },
  google: {
    status: "unsupported",
    reason: "Google usage is checked in AI Studio.",
    dashboardUrl: "https://aistudio.google.com/app/apikey",
    nowTs: NOW,
  },
  openrouter: {
    status: "ok",
    balanceRemaining: 37.42,
    grantedTotal: null,
    currency: "USD",
    rateLimitRemainingRequests: 25,
    retryAfterS: 90,
    lastRefreshedAt: NOW - 5,
    nowTs: NOW,
  },
}

function row(model: string, overrides: Partial<Row> = {}): Row {
  return {
    model,
    inputTokens: 0,
    outputTokens: 0,
    totalTokens: 0,
    cost: 0,
    requestCount: 0,
    latencyMs: 0,
    ...overrides,
  }
}

function fakeResolver(model: string) {
  if (model.startsWith("anthropic/")) {
    return { provider: "Anthropic", color: "#f59e0b" }
  }
  if (model.startsWith("claude")) return { provider: "Anthropic", color: "#f59e0b" }
  if (model.startsWith("gpt")) return { provider: "OpenAI", color: "#10b981" }
  if (model.startsWith("gemini")) return { provider: "Google", color: "#3b82f6" }
  return { provider: "", color: "" }
}

function buildRows(): Row[] {
  return [
    row("anthropic/claude-sonnet-4", {
      totalTokens: 900_000,
      inputTokens: 700_000,
      outputTokens: 200_000,
      cost: 6.25,
      requestCount: 18,
      latencyMs: 640,
    }),
    row("claude-opus-4-7", {
      totalTokens: 300_000,
      inputTokens: 220_000,
      outputTokens: 80_000,
      cost: 3.75,
      requestCount: 7,
      latencyMs: 820,
    }),
    row("gpt-4.1", {
      totalTokens: 200_000,
      inputTokens: 180_000,
      outputTokens: 20_000,
      cost: 1.5,
      requestCount: 12,
      latencyMs: 430,
    }),
    row("gemini-1.5-pro", {
      totalTokens: 100_000,
      inputTokens: 70_000,
      outputTokens: 30_000,
      cost: 0.75,
      requestCount: 5,
      latencyMs: 510,
    }),
  ]
}

function buildGroups(): ProviderGroup<Row>[] {
  return groupByProvider(buildRows(), openRouterAwareResolver(fakeResolver))
}

function grandTotal(groups: ProviderGroup<Row>[]): number {
  return groups.reduce((sum, group) => sum + group.totals.totalTokens, 0)
}

function renderConstellation(options: {
  groups?: ProviderGroup<Row>[]
  defaultExpanded?: boolean
  className?: string
} = {}) {
  const groups = options.groups ?? buildGroups()
  return render(
    <ProviderRollup
      groups={groups}
      grandTotalTokens={grandTotal(groups)}
      defaultExpanded={options.defaultExpanded}
      className={options.className}
      renderStatusBadge={(providerKey) => (
        <ProviderStatusBadge
          provider={providerKey}
          {...BADGE_STATE_BY_PROVIDER[providerKey]}
        />
      )}
      renderExpansion={(providerKey) => (
        <ProviderCardExpansion
          provider={providerKey}
          {...EXPANSION_STATE_BY_PROVIDER[providerKey]}
        />
      )}
      renderRow={(usage) => (
        <article
          data-testid={`model-row-${usage.model}`}
          aria-label={`${usage.model}: ${usage.totalTokens} tokens, ${usage.requestCount} requests`}
        >
          {usage.model} - {usage.latencyMs} ms
        </article>
      )}
    />,
  )
}

describe("<ProviderConstellation /> integration surface", () => {
  describe("interaction", () => {
    it("starts collapsed with provider summaries visible and model rows hidden", () => {
      const { getByTestId, queryByTestId } = renderConstellation()

      expect(getByTestId("provider-rollup-summary-openrouter")).toBeTruthy()
      expect(getByTestId("provider-rollup-summary-anthropic")).toBeTruthy()
      expect(
        queryByTestId("model-row-anthropic/claude-sonnet-4"),
      ).toBeNull()
      expect(queryByTestId("provider-rollup-body-openrouter")).toBeNull()
    })

    it("expands only the clicked provider group", () => {
      const { getByTestId, queryByTestId } = renderConstellation()

      fireEvent.click(getByTestId("provider-rollup-summary-openrouter"))

      expect(
        getByTestId("provider-rollup-group-openrouter").getAttribute(
          "data-expanded",
        ),
      ).toBe("true")
      expect(getByTestId("model-row-anthropic/claude-sonnet-4")).toBeTruthy()
      expect(queryByTestId("model-row-claude-opus-4-7")).toBeNull()
    })

    it("collapses an expanded provider group on the second click", () => {
      const { getByTestId, queryByTestId } = renderConstellation()
      const summary = getByTestId("provider-rollup-summary-openrouter")

      fireEvent.click(summary)
      fireEvent.click(summary)

      expect(queryByTestId("provider-rollup-body-openrouter")).toBeNull()
      expect(
        getByTestId("provider-rollup-group-openrouter").getAttribute(
          "data-expanded",
        ),
      ).toBe("false")
    })

    it("supports keyboard activation through the native summary button", async () => {
      const user = userEvent.setup()
      const { getByTestId } = renderConstellation()
      const summary = getByTestId("provider-rollup-summary-openrouter")

      await user.tab()
      expect(summary).toHaveFocus()
      await user.keyboard("{Enter}")

      expect(summary.getAttribute("aria-expanded")).toBe("true")
      expect(getByTestId("model-row-anthropic/claude-sonnet-4")).toBeTruthy()
    })

    it("keeps status badge clicks from toggling the summary group", () => {
      const { getAllByTestId, getByTestId, queryByTestId } =
        renderConstellation()

      fireEvent.click(getAllByTestId("provider-status-badge")[0])

      expect(
        getByTestId("provider-rollup-summary-openrouter").getAttribute(
          "aria-expanded",
        ),
      ).toBe("false")
      expect(queryByTestId("provider-rollup-body-openrouter")).toBeNull()
    })
  })

  describe("accessibility", () => {
    it("summary button exposes aggregate totals in its aria-label", () => {
      const { getByTestId } = renderConstellation()
      const label =
        getByTestId("provider-rollup-summary-openrouter").getAttribute(
          "aria-label",
        ) ?? ""

      expect(label).toContain("OpenRouter")
      expect(label).toContain("1 model")
      expect(label).toContain("900.0K tokens")
      expect(label).toContain("$6.25")
      expect(label).toContain("18 requests")
    })

    it("summary aria-expanded stays in sync with the rendered panel", () => {
      const { getByTestId, queryByTestId } = renderConstellation()
      const summary = getByTestId("provider-rollup-summary-openai")

      expect(summary.getAttribute("aria-expanded")).toBe("false")
      expect(queryByTestId("provider-rollup-body-openai")).toBeNull()

      fireEvent.click(summary)

      expect(summary.getAttribute("aria-expanded")).toBe("true")
      expect(getByTestId("provider-rollup-body-openai")).toBeTruthy()
    })

    it("status badges surface concrete balance and rate-limit numbers", () => {
      const { getByTestId } = renderConstellation()
      const openaiBadge = getByTestId(
        "provider-rollup-status-slot-openai",
      ).querySelector('[data-testid="provider-status-badge"]')
      const label = openaiBadge?.getAttribute("aria-label") ?? ""

      expect(label).toContain("$4.00")
      expect(label).toContain("$100.00")
      expect(label).toContain("0 req remaining")
      expect(label).toContain("reason: balance 4.0% < 5%; rate-limit saturated.")
    })

    it("expanded detail values carry accessible row labels", () => {
      const { getByTestId } = renderConstellation()

      fireEvent.click(getByTestId("provider-rollup-summary-openrouter"))

      expect(
        getByTestId("provider-card-expansion-balance-value").getAttribute(
          "aria-label",
        ),
      ).toBe("Balance: $37.42")
      expect(
        getByTestId("provider-card-expansion-rate-limit-value").getAttribute(
          "aria-label",
        ),
      ).toBe("Rate-limit: 25 req remaining (retry after ~2m)")
      expect(
        getByTestId("provider-card-expansion-last-synced-value").getAttribute(
          "aria-label",
        ),
      ).toBe("Last synced: 5s ago")
    })

    it("unsupported providers expose advisory copy and a dashboard link", () => {
      const { getByTestId } = renderConstellation()

      fireEvent.click(getByTestId("provider-rollup-summary-google"))

      expect(
        getByTestId(
          "provider-card-expansion-unsupported-message",
        ).textContent,
      ).toBe("Google usage is checked in AI Studio.")
      const link = getByTestId(
        "provider-card-expansion-dashboard-link",
      ) as HTMLAnchorElement
      expect(link.href).toBe("https://aistudio.google.com/app/apikey")
      expect(link.getAttribute("rel")).toBe("noopener noreferrer")
    })
  })

  describe("responsive layout contracts", () => {
    it("summary button uses the two-row wrapping layout", () => {
      const { getByTestId } = renderConstellation()
      const summary = getByTestId("provider-rollup-summary-openrouter")

      expect(summary.className).toContain("flex")
      expect(summary.className).toContain("flex-col")
      expect(summary.className).toContain("gap-1.5")
      expect(summary.className).not.toContain("truncate")
    })

    it("metrics row wraps without clipping long value clusters", () => {
      const { getByTestId } = renderConstellation()
      const metricsRow = getByTestId(
        "provider-rollup-model-count-openrouter",
      ).parentElement

      expect(metricsRow?.className).toContain("flex-wrap")
      expect(metricsRow?.className).toContain("gap-x-3")
      expect(metricsRow?.className).toContain("gap-y-1")
    })

    it("right-side totals preserve tabular non-wrapping numeric labels", () => {
      const { getByTestId } = renderConstellation()

      expect(
        getByTestId("provider-rollup-tokens-openrouter").className,
      ).toContain("whitespace-nowrap")
      expect(
        getByTestId("provider-rollup-cost-openrouter").className,
      ).toContain("tabular-nums")
      expect(
        getByTestId("provider-rollup-pct-openrouter").textContent,
      ).toBe("60.0%")
    })

    it("long provider labels are allowed to break instead of truncating", () => {
      const groups: ProviderGroup<Row>[] = [
        {
          providerKey: "very-long-provider-name",
          providerLabel: "VeryLongProviderNameWithoutSpaces",
          color: "#737373",
          totals: {
            inputTokens: 1,
            outputTokens: 1,
            totalTokens: 2,
            cost: 0.002,
            requestCount: 1,
          },
          rows: [row("custom-model", { totalTokens: 2, cost: 0.002 })],
        },
      ]
      const { getByTestId } = renderConstellation({ groups })

      expect(
        getByTestId("provider-rollup-label-very-long-provider-name").className,
      ).toContain("break-words")
      expect(
        getByTestId("provider-rollup-label-very-long-provider-name").className,
      ).not.toContain("truncate")
    })

    it("expanded panels indent below the summary without changing the list shell", () => {
      const { getAllByTestId, getByTestId } = renderConstellation({
        defaultExpanded: true,
        className: "max-w-[320px]",
      })

      expect(getByTestId("provider-rollup").className).toContain(
        "max-w-[320px]",
      )
      expect(getByTestId("provider-rollup-body-openrouter").className).toContain(
        "ml-6",
      )
      expect(getAllByTestId("provider-card-expansion")[0].className).toContain(
        "flex-col",
      )
    })
  })
})
