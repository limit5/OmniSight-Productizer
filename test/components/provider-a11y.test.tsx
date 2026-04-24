/**
 * Z.4 (#293) checkbox 6 — provider-component accessibility lockdown.
 *
 * The spec (TODO.md Z.4 row):
 *   "無障礙：`aria-label` 包含 balance 具體數字（不要只有顏色 + icon）。"
 *   — accessibility: `aria-label` includes concrete balance numbers,
 *   not just colour + icon.
 *
 * Checkbox 1 (ProviderStatusBadge) and checkbox 2 (ProviderCardExpansion)
 * already carry per-component aria-label coverage. This file pulls those
 * contracts up into an explicit accessibility-first suite that:
 *
 *   1. Sweeps the provider × tier matrix so drift in any single cell
 *      surfaces with a clear, accessibility-phrased failure message.
 *   2. Locks anti-regression invariants: aria-label MUST contain a
 *      currency-prefixed dollar amount (`$` / `¥`) whenever
 *      `balanceRemaining` is a finite number, AND MUST contain the
 *      integer req / tokens counts whenever those are finite. It MUST
 *      NOT collapse to only the tier word ("GREEN" / "YELLOW" / "RED")
 *      or only the provider name.
 *   3. Covers the edge case of a gray-no-data tier that still has a
 *      concrete balance (backend stale-cache path) — the aria-label
 *      must still expose the number so screen-reader users aren't told
 *      "no data" while sighted users see "$X.XX" on screen.
 *
 * This is the final Z.4 a11y gate: any future change to describeProviderStatus
 * or the expansion row aria-labels that drops a concrete number will
 * fail here, and the assertion message will point the reader at the
 * spec line above.
 */

import { describe, expect, it } from "vitest"
import { render } from "@testing-library/react"

import {
  ProviderStatusBadge,
  computeProviderStatus,
  describeProviderStatus,
  type ProviderStatusBadgeProps,
} from "@/components/omnisight/provider-status-badge"
import {
  ProviderCardExpansion,
  type ProviderCardExpansionProps,
} from "@/components/omnisight/provider-card-expansion"

// The nine providers the backend `/runtime/providers/balance` endpoint
// recognises (see backend/routers/llm_balance.py::_VALID_PROVIDER_NAMES).
// Locked so an accidental rename in either direction breaks the matrix
// sweep instead of silently skipping a provider.
const PROVIDER_MATRIX = [
  "anthropic",
  "openai",
  "google",
  "xai",
  "groq",
  "together",
  "deepseek",
  "openrouter",
  "ollama",
] as const

type TierCase = {
  name: "green" | "yellow" | "red"
  props: Omit<ProviderStatusBadgeProps, "provider">
  /** Substrings the aria-label MUST include (concrete numbers). */
  mustContain: string[]
}

const TIER_CASES: TierCase[] = [
  {
    name: "green",
    props: {
      status: "ok",
      balanceRemaining: 80,
      grantedTotal: 100,
      currency: "USD",
      rateLimitRemainingRequests: 900,
      rateLimitLimitRequests: 1000,
    },
    mustContain: ["$80.00", "$100.00", "900 req remaining"],
  },
  {
    name: "yellow",
    props: {
      status: "ok",
      balanceRemaining: 15,
      grantedTotal: 100,
      currency: "USD",
      rateLimitRemainingRequests: 20,
      rateLimitLimitRequests: 100,
    },
    mustContain: ["$15.00", "$100.00", "20 req remaining"],
  },
  {
    name: "red",
    props: {
      status: "ok",
      balanceRemaining: 2,
      grantedTotal: 100,
      currency: "USD",
      rateLimitRemainingRequests: 0,
      rateLimitLimitRequests: 100,
    },
    mustContain: ["$2.00", "$100.00", "0 req remaining"],
  },
]

function ariaLabel(el: HTMLElement | null): string {
  if (!el) return ""
  return el.getAttribute("aria-label") ?? ""
}

