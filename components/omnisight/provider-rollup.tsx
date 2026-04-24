"use client"

/**
 * Z.4 (#293) checkbox 3 — provider-level roll-up.
 *
 * `TokenUsageStats` used to render one flat card per model; with 9+
 * configured providers and 2-3 models each the list scrolled off-screen
 * before the operator could read it. This component groups the per-model
 * rows by provider, shows a one-line summary (aggregated tokens +
 * aggregated cost + request count), and reveals the original per-model
 * cards only when the operator clicks the summary row open.
 *
 * Scope discipline — this checkbox is ONLY the grouping shell:
 *   * OpenRouter namespace special-case (`anthropic/claude-sonnet-4`
 *     surfaced under "OpenRouter" instead of "Anthropic") is checkbox 4.
 *   * Mounting <ProviderStatusBadge> (checkbox 1) and <ProviderCardExpansion>
 *     (checkbox 2) in the summary row wants live balance + rate-limit
 *     props, which come from checkbox 5 (`useEngine` 60 s poll of
 *     `/runtime/providers/balance`). The component accepts optional
 *     `providerMeta` + `renderStatusBadge` / `renderExpansion` props so
 *     checkbox 5 can wire both without a refactor; when absent we just
 *     render the aggregated totals (the value this row delivers on its
 *     own).
 *   * Playwright visual regression is checkbox 7.
 *
 * The component tracks per-provider expanded state locally (`useState`).
 * This is genuine client-only UI state — not a server-derived cache —
 * so the SOP module-global audit answer is #3 ("intentionally per-
 * instance, not shared"), same as every other dashboard collapse/expand
 * in the panel (budget settings, heatmap section, per-model detail row).
 */

import { useState } from "react"
import { ChevronDown, ChevronRight } from "lucide-react"

export interface ProviderRollupRow {
  model: string
  inputTokens: number
  outputTokens: number
  totalTokens: number
  cost: number
  requestCount: number
}

export interface ProviderTotals {
  inputTokens: number
  outputTokens: number
  totalTokens: number
  cost: number
  requestCount: number
}

export interface ProviderGroup<T extends ProviderRollupRow> {
  /** Lowercased stable key — dedup between rollup entries + data-testid. */
  providerKey: string
  /** Human-readable label as surfaced by `getModelInfo(model).provider`
   *  (e.g. "Anthropic", "Google", "Unknown"). */
  providerLabel: string
  /** Dominant model colour for the group header bar. First row's colour
   *  is used so the summary looks visually consistent with the per-model
   *  rows it owns. */
  color: string
  totals: ProviderTotals
  rows: T[]
}

export interface ProviderResolver {
  (model: string): { provider: string; color: string }
}

const UNKNOWN_PROVIDER_LABEL = "Unknown"

/** Z.4 (#293) checkbox 4 — OpenRouter special case.
 *
 *  OpenRouter routes requests to upstream vendors using the
 *  ``<namespace>/<model>`` convention (``anthropic/claude-sonnet-4``,
 *  ``google/gemini-1.5-pro``, ``qwen/qwen3-235b``,
 *  ``nvidia/llama-3.1-nemotron-ultra-253b``). The base resolver
 *  (``getModelInfo``) strips the namespace for display and looks up the
 *  inner vendor, so `anthropic/claude-sonnet-4` resolves to
 *  ``provider: "Anthropic"`` — which is correct for the *upstream*
 *  model but wrong for *billing/quota tracking*: the credentials + rate
 *  limits + balance belong to the OpenRouter account, not Anthropic.
 *
 *  This wrapper detects slash-namespaced model names and overrides
 *  the resolved provider to ``OpenRouter`` with the canonical purple
 *  swatch (matches ``agent-matrix-wall.tsx::PROVIDER_COLORS``), so the
 *  roll-up summary groups every routed call under OpenRouter. Per-row
 *  cards inside the group still surface the base model — ``renderRow``
 *  receives the untouched row (``row.model === "anthropic/claude-
 *  sonnet-4"``) and ``getModelInfo`` keeps its namespace-strip logic,
 *  so the chip reads ``Sonnet`` / full ``anthropic/claude-sonnet-4``
 *  label — satisfying the spec's "sub-label 顯示實際 base model".
 */
export const OPENROUTER_PROVIDER_LABEL = "OpenRouter"
export const OPENROUTER_PROVIDER_COLOR = "#a855f7"

/** `true` iff the model string follows the OpenRouter-style
 *  ``<namespace>/<model>`` convention: a slash that is neither the
 *  first nor the last character. Matches the exact predicate used by
 *  `getModelInfo` when it strips namespaces for display, so the two
 *  stay consistent — a string treated as namespaced for display is
 *  also treated as namespaced for bucketing.
 *
 *  Examples: `"anthropic/claude-sonnet-4"` → true;
 *            `"claude-sonnet-4"`            → false;
 *            `"/foo"` / `"foo/"` / `""`     → false (malformed). */
