"use client"

/**
 * Z.4 #293 checkbox 7 — e2e fixture page for ProviderRollup visual tests.
 *
 * This page exists solely to give the Playwright visual-regression spec
 * (`e2e/z4-provider-rollup-visual.spec.ts`) a minimal DOM surface to
 * screenshot. It renders `<ProviderRollup>` + `<ProviderStatusBadge>` +
 * `<ProviderCardExpansion>` with hard-coded scenario fixtures, driven
 * by a single `?scenario=` URL param (`fully-configured` | `all-empty` |
 * `mixed`). The full dashboard at `/` is too noisy for visual regression
 * — every one of its 40+ hooks is a timing / data dependency that has
 * nothing to do with the per-provider roll-up we're trying to lock.
 *
 * Importantly: this page does NOT call `useEngine`, `useAuth`, or any
 * other hook that would trigger backend calls. It is a pure render of
 * the components under test with data the spec controls exactly.
 *
 * If the checkbox surface ever changes — new props on the rollup, a new
 * envelope field — update the fixtures in this file to match, so the
 * screenshot stays a faithful representation of what production renders.
 */

import { useMemo } from "react"
import { useSearchParams } from "next/navigation"
import { Suspense } from "react"

import {
  ProviderRollup,
  groupByProvider,
  openRouterAwareResolver,
  type ProviderRollupRow,
} from "@/components/omnisight/provider-rollup"
import { ProviderStatusBadge } from "@/components/omnisight/provider-status-badge"
import { ProviderCardExpansion } from "@/components/omnisight/provider-card-expansion"
import type { ProviderBalanceEnvelope } from "@/lib/api"
import { getModelInfo } from "@/components/omnisight/agent-matrix-wall"

type Scenario = "fully-configured" | "all-empty" | "mixed"

// Frozen clock. The visual spec's `page.addInitScript` hook freezes
// `Date.now()` to this exact epoch ms; the ProviderCardExpansion's
// `formatLastSynced` helper then renders stable "N s ago" labels against
// the `last_refreshed_at` values below (which are offset backwards).
const FROZEN_NOW_SEC = 1777887600  // 2026-04-25T10:00:00Z

interface FixtureRow extends ProviderRollupRow {
  color: string
  provider: string
}

/** Token-usage rows chosen so `getModelInfo` resolves cleanly to each
 *  of the providers whose balance envelopes we render (see fixture
 *  builders below). Groq / Together deliberately have no matching
 *  AI_MODEL_INFO entry — those providers cannot form their own group in
 *  the current UI, and the spec locks that behaviour. */
function buildRows(): FixtureRow[] {
  const baseRows: Array<{ model: string; cost: number; tokens: number }> = [
    { model: "claude-opus-4-7",        cost: 4.25, tokens: 520_000 },
    { model: "gpt-4o",                 cost: 2.15, tokens: 310_000 },
    { model: "gemini-1.5-pro",         cost: 1.10, tokens: 480_000 },
    { model: "grok-3",                 cost: 0.78, tokens: 140_000 },
    { model: "deepseek-chat",          cost: 0.22, tokens: 890_000 },
    { model: "anthropic/claude-sonnet-4", cost: 3.60, tokens: 410_000 },
    { model: "ollama",                 cost: 0.00, tokens: 120_000 },
  ]
  return baseRows.map(({ model, cost, tokens }) => {
    const info = getModelInfo(model)
    return {
      model,
      inputTokens: Math.floor(tokens * 0.7),
      outputTokens: Math.floor(tokens * 0.3),
      totalTokens: tokens,
      cost,
      requestCount: Math.max(1, Math.floor(cost * 10)),
      provider: info.provider,
      color: info.color,
    }
  })
}

/** Full nine-provider array matching
 *  `backend/routers/llm_balance.py::_VALID_PROVIDER_NAMES`. All ok /
 *  green / healthy for the "fully-configured" scenario. */
function fullyConfiguredBalances(): ProviderBalanceEnvelope[] {
  const base: Array<Partial<ProviderBalanceEnvelope> & { provider: string }> = [
    { status: "ok", provider: "anthropic",  currency: "USD", balance_remaining: 85.0,  granted_total: 100.0 },
    { status: "ok", provider: "deepseek",   currency: "CNY", balance_remaining: 180.0, granted_total: 200.0 },
    { status: "ok", provider: "google",     currency: "USD", balance_remaining: 72.5,  granted_total: 100.0 },
    { status: "ok", provider: "groq",       currency: "USD", balance_remaining: 45.0,  granted_total: 50.0  },
    { status: "ok", provider: "ollama",     currency: "USD", balance_remaining: null,  granted_total: null  },
    { status: "ok", provider: "openai",     currency: "USD", balance_remaining: 210.0, granted_total: 250.0 },
    { status: "ok", provider: "openrouter", currency: "USD", balance_remaining: 28.50, granted_total: null  },
    { status: "ok", provider: "together",   currency: "USD", balance_remaining: 38.0,  granted_total: 50.0  },
    { status: "ok", provider: "xai",        currency: "USD", balance_remaining: 95.0,  granted_total: 150.0 },
  ]
  return base.map((env, idx) => ({
    ...(env as ProviderBalanceEnvelope),
    last_refreshed_at: FROZEN_NOW_SEC - (15 + idx * 5),
    usage_total: 0,
  }))
}

