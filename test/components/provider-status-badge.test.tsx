/**
 * Z.4 (#293) checkbox 1 — <ProviderStatusBadge> contract tests.
 *
 * Locks the four-tier threshold logic per the spec:
 *   green  — balance > 20% AND rate-limit remaining > 10%
 *   yellow — balance < 20% OR rate-limit remaining ≤ 20% (≥ 80% used)
 *   red    — balance < 5% OR rate-limit saturated (remaining == 0)
 *   gray   — status === "unsupported" / no data / loading
 *
 * We test the pure `computeProviderStatus` helper for the decision tree
 * (fast, no DOM) and the React component for render output + a11y
 * (tier → colour + icon, aria-label carries concrete numbers, tooltip
 * explains unsupported).
 */

import { describe, expect, it } from "vitest"
import { render } from "@testing-library/react"

import {
  ProviderStatusBadge,
  computeProviderStatus,
  describeProviderStatus,
  type ProviderStatusTier,
} from "@/components/omnisight/provider-status-badge"

describe("computeProviderStatus()", () => {
  describe("unsupported envelope → gray", () => {
    it("returns gray with no numeric fields when status is unsupported", () => {
      const res = computeProviderStatus({ status: "unsupported" })
      expect(res.tier).toBe<ProviderStatusTier>("gray")
      expect(res.reasons).toEqual(["unsupported"])
      expect(res.balancePct).toBeNull()
      expect(res.rateLimitRemainingPct).toBeNull()
      expect(res.rateLimitSaturated).toBe(false)
    })

    it("returns gray regardless of balance / rate-limit data when unsupported", () => {
      const res = computeProviderStatus({
        status: "unsupported",
        balanceRemaining: 100,
        grantedTotal: 200,
        rateLimitRemainingRequests: 0,
      })
      expect(res.tier).toBe("gray")
    })
  })

  describe("red tier", () => {
    it("fires red when balance < 5% (4.9%)", () => {
      const res = computeProviderStatus({
        status: "ok",
        balanceRemaining: 4.9,
        grantedTotal: 100,
      })
      expect(res.tier).toBe("red")
      expect(res.reasons.some(r => r.includes("balance"))).toBe(true)
    })

    it("fires red when remaining_requests is exactly 0 (saturated)", () => {
      const res = computeProviderStatus({
        status: "ok",
        balanceRemaining: 50,
        grantedTotal: 100,
        rateLimitRemainingRequests: 0,
        rateLimitLimitRequests: 100,
      })
      expect(res.tier).toBe("red")
      expect(res.rateLimitSaturated).toBe(true)
      expect(res.reasons).toContain("rate-limit saturated")
    })

    it("fires red when remaining_tokens is exactly 0 even if requests are fine", () => {
      const res = computeProviderStatus({
        status: "ok",
        balanceRemaining: 50,
        grantedTotal: 100,
        rateLimitRemainingRequests: 999,
        rateLimitLimitRequests: 1000,
        rateLimitRemainingTokens: 0,
        rateLimitLimitTokens: 1_000_000,
      })
      expect(res.tier).toBe("red")
      expect(res.rateLimitSaturated).toBe(true)
    })

    it("red supersedes yellow — balance 2% + rate-limit 50% used stays red", () => {
      const res = computeProviderStatus({
        status: "ok",
        balanceRemaining: 2,
        grantedTotal: 100,
        rateLimitRemainingRequests: 50,
        rateLimitLimitRequests: 100,
      })
      expect(res.tier).toBe("red")
    })
  })

  describe("yellow tier", () => {
    it("fires yellow when balance is strictly between 5% and 20% (15%)", () => {
      const res = computeProviderStatus({
        status: "ok",
        balanceRemaining: 15,
        grantedTotal: 100,
      })
      expect(res.tier).toBe("yellow")
    })

    it("fires yellow at exactly 80% rate-limit usage (20% remaining)", () => {
      const res = computeProviderStatus({
        status: "ok",
        balanceRemaining: 80,
        grantedTotal: 100,
        rateLimitRemainingRequests: 20,
        rateLimitLimitRequests: 100,
      })
      expect(res.tier).toBe("yellow")
      expect(res.reasons.some(r => r.includes("rate-limit"))).toBe(true)
    })

    it("yellow for the (10, 20]% rate-limit remaining band even when balance is healthy", () => {
      // This band falls out of strict green (> 10%) but equals the 20%
      // yellow edge. Spec: 80% used fires yellow → fires at 20% remaining.
      const res = computeProviderStatus({
        status: "ok",
        balanceRemaining: 90,
        grantedTotal: 100,
        rateLimitRemainingRequests: 15,
        rateLimitLimitRequests: 100,
      })
      expect(res.tier).toBe("yellow")
    })

    it("picks the tighter of the two rate-limit counters", () => {
      // Requests look healthy (50%), tokens are tight (10% → yellow).
      const res = computeProviderStatus({
        status: "ok",
        balanceRemaining: 90,
        grantedTotal: 100,
        rateLimitRemainingRequests: 500,
        rateLimitLimitRequests: 1000,
        rateLimitRemainingTokens: 100_000,
        rateLimitLimitTokens: 1_000_000,
      })
      expect(res.tier).toBe("yellow")
      expect(res.rateLimitRemainingPct).toBeCloseTo(10, 3)
    })
  })

  describe("green tier", () => {
    it("fires green at balance > 20% AND rate-limit remaining > 10%", () => {
      const res = computeProviderStatus({
        status: "ok",
        balanceRemaining: 80,
        grantedTotal: 100,
        rateLimitRemainingRequests: 80,
        rateLimitLimitRequests: 100,
      })
      expect(res.tier).toBe("green")
      expect(res.balancePct).toBeCloseTo(80, 3)
      expect(res.rateLimitRemainingPct).toBeCloseTo(80, 3)
    })

    it("green when only balance signal is present and healthy", () => {
      // No rate-limit data → balance alone decides.
      const res = computeProviderStatus({
        status: "ok",
        balanceRemaining: 50,
        grantedTotal: 100,
      })
      expect(res.tier).toBe("green")
    })

    it("green when only rate-limit signal is present and healthy", () => {
      // No balance data → rate-limit alone decides.
      const res = computeProviderStatus({
        status: "ok",
        rateLimitRemainingRequests: 50,
        rateLimitLimitRequests: 100,
      })
      expect(res.tier).toBe("green")
    })

    it("balance exactly at 20% is not green (strict >)", () => {
      // Spec says "> 20%" so 20.0% isn't green. But it isn't < 20%
      // either, so it also isn't yellow-by-balance. With healthy
      // rate-limit this lands in the yellow fallthrough band.
      const res = computeProviderStatus({
        status: "ok",
        balanceRemaining: 20,
        grantedTotal: 100,
        rateLimitRemainingRequests: 80,
        rateLimitLimitRequests: 100,
      })
      expect(res.tier).toBe("yellow")
    })
  })

  describe("gray loading / no-data", () => {
    it("gray when loading=true", () => {
      const res = computeProviderStatus({
        status: "ok",
        loading: true,
      })
      expect(res.tier).toBe("gray")
      expect(res.reasons).toEqual(["loading"])
    })

    it("gray when no balance and no rate-limit signal available", () => {
      const res = computeProviderStatus({ status: "ok" })
      expect(res.tier).toBe("gray")
      expect(res.reasons).toEqual(["no data"])
    })
  })

  describe("balance pct edge cases", () => {
    it("returns null balancePct when grantedTotal is 0", () => {
      const res = computeProviderStatus({
        status: "ok",
        balanceRemaining: 5,
        grantedTotal: 0,
      })
      expect(res.balancePct).toBeNull()
    })

    it("returns null balancePct when grantedTotal is null", () => {
      const res = computeProviderStatus({
        status: "ok",
        balanceRemaining: 5,
        grantedTotal: null,
      })
      expect(res.balancePct).toBeNull()
    })
  })
})

