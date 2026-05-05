"use client"

/**
 * WP.7.8 -- operator feature flag registry page.
 *
 * Auth gating
 * -----------
 * All authenticated roles may inspect the registry. Admin and
 * super_admin roles may toggle global state; lower roles render the
 * same rows with disabled controls. The backend remains authoritative:
 * GET /feature-flags uses current_user, PATCH /feature-flags/{name}
 * uses require_admin and writes the N10 audit row.
 *
 * Module-global state audit
 * -------------------------
 * None introduced. The page keeps per-component React state only and
 * calls typed `lib/api.ts` wrappers. Cross-worker cache coherence after
 * toggles is handled server-side by WP.7.4 Redis invalidation.
 */

import { useCallback, useEffect, useMemo, useState } from "react"
import Link from "next/link"
import {
  ArrowLeft,
  Check,
  ChevronRight,
  CircleAlert,
  Flag,
  Loader2,
  RefreshCw,
  ShieldAlert,
  ToggleLeft,
  ToggleRight,
  X,
} from "lucide-react"
import {
  ApiError,
  listFeatureFlags,
  patchFeatureFlag,
  type FeatureFlagRow,
  type FeatureFlagState,
  type FeatureFlagTier,
} from "@/lib/api"
import { useAuth } from "@/lib/auth-context"

const TIER_LABELS: Record<FeatureFlagTier, string> = {
  debug: "DEBUG",
  dogfood: "DOGFOOD",
  preview: "PREVIEW",
  release: "RELEASE",
  runtime: "RUNTIME",
}

const ROLE_ORDER = ["viewer", "operator", "admin", "super_admin"]

function roleAtLeast(role: string | undefined, minRole: string): boolean {
  const have = role ? ROLE_ORDER.indexOf(role) : -1
  const need = ROLE_ORDER.indexOf(minRole)
  return have >= 0 && need >= 0 && have >= need
}

function formatExpiry(value: string | null): string {
  if (!value) return "none"
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return value
  return d.toISOString().slice(0, 10)
}

