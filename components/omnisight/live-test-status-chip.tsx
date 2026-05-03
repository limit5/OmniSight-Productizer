"use client"

/**
 * Z.7.7 — "Last live-test pass: Xh ago" chip for the Z provider
 * observability section inside <TokenUsageStats>.
 *
 * Polls GET /runtime/live-test-status every 5 minutes (300 s) — the
 * nightly CI run fires at 06:00 UTC so a 5-minute polling cadence is
 * fast enough to surface a new result within one polling window while
 * not hammering the endpoint.
 *
 * Four visible states:
 *   green  — status "pass", shows "Last live-test pass: Xh ago"
 *   red    — status "fail", shows "Last live-test FAIL: Xh ago"
 *   gray   — status "never_run" or "unknown", shows "Live tests: never run"
 *   yellow — loading / polling error, shows "Live tests: loading…"
 *
 * Scope discipline — this component is ONLY the chip. The nightly
 * workflow (Z.7.7), budget guard (Z.7.9), and failure escalation
 * (Z.7.8) are separate deliverables.
 *
 * Module-global audit (SOP Step 1):
 *   No module-global state. The polling interval is owned by this
 *   component's useEffect and cleaned up on unmount (qualified answer
 *   #3 — per-instance, intentionally not shared, since two mounted
 *   chips would make two reads, which is harmless for an observability
 *   chip). In practice only one instance mounts (inside TokenUsageStats).
 */

import { useEffect, useState } from "react"
import { CheckCircle2, XCircle, HelpCircle, Loader2 } from "lucide-react"
import { fetchLiveTestStatus, type LiveTestStatusResponse } from "@/lib/api"

const POLL_INTERVAL_MS = 5 * 60 * 1_000  // 5 minutes

/** Format an ISO-8601 timestamp as a human-readable "Xh ago" / "Xm ago" string. */
function _ago(iso: string | null): string {
  if (!iso) return "unknown time"
  const ts = Date.parse(iso)
  if (!Number.isFinite(ts)) return "unknown time"
  const diff = (Date.now() - ts) / 1_000  // seconds
  if (diff < 120) return "just now"
  if (diff < 3_600) return `${Math.round(diff / 60)}m ago`
  if (diff < 86_400) return `${Math.round(diff / 3_600)}h ago`
  return `${Math.round(diff / 86_400)}d ago`
}

export interface LiveTestStatusChipProps {
  className?: string
}

export function LiveTestStatusChip({ className = "" }: LiveTestStatusChipProps) {
  const [data, setData] = useState<LiveTestStatusResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(false)

  useEffect(() => {
    let cancelled = false

    const load = async () => {
      try {
        const resp = await fetchLiveTestStatus()
        if (!cancelled) {
          setData(resp)
          setError(false)
          setLoading(false)
        }
      } catch {
        if (!cancelled) {
          setError(true)
          setLoading(false)
        }
      }
    }

    load()
    const interval = setInterval(load, POLL_INTERVAL_MS)
    return () => {
      cancelled = true
      clearInterval(interval)
    }
  }, [])

  if (loading) {
    return (
      <div
        className={`flex items-center gap-2 px-4 py-2 ${className}`}
        data-testid="live-test-status-chip"
        data-status="loading"
      >
        <Loader2 size={11} className="text-[var(--muted-foreground)] animate-spin shrink-0" />
        <span className="font-mono text-[10px] text-[var(--muted-foreground)]">
          Live tests: loading…
        </span>
      </div>
    )
  }

  if (error || !data) {
    return (
      <div
        className={`flex items-center gap-2 px-4 py-2 ${className}`}
        data-testid="live-test-status-chip"
        data-status="error"
        title="Could not fetch live test status from the backend"
      >
        <HelpCircle size={11} className="text-[var(--muted-foreground)] shrink-0" />
        <span className="font-mono text-[10px] text-[var(--muted-foreground)]">
          Live tests: unavailable
        </span>
      </div>
    )
  }

  const status = data.status

  if (status === "never_run" || status === "unknown") {
    return (
      <div
        className={`flex items-center gap-2 px-4 py-2 ${className}`}
        data-testid="live-test-status-chip"
        data-status="never_run"
        title="Nightly live integration tests have not run yet. They run at 06:00 UTC (14:00 Asia/Taipei)."
      >
        <HelpCircle size={11} className="text-[var(--muted-foreground)] shrink-0" />
        <span className="font-mono text-[10px] text-[var(--muted-foreground)]">
          Live tests: never run
        </span>
      </div>
    )
  }

  const isPass = status === "pass"
  const ago = _ago(data.timestamp)
  const costStr = data.estimated_cost_usd != null
    ? ` · est. $${data.estimated_cost_usd.toFixed(4)}`
    : ""
  const testsStr = data.tests_run != null
    ? ` · ${data.tests_passed ?? 0}/${data.tests_run} tests`
    : ""
  const tooltip = isPass
    ? `Nightly LLM live tests passed ${ago}${testsStr}${costStr}. Run: ${data.run_id ?? "—"}`
    : `Nightly LLM live tests FAILED ${ago}${testsStr}. Run: ${data.run_id ?? "—"}. Check GitHub Actions for details.`

  return (
    <div
      className={`flex items-center gap-2 px-4 py-2 ${className}`}
      data-testid="live-test-status-chip"
      data-status={status}
      title={tooltip}
      aria-label={tooltip}
    >
      {isPass ? (
        <CheckCircle2 size={11} className="text-[var(--validation-emerald)] shrink-0" />
      ) : (
        <XCircle size={11} className="text-[var(--critical-red)] shrink-0" />
      )}
      <span
        className={`font-mono text-[10px] font-medium ${
          isPass ? "text-[var(--validation-emerald)]" : "text-[var(--critical-red)]"
        }`}
      >
        {isPass ? "Last live-test pass:" : "Last live-test FAIL:"}
      </span>
      <span className="font-mono text-[10px] text-[var(--muted-foreground)]">
        {ago}
      </span>
      {data.tests_run != null && (
        <span className="font-mono text-[9px] text-[var(--muted-foreground)]/70 hidden sm:inline">
          ({data.tests_passed ?? 0}/{data.tests_run})
        </span>
      )}
    </div>
  )
}
