"use client"

/**
 * Z.4 (#293) checkbox 1 — <ProviderStatusBadge provider="deepseek" />.
 *
 * A compact chip that rolls up a provider's balance + rate-limit health
 * into a single colour + one glyph. The four tiers match the Z.4 spec
 * (TODO.md Z.4 row 1):
 *
 *   green  — balance > 20% AND rate-limit remaining > 10%
 *   yellow — balance < 20% OR rate-limit ≥ 80% used (remaining ≤ 20%)
 *   red    — balance < 5%  OR rate-limit saturated (remaining == 0)
 *   gray   — status === "unsupported" (provider doesn't expose a public
 *            balance API with API-key auth — click/hover surfaces why)
 *
 * Hierarchy: unsupported > red > yellow > green. When any red trigger
 * fires the badge is red even if the rate-limit is healthy, and vice
 * versa. This is the same "worst-signal wins" rule operators already
 * know from the daily-budget bar in <TokenUsageStats />.
 *
 * Data source shape mirrors the existing backend envelopes:
 *   - balance: ``backend/llm_balance.py::BalanceInfo`` dict, surfaced
 *     by ``GET /runtime/providers/{provider}/balance`` (Z.2 complete).
 *     ``balanceRemaining`` + ``grantedTotal`` are both required to
 *     compute a percentage; when ``grantedTotal`` is ``null`` (OpenRouter
 *     reports usage_total + limit instead — caller maps to the same two
 *     fields before passing us) we fall through to "insufficient data"
 *     rather than guess at an absolute-dollar threshold.
 *   - rate-limit: ``backend/agents/llm.py`` normalised dict, written
 *     to ``SharedKV("provider_ratelimit")`` by the TokenTrackingCallback
 *     (Z.1 complete). Fields: remaining_requests / remaining_tokens /
 *     reset_at_ts / retry_after_s. We treat "saturated" as either
 *     counter being exactly 0 (rate-limit already hit, vendor will
 *     429 the next call) and "near-saturated" as either counter's
 *     usage ≥ 80% (remaining ≤ 20%).
 *
 * Accessibility: the badge carries an ``aria-label`` with the specific
 * balance dollars + rate-limit remaining counts so screen-reader users
 * get the numbers, not just "red circle" — per the Z.4 a11y checkbox.
 * The tooltip (``title``) carries the same information for sighted
 * mouse users; unsupported providers get a sentence explaining why
 * rather than cryptic empty-state.
 *
 * Scope discipline — this checkbox is ONLY the sub-component. The
 * card-expansion panel (checkbox 2), provider roll-up (3), OpenRouter
 * namespace handling (4), useEngine polling (5), and Playwright screen-
 * shots (7) are separate Z.4 rows and NOT delivered here. The pure
 * ``computeProviderStatus`` helper is exported so Z.5's test row and
 * downstream Z.4 rows can lock the threshold contract without having
 * to render JSX.
 */

import { AlertTriangle, CheckCircle2, Circle, HelpCircle } from "lucide-react"

export type ProviderStatusTier = "green" | "yellow" | "red" | "gray"

export interface ProviderStatusBadgeProps {
  provider: string
  /** Envelope status from GET /runtime/providers/{provider}/balance. */
  status?: "ok" | "unsupported" | "error"
  /** Human-readable reason for the unsupported envelope (rendered in the
   *  tooltip so operators know why the badge is gray). */
  reason?: string
  /** Balance fields from BalanceInfo (``balance_remaining`` +
   *  ``granted_total``). Both required to compute a percentage; when
   *  only an absolute is present we fall through to "insufficient data"
   *  rather than guess at a dollar-threshold. */
  balanceRemaining?: number | null
  grantedTotal?: number | null
  currency?: string | null
  /** Rate-limit fields from the normalised SharedKV("provider_ratelimit")
   *  dict. ``limit*`` are optional vendor-reported ceilings; when missing
   *  we can still detect saturation (remaining == 0) but cannot compute
   *  a remaining-percent, so near-saturated falls through. */
  rateLimitRemainingRequests?: number | null
  rateLimitRemainingTokens?: number | null
  rateLimitLimitRequests?: number | null
  rateLimitLimitTokens?: number | null
  resetAtTs?: number | null
  /** When the badge is referenced without any balance/rate-limit data
   *  at all (first mount before the 60 s poll lands), render a gray
   *  "loading" state rather than a misleading green. */
  loading?: boolean
  className?: string
}

export interface ProviderStatusResult {
  tier: ProviderStatusTier
  /** Ordered list of reasons that pushed the badge into its tier —
   *  fed into the tooltip + aria-label so the operator knows *which*
   *  signal is off. */
  reasons: string[]
  /** Computed (balanceRemaining / grantedTotal) * 100, or null when
   *  either field is missing / grantedTotal <= 0. */
  balancePct: number | null
  /** Minimum of (remaining_requests / limit_requests) and
   *  (remaining_tokens / limit_tokens) — whichever is tighter — as a
   *  percentage. Null when no ratio is computable. */
  rateLimitRemainingPct: number | null
  /** True when any remaining counter is exactly 0 — provider will 429
   *  the next call. */
  rateLimitSaturated: boolean
}

