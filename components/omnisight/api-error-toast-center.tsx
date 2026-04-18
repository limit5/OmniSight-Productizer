"use client"

/**
 * B13 Part C (#339) — API Error Toast Center.
 *
 * Subscribes to the `onApiError` bus exported by `lib/api.ts` and surfaces
 * a short-lived FUI-styled toast for error kinds that should NOT be a
 * full-page bounce (401 → /login, 503 bootstrap_required → /setup-required
 * already redirect inside `request()` before the bus fires).
 *
 * Phase 1 (row 191): 403 `forbidden` → warning toast「權限不足」.
 * Later rows will extend the handler for 500 / 502 / 503-other / offline.
 *
 * Styling mirrors `components/omnisight/toast-center.tsx` — corner brackets,
 * holo-glass, neural-cyan/orange accents — so the error UX stays coherent
 * with the decision-pending toasts.
 */

import { useCallback, useEffect, useState } from "react"
import { ShieldAlert, X } from "lucide-react"
import { onApiError, type ApiError } from "@/lib/api"

const AUTO_DISMISS_MS = 5000
const MAX_TOASTS = 3

interface ToastItem {
  id: string
  kind: ApiError["kind"]
  title: string
  description: string
  createdAt: number
}

function _itemFor(err: ApiError): ToastItem | null {
  if (err.kind === "forbidden") {
    return {
      id: `forbidden-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      kind: "forbidden",
      title: "權限不足",
      description: "您沒有此操作的存取權限，請聯繫系統管理員。",
      createdAt: Date.now(),
    }
  }
  return null
}

export function ApiErrorToastCenter() {
  const [toasts, setToasts] = useState<ToastItem[]>([])

  const dismiss = useCallback((id: string) => {
    setToasts((cur) => cur.filter((t) => t.id !== id))
  }, [])

  useEffect(() => {
    const off = onApiError((err) => {
      const item = _itemFor(err)
      if (!item) return
      setToasts((cur) => [item, ...cur].slice(0, MAX_TOASTS))
    })
    return off
  }, [])

  useEffect(() => {
    if (toasts.length === 0) return
    const timers = toasts.map((t) => {
      const remaining = Math.max(0, AUTO_DISMISS_MS - (Date.now() - t.createdAt))
      return setTimeout(() => dismiss(t.id), remaining)
    })
    return () => { for (const t of timers) clearTimeout(t) }
  }, [toasts, dismiss])

  if (toasts.length === 0) return null

  return (
    <div
      aria-live="polite"
      aria-atomic="true"
      aria-label="api error toasts"
      className="fixed bottom-4 right-4 z-[60] flex flex-col-reverse gap-2 w-[min(360px,calc(100vw-2rem))] pointer-events-none"
    >
      {toasts.map((t) => {
        const color = "var(--fui-orange,#f59e0b)"
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
              <ShieldAlert className="w-4 h-4 shrink-0 mt-0.5" style={{ color }} aria-hidden />
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 mb-0.5">
                  <span
                    className="font-mono text-[9px] tracking-[0.25em] font-bold"
                    style={{ color }}
                  >
                    WARNING
                  </span>
                  <span className="font-mono text-[9px] text-[var(--muted-foreground,#94a3b8)]">
                    HTTP 403
                  </span>
                </div>
                <div className="font-mono font-bold text-[12px] tracking-[0.04em] leading-tight text-[var(--foreground,#e2e8f0)]">
                  {t.title}
                </div>
                <div className="font-mono text-[10px] text-[var(--muted-foreground,#94a3b8)] leading-tight mt-0.5">
                  {t.description}
                </div>
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
