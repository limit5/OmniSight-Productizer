"use client"

/**
 * B13 Part C (#339) — API Error Toast Center.
 *
 * Subscribes to the `onApiError` bus exported by `lib/api.ts` and surfaces
 * a short-lived FUI-styled toast for error kinds that should NOT be a
 * full-page bounce (401 → /login, 503 bootstrap_required → /setup-required
 * already redirect inside `request()` before the bus fires).
 *
 * Rows handled so far:
 *   - row 191: 403 `forbidden`     → warning toast「權限不足」
 *   - row 192: 500 `server_error`  → error toast「系統錯誤」+ expandable
 *                                    region showing the trace ID so the
 *                                    operator can paste it into a bug
 *                                    report without hunting in devtools.
 *   - row 193: 502 `bad_gateway` / 503 `service_unavailable` (non-bootstrap)
 *              → warning toast「服務暫時不可用」with a visible countdown.
 *              `request()` already retried 429/503 internally with backoff
 *              up to MAX_RETRIES; by the time the toast fires those fetch-
 *              level retries are exhausted, so the UI offers one more
 *              recovery: a 10s countdown followed by a full-page reload,
 *              which re-fires any initial data fetches the page owns.
 *              The operator can cancel by dismissing the toast.
 *   - row 194: `offline` (TypeError from fetch — DNS / no network)
 *              → info toast「網路連線中斷，嘗試重新連線...」with a spinning
 *              retry indicator. Coalesces to a single toast (rapid
 *              fire-fail-fire-fail loops won't stack), stays on-screen
 *              until either (a) the user dismisses it or (b) the browser
 *              fires an `online` event, at which point we reload the
 *              page to re-fire any failed initial fetches.
 *
 * Styling mirrors `components/omnisight/toast-center.tsx` — corner brackets,
 * holo-glass, variant-mapped accent colours (orange for warning, red for
 * error, cyan for info) — so the error UX stays coherent with the
 * decision-pending toasts.
 */

import { useCallback, useEffect, useState } from "react"
import { AlertOctagon, ChevronDown, ChevronRight, RefreshCw, ShieldAlert, WifiOff, X } from "lucide-react"
import { onApiError, type ApiError } from "@/lib/api"

const AUTO_DISMISS_WARNING_MS = 5000
// Errors (500) stay on-screen longer so the operator can expand the
// technical detail and copy the trace ID before dismissal.
const AUTO_DISMISS_ERROR_MS = 10_000
// 502/503 auto-retry countdown. Long enough that the operator can cancel
// by hitting dismiss, short enough that transient upstream blips don't
// leave the page stuck.
const AUTO_RETRY_MS = 10_000
const MAX_TOASTS = 3

type ToastVariant = "warning" | "error" | "info"

interface ToastItem {
  id: string
  kind: ApiError["kind"]
  variant: ToastVariant
  title: string
  description: string
  httpLabel: string
  traceId: string | null
  createdAt: number
  autoDismissMs: number
  // When set, expiring the auto-dismiss timer also triggers a full page
  // reload (the 502/503 "auto-retry" UX). User dismissal cancels both.
  retryOnExpire?: boolean
}

function _itemFor(err: ApiError): ToastItem | null {
  if (err.kind === "forbidden") {
    // Phase-3 P5 diagnostic (2026-04-20): embed the actual request URL +
    // method + response body in the description so the operator can tell
    // from the on-screen toast WHICH endpoint 403'd. Server-side caddy
    // + cloudflared + backend logs show NO 403 during the cascade the
    // operator reports, so the source must be either a browser-side
    // block (CSP, X-Frame-Options) surfaced as an ApiError, or a CF-
    // edge-level deny that bypasses our origin. Logging the URL
    // end-to-end narrows it in one operator screenshot.
    //
    // Also console.error the full ApiError for a copy-pastable log.
    // eslint-disable-next-line no-console
    console.error("[TOAST-403-DIAG] forbidden ApiError:", {
      path: err.path,
      method: err.method,
      status: err.status,
      body: err.body?.slice(0, 200),
      traceId: err.traceId,
    })
    // Ship the same info to the backend diag endpoint so we can read
    // it from server-side logs without asking the operator to screenshot
    // the Console. Fire-and-forget, uses fetch directly to bypass the
    // request() wrapper (which would retry this on 429 and add noise).
    if (typeof window !== "undefined") {
      fetch("/api/v1/__diag_render_count", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          component: "TOAST-403",
          count: err.status,
          ts: Date.now(),
          path: `${err.method} ${err.path} :: ${(err.body || "").slice(0, 120)}`,
          instance: err.traceId || "no-trace",
        }),
      }).catch(() => {
        /* ignore */
      })
    }
    return {
      id: `forbidden-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      kind: "forbidden",
      variant: "warning",
      title: "權限不足",
      description: `${err.method} ${err.path} — ${(err.body || "").slice(0, 120)}`,
      httpLabel: "HTTP 403",
      traceId: err.traceId,
      createdAt: Date.now(),
      autoDismissMs: AUTO_DISMISS_WARNING_MS,
    }
  }
  if (err.kind === "server_error") {
    return {
      id: `server_error-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      kind: "server_error",
      variant: "error",
      title: "系統錯誤",
      description: "系統發生內部錯誤，我們已收到通知。",
      httpLabel: "HTTP 500",
      traceId: err.traceId,
      createdAt: Date.now(),
      autoDismissMs: AUTO_DISMISS_ERROR_MS,
    }
  }
  if (err.kind === "bad_gateway" || err.kind === "service_unavailable") {
    const isBadGateway = err.kind === "bad_gateway"
    return {
      id: `${err.kind}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      kind: err.kind,
      variant: "warning",
      title: "服務暫時不可用",
      description: isBadGateway
        ? "後端服務無法回應，將自動重試..."
        : "服務正在維護或暫時不可用，將自動重試...",
      httpLabel: isBadGateway ? "HTTP 502" : "HTTP 503",
      traceId: err.traceId,
      createdAt: Date.now(),
      autoDismissMs: AUTO_RETRY_MS,
      retryOnExpire: true,
    }
  }
  if (err.kind === "offline") {
    // No autoDismiss / no countdown — the toast lives until the operator
    // dismisses or the browser's `online` event triggers an auto-reload.
    return {
      id: `offline-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      kind: "offline",
      variant: "info",
      title: "網路連線中斷",
      description: "嘗試重新連線...",
      httpLabel: "OFFLINE",
      traceId: null,
      createdAt: Date.now(),
      autoDismissMs: 0,
    }
  }
  return null
}