const GREEN_BALANCE_THRESHOLD = 20
const YELLOW_BALANCE_THRESHOLD = 20
const RED_BALANCE_THRESHOLD = 5
const GREEN_RATE_LIMIT_THRESHOLD = 10
const YELLOW_RATE_LIMIT_THRESHOLD = 20

function safePercent(numer: number, denom: number): number | null {
  if (!Number.isFinite(numer) || !Number.isFinite(denom)) return null
  if (denom <= 0) return null
  return (numer / denom) * 100
}

/**
 * Pure threshold logic — no React, no DOM. Exported so downstream
 * tests and sibling Z.4 rows can re-use the same decision tree
 * without standing up a render tree.
 */
export function computeProviderStatus(
  props: Pick<
    ProviderStatusBadgeProps,
    | "status"
    | "balanceRemaining"
    | "grantedTotal"
    | "rateLimitRemainingRequests"
    | "rateLimitRemainingTokens"
    | "rateLimitLimitRequests"
    | "rateLimitLimitTokens"
    | "loading"
  >,
): ProviderStatusResult {
  const reasons: string[] = []

  if (props.status === "unsupported") {
    return {
      tier: "gray",
      reasons: ["unsupported"],
      balancePct: null,
      rateLimitRemainingPct: null,
      rateLimitSaturated: false,
    }
  }

  const balancePct =
    props.balanceRemaining !== null && props.balanceRemaining !== undefined &&
    props.grantedTotal !== null && props.grantedTotal !== undefined
      ? safePercent(props.balanceRemaining, props.grantedTotal)
      : null

  const reqPct =
    props.rateLimitRemainingRequests !== null && props.rateLimitRemainingRequests !== undefined &&
    props.rateLimitLimitRequests !== null && props.rateLimitLimitRequests !== undefined
      ? safePercent(props.rateLimitRemainingRequests, props.rateLimitLimitRequests)
      : null
  const tokPct =
    props.rateLimitRemainingTokens !== null && props.rateLimitRemainingTokens !== undefined &&
    props.rateLimitLimitTokens !== null && props.rateLimitLimitTokens !== undefined
      ? safePercent(props.rateLimitRemainingTokens, props.rateLimitLimitTokens)
      : null

  const rateLimitRemainingPct =
    reqPct !== null && tokPct !== null
      ? Math.min(reqPct, tokPct)
      : reqPct !== null
        ? reqPct
        : tokPct

  const rateLimitSaturated =
    props.rateLimitRemainingRequests === 0 ||
    props.rateLimitRemainingTokens === 0

  // No data at all → gray loading state rather than a cheerful green.
  const hasAnySignal =
    balancePct !== null || rateLimitRemainingPct !== null || rateLimitSaturated
  if (props.loading || !hasAnySignal) {
    return {
      tier: "gray",
      reasons: props.loading ? ["loading"] : ["no data"],
      balancePct,
      rateLimitRemainingPct,
      rateLimitSaturated,
    }
  }

  // Red conditions (worst wins).
  if (balancePct !== null && balancePct < RED_BALANCE_THRESHOLD) {
    reasons.push(`balance ${balancePct.toFixed(1)}% < ${RED_BALANCE_THRESHOLD}%`)
  }
  if (rateLimitSaturated) {
    reasons.push("rate-limit saturated")
  }
  if (reasons.length > 0) {
    return {
      tier: "red",
      reasons,
      balancePct,
      rateLimitRemainingPct,
      rateLimitSaturated,
    }
  }

  // Yellow conditions.
  if (balancePct !== null && balancePct < YELLOW_BALANCE_THRESHOLD) {
    reasons.push(`balance ${balancePct.toFixed(1)}% < ${YELLOW_BALANCE_THRESHOLD}%`)
  }
  if (
    rateLimitRemainingPct !== null &&
    rateLimitRemainingPct <= YELLOW_RATE_LIMIT_THRESHOLD
  ) {
    reasons.push(
      `rate-limit ${(100 - rateLimitRemainingPct).toFixed(1)}% used`,
    )
  }
  if (reasons.length > 0) {
    return {
      tier: "yellow",
      reasons,
      balancePct,
      rateLimitRemainingPct,
      rateLimitSaturated,
    }
  }

  // Green conditions. Spec: balance > 20% AND rate-limit remaining > 10%.
  // If the relevant signal is missing we still green — the vendor just
  // didn't give us enough to flag a warning, and a yellow-by-default
  // would cry-wolf on every Anthropic card that has rate-limit but no
  // balance.
  const balanceOk = balancePct === null || balancePct > GREEN_BALANCE_THRESHOLD
  const rateLimitOk =
    rateLimitRemainingPct === null ||
    rateLimitRemainingPct > GREEN_RATE_LIMIT_THRESHOLD
  if (balanceOk && rateLimitOk) {
    return {
      tier: "green",
      reasons: ["healthy"],
      balancePct,
      rateLimitRemainingPct,
      rateLimitSaturated,
    }
  }

  // Fallthrough — the (10, 20]% rate-limit remaining band falls out of
  // strict green but isn't yet at the yellow trigger threshold. Treat as
  // yellow so the operator sees a warning before it saturates.
  return {
    tier: "yellow",
    reasons: ["rate-limit usage approaching 80%"],
    balancePct,
    rateLimitRemainingPct,
    rateLimitSaturated,
  }
}

