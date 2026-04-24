"use client"

/**
 * Z.4 (#293) checkbox 2 — <ProviderCardExpansion>.
 *
 * The per-provider card expands into a three-line detail block:
 *
 *   Balance: $X.XX / $Y.YY                             ← balance row
 *   Rate-limit: AAA req remaining / BBB tokens remaining (reset in NN s)
 *   Last synced: MM:SS ago
 *
 * Unsupported providers short-circuit to a one-line advisory plus an
 * external link to the vendor's own billing dashboard:
 *
 *   This provider does not expose a public balance API. Open the
 *   provider dashboard → https://console.example.com/usage
 *
 * Scope discipline (same pattern as checkbox 1) — this module only
 * implements the expansion block. The provider-card shell, card-
 * expansion state, and per-provider roll-up of model rows are delivered
 * by checkbox 3, and the 60 s ``GET /runtime/providers/balance`` poll
 * that feeds real data lands in checkbox 5. Both are free to consume
 * this component verbatim: it accepts the exact envelope field set
 * ``GET /runtime/providers/{provider}/balance`` returns (status / reason /
 *  balance_remaining / granted_total / currency / rate_limit_* / last_
 * refreshed_at), so the caller does not need a translation layer.
 *
 * The component is a pure function of its props — no useState, no
 * useEffect, no useRef. Cross-worker consistency is moot: every worker
 * rendering the same HTML for the same props is answer #1 of the SOP
 * Step 1 module-global audit ("same inputs → same output"). Read-after-
 * write is N/A because nothing is written.
 */

import { ExternalLink } from "lucide-react"

/** Canonical billing / usage dashboard URLs per provider. Lazy-edited
 *  by the caller via ``dashboardUrl`` prop when a tenant has a private
 *  CN / EU console. We intentionally keep this map small and obvious
 *  rather than plumbing it through ``Settings`` — the default link
 *  surface is "take the operator to somewhere sane when our balance
 *  API is blind", and any tenant whose vendor console differs already
 *  has a customised deployment. */
export const DEFAULT_PROVIDER_DASHBOARD_URLS: Record<string, string> = {
  anthropic: "https://console.anthropic.com/settings/billing",
  openai: "https://platform.openai.com/usage",
  google: "https://aistudio.google.com/app/apikey",
  xai: "https://console.x.ai/",
  groq: "https://console.groq.com/settings/billing",
  together: "https://api.together.ai/settings/billing",
  deepseek: "https://platform.deepseek.com/usage",
  openrouter: "https://openrouter.ai/credits",
  ollama: "",
}

export interface ProviderCardExpansionProps {
  provider: string
  /** Envelope status from GET /runtime/providers/{provider}/balance. */
  status?: "ok" | "unsupported" | "error"
  /** Human-readable reason surfaced on unsupported / error rows. */
  reason?: string
  /** Balance fields. ``grantedTotal`` may be null on OpenRouter-style
   *  responses that expose only a spendable remainder. */
  balanceRemaining?: number | null
  grantedTotal?: number | null
  currency?: string | null
  /** Rate-limit fields from the normalised SharedKV("provider_ratelimit")
   *  dict populated by TokenTrackingCallback (Z.1). ``resetAtTs`` is a
   *  unix epoch second at which the bucket refreshes; when unset the row
   *  degrades to "(reset unknown)" rather than a misleading "0 s". */
  rateLimitRemainingRequests?: number | null
  rateLimitRemainingTokens?: number | null
  resetAtTs?: number | null
  /** Retry-after from the last 429 response (seconds). Used only when
   *  ``resetAtTs`` is missing so the row can still give the operator a
   *  "cool down for ~N s" hint. */
  retryAfterS?: number | null
  /** Epoch seconds when the balance envelope was last refreshed. */
  lastRefreshedAt?: number | null
  /** Provider-console link surfaced to unsupported rows. When absent
   *  we fall back to ``DEFAULT_PROVIDER_DASHBOARD_URLS`` keyed by
   *  ``provider``. Empty string → link suppressed (used for Ollama,
   *  which has no public console). */
  dashboardUrl?: string
  /** Error message surfaced on ``status === "error"`` envelopes
   *  (auth fail / provider 5xx / missing key). Falls back to
   *  ``reason`` when this is not supplied. */
  errorMessage?: string
  /** Injected "now" for deterministic tests. Production callers leave
   *  this undefined and we call ``Date.now() / 1000``. */
  nowTs?: number
  className?: string
}

function formatCurrency(
  amount: number,
  currency: string | null | undefined,
): string {
  const prefix = currency === "CNY" ? "¥" : "$"
  if (Math.abs(amount) >= 1) return `${prefix}${amount.toFixed(2)}`
  return `${prefix}${amount.toFixed(3)}`
}

function formatInteger(value: number): string {
  if (!Number.isFinite(value)) return "—"
  return Math.trunc(value).toLocaleString()
}