export function isOpenRouterModel(model: string): boolean {
  if (!model) return false
  const slashIdx = model.indexOf("/")
  return slashIdx > 0 && slashIdx < model.length - 1
}

/** Wraps a base resolver so slash-namespaced models bucket under the
 *  synthetic ``OpenRouter`` provider. Exported so `TokenUsageStats`
 *  can share the exact resolver contract with the Z.5 regression
 *  matrix + future callers. */
export function openRouterAwareResolver(
  base: ProviderResolver,
): ProviderResolver {
  return (model) => {
    if (isOpenRouterModel(model)) {
      return {
        provider: OPENROUTER_PROVIDER_LABEL,
        color: OPENROUTER_PROVIDER_COLOR,
      }
    }
    return base(model)
  }
}

/**
 * Pure helper — groups per-model rows by their resolved provider name.
 * Exported so contract tests + Z.5 regression matrix can lock the
 * grouping contract without rendering JSX.
 *
 * Ordering:
 *   * Groups are returned sorted by aggregated `totalTokens` DESC, so
 *     the most-active provider appears first (matches the per-model
 *     sort the flat list used).
 *   * Rows within each group keep their input order (caller typically
 *     sorts upstream — `TokenUsageStats` sorts by `totalTokens` DESC +
 *     appends placeholder rows).
 *
 * Empty-provider handling: `resolve(model).provider === ""` (unknown
 * vendor models like `my-custom-model`) get bucketed under
 * "Unknown" rather than dropped, so the operator still sees them —
 * this matches the existing flat-list behaviour that rendered all rows
 * regardless of provider resolution.
 */
export function groupByProvider<T extends ProviderRollupRow>(
  rows: T[],
  resolve: ProviderResolver,
): ProviderGroup<T>[] {
  const byKey = new Map<string, ProviderGroup<T>>()
  for (const row of rows) {
    const info = resolve(row.model)
    const label = info.provider?.trim() || UNKNOWN_PROVIDER_LABEL
    const key = label.toLowerCase()
    let group = byKey.get(key)
    if (!group) {
      group = {
        providerKey: key,
        providerLabel: label,
        color: info.color || "#737373",
        totals: {
          inputTokens: 0,
          outputTokens: 0,
          totalTokens: 0,
          cost: 0,
          requestCount: 0,
        },
        rows: [],
      }
      byKey.set(key, group)
    }
    group.totals.inputTokens += row.inputTokens
    group.totals.outputTokens += row.outputTokens
    group.totals.totalTokens += row.totalTokens
    group.totals.cost += row.cost
    group.totals.requestCount += row.requestCount
    group.rows.push(row)
  }
  return [...byKey.values()].sort(
    (a, b) => b.totals.totalTokens - a.totals.totalTokens,
  )
}

function formatTokens(num: number): string {
  if (num >= 1_000_000) return (num / 1_000_000).toFixed(2) + "M"
  if (num >= 1_000) return (num / 1_000).toFixed(1) + "K"
  return num.toString()
}

function formatCost(cost: number): string {
  if (cost >= 1) return "$" + cost.toFixed(2)
  return "$" + cost.toFixed(3)
}

export interface ProviderRollupProps<T extends ProviderRollupRow> {
  groups: ProviderGroup<T>[]
  /** Grand-total tokens across every group, for the "% of total usage"
   *  line in each summary row. Computed upstream so the rollup does not
   *  silently double-sum (the caller already has the number on hand
   *  for the TOKEN USAGE header). */
  grandTotalTokens: number
  /** Render function for each per-model row inside an expanded group.
   *  The rollup owns the outer `<li>` wrapping but the caller keeps
   *  control of the model-card layout so this checkbox does not have
   *  to re-implement cache / context / turn-stats rows. */
  renderRow: (row: T) => React.ReactNode
  /** Default: all groups start collapsed (summary-only view — this is
   *  the point of the roll-up). Set `true` to preserve the pre-rollup
   *  "everything expanded" behaviour; used by the Z.5 screenshot matrix
   *  so each provider's per-model rows show in the mixed-state capture. */
  defaultExpanded?: boolean
  /** Optional — when checkbox 5 wires the balance + rate-limit poll,
   *  pass a render function for the provider-level status badge here
   *  so the summary row mounts <ProviderStatusBadge /> inline. Absent
   *  = no badge column (what this checkbox does by itself). */
  renderStatusBadge?: (providerKey: string) => React.ReactNode
  /** Optional — same pattern for the <ProviderCardExpansion /> block.
   *  When supplied, the rollup renders it BEFORE the per-model rows
   *  inside the expanded panel so operators see balance + rate-limit
   *  at the top of the group. */
  renderExpansion?: (providerKey: string) => React.ReactNode
  className?: string
}