function formatCurrency(amount: number, currency: string | null | undefined): string {
  const prefix = currency === "CNY" ? "¥" : "$"
  if (Math.abs(amount) >= 1) return `${prefix}${amount.toFixed(2)}`
  return `${prefix}${amount.toFixed(3)}`
}

/**
 * Compose the aria-label / tooltip text. Includes concrete numbers
 * (balance, rate-limit remaining) so screen-reader users get the
 * quantitative signal, not just a colour — Z.4 a11y row requires this.
 */
export function describeProviderStatus(
  props: ProviderStatusBadgeProps,
  result: ProviderStatusResult,
): string {
  const parts: string[] = [`${props.provider}:`]
  if (result.tier === "gray" && props.status === "unsupported") {
    parts.push(
      props.reason ||
        "provider does not expose a public balance API — see provider dashboard",
    )
    return parts.join(" ")
  }
  if (result.tier === "gray" && result.reasons[0] === "loading") {
    parts.push("loading balance + rate-limit…")
    return parts.join(" ")
  }
  if (result.tier === "gray") {
    parts.push("no balance or rate-limit data available")
    return parts.join(" ")
  }
  const tierLabel = result.tier.toUpperCase()
  parts.push(tierLabel + ".")
  if (
    props.balanceRemaining !== null &&
    props.balanceRemaining !== undefined
  ) {
    if (
      props.grantedTotal !== null &&
      props.grantedTotal !== undefined &&
      props.grantedTotal > 0
    ) {
      parts.push(
        `balance ${formatCurrency(props.balanceRemaining, props.currency)} / ${formatCurrency(props.grantedTotal, props.currency)} (${result.balancePct?.toFixed(1)}%).`,
      )
    } else {
      parts.push(`balance ${formatCurrency(props.balanceRemaining, props.currency)}.`)
    }
  }
  const req = props.rateLimitRemainingRequests
  const tok = props.rateLimitRemainingTokens
  if ((req !== null && req !== undefined) || (tok !== null && tok !== undefined)) {
    const bits: string[] = []
    if (req !== null && req !== undefined) bits.push(`${req} req remaining`)
    if (tok !== null && tok !== undefined) bits.push(`${tok} tokens remaining`)
    parts.push(`rate-limit: ${bits.join(", ")}.`)
  }
  if (result.reasons.length > 0 && result.tier !== "green") {
    parts.push(`reason: ${result.reasons.join("; ")}.`)
  }
  return parts.join(" ")
}

function tierStyles(tier: ProviderStatusTier): {
  bg: string
  fg: string
  Icon: typeof CheckCircle2
} {
  switch (tier) {
    case "green":
      return {
        bg: "color-mix(in srgb, var(--validation-emerald) 20%, transparent)",
        fg: "var(--validation-emerald)",
        Icon: CheckCircle2,
      }
    case "yellow":
      return {
        bg: "color-mix(in srgb, #eab308 20%, transparent)",
        fg: "#eab308",
        Icon: AlertTriangle,
      }
    case "red":
      return {
        bg: "color-mix(in srgb, var(--critical-red) 20%, transparent)",
        fg: "var(--critical-red)",
        Icon: AlertTriangle,
      }
    case "gray":
    default:
      return {
        bg: "color-mix(in srgb, var(--muted-foreground) 15%, transparent)",
        fg: "var(--muted-foreground)",
        Icon: HelpCircle,
      }
  }
}

export function ProviderStatusBadge(props: ProviderStatusBadgeProps) {
  const result = computeProviderStatus(props)
  const label = describeProviderStatus(props, result)
  const { bg, fg, Icon } = tierStyles(result.tier)
  const isUnsupported = props.status === "unsupported"
  const shouldPulse = result.tier === "red"

  return (
    <span
      role="status"
      aria-label={label}
      title={label}
      data-testid="provider-status-badge"
      data-provider={props.provider}
      data-tier={result.tier}
      data-unsupported={isUnsupported ? "true" : undefined}
      className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded font-mono text-[10px] font-semibold leading-none ${props.className ?? ""}`}
      style={{ backgroundColor: bg, color: fg }}
    >
      <Icon
        size={10}
        className={shouldPulse ? "animate-pulse" : ""}
        aria-hidden="true"
      />
      {isUnsupported ? (
        <span data-testid="provider-status-badge-unsupported-label">
          N/A
        </span>
      ) : result.tier === "gray" ? (
        <span data-testid="provider-status-badge-loading-label">—</span>
      ) : (
        <Circle
          size={6}
          fill={fg}
          stroke="none"
          aria-hidden="true"
          data-testid="provider-status-badge-dot"
        />
      )}
    </span>
  )
}