/**
 * Pure helper — format the "Last synced: MM:SS ago" hint. Exported so
 * downstream Z.4 tests + the card-roll-up row (checkbox 3) can re-use
 * the same rounding without re-deriving it.
 *
 * Edge semantics:
 *   * ``lastRefreshedAt`` null / undefined / non-finite → ``"never"``.
 *   * Future timestamp (clock skew after a refresh races with the
 *     injected nowTs in tests) → ``"just now"``.
 *   * <60 s → ``"Ns ago"`` for an honest just-landed signal.
 *   * < 1 h → ``"M:SS ago"``.
 *   * >= 1 h → ``"H:MM:SS ago"``.
 *   * >= 1 d → ``"Xd ago"`` (coarse; if we're a day behind the 60 s
 *     poll is down and the colour badge already screams about it —
 *     detail noise buys nothing here).
 */
export function formatLastSynced(
  lastRefreshedAt: number | null | undefined,
  nowTs: number,
): string {
  if (
    lastRefreshedAt === null ||
    lastRefreshedAt === undefined ||
    !Number.isFinite(lastRefreshedAt)
  ) {
    return "never"
  }
  const deltaSeconds = nowTs - lastRefreshedAt
  if (deltaSeconds < 0) return "just now"
  if (deltaSeconds < 1) return "just now"
  if (deltaSeconds < 60) {
    return `${Math.floor(deltaSeconds)}s ago`
  }
  const dayInSeconds = 86_400
  if (deltaSeconds >= dayInSeconds) {
    const days = Math.floor(deltaSeconds / dayInSeconds)
    return `${days}d ago`
  }
  const total = Math.floor(deltaSeconds)
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const seconds = total % 60
  const pad = (n: number) => n.toString().padStart(2, "0")
  if (hours > 0) {
    return `${hours}:${pad(minutes)}:${pad(seconds)} ago`
  }
  return `${minutes}:${pad(seconds)} ago`
}

/**
 * Pure helper — format the "(reset in NN s)" trailer. Null / past-due
 * timestamps render as blank so the surrounding row can omit the
 * parenthetical cleanly. Exported for test ergonomics.
 */
export function formatRateLimitReset(
  resetAtTs: number | null | undefined,
  retryAfterS: number | null | undefined,
  nowTs: number,
): string {
  if (
    resetAtTs !== null &&
    resetAtTs !== undefined &&
    Number.isFinite(resetAtTs)
  ) {
    const delta = resetAtTs - nowTs
    if (delta <= 0) return "reset due"
    if (delta < 60) return `reset in ${Math.ceil(delta)}s`
    if (delta < 3600) return `reset in ${Math.ceil(delta / 60)}m`
    return `reset in ${Math.ceil(delta / 3600)}h`
  }
  if (
    retryAfterS !== null &&
    retryAfterS !== undefined &&
    Number.isFinite(retryAfterS) &&
    retryAfterS > 0
  ) {
    if (retryAfterS < 60) return `retry after ~${Math.ceil(retryAfterS)}s`
    if (retryAfterS < 3600) return `retry after ~${Math.ceil(retryAfterS / 60)}m`
    return `retry after ~${Math.ceil(retryAfterS / 3600)}h`
  }
  return ""
}

function resolveDashboardUrl(
  provider: string,
  explicit: string | undefined,
): string {
  if (explicit !== undefined) return explicit
  return DEFAULT_PROVIDER_DASHBOARD_URLS[provider.toLowerCase()] ?? ""
}

/**
 * Static copy for the unsupported advisory. Locked so tests + i18n
 * future work have a single source of truth. Kept in English here —
 * the existing TokenUsageStats section uses English labels (``TOKEN
 * USAGE`` / ``CACHE HIT``); when the dashboard picks up i18n this
 * string moves into the locale catalogue along with its peers.
 */
export const UNSUPPORTED_ADVISORY =
  "This provider does not expose a public balance API. Open the provider dashboard to view usage."