export function ProviderRollup<T extends ProviderRollupRow>({
  groups,
  grandTotalTokens,
  renderRow,
  defaultExpanded = false,
  renderStatusBadge,
  renderExpansion,
  className = "",
}: ProviderRollupProps<T>) {
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})

  const isExpanded = (key: string): boolean =>
    key in expanded ? expanded[key] : defaultExpanded

  const toggle = (key: string) => {
    setExpanded((prev) => ({
      ...prev,
      [key]: !(key in prev ? prev[key] : defaultExpanded),
    }))
  }

  return (
    <ul
      className={`space-y-2 list-none p-0 m-0 ${className}`}
      data-testid="provider-rollup"
    >
      {groups.map((group) => {
        const open = isExpanded(group.providerKey)
        const pct =
          grandTotalTokens > 0
            ? (group.totals.totalTokens / grandTotalTokens) * 100
            : 0
        const summaryAriaLabel =
          `${group.providerLabel}: ${group.rows.length} model${
            group.rows.length === 1 ? "" : "s"
          }, ${formatTokens(group.totals.totalTokens)} tokens, ` +
          `${formatCost(group.totals.cost)}, ${group.totals.requestCount} requests`
        return (
          <li
            key={group.providerKey}
            data-testid={`provider-rollup-group-${group.providerKey}`}
            data-provider-key={group.providerKey}
            data-expanded={open ? "true" : "false"}
          >
            <button
              type="button"
              onClick={() => toggle(group.providerKey)}
              aria-expanded={open}
              aria-label={summaryAriaLabel}
              className={`w-full text-left p-3 rounded-lg transition-all bg-[var(--secondary)] hover:bg-[var(--secondary)]/80 flex items-center gap-2`}
              data-testid={`provider-rollup-summary-${group.providerKey}`}
            >
              <span
                className="shrink-0 text-[var(--muted-foreground)]"
                aria-hidden="true"
                data-testid={`provider-rollup-chevron-${group.providerKey}`}
              >
                {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
              </span>
              <span
                className="w-3 h-3 rounded-full shrink-0"
                style={{ backgroundColor: group.color }}
                aria-hidden="true"
              />
              <span
                className="font-mono text-xs font-semibold text-[var(--foreground)] min-w-0 truncate"
                data-testid={`provider-rollup-label-${group.providerKey}`}
              >
                {group.providerLabel}
              </span>
              <span
                className="font-mono text-[10px] text-[var(--muted-foreground)] shrink-0"
                data-testid={`provider-rollup-model-count-${group.providerKey}`}
              >
                {group.rows.length} {group.rows.length === 1 ? "model" : "models"}
              </span>
              {renderStatusBadge ? (
                <span
                  className="shrink-0 ml-1"
                  data-testid={`provider-rollup-status-slot-${group.providerKey}`}
                  onClickCapture={(e) => {
                    // Keep badge-hosted click handlers (tooltip triggers
                    // etc.) from toggling the summary row. The badge
                    // itself is non-interactive presentation today, but
                    // checkbox 5 may later turn the badge into a link to
                    // the provider card — guard early so that change
                    // doesn't silently break the toggle.
                    e.stopPropagation()
                  }}
                >
                  {renderStatusBadge(group.providerKey)}
                </span>
              ) : null}
              <span className="ml-auto flex items-center gap-3 shrink-0">
                <span
                  className="font-mono text-[11px] text-[var(--validation-emerald)]"
                  data-testid={`provider-rollup-tokens-${group.providerKey}`}
                >
                  {formatTokens(group.totals.totalTokens)} tokens
                </span>
                <span
                  className="font-mono text-[11px] font-semibold text-[var(--hardware-orange)]"
                  data-testid={`provider-rollup-cost-${group.providerKey}`}
                >
                  {formatCost(group.totals.cost)}
                </span>
                <span
                  className="font-mono text-[10px] text-[var(--muted-foreground)] w-14 text-right"
                  data-testid={`provider-rollup-pct-${group.providerKey}`}
                >
                  {pct.toFixed(1)}%
                </span>
              </span>
            </button>
            {open ? (
              <div
                className="mt-2 ml-6 space-y-2"
                data-testid={`provider-rollup-body-${group.providerKey}`}
              >
                {renderExpansion ? renderExpansion(group.providerKey) : null}
                {group.rows.map((row) => (
                  <div key={row.model}>{renderRow(row)}</div>
                ))}
              </div>
            ) : null}
          </li>
        )
      })}
    </ul>
  )
}