/** One envelope per tier: green / yellow / red / unsupported / error,
 *  plus one provider deliberately absent so `renderStatusBadge` hits
 *  the "no envelope" gray-loading fallback. */
function mixedBalances(): ProviderBalanceEnvelope[] {
  return [
    { status: "ok",          provider: "anthropic",  currency: "USD", balance_remaining: 72.0, granted_total: 100.0,
      last_refreshed_at: FROZEN_NOW_SEC - 45 },
    { status: "ok",          provider: "openai",     currency: "USD", balance_remaining: 18.0, granted_total: 100.0,
      last_refreshed_at: FROZEN_NOW_SEC - 60 },
    { status: "ok",          provider: "deepseek",   currency: "CNY", balance_remaining: 6.0,  granted_total: 200.0,
      last_refreshed_at: FROZEN_NOW_SEC - 120 },
    { status: "unsupported", provider: "xai",
      reason: "provider does not expose a public balance API with API-key authentication" },
    { status: "error",       provider: "openrouter", message: "401 Unauthorized: invalid API key",
      last_refreshed_at: FROZEN_NOW_SEC - 600 },
    { status: "ok",          provider: "ollama",     currency: "USD", balance_remaining: null, granted_total: null,
      last_refreshed_at: FROZEN_NOW_SEC - 5 },
    // google / groq / together intentionally absent → gray-loading badge.
  ]
}

function Inner() {
  const params = useSearchParams()
  const scenario = (params.get("scenario") || "fully-configured") as Scenario

  const balances: ProviderBalanceEnvelope[] | null =
    scenario === "fully-configured" ? fullyConfiguredBalances()
    : scenario === "mixed"           ? mixedBalances()
    : []  // "all-empty" — fetched once, empty array → gray no-data tier

  const rows = useMemo(() => buildRows(), [])
  const groups = useMemo(
    () => groupByProvider(
      rows,
      openRouterAwareResolver((model) => {
        const info = getModelInfo(model)
        return { provider: info.provider, color: info.color }
      }),
    ),
    [rows],
  )
  const grandTotalTokens = rows.reduce((acc, r) => acc + r.totalTokens, 0)

  return (
    <div
      className="min-h-screen bg-[var(--background)] text-[var(--foreground)] p-6"
      data-testid="e2e-fixture-root"
      data-scenario={scenario}
    >
      <h1 className="font-mono text-sm mb-4 text-[var(--muted-foreground)] uppercase tracking-wider">
        ProviderRollup fixture — scenario: {scenario}
      </h1>

      <ProviderRollup
        groups={groups}
        grandTotalTokens={grandTotalTokens}
        defaultExpanded
        renderStatusBadge={(providerKey) => {
          const env = balances.find((e) => e.provider === providerKey)
          if (!env) {
            return (
              <ProviderStatusBadge
                provider={providerKey}
                loading={false}
              />
            )
          }
          return (
            <ProviderStatusBadge
              provider={providerKey}
              status={env.status}
              reason={env.reason}
              balanceRemaining={env.balance_remaining ?? null}
              grantedTotal={env.granted_total ?? null}
              currency={env.currency ?? null}
            />
          )
        }}
        renderExpansion={(providerKey) => {
          const env = balances.find((e) => e.provider === providerKey)
          if (!env) return null
          return (
            <ProviderCardExpansion
              provider={providerKey}
              status={env.status}
              reason={env.reason}
              balanceRemaining={env.balance_remaining ?? null}
              grantedTotal={env.granted_total ?? null}
              currency={env.currency ?? null}
              lastRefreshedAt={env.last_refreshed_at ?? null}
              errorMessage={env.message}
              nowTs={FROZEN_NOW_SEC}
            />
          )
        }}
        renderRow={(row) => {
          const info = getModelInfo(row.model)
          return (
            <div
              className="p-3 rounded-lg bg-[var(--secondary)] flex items-center justify-between"
              data-testid={`fixture-model-row-${row.model}`}
            >
              <div className="flex items-center gap-2 min-w-0">
                <span
                  className="w-2 h-2 rounded-full shrink-0"
                  style={{ backgroundColor: info.color }}
                  aria-hidden="true"
                />
                <span className="font-mono text-xs truncate">{info.shortLabel}</span>
                <span className="font-mono text-[10px] text-[var(--muted-foreground)] truncate">
                  {row.model}
                </span>
              </div>
              <div className="flex items-center gap-3 shrink-0 font-mono text-[10px]">
                <span className="text-[var(--validation-emerald)]">
                  {row.totalTokens.toLocaleString()} tokens
                </span>
                <span className="text-[var(--hardware-orange)]">
                  ${row.cost.toFixed(2)}
                </span>
              </div>
            </div>
          )
        }}
      />
    </div>
  )
}

export default function ProviderRollupFixturePage() {
  return (
    <Suspense fallback={<div data-testid="e2e-fixture-loading">loading…</div>}>
      <Inner />
    </Suspense>
  )
}
