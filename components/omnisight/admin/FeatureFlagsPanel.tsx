"use client"

/**
 * OP-884 D12 -- operator feature-flag panel.
 *
 * Drop-in admin component for the operator dashboard. Renders the
 * registry rows the WP.7.8 ``/feature-flags`` endpoint returns and
 * exposes the OP-884 controls (state toggle, rollout_pct slider,
 * allowed_tenants editor) behind the existing admin-only PATCH route.
 *
 * Auth gating
 * -----------
 * The backend remains authoritative: GET is read-everyone, PATCH is
 * admin-only. ``can_toggle`` from the list response gates the controls;
 * lower roles see the same rows with disabled inputs.
 *
 * Module-global state audit
 * -------------------------
 * No mutable module-global state. The component holds per-render
 * React state and delegates network IO to the typed ``lib/api.ts``
 * wrappers; cross-worker cache coherence after a PATCH is handled
 * server-side by ``publish_feature_flags_invalidate()``.
 */

import { useCallback, useEffect, useMemo, useState } from "react"
import {
  Check,
  CircleAlert,
  Flag,
  Loader2,
  RefreshCw,
  Save,
  ShieldAlert,
  X,
} from "lucide-react"
import {
  ApiError,
  listFeatureFlags,
  patchFeatureFlag,
  type FeatureFlagRow,
} from "@/lib/api"

const TIER_LABELS: Record<string, string> = {
  debug: "DEBUG",
  dogfood: "DOGFOOD",
  preview: "PREVIEW",
  release: "RELEASE",
  runtime: "RUNTIME",
}

function clampPct(value: number): number {
  if (Number.isNaN(value)) return 0
  if (value < 0) return 0
  if (value > 100) return 100
  return Math.round(value)
}

function parseTenantList(raw: string): string[] {
  return raw
    .split(/[\s,]+/)
    .map((s) => s.trim())
    .filter(Boolean)
}

function formatTenantList(values: string[] | undefined): string {
  if (!values || values.length === 0) return ""
  return values.join(", ")
}

interface RowDraft {
  rollout_pct: number
  allowed_tenants_text: string
}

interface FeatureFlagsPanelProps {
  /** Optional title override -- defaults to "Feature Flags". */
  title?: string
  /** If true, the panel renders even when the user lacks admin role
   *  but every control is disabled. Defaults to true so the same
   *  component is usable as a read-only inspector. */
  showReadOnly?: boolean
}

