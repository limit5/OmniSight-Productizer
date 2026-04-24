/**
 * Z.4 (#293) checkbox 2 — <ProviderCardExpansion> contract tests.
 *
 * Locks the layout the spec demands:
 *   * Balance: $X.XX / $Y.YY (absolute + granted total when available)
 *   * Rate-limit: AAA req remaining / BBB tokens remaining (reset in NN s)
 *   * Last synced: MM:SS ago
 *   * Unsupported: advisory copy + external link to the provider console.
 *
 * Pure helpers (formatLastSynced / formatRateLimitReset) get their own
 * case table so the rounding boundaries are locked without rendering a
 * tree; the React component tests cover the full render contract +
 * a11y + unsupported fallback + error-envelope surfacing.
 */

import { describe, expect, it } from "vitest"
import { render } from "@testing-library/react"

import {
  ProviderCardExpansion,
  DEFAULT_PROVIDER_DASHBOARD_URLS,
  UNSUPPORTED_ADVISORY,
  formatLastSynced,
  formatRateLimitReset,
} from "@/components/omnisight/provider-card-expansion"

describe("formatLastSynced()", () => {
  const NOW = 1_700_000_000

  it('returns "never" when lastRefreshedAt is null', () => {
    expect(formatLastSynced(null, NOW)).toBe("never")
  })

  it('returns "never" when lastRefreshedAt is undefined', () => {
    expect(formatLastSynced(undefined, NOW)).toBe("never")
  })

  it('returns "just now" when timestamp is in the future (clock skew)', () => {
    expect(formatLastSynced(NOW + 5, NOW)).toBe("just now")
  })

  it('returns "just now" when delta is under 1 second', () => {
    expect(formatLastSynced(NOW - 0.3, NOW)).toBe("just now")
  })

  it('returns "Ns ago" for deltas under a minute', () => {
    expect(formatLastSynced(NOW - 15, NOW)).toBe("15s ago")
  })

  it('returns "M:SS ago" for deltas under an hour', () => {
    expect(formatLastSynced(NOW - (2 * 60 + 5), NOW)).toBe("2:05 ago")
  })

  it('returns "H:MM:SS ago" for deltas >= 1 hour', () => {
    expect(formatLastSynced(NOW - (3 * 3600 + 4 * 60 + 9), NOW)).toBe(
      "3:04:09 ago",
    )
  })

  it('returns "Xd ago" for deltas >= 1 day', () => {
    expect(formatLastSynced(NOW - (2 * 86_400 + 3600), NOW)).toBe("2d ago")
  })
})

describe("formatRateLimitReset()", () => {
  const NOW = 1_700_000_000

  it("returns blank when no reset or retry-after signal is present", () => {
    expect(formatRateLimitReset(null, null, NOW)).toBe("")
    expect(formatRateLimitReset(undefined, undefined, NOW)).toBe("")
  })

  it('returns "reset due" when resetAtTs is in the past', () => {
    expect(formatRateLimitReset(NOW - 5, null, NOW)).toBe("reset due")
  })

  it("rounds seconds with ceil so a fractional delta stays user-friendly", () => {
    expect(formatRateLimitReset(NOW + 4.1, null, NOW)).toBe("reset in 5s")
  })

  it('formats minutes once delta crosses 60 s', () => {
    expect(formatRateLimitReset(NOW + 90, null, NOW)).toBe("reset in 2m")
  })

  it('formats hours once delta crosses 3600 s', () => {
    expect(formatRateLimitReset(NOW + 5400, null, NOW)).toBe("reset in 2h")
  })

  it('falls back to retry-after when resetAtTs is absent', () => {
    expect(formatRateLimitReset(null, 30, NOW)).toBe("retry after ~30s")
    expect(formatRateLimitReset(null, 120, NOW)).toBe("retry after ~2m")
  })

  it("ignores non-positive retryAfter", () => {
    expect(formatRateLimitReset(null, 0, NOW)).toBe("")
    expect(formatRateLimitReset(null, -5, NOW)).toBe("")
  })
})