function _variantStyle(variant: ToastVariant) {
  if (variant === "error") {
    return {
      color: "var(--critical-red,#ef4444)",
      Icon: AlertOctagon,
      headerLabel: "ERROR",
    }
  }
  if (variant === "info") {
    return {
      color: "var(--fui-cyan,#22d3ee)",
      Icon: WifiOff,
      headerLabel: "INFO",
    }
  }
  return {
    color: "var(--fui-orange,#f59e0b)",
    Icon: ShieldAlert,
    headerLabel: "WARNING",
  }
}

export function ApiErrorToastCenter() {
  const [toasts, setToasts] = useState<ToastItem[]>([])
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  // Ticking clock so retry-on-expire toasts can show a live countdown.
  // Only started when at least one countdown-bearing toast is visible.
  const [now, setNow] = useState<number>(() => Date.now())

  const dismiss = useCallback((id: string) => {
    setToasts((cur) => cur.filter((t) => t.id !== id))
    setExpanded((cur) => {
      if (!cur.has(id)) return cur
      const next = new Set(cur)
      next.delete(id)
      return next
    })
  }, [])

  const toggleExpanded = useCallback((id: string) => {
    setExpanded((cur) => {
      const next = new Set(cur)
      if (next.has(id)) next.delete(id); else next.add(id)
      return next
    })
  }, [])

  useEffect(() => {
    const off = onApiError((err) => {
      const item = _itemFor(err)
      if (!item) return
      setToasts((cur) => {
        // Offline coalesces — multiple failed requests during one outage
        // produce many `offline` ApiErrors but only one toast is useful.
        // Keep the existing toast (preserves its createdAt + id) so the
        // online-listener reload trigger doesn't lose its target.
        if (item.kind === "offline" && cur.some((t) => t.kind === "offline")) {
          return cur
        }
        return [item, ...cur].slice(0, MAX_TOASTS)
      })
    })
    return off
  }, [])

  useEffect(() => {
    if (toasts.length === 0) return
    const timers = toasts
      // autoDismissMs === 0 means "live forever until user dismiss / online
      // event" — used by the offline toast.
      .filter((t) => t.autoDismissMs > 0)
      .map((t) => {
        const remaining = Math.max(0, t.autoDismissMs - (Date.now() - t.createdAt))
        return setTimeout(() => {
          // Retry-on-expire toasts trigger a full page reload, which re-
          // fires any initial data fetches the page owns. Dismissal (user
          // click) removes the toast before the timer fires and cancels
          // the reload via the cleanup below.
          if (t.retryOnExpire && typeof window !== "undefined") {
            window.location.reload()
            return
          }
          dismiss(t.id)
        }, remaining)
      })
    return () => { for (const timer of timers) clearTimeout(timer) }
  }, [toasts, dismiss])

  // row 194: when the browser fires `online` while an offline toast is
  // visible, reload the page so any failed initial fetches re-fire. The
  // listener is only mounted while a relevant toast exists so we don't
  // hijack reloads triggered by other sources of intermittent connectivity.
  useEffect(() => {
    if (typeof window === "undefined") return
    const hasOffline = toasts.some((t) => t.kind === "offline")
    if (!hasOffline) return
    const onOnline = () => { window.location.reload() }
    window.addEventListener("online", onOnline)
    return () => window.removeEventListener("online", onOnline)
  }, [toasts])

  // 1Hz tick ONLY while a countdown toast is visible, so steady-state
  // idle re-renders cost nothing. Refresh the baseline synchronously when
  // the first countdown toast appears so the first render doesn't show a
  // stale value (`now` was frozen at mount time, which can precede the
  // toast's `createdAt` by seconds under fake timers / slow scheduling).
  useEffect(() => {
    const hasCountdown = toasts.some((t) => t.retryOnExpire)
    if (!hasCountdown) return
    setNow(Date.now())
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [toasts])

  if (toasts.length === 0) return null

  return (
    <div
      aria-live="polite"
      aria-atomic="true"
      aria-label="api error toasts"
      className="fixed bottom-4 right-4 z-[60] flex flex-col-reverse gap-2 w-[min(360px,calc(100vw-2rem))] pointer-events-none"
    >
      {toasts.map((t) => {
        const style = _variantStyle(t.variant)
        const { color, Icon, headerLabel } = style
        const isExpanded = expanded.has(t.id)
        const hasDetails = t.traceId !== null
        const countdownSec = t.retryOnExpire
          ? Math.max(0, Math.ceil((t.autoDismissMs - (now - t.createdAt)) / 1000))
          : null
        return (
          <div
            key={t.id}
            data-testid={`api-error-toast-${t.kind}`}
            role="alert"
            className="pointer-events-auto holo-glass-simple corner-brackets-full rounded-sm border backdrop-blur-sm"
            style={{
              borderColor: color,
              boxShadow: `0 8px 28px -10px ${color}, 0 0 0 1px ${color}, inset 0 0 28px -18px ${color}`,
            }}
          >
            <div className="flex items-start gap-2 p-2.5">
              <Icon className="w-4 h-4 shrink-0 mt-0.5" style={{ color }} aria-hidden />
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 mb-0.5">
                  <span
                    className="font-mono text-[9px] tracking-[0.25em] font-bold"
                    style={{ color }}
                  >
                    {headerLabel}
                  </span>
                  <span className="font-mono text-[9px] text-[var(--muted-foreground,#94a3b8)]">
                    {t.httpLabel}
                  </span>
                </div>
                <div className="font-mono font-bold text-[12px] tracking-[0.04em] leading-tight text-[var(--foreground,#e2e8f0)]">
                  {t.title}
                </div>
                <div className="font-mono text-[10px] text-[var(--muted-foreground,#94a3b8)] leading-tight mt-0.5">
                  {t.description}
                </div>
                {countdownSec !== null && (
                  <div
                    data-testid={`api-error-countdown-${t.kind}`}
                    className="mt-1 flex items-center gap-1 font-mono text-[10px] tracking-[0.1em]"
                    style={{ color }}
                  >
                    <RefreshCw className="w-3 h-3 animate-spin" aria-hidden />
                    <span>
                      自動重試 <span className="font-bold">{countdownSec}</span>s
                    </span>
                  </div>
                )}
                {t.kind === "offline" && (
                  <div
                    data-testid={`api-error-retry-${t.kind}`}
                    className="mt-1 flex items-center gap-1 font-mono text-[10px] tracking-[0.1em]"
                    style={{ color }}
                  >
                    <RefreshCw className="w-3 h-3 animate-spin" aria-hidden />
                    <span>等待網路恢復</span>
                  </div>
                )}
                {hasDetails && (
                  <>
                    <button
                      type="button"
                      onClick={() => toggleExpanded(t.id)}
                      aria-expanded={isExpanded}
                      aria-controls={`api-error-details-${t.id}`}
                      data-testid={`api-error-toggle-${t.kind}`}
                      className="mt-1 flex items-center gap-1 font-mono text-[9px] tracking-[0.15em] text-[var(--muted-foreground,#94a3b8)] hover:text-[var(--foreground,#e2e8f0)] uppercase"
                    >
                      {isExpanded
                        ? <ChevronDown className="w-3 h-3" aria-hidden />
                        : <ChevronRight className="w-3 h-3" aria-hidden />}
                      技術詳情
                    </button>
                    {isExpanded && (
                      <div
                        id={`api-error-details-${t.id}`}
                        data-testid={`api-error-details-${t.kind}`}
                        className="mt-1 rounded-sm border border-[var(--border,#334155)] bg-black/30 px-2 py-1.5 font-mono text-[10px] text-[var(--foreground,#e2e8f0)]"
                      >
                        <div className="flex items-center gap-1.5 text-[var(--muted-foreground,#94a3b8)] text-[9px] tracking-[0.2em] uppercase mb-0.5">
                          Trace ID
                        </div>
                        <div
                          data-testid={`api-error-trace-${t.kind}`}
                          className="select-all break-all text-[10px]"
                          style={{ color }}
                        >
                          {t.traceId}
                        </div>
                      </div>
                    )}
                  </>
                )}
              </div>
              <button
                onClick={() => dismiss(t.id)}
                aria-label="dismiss"
                className="p-0.5 rounded-sm text-[var(--muted-foreground,#94a3b8)] hover:text-[var(--foreground,#e2e8f0)] hover:bg-white/5 shrink-0"
              >
                <X className="w-3.5 h-3.5" aria-hidden />
              </button>
            </div>
          </div>
        )
      })}
    </div>
  )
}