export function ProviderCardExpansion(props: ProviderCardExpansionProps) {
  const {
    provider,
    status,
    reason,
    balanceRemaining,
    grantedTotal,
    currency,
    rateLimitRemainingRequests,
    rateLimitRemainingTokens,
    resetAtTs,
    retryAfterS,
    lastRefreshedAt,
    dashboardUrl,
    errorMessage,
    nowTs,
    className = "",
  } = props

  const now =
    nowTs !== undefined && Number.isFinite(nowTs)
      ? nowTs
      : Date.now() / 1000

  const isUnsupported = status === "unsupported"
  const isError = status === "error"
  const resolvedDashboardUrl = resolveDashboardUrl(provider, dashboardUrl)

  if (isUnsupported) {
    return (
      <div
        className={`flex flex-col gap-1 px-3 py-2 rounded-lg bg-[var(--secondary)]/40 ${className}`}
        data-testid="provider-card-expansion"
        data-provider={provider}
        data-status="unsupported"
      >
        <p
          className="font-mono text-[11px] leading-snug text-[var(--muted-foreground)]"
          data-testid="provider-card-expansion-unsupported-message"
        >
          {reason || UNSUPPORTED_ADVISORY}
        </p>
        {resolvedDashboardUrl ? (
          <a
            href={resolvedDashboardUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1 self-start font-mono text-[11px] text-[var(--neural-blue)] hover:underline"
            data-testid="provider-card-expansion-dashboard-link"
          >
            Open {provider} dashboard
            <ExternalLink size={10} aria-hidden="true" />
          </a>
        ) : null}
      </div>
    )
  }

  // "ok" (happy path) and "error" envelopes share the same three-row
  // layout. Error envelopes surface the backend message under the
  // balance row so operators can see which credential is blocking the
  // refresh without having to dig into the browser devtools network tab.
  const balanceHasAbsolute =
    balanceRemaining !== null &&
    balanceRemaining !== undefined &&
    Number.isFinite(balanceRemaining)
  const balanceHasGranted =
    grantedTotal !== null &&
    grantedTotal !== undefined &&
    Number.isFinite(grantedTotal) &&
    grantedTotal > 0

  const balanceLabel = (() => {
    if (!balanceHasAbsolute) return "—"
    const abs = formatCurrency(balanceRemaining as number, currency)
    if (balanceHasGranted) {
      return `${abs} / ${formatCurrency(grantedTotal as number, currency)}`
    }
    return abs
  })()

  const hasReqCounter =
    rateLimitRemainingRequests !== null &&
    rateLimitRemainingRequests !== undefined &&
    Number.isFinite(rateLimitRemainingRequests)
  const hasTokCounter =
    rateLimitRemainingTokens !== null &&
    rateLimitRemainingTokens !== undefined &&
    Number.isFinite(rateLimitRemainingTokens)
  const rateLimitBits: string[] = []
  if (hasReqCounter) {
    rateLimitBits.push(
      `${formatInteger(rateLimitRemainingRequests as number)} req remaining`,
    )
  }
  if (hasTokCounter) {
    rateLimitBits.push(
      `${formatInteger(rateLimitRemainingTokens as number)} tokens remaining`,
    )
  }
  const resetTrailer = formatRateLimitReset(resetAtTs, retryAfterS, now)
  const rateLimitLabel =
    rateLimitBits.length > 0
      ? resetTrailer
        ? `${rateLimitBits.join(" / ")} (${resetTrailer})`
        : rateLimitBits.join(" / ")
      : "—"

  const lastSyncedLabel = formatLastSynced(lastRefreshedAt, now)

  return (
    <div
      className={`flex flex-col gap-1 px-3 py-2 rounded-lg bg-[var(--secondary)]/40 ${className}`}
      data-testid="provider-card-expansion"
      data-provider={provider}
      data-status={status ?? "ok"}
    >
      <div
        className="flex items-baseline justify-between gap-2"
        data-testid="provider-card-expansion-balance-row"
      >
        <span className="font-mono text-[11px] text-[var(--muted-foreground)] uppercase tracking-wider">
          Balance
        </span>
        <span
          className={`font-mono text-[11px] ${
            balanceHasAbsolute
              ? "text-[var(--foreground)]"
              : "text-[var(--muted-foreground)]"
          }`}
          data-testid="provider-card-expansion-balance-value"
          aria-label={
            balanceHasAbsolute
              ? `Balance: ${balanceLabel}`
              : "Balance: not reported"
          }
        >
          {balanceLabel}
        </span>
      </div>

      <div
        className="flex items-baseline justify-between gap-2"
        data-testid="provider-card-expansion-rate-limit-row"
      >
        <span className="font-mono text-[11px] text-[var(--muted-foreground)] uppercase tracking-wider">
          Rate-limit
        </span>
        <span
          className={`font-mono text-[11px] ${
            rateLimitBits.length > 0
              ? "text-[var(--foreground)]"
              : "text-[var(--muted-foreground)]"
          }`}
          data-testid="provider-card-expansion-rate-limit-value"
          aria-label={
            rateLimitBits.length > 0
              ? `Rate-limit: ${rateLimitLabel}`
              : "Rate-limit: not reported"
          }
        >
          {rateLimitLabel}
        </span>
      </div>

      <div
        className="flex items-baseline justify-between gap-2"
        data-testid="provider-card-expansion-last-synced-row"
      >
        <span className="font-mono text-[10px] text-[var(--muted-foreground)]/70 uppercase tracking-wider">
          Last synced
        </span>
        <span
          className="font-mono text-[10px] text-[var(--muted-foreground)]/70"
          data-testid="provider-card-expansion-last-synced-value"
          aria-label={`Last synced: ${lastSyncedLabel}`}
        >
          {lastSyncedLabel}
        </span>
      </div>

      {isError ? (
        <p
          className="font-mono text-[10px] text-[var(--critical-red)] mt-1 leading-snug"
          data-testid="provider-card-expansion-error-message"
          role="status"
        >
          {errorMessage || reason || "Balance refresh failed"}
        </p>
      ) : null}
    </div>
  )
}