describe("<ProviderCardExpansion> — unsupported envelope", () => {
  it("renders the advisory message + link to the resolved provider dashboard", () => {
    const { getByTestId, queryByTestId } = render(
      <ProviderCardExpansion
        provider="anthropic"
        status="unsupported"
      />,
    )
    const root = getByTestId("provider-card-expansion")
    expect(root.getAttribute("data-status")).toBe("unsupported")
    expect(root.getAttribute("data-provider")).toBe("anthropic")

    const msg = getByTestId("provider-card-expansion-unsupported-message")
    expect(msg.textContent).toBe(UNSUPPORTED_ADVISORY)

    const link = getByTestId("provider-card-expansion-dashboard-link") as HTMLAnchorElement
    expect(link.href).toBe(DEFAULT_PROVIDER_DASHBOARD_URLS.anthropic)
    expect(link.getAttribute("target")).toBe("_blank")
    expect(link.getAttribute("rel")).toBe("noopener noreferrer")

    // The three detail rows are not rendered on the unsupported branch.
    expect(queryByTestId("provider-card-expansion-balance-row")).toBeNull()
    expect(queryByTestId("provider-card-expansion-rate-limit-row")).toBeNull()
    expect(queryByTestId("provider-card-expansion-last-synced-row")).toBeNull()
  })

  it("prefers the backend-provided reason string when present", () => {
    const custom =
      "OpenAI /v1/usage requires a session cookie, not API key auth"
    const { getByTestId } = render(
      <ProviderCardExpansion
        provider="openai"
        status="unsupported"
        reason={custom}
      />,
    )
    expect(
      getByTestId("provider-card-expansion-unsupported-message").textContent,
    ).toBe(custom)
  })

  it("respects an explicit dashboardUrl override (tenant CN console etc.)", () => {
    const { getByTestId } = render(
      <ProviderCardExpansion
        provider="deepseek"
        status="unsupported"
        dashboardUrl="https://platform.deepseek.com/zh-cn/usage"
      />,
    )
    const link = getByTestId(
      "provider-card-expansion-dashboard-link",
    ) as HTMLAnchorElement
    expect(link.href).toBe("https://platform.deepseek.com/zh-cn/usage")
  })

  it("suppresses the link entirely when dashboardUrl=''", () => {
    const { queryByTestId } = render(
      <ProviderCardExpansion
        provider="ollama"
        status="unsupported"
        dashboardUrl=""
      />,
    )
    expect(
      queryByTestId("provider-card-expansion-dashboard-link"),
    ).toBeNull()
  })

  it("suppresses the link when the default map has no entry (ollama)", () => {
    // DEFAULT_PROVIDER_DASHBOARD_URLS.ollama === "" by design.
    const { queryByTestId } = render(
      <ProviderCardExpansion provider="ollama" status="unsupported" />,
    )
    expect(
      queryByTestId("provider-card-expansion-dashboard-link"),
    ).toBeNull()
  })
})

describe("<ProviderCardExpansion> — ok envelope", () => {
  const NOW = 1_700_000_000

  it("renders balance + rate-limit + last-synced rows with spec-exact labels", () => {
    const { getByTestId } = render(
      <ProviderCardExpansion
        provider="deepseek"
        status="ok"
        balanceRemaining={8.5}
        grantedTotal={10}
        currency="USD"
        rateLimitRemainingRequests={47}
        rateLimitRemainingTokens={199_876}
        resetAtTs={NOW + 42}
        lastRefreshedAt={NOW - 125}
        nowTs={NOW}
      />,
    )

    const balance = getByTestId("provider-card-expansion-balance-value")
    expect(balance.textContent).toBe("$8.50 / $10.00")

    const rate = getByTestId("provider-card-expansion-rate-limit-value")
    expect(rate.textContent).toBe(
      "47 req remaining / 199,876 tokens remaining (reset in 42s)",
    )

    const synced = getByTestId("provider-card-expansion-last-synced-value")
    expect(synced.textContent).toBe("2:05 ago")
  })

  it('renders "—" for balance when balanceRemaining is null', () => {
    const { getByTestId } = render(
      <ProviderCardExpansion
        provider="anthropic"
        status="ok"
        balanceRemaining={null}
        rateLimitRemainingRequests={50}
        lastRefreshedAt={NOW}
        nowTs={NOW}
      />,
    )
    expect(
      getByTestId("provider-card-expansion-balance-value").textContent,
    ).toBe("—")
  })

  it('renders the absolute alone when grantedTotal is null (OpenRouter remainder-only shape)', () => {
    const { getByTestId } = render(
      <ProviderCardExpansion
        provider="openrouter"
        status="ok"
        balanceRemaining={17.42}
        grantedTotal={null}
        currency="USD"
        nowTs={NOW}
      />,
    )
    expect(
      getByTestId("provider-card-expansion-balance-value").textContent,
    ).toBe("$17.42")
  })

  it("uses ¥ symbol for CNY currency (DeepSeek CN region)", () => {
    const { getByTestId } = render(
      <ProviderCardExpansion
        provider="deepseek"
        status="ok"
        balanceRemaining={50}
        grantedTotal={100}
        currency="CNY"
        nowTs={NOW}
      />,
    )
    expect(
      getByTestId("provider-card-expansion-balance-value").textContent,
    ).toBe("¥50.00 / ¥100.00")
  })

  it('omits the reset trailer when no resetAtTs + no retryAfter are available', () => {
    const { getByTestId } = render(
      <ProviderCardExpansion
        provider="groq"
        status="ok"
        rateLimitRemainingRequests={100}
        rateLimitRemainingTokens={50_000}
        nowTs={NOW}
      />,
    )
    expect(
      getByTestId("provider-card-expansion-rate-limit-value").textContent,
    ).toBe("100 req remaining / 50,000 tokens remaining")
  })

  it('renders only the req counter when tokens counter is absent', () => {
    const { getByTestId } = render(
      <ProviderCardExpansion
        provider="groq"
        status="ok"
        rateLimitRemainingRequests={9}
        resetAtTs={NOW + 120}
        nowTs={NOW}
      />,
    )
    expect(
      getByTestId("provider-card-expansion-rate-limit-value").textContent,
    ).toBe("9 req remaining (reset in 2m)")
  })

  it('renders "—" for rate-limit when no counters are present', () => {
    const { getByTestId } = render(
      <ProviderCardExpansion
        provider="together"
        status="ok"
        balanceRemaining={1.5}
        nowTs={NOW}
      />,
    )
    expect(
      getByTestId("provider-card-expansion-rate-limit-value").textContent,
    ).toBe("—")
  })

  it('surfaces "never" for last-synced when lastRefreshedAt is null', () => {
    const { getByTestId } = render(
      <ProviderCardExpansion
        provider="openai"
        status="ok"
        rateLimitRemainingRequests={100}
        lastRefreshedAt={null}
        nowTs={NOW}
      />,
    )
    expect(
      getByTestId("provider-card-expansion-last-synced-value").textContent,
    ).toBe("never")
  })

  it("aria-labels carry the concrete numbers for screen-readers", () => {
    const { getByTestId } = render(
      <ProviderCardExpansion
        provider="deepseek"
        status="ok"
        balanceRemaining={50}
        grantedTotal={100}
        currency="USD"
        rateLimitRemainingRequests={500}
        resetAtTs={NOW + 10}
        lastRefreshedAt={NOW - 5}
        nowTs={NOW}
      />,
    )
    const balance = getByTestId("provider-card-expansion-balance-value")
    expect(balance.getAttribute("aria-label")).toBe(
      "Balance: $50.00 / $100.00",
    )
    const rate = getByTestId("provider-card-expansion-rate-limit-value")
    expect(rate.getAttribute("aria-label")).toBe(
      "Rate-limit: 500 req remaining (reset in 10s)",
    )
    const synced = getByTestId("provider-card-expansion-last-synced-value")
    expect(synced.getAttribute("aria-label")).toBe("Last synced: 5s ago")
  })

  it('renders aria-label "not reported" when a row has no data', () => {
    const { getByTestId } = render(
      <ProviderCardExpansion
        provider="anthropic"
        status="ok"
        rateLimitRemainingRequests={100}
        nowTs={1_700_000_000}
      />,
    )
    expect(
      getByTestId("provider-card-expansion-balance-value").getAttribute(
        "aria-label",
      ),
    ).toBe("Balance: not reported")
  })
})