describe("describeProviderStatus() — a11y label content", () => {
  it("includes the provider name + tier + numeric balance for a healthy card", () => {
    const props = {
      provider: "deepseek",
      status: "ok" as const,
      balanceRemaining: 80,
      grantedTotal: 100,
      currency: "USD",
      rateLimitRemainingRequests: 900,
      rateLimitLimitRequests: 1000,
    }
    const result = computeProviderStatus(props)
    const label = describeProviderStatus(props, result)
    expect(label).toContain("deepseek")
    expect(label).toContain("GREEN")
    expect(label).toContain("$80.00")
    expect(label).toContain("$100.00")
    expect(label).toContain("900 req remaining")
  })

  it("unsupported tooltip carries the explicit reason", () => {
    const props = {
      provider: "anthropic",
      status: "unsupported" as const,
      reason: "Anthropic does not expose a public balance API",
    }
    const result = computeProviderStatus(props)
    const label = describeProviderStatus(props, result)
    expect(label).toContain("anthropic")
    expect(label).toContain("Anthropic does not expose a public balance API")
  })

  it("uses ¥ symbol for CNY currency (DeepSeek CN region)", () => {
    const props = {
      provider: "deepseek",
      status: "ok" as const,
      balanceRemaining: 50,
      grantedTotal: 100,
      currency: "CNY",
    }
    const result = computeProviderStatus(props)
    const label = describeProviderStatus(props, result)
    expect(label).toContain("¥50.00")
  })

  it("includes reason string for non-green tiers", () => {
    const props = {
      provider: "deepseek",
      status: "ok" as const,
      balanceRemaining: 2,
      grantedTotal: 100,
    }
    const result = computeProviderStatus(props)
    const label = describeProviderStatus(props, result)
    expect(label).toContain("RED")
    expect(label.toLowerCase()).toContain("reason")
  })
})