describe("Z.4 #6 aria-label MUST carry concrete balance numbers (not only colour + icon)", () => {
  // --- <ProviderStatusBadge> ---------------------------------------------

  describe("<ProviderStatusBadge> matrix — every provider × every tier", () => {
    for (const provider of PROVIDER_MATRIX) {
      for (const tier of TIER_CASES) {
        it(`${provider} @ ${tier.name}: aria-label contains every concrete number`, () => {
          const { getByTestId } = render(
            <ProviderStatusBadge provider={provider} {...tier.props} />,
          )
          const badge = getByTestId("provider-status-badge")
          const label = ariaLabel(badge)
          for (const snippet of tier.mustContain) {
            expect(label, `${provider}/${tier.name} aria-label="${label}" missing "${snippet}"`).toContain(snippet)
          }
          // Anti-regression: aria-label MUST NOT be just the tier or the
          // provider string alone. If it is, someone stripped the numbers.
          expect(label.length).toBeGreaterThan(tier.name.length + provider.length + 5)
          expect(label).toContain(provider)
        })
      }
    }
  })

  describe("<ProviderStatusBadge> currency + edge-case aria-label coverage", () => {
    it("¥ currency surfaces the number, not just 'CNY'", () => {
      const { getByTestId } = render(
        <ProviderStatusBadge
          provider="deepseek"
          status="ok"
          balanceRemaining={50}
          grantedTotal={100}
          currency="CNY"
          rateLimitRemainingRequests={999}
          rateLimitLimitRequests={1000}
        />,
      )
      const label = ariaLabel(getByTestId("provider-status-badge"))
      expect(label).toContain("¥50.00")
      expect(label).toContain("¥100.00")
      expect(label).toContain("999 req remaining")
    })

    it("sub-dollar balance uses 3 decimals (`$0.500`) — still a concrete number", () => {
      const { getByTestId } = render(
        <ProviderStatusBadge
          provider="openrouter"
          status="ok"
          balanceRemaining={0.5}
          grantedTotal={10}
          currency="USD"
        />,
      )
      const label = ariaLabel(getByTestId("provider-status-badge"))
      expect(label).toContain("$0.500")
      expect(label).toContain("$10.00")
    })

    it("balance absolute without grantedTotal still surfaces the $ amount", () => {
      // OpenRouter-style remainder-only shape: we can't compute a % but
      // MUST still expose the absolute number. The aria-label must not
      // degrade to tier-only text.
      const { getByTestId } = render(
        <ProviderStatusBadge
          provider="openrouter"
          status="ok"
          balanceRemaining={37.42}
          grantedTotal={null}
          currency="USD"
        />,
      )
      const label = ariaLabel(getByTestId("provider-status-badge"))
      expect(label).toContain("$37.42")
      expect(label).not.toMatch(/^\s*openrouter:\s*(green|yellow|red|gray)\.?\s*$/i)
    })

    it("rate-limit-only provider (Anthropic) still reports counts in aria-label", () => {
      const { getByTestId } = render(
        <ProviderStatusBadge
          provider="anthropic"
          status="ok"
          rateLimitRemainingRequests={47}
          rateLimitLimitRequests={50}
          rateLimitRemainingTokens={199876}
          rateLimitLimitTokens={200000}
        />,
      )
      const label = ariaLabel(getByTestId("provider-status-badge"))
      expect(label).toContain("47 req remaining")
      expect(label).toContain("199876 tokens remaining")
    })

    it("gray no-data + concrete balance still surfaces the $ number (anti-degrade)", () => {
      // Balance absolute without grantedTotal + no rate-limit signal →
      // compute can't tier (no percentage, no saturation) and lands in
      // gray "no data". The checkbox-6 enhancement requires that the
      // aria-label STILL carry the concrete $ amount the backend sent,
      // rather than lying with "no balance or rate-limit data available".
      const props: ProviderStatusBadgeProps = {
        provider: "deepseek",
        status: "ok",
        balanceRemaining: 12.34,
        grantedTotal: null,
        currency: "USD",
      }
      const result = computeProviderStatus(props)
      expect(result.tier).toBe("gray")
      const label = describeProviderStatus(props, result)
      expect(label).toContain("$12.34")
      expect(label).not.toBe("deepseek: no balance or rate-limit data available")
    })

    it("gray no-data with ONLY rate-limit counters (no balance) still reports counts", () => {
      // Anthropic-style: no balance API, but rate-limit header arrived
      // without a limit counter → reqPct / tokPct null, but the raw
      // remaining count is still meaningful to the operator. Verify it
      // surfaces even when compute lands in gray.
      const props: ProviderStatusBadgeProps = {
        provider: "anthropic",
        status: "ok",
        rateLimitRemainingRequests: 42,
      }
      const result = computeProviderStatus(props)
      expect(result.tier).toBe("gray")
      const label = describeProviderStatus(props, result)
      expect(label).toContain("42 req remaining")
      expect(label).not.toBe("anthropic: no balance or rate-limit data available")
    })

    it("gray no-data with truly empty props says 'no data' (legit fallback, not regression)", () => {
      // Complement to the two tests above: if there's NO concrete data
      // at all, the aria-label IS legitimately "no data" — that isn't
      // a regression, it's honest reporting.
      const props: ProviderStatusBadgeProps = {
        provider: "deepseek",
        status: "ok",
      }
      const result = computeProviderStatus(props)
      expect(result.tier).toBe("gray")
      const label = describeProviderStatus(props, result)
      expect(label).toBe("deepseek: no balance or rate-limit data available")
    })

    it("unsupported + stale-cache balance still surfaces $ number in aria-label", () => {
      const props: ProviderStatusBadgeProps = {
        provider: "openai",
        status: "unsupported",
        reason: "OpenAI /v1/usage requires a session cookie, not an API key.",
        balanceRemaining: 5,
        grantedTotal: 100,
        currency: "USD",
      }
      const result = computeProviderStatus(props)
      const label = describeProviderStatus(props, result)
      expect(label).toContain("OpenAI /v1/usage requires a session cookie")
      expect(label).toContain("$5.00")
      expect(label).toContain("$100.00")
    })

    it("loading (gray, no data) aria-label is explicitly 'loading…' not empty", () => {
      const { getByTestId } = render(
        <ProviderStatusBadge provider="groq" loading={true} />,
      )
      const label = ariaLabel(getByTestId("provider-status-badge"))
      expect(label).toContain("groq")
      expect(label.toLowerCase()).toContain("loading")
    })

    it("aria-label NEVER equals only the tier colour word", () => {
      // Negative assertion — defend against a refactor that tries to be
      // clever and uses the tier letter alone.
      for (const tier of TIER_CASES) {
        const props = { provider: "deepseek", ...tier.props }
        const result = computeProviderStatus(props)
        const label = describeProviderStatus(props, result)
        expect(label.trim().toUpperCase()).not.toBe(tier.name.toUpperCase())
        expect(label.trim().toUpperCase()).not.toBe(`DEEPSEEK: ${tier.name.toUpperCase()}.`)
      }
    })

    it("every tier aria-label includes the currency prefix character", () => {
      for (const tier of TIER_CASES) {
        const props = {
          provider: "deepseek",
          ...tier.props,
        }
        const result = computeProviderStatus(props)
        const label = describeProviderStatus(props, result)
        expect(label).toMatch(/[$¥]/)
      }
    })
  })

  // --- <ProviderCardExpansion> -------------------------------------------

  describe("<ProviderCardExpansion> row aria-labels carry concrete numbers", () => {
    const baseExpansionProps: ProviderCardExpansionProps = {
      provider: "deepseek",
      status: "ok",
      balanceRemaining: 42,
      grantedTotal: 100,
      currency: "USD",
      rateLimitRemainingRequests: 500,
      rateLimitLimitRequests: 1000,
      rateLimitRemainingTokens: 50000,
      resetAtTs: 1_700_000_100,
      lastRefreshedAt: 1_700_000_000,
      nowTs: 1_700_000_060,
    }

    it("balance row aria-label contains the concrete $ amount, not just 'Balance:'", () => {
      const { getByTestId } = render(<ProviderCardExpansion {...baseExpansionProps} />)
      const row = getByTestId("provider-card-expansion-balance-value")
      const label = ariaLabel(row)
      expect(label).toContain("$42.00")
      expect(label).toContain("$100.00")
      expect(label).not.toBe("Balance:")
    })

    it("rate-limit row aria-label contains concrete req / tokens counts", () => {
      const { getByTestId } = render(<ProviderCardExpansion {...baseExpansionProps} />)
      const row = getByTestId("provider-card-expansion-rate-limit-value")
      const label = ariaLabel(row)
      expect(label).toContain("500 req remaining")
      expect(label).toContain("50,000 tokens remaining")
      expect(label).not.toBe("Rate-limit:")
    })

    it("missing balance row aria-label says 'not reported' (not empty, not just 'Balance:')", () => {
      const { getByTestId } = render(
        <ProviderCardExpansion
          {...baseExpansionProps}
          balanceRemaining={null}
          grantedTotal={null}
        />,
      )
      const row = getByTestId("provider-card-expansion-balance-value")
      const label = ariaLabel(row)
      expect(label).toBe("Balance: not reported")
    })

    it("CNY currency row surfaces ¥ + concrete amount", () => {
      const { getByTestId } = render(
        <ProviderCardExpansion {...baseExpansionProps} currency="CNY" balanceRemaining={88} grantedTotal={200} />,
      )
      const row = getByTestId("provider-card-expansion-balance-value")
      const label = ariaLabel(row)
      expect(label).toContain("¥88.00")
      expect(label).toContain("¥200.00")
    })

    it("error envelope still surfaces concrete balance + rate-limit numbers in row aria-labels", () => {
      // Stale cache on an error envelope — numbers may be behind but
      // the aria-label must still carry them, not just the tier colour.
      const { getByTestId } = render(
        <ProviderCardExpansion
          {...baseExpansionProps}
          status="error"
          errorMessage="deepseek returned HTTP 503"
        />,
      )
      expect(ariaLabel(getByTestId("provider-card-expansion-balance-value"))).toContain("$42.00")
      expect(ariaLabel(getByTestId("provider-card-expansion-rate-limit-value"))).toContain("500 req remaining")
    })

    it("last-synced row aria-label always carries concrete timing ('Xs ago' / 'never')", () => {
      const { getByTestId, rerender } = render(<ProviderCardExpansion {...baseExpansionProps} />)
      expect(ariaLabel(getByTestId("provider-card-expansion-last-synced-value"))).toBe(
        "Last synced: 1:00 ago",
      )
      rerender(
        <ProviderCardExpansion
          {...baseExpansionProps}
          lastRefreshedAt={null}
        />,
      )
      expect(ariaLabel(getByTestId("provider-card-expansion-last-synced-value"))).toBe(
        "Last synced: never",
      )
    })
  })

  // --- cross-component integration lock ----------------------------------

  describe("badge + expansion share the same concrete-number contract", () => {
    it("both components report the same balance number when given identical data", () => {
      const shared = {
        provider: "deepseek",
        status: "ok" as const,
        balanceRemaining: 73.25,
        grantedTotal: 100,
        currency: "USD",
      }
      const { getByTestId: getBadge, unmount: unmountBadge } = render(
        <ProviderStatusBadge {...shared} />,
      )
      const badgeLabel = ariaLabel(getBadge("provider-status-badge"))
      unmountBadge()
      const { getByTestId: getExp } = render(
        <ProviderCardExpansion {...shared} nowTs={1_700_000_000} />,
      )
      const expLabel = ariaLabel(getExp("provider-card-expansion-balance-value"))
      expect(badgeLabel).toContain("$73.25")
      expect(expLabel).toContain("$73.25")
    })
  })
})