export function FeatureFlagsPanel({
  title = "Feature Flags",
  showReadOnly = true,
}: FeatureFlagsPanelProps = {}) {
  const [rows, setRows] = useState<FeatureFlagRow[]>([])
  const [canToggle, setCanToggle] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [busyFlagName, setBusyFlagName] = useState<string | null>(null)
  const [rowError, setRowError] = useState<{
    flagName: string
    message: string
  } | null>(null)
  const [drafts, setDrafts] = useState<Record<string, RowDraft>>({})

  const refresh = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await listFeatureFlags()
      setRows(res.feature_flags)
      setCanToggle(res.can_toggle)
      const initialDrafts: Record<string, RowDraft> = {}
      for (const row of res.feature_flags) {
        initialDrafts[row.flag_name] = {
          rollout_pct: row.rollout_pct ?? 100,
          allowed_tenants_text: formatTenantList(row.allowed_tenants),
        }
      }
      setDrafts(initialDrafts)
    } catch (exc) {
      const msg = exc instanceof Error ? exc.message : String(exc)
      setError(msg)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const applyPatch = useCallback(
    async (
      row: FeatureFlagRow,
      patch: {
        state?: "enabled" | "disabled"
        rollout_pct?: number
        allowed_tenants?: string[]
      },
    ) => {
      setBusyFlagName(row.flag_name)
      setRowError(null)
      try {
        const res = await patchFeatureFlag(row.flag_name, patch)
        setRows((current) =>
          current.map((r) =>
            r.flag_name === row.flag_name ? res.feature_flag : r,
          ),
        )
        setDrafts((current) => ({
          ...current,
          [row.flag_name]: {
            rollout_pct: res.feature_flag.rollout_pct ?? 100,
            allowed_tenants_text: formatTenantList(
              res.feature_flag.allowed_tenants,
            ),
          },
        }))
      } catch (exc) {
        const detail =
          exc instanceof ApiError
            ? (exc.parsed as { detail?: string } | null)?.detail ?? exc.body
            : exc instanceof Error
              ? exc.message
              : String(exc)
        setRowError({
          flagName: row.flag_name,
          message: detail || "update failed",
        })
      } finally {
        setBusyFlagName(null)
      }
    },
    [],
  )

  const onToggleState = useCallback(
    (row: FeatureFlagRow) => {
      const next = row.state === "enabled" ? "disabled" : "enabled"
      void applyPatch(row, { state: next })
    },
    [applyPatch],
  )

  const onSaveRollout = useCallback(
    (row: FeatureFlagRow) => {
      const draft = drafts[row.flag_name]
      if (!draft) return
      const rollout_pct = clampPct(draft.rollout_pct)
      const allowed_tenants = parseTenantList(draft.allowed_tenants_text)
      void applyPatch(row, { rollout_pct, allowed_tenants })
    },
    [applyPatch, drafts],
  )

  const onPctChange = useCallback((flagName: string, value: number) => {
    setDrafts((current) => ({
      ...current,
      [flagName]: {
        rollout_pct: clampPct(value),
        allowed_tenants_text: current[flagName]?.allowed_tenants_text ?? "",
      },
    }))
  }, [])

  const onAllowedChange = useCallback((flagName: string, value: string) => {
    setDrafts((current) => ({
      ...current,
      [flagName]: {
        rollout_pct: current[flagName]?.rollout_pct ?? 100,
        allowed_tenants_text: value,
      },
    }))
  }, [])

  const dirty = useMemo(() => {
    const out: Record<string, boolean> = {}
    for (const row of rows) {
      const draft = drafts[row.flag_name]
      if (!draft) {
        out[row.flag_name] = false
        continue
      }
      const draftAllowed = parseTenantList(draft.allowed_tenants_text)
      const currentAllowed = row.allowed_tenants ?? []
      out[row.flag_name] =
        clampPct(draft.rollout_pct) !== (row.rollout_pct ?? 100) ||
        draftAllowed.length !== currentAllowed.length ||
        draftAllowed.some((t, i) => t !== currentAllowed[i])
    }
    return out
  }, [rows, drafts])

  if (!showReadOnly && !canToggle) {
    return null
  }

  return (
    <section
      className="rounded border border-[var(--border)] bg-[var(--card)]"
      data-testid="feature-flags-panel"
    >
      <header className="flex items-center justify-between px-4 py-3 border-b border-[var(--border)]">
        <h2 className="text-sm font-semibold flex items-center gap-2">
          <Flag size={14} />
          {title}
        </h2>
        <button
          type="button"
          onClick={() => void refresh()}
          disabled={loading}
          className="inline-flex items-center gap-1 px-2 py-1 rounded border border-[var(--border)] hover:bg-[var(--secondary)]/40 text-[10px] font-mono disabled:opacity-50"
          data-testid="feature-flags-panel-refresh"
        >
          <RefreshCw size={10} className={loading ? "animate-spin" : ""} />
          Refresh
        </button>
      </header>

      {!canToggle && (
        <div
          className="px-4 py-2 border-b border-[var(--border)] text-[10px] font-mono text-[var(--muted-foreground)] flex items-center gap-2"
          data-testid="feature-flags-panel-readonly"
        >
          <ShieldAlert size={10} />
          Read-only. Admin role required to flip flags or adjust rollout.
        </div>
      )}

      {error && (
        <div
          className="px-4 py-2 border-b border-[var(--destructive)]/40 bg-[var(--destructive)]/10 text-[10px] font-mono text-[var(--destructive)] flex items-center gap-2"
          data-testid="feature-flags-panel-error"
        >
          <CircleAlert size={10} />
          Failed to load: {error}
        </div>
      )}

      <div className="overflow-x-auto">
        <table className="w-full font-mono text-xs">
          <thead>
            <tr className="border-b border-[var(--border)] text-[10px] text-[var(--muted-foreground)]">
              <th className="text-left px-3 py-2">flag</th>
              <th className="text-left px-3 py-2">tier</th>
              <th className="text-left px-3 py-2">state</th>
              <th className="text-left px-3 py-2 w-44">rollout %</th>
              <th className="text-left px-3 py-2">allowed tenants</th>
              <th className="text-right px-3 py-2">actions</th>
            </tr>
          </thead>
          <tbody>
            {loading && (
              <tr>
                <td
                  colSpan={6}
                  className="text-center py-6 text-[var(--muted-foreground)]"
                  data-testid="feature-flags-panel-loading"
                >
                  <Loader2 size={12} className="animate-spin inline-block mr-2" />
                  Loading…
                </td>
              </tr>
            )}
            {!loading && rows.length === 0 && !error && (
              <tr>
                <td
                  colSpan={6}
                  className="text-center py-6 text-[var(--muted-foreground)]"
                  data-testid="feature-flags-panel-empty"
                >
                  No feature flags registered.
                </td>
              </tr>
            )}
            {!loading &&
              rows.map((row) => {
                const busy = busyFlagName === row.flag_name
                const enabled = row.state === "enabled"
                const draft = drafts[row.flag_name] ?? {
                  rollout_pct: row.rollout_pct ?? 100,
                  allowed_tenants_text: formatTenantList(row.allowed_tenants),
                }
                const isError = rowError?.flagName === row.flag_name
                const rowDirty = dirty[row.flag_name]
                return (
                  <tr
                    key={row.flag_name}
                    className="border-b border-[var(--border)] last:border-b-0 hover:bg-[var(--secondary)]/20 align-top"
                    data-testid={`feature-flag-row-${row.flag_name}`}
                  >
                    <td className="px-3 py-2 font-semibold whitespace-nowrap">
                      {row.flag_name}
                    </td>
                    <td className="px-3 py-2">
                      <span className="inline-flex px-1.5 py-0.5 rounded bg-[var(--secondary)]/40 text-[10px]">
                        {TIER_LABELS[row.tier] ?? row.tier}
                      </span>
                    </td>
                    <td className="px-3 py-2">
                      <button
                        type="button"
                        disabled={!canToggle || busy}
                        onClick={() => onToggleState(row)}
                        aria-label={
                          enabled
                            ? `Disable feature flag ${row.flag_name}`
                            : `Enable feature flag ${row.flag_name}`
                        }
                        className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-[10px] ${
                          enabled
                            ? "bg-[var(--neural-green)]/15 text-[var(--neural-green)]"
                            : "bg-[var(--muted)]/40 text-[var(--muted-foreground)]"
                        } disabled:opacity-50`}
                        data-testid={`feature-flag-toggle-${row.flag_name}`}
                      >
                        {enabled ? <Check size={10} /> : <X size={10} />}
                        {row.state}
                      </button>
                    </td>
                    <td className="px-3 py-2">
                      <div className="flex items-center gap-2">
                        <input
                          type="range"
                          min={0}
                          max={100}
                          step={1}
                          value={draft.rollout_pct}
                          disabled={!canToggle || busy}
                          onChange={(e) =>
                            onPctChange(
                              row.flag_name,
                              Number(e.currentTarget.value),
                            )
                          }
                          className="w-24"
                          aria-label={`Rollout percent for ${row.flag_name}`}
                          data-testid={`feature-flag-rollout-${row.flag_name}`}
                        />
                        <input
                          type="number"
                          min={0}
                          max={100}
                          value={draft.rollout_pct}
                          disabled={!canToggle || busy}
                          onChange={(e) =>
                            onPctChange(
                              row.flag_name,
                              Number(e.currentTarget.value),
                            )
                          }
                          className="w-12 px-1 py-0.5 rounded border border-[var(--border)] bg-[var(--background)] text-[10px]"
                          data-testid={`feature-flag-rollout-pct-${row.flag_name}`}
                        />
                        <span className="text-[10px] text-[var(--muted-foreground)]">
                          %
                        </span>
                      </div>
                    </td>
                    <td className="px-3 py-2">
                      <input
                        type="text"
                        placeholder="(empty = use rollout %)"
                        value={draft.allowed_tenants_text}
                        disabled={!canToggle || busy}
                        onChange={(e) =>
                          onAllowedChange(row.flag_name, e.currentTarget.value)
                        }
                        className="w-full px-2 py-0.5 rounded border border-[var(--border)] bg-[var(--background)] text-[10px] font-mono"
                        aria-label={`Allowed tenants for ${row.flag_name}`}
                        data-testid={`feature-flag-allowed-${row.flag_name}`}
                      />
                    </td>
                    <td className="px-3 py-2 text-right">
                      <button
                        type="button"
                        disabled={!canToggle || busy || !rowDirty}
                        onClick={() => onSaveRollout(row)}
                        className="inline-flex items-center gap-1 px-2 py-0.5 rounded border border-[var(--border)] hover:bg-[var(--secondary)]/40 text-[10px] disabled:opacity-50"
                        data-testid={`feature-flag-save-${row.flag_name}`}
                      >
                        {busy ? (
                          <Loader2 size={10} className="animate-spin" />
                        ) : (
                          <Save size={10} />
                        )}
                        Save
                      </button>
                      {isError && (
                        <div
                          className="text-[10px] text-[var(--destructive)] mt-1"
                          data-testid={`feature-flag-row-error-${row.flag_name}`}
                        >
                          {rowError?.message}
                        </div>
                      )}
                    </td>
                  </tr>
                )
              })}
          </tbody>
        </table>
      </div>
    </section>
  )
}

export default FeatureFlagsPanel