describe("<ProviderStatusBadge> render output", () => {
  it("renders a role=status element with a data-tier attribute", () => {
    const { getByTestId } = render(
      <ProviderStatusBadge
        provider="deepseek"
        status="ok"
        balanceRemaining={80}
        grantedTotal={100}
      />,
    )
    const el = getByTestId("provider-status-badge")
    expect(el.getAttribute("role")).toBe("status")
    expect(el.getAttribute("data-tier")).toBe("green")
    expect(el.getAttribute("data-provider")).toBe("deepseek")
  })

  it("renders tier=gray + data-unsupported=true for unsupported", () => {
    const { getByTestId } = render(
      <ProviderStatusBadge
        provider="anthropic"
        status="unsupported"
        reason="No public balance API"
      />,
    )
    const el = getByTestId("provider-status-badge")
    expect(el.getAttribute("data-tier")).toBe("gray")
    expect(el.getAttribute("data-unsupported")).toBe("true")
    // Unsupported renders an "N/A" label instead of the dot.
    expect(getByTestId("provider-status-badge-unsupported-label")).toBeTruthy()
  })

  it("renders tier=red when balance < 5%", () => {
    const { getByTestId } = render(
      <ProviderStatusBadge
        provider="openrouter"
        status="ok"
        balanceRemaining={2}
        grantedTotal={100}
      />,
    )
    const el = getByTestId("provider-status-badge")
    expect(el.getAttribute("data-tier")).toBe("red")
  })

  it("renders tier=yellow when rate-limit is at 80% used", () => {
    const { getByTestId } = render(
      <ProviderStatusBadge
        provider="openai"
        status="ok"
        balanceRemaining={80}
        grantedTotal={100}
        rateLimitRemainingRequests={20}
        rateLimitLimitRequests={100}
      />,
    )
    const el = getByTestId("provider-status-badge")
    expect(el.getAttribute("data-tier")).toBe("yellow")
  })

  it("aria-label contains the balance dollar amounts", () => {
    const { getByTestId } = render(
      <ProviderStatusBadge
        provider="deepseek"
        status="ok"
        balanceRemaining={80}
        grantedTotal={100}
        currency="USD"
      />,
    )
    const el = getByTestId("provider-status-badge")
    const aria = el.getAttribute("aria-label") ?? ""
    expect(aria).toContain("$80.00")
    expect(aria).toContain("$100.00")
  })

  it("aria-label contains the rate-limit remaining counts", () => {
    const { getByTestId } = render(
      <ProviderStatusBadge
        provider="openai"
        status="ok"
        rateLimitRemainingRequests={47}
        rateLimitLimitRequests={50}
        rateLimitRemainingTokens={199876}
        rateLimitLimitTokens={200000}
      />,
    )
    const el = getByTestId("provider-status-badge")
    const aria = el.getAttribute("aria-label") ?? ""
    expect(aria).toContain("47 req remaining")
    expect(aria).toContain("199876 tokens remaining")
  })

  it("title matches aria-label (tooltip = screen-reader label)", () => {
    const { getByTestId } = render(
      <ProviderStatusBadge
        provider="deepseek"
        status="ok"
        balanceRemaining={50}
        grantedTotal={100}
      />,
    )
    const el = getByTestId("provider-status-badge")
    expect(el.getAttribute("title")).toBe(el.getAttribute("aria-label"))
  })

  it("unsupported title includes the reason (so hover explains why)", () => {
    const reason = "OpenAI /v1/usage requires session cookie, not API key"
    const { getByTestId } = render(
      <ProviderStatusBadge
        provider="openai"
        status="unsupported"
        reason={reason}
      />,
    )
    const el = getByTestId("provider-status-badge")
    expect(el.getAttribute("title")).toContain(reason)
  })

  it("renders a loading state when loading=true with no data", () => {
    const { getByTestId } = render(
      <ProviderStatusBadge provider="openrouter" loading={true} />,
    )
    const el = getByTestId("provider-status-badge")
    expect(el.getAttribute("data-tier")).toBe("gray")
    expect(getByTestId("provider-status-badge-loading-label")).toBeTruthy()
  })
})