export default function AdminFeatureFlagsPage() {
  const { user, authMode, loading: authLoading } = useAuth()
  const [rows, setRows] = useState<FeatureFlagRow[]>([])
  const [canToggleFromServer, setCanToggleFromServer] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [busyFlagName, setBusyFlagName] = useState<string | null>(null)
  const [rowError, setRowError] = useState<{ flagName: string; message: string } | null>(null)

  const canToggle = useMemo(() => {
    if (authMode === "open") return true
    return canToggleFromServer && roleAtLeast(user?.role, "admin")
  }, [authMode, canToggleFromServer, user?.role])

  const refresh = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await listFeatureFlags()
      setRows(res.feature_flags)
      setCanToggleFromServer(res.can_toggle)
    } catch (exc) {
      const msg = exc instanceof Error ? exc.message : String(exc)
      setError(msg)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (authLoading) return
    void refresh()
  }, [authLoading, refresh])

  const onToggle = useCallback(
    async (row: FeatureFlagRow) => {
      const nextState: FeatureFlagState =
        row.state === "enabled" ? "disabled" : "enabled"
      setBusyFlagName(row.flag_name)
      setRowError(null)
      try {
        const res = await patchFeatureFlag(row.flag_name, nextState)
        setRows((current) =>
          current.map((r) =>
            r.flag_name === row.flag_name ? res.feature_flag : r,
          ),
        )
      } catch (exc) {
        const detail =
          exc instanceof ApiError
            ? (exc.parsed as { detail?: string } | null)?.detail ?? exc.body
            : exc instanceof Error
              ? exc.message
              : String(exc)
        setRowError({ flagName: row.flag_name, message: detail || "toggle failed" })
      } finally {
        setBusyFlagName(null)
      }
    },
    [],
  )

  if (authLoading) {
    return (
      <main className="min-h-screen flex items-center justify-center bg-[var(--background)] text-[var(--foreground)]">
        <div className="font-mono text-xs text-[var(--muted-foreground)] flex items-center gap-2">
          <Loader2 size={14} className="animate-spin" />
          Verifying operator session...
        </div>
      </main>
    )
  }

  return (
    <main
      className="min-h-screen bg-[var(--background)] text-[var(--foreground)] p-6 md:p-10"
      data-testid="admin-feature-flags-page"
    >
      <div className="max-w-6xl mx-auto">
        <header className="flex items-center justify-between mb-6">
          <div>
            <div className="flex items-center gap-2 text-[10px] font-mono text-[var(--muted-foreground)] mb-1">
              <Link href="/" className="hover:text-[var(--foreground)] inline-flex items-center gap-1">
                <ArrowLeft size={10} /> dashboard
              </Link>
              <ChevronRight size={10} />
              <span>admin</span>
              <ChevronRight size={10} />
              <span className="text-[var(--foreground)]">feature flags</span>
            </div>
            <h1 className="text-xl font-semibold flex items-center gap-2">
              <Flag size={20} />
              Feature Flags
            </h1>
            <p className="text-xs text-[var(--muted-foreground)] mt-1">
              Registry-backed global state. Admin toggles are written to the
              N10 audit chain; viewer and operator roles inspect read-only.
            </p>
          </div>
          <button
            type="button"
            onClick={() => void refresh()}
            disabled={loading}
            className="inline-flex items-center gap-1 px-3 py-1.5 rounded border border-[var(--border)] bg-[var(--card)] hover:bg-[var(--secondary)]/40 text-xs font-mono disabled:opacity-50"
            data-testid="feature-flags-refresh"
          >
            <RefreshCw size={12} className={loading ? "animate-spin" : ""} />
            Refresh
          </button>
        </header>

        {!canToggle && (
          <div
            className="mb-4 rounded border border-[var(--border)] bg-[var(--card)] p-3 text-xs font-mono text-[var(--muted-foreground)] flex items-center gap-2"
            data-testid="feature-flags-readonly"
          >
            <ShieldAlert size={12} />
            <span>Read-only session. Role admin or higher is required to toggle flags.</span>
          </div>
        )}

        {error && (
          <div
            className="mb-4 rounded border border-[var(--destructive)]/40 bg-[var(--destructive)]/10 p-3 text-xs font-mono text-[var(--destructive)] flex items-center gap-2"
            data-testid="feature-flags-error"
          >
            <CircleAlert size={12} />
            <span>Failed to load feature flags: {error}</span>
          </div>
        )}

        <div className="rounded border border-[var(--border)] bg-[var(--card)] overflow-x-auto">
          <table className="w-full font-mono text-xs">
            <thead>
              <tr className="border-b border-[var(--border)] text-[10px] text-[var(--muted-foreground)]">
                <th className="text-left px-3 py-2">flag</th>
                <th className="text-left px-3 py-2">tier</th>
                <th className="text-left px-3 py-2">state</th>
                <th className="text-left px-3 py-2">owner</th>
                <th className="text-left px-3 py-2">expires</th>
                <th className="text-right px-3 py-2">actions</th>
              </tr>
            </thead>
            <tbody>
              {loading && (
                <tr>
                  <td
                    colSpan={6}
                    className="text-center py-8 text-[var(--muted-foreground)]"
                    data-testid="feature-flags-loading"
                  >
                    <Loader2 size={14} className="animate-spin inline-block mr-2" />
                    Loading feature flags...
                  </td>
                </tr>
              )}
              {!loading && rows.length === 0 && !error && (
                <tr>
                  <td
                    colSpan={6}
                    className="text-center py-8 text-[var(--muted-foreground)]"
                    data-testid="feature-flags-empty"
                  >
                    No feature flags registered.
                  </td>
                </tr>
              )}
              {!loading &&
                rows.map((row) => {
                  const busy = busyFlagName === row.flag_name
                  const enabled = row.state === "enabled"
                  const isError = rowError?.flagName === row.flag_name
                  return (
                    <tr
                      key={row.flag_name}
                      className="border-b border-[var(--border)] last:border-b-0 hover:bg-[var(--secondary)]/20"
                      data-testid={`feature-flag-row-${row.flag_name}`}
                    >
                      <td className="px-3 py-2 font-semibold">{row.flag_name}</td>
                      <td className="px-3 py-2">
                        <span className="inline-flex px-1.5 py-0.5 rounded bg-[var(--secondary)]/40 text-[10px]">
                          {TIER_LABELS[row.tier]}
                        </span>
                      </td>
                      <td className="px-3 py-2">
                        <span
                          className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] ${
                            enabled
                              ? "bg-[var(--neural-green)]/15 text-[var(--neural-green)]"
                              : "bg-[var(--muted)]/40 text-[var(--muted-foreground)]"
                          }`}
                          data-testid={`feature-flag-state-${row.flag_name}`}
                        >
                          {enabled ? <Check size={10} /> : <X size={10} />}
                          {row.state}
                        </span>
                      </td>
                      <td className="px-3 py-2">{row.owner || "unowned"}</td>
                      <td className="px-3 py-2">{formatExpiry(row.expires_at)}</td>
                      <td className="px-3 py-2 text-right">
                        <button
                          type="button"
                          disabled={!canToggle || busy}
                          onClick={() => void onToggle(row)}
                          aria-label={
                            enabled
                              ? `Disable feature flag ${row.flag_name}`
                              : `Enable feature flag ${row.flag_name}`
                          }
                          className="inline-flex items-center gap-1 px-2 py-0.5 rounded border border-[var(--border)] hover:bg-[var(--secondary)]/40 text-[10px] disabled:opacity-50"
                          data-testid={`feature-flag-toggle-${row.flag_name}`}
                        >
                          {busy ? (
                            <Loader2 size={10} className="animate-spin" />
                          ) : enabled ? (
                            <ToggleRight size={12} />
                          ) : (
                            <ToggleLeft size={12} />
                          )}
                          {enabled ? "Disable" : "Enable"}
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
      </div>
    </main>
  )
}