describe("<ProviderCardExpansion> — error envelope", () => {
  const NOW = 1_700_000_000

  it('still renders the three rows and adds an error paragraph', () => {
    const { getByTestId } = render(
      <ProviderCardExpansion
        provider="deepseek"
        status="error"
        errorMessage="fetch failed: HTTP 502"
        rateLimitRemainingRequests={10}
        lastRefreshedAt={NOW - 30}
        nowTs={NOW}
      />,
    )
    const root = getByTestId("provider-card-expansion")
    expect(root.getAttribute("data-status")).toBe("error")
    const err = getByTestId("provider-card-expansion-error-message")
    expect(err.textContent).toBe("fetch failed: HTTP 502")
    expect(err.getAttribute("role")).toBe("status")
  })

  it('falls back to reason then generic copy when errorMessage is missing', () => {
    const { getByTestId, rerender } = render(
      <ProviderCardExpansion
        provider="groq"
        status="error"
        reason="authentication failed"
        nowTs={NOW}
      />,
    )
    expect(
      getByTestId("provider-card-expansion-error-message").textContent,
    ).toBe("authentication failed")

    rerender(
      <ProviderCardExpansion
        provider="groq"
        status="error"
        nowTs={NOW}
      />,
    )
    expect(
      getByTestId("provider-card-expansion-error-message").textContent,
    ).toBe("Balance refresh failed")
  })
})

describe("DEFAULT_PROVIDER_DASHBOARD_URLS contract", () => {
  it("covers every provider the balance endpoint recognises", () => {
    // Mirrors backend/routers/llm_balance.py::_VALID_PROVIDER_NAMES.
    const expected = [
      "anthropic",
      "google",
      "openai",
      "xai",
      "groq",
      "deepseek",
      "together",
      "openrouter",
      "ollama",
    ]
    for (const name of expected) {
      expect(DEFAULT_PROVIDER_DASHBOARD_URLS).toHaveProperty(name)
    }
  })

  it("provides an HTTPS URL for every supported provider (ollama is blank by design)", () => {
    for (const [name, url] of Object.entries(DEFAULT_PROVIDER_DASHBOARD_URLS)) {
      if (name === "ollama") {
        expect(url).toBe("")
        continue
      }
      expect(url).toMatch(/^https:\/\//)
    }
  })
})
