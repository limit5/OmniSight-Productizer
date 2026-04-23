"use client"

/**
 * Q.2 (#296) — security alerts overlay.
 *
 * Listens for ``security.new_device_login`` SSE events emitted by
 * ``backend.events.emit_new_device_login`` and renders a sticky toast
 * with two buttons:
 *   - 是我 (dismiss)
 *   - 這不是我 → 踢掉 (calls ``DELETE /auth/sessions/{token_hint}``)
 *
 * The bus does not yet enforce ``broadcast_scope=user`` server-side
 * (Q.4 #298 follow-up), so this component additionally filters by
 * ``data.user_id === currentUser.id`` before showing anything. We do
 * NOT attempt geo-IP lookup client-side — the IP is rendered raw in
 * the toast body so the user has the same evidence the backend log
 * has, and a future GeoIP enrichment can plug in via the same field.
 */

import { useCallback, useEffect, useState } from "react"
import { ShieldAlert, X } from "lucide-react"
import { revokeSession, subscribeEvents, type SSEEvent } from "@/lib/api"
import { useAuth } from "@/lib/auth-context"

interface SecurityAlertItem {
  id: string
  user_id: string
  token_hint: string
  ip: string
  user_agent: string
  timestamp: string
}

const MAX_ALERTS = 3

export function SecurityAlertsCenter() {
  const { user } = useAuth()
  const [alerts, setAlerts] = useState<SecurityAlertItem[]>([])
  const [busy, setBusy] = useState<Record<string, boolean>>({})

  const dismiss = useCallback((id: string) => {
    setAlerts((cur) => cur.filter((a) => a.id !== id))
  }, [])

  const handleNotMe = useCallback(async (alert: SecurityAlertItem) => {
    setBusy((b) => ({ ...b, [alert.id]: true }))
    try {
      await revokeSession(alert.token_hint)
    } catch {
      // visual-only — operator can fall back to /settings/security
    } finally {
      setBusy((b) => {
        const next = { ...b }
        delete next[alert.id]
        return next
      })
      dismiss(alert.id)
    }
  }, [dismiss])

  useEffect(() => {
    if (!user?.id) return
    const sub = subscribeEvents((ev: SSEEvent) => {
      if (ev.event !== "security.new_device_login") return
      const d = ev.data
      if (!d || d.user_id !== user.id) return
      const id = `${d.token_hint}-${d.timestamp}`
      setAlerts((cur) => {
        if (cur.some((a) => a.id === id)) return cur
        const item: SecurityAlertItem = {
          id,
          user_id: d.user_id,
          token_hint: d.token_hint,
          ip: d.ip || "",
          user_agent: d.user_agent || "",
          timestamp: d.timestamp,
        }
        return [item, ...cur].slice(0, MAX_ALERTS)
      })
    })
    return () => sub.close()
  }, [user?.id])

  if (alerts.length === 0) return null

  return (
    <div
      aria-live="assertive"
      aria-atomic="true"
      aria-label="security alerts"
      data-testid="security-alerts-center"
      className="fixed top-4 right-4 z-[70] flex flex-col gap-2 w-[min(380px,calc(100vw-2rem))] pointer-events-none"
    >
      {alerts.map((alert) => {
        const isBusy = !!busy[alert.id]
        return (
          <div
            key={alert.id}
            role="alert"
            data-testid={`security-alert-${alert.token_hint}`}
            className="pointer-events-auto rounded-sm border backdrop-blur-sm holo-glass-simple"
            style={{
              borderColor: "var(--critical-red,#ef4444)",
              boxShadow:
                "0 8px 28px -10px var(--critical-red,#ef4444), 0 0 0 1px var(--critical-red,#ef4444), inset 0 0 28px -18px var(--critical-red,#ef4444)",
            }}
          >
            <div className="flex items-start gap-2 p-2.5 pb-1.5">
              <ShieldAlert
                className="w-4 h-4 shrink-0 mt-0.5"
                style={{ color: "var(--critical-red,#ef4444)" }}
                aria-hidden
              />
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 mb-0.5">
                  <span
                    className="font-mono text-[9px] tracking-[0.25em] font-bold"
                    style={{ color: "var(--critical-red,#ef4444)" }}
                  >
                    SECURITY
                  </span>
                  <span className="font-mono text-[9px] text-[var(--muted-foreground,#94a3b8)] truncate">
                    new device login
                  </span>
                </div>
                <div className="font-mono font-bold text-[12px] tracking-[0.04em] leading-tight text-[var(--foreground,#e2e8f0)] break-words">
                  新裝置從 {alert.ip || "未知 IP"} 登入
                </div>
                <div className="font-mono text-[10px] text-[var(--muted-foreground,#94a3b8)] leading-tight mt-0.5 break-words">
                  是你嗎？User-Agent: {alert.user_agent.slice(0, 80) || "未知"}
                </div>
              </div>
              <button
                onClick={() => dismiss(alert.id)}
                aria-label="dismiss"
                className="p-0.5 rounded-sm text-[var(--muted-foreground,#94a3b8)] hover:text-[var(--foreground,#e2e8f0)] hover:bg-white/5 shrink-0"
              >
                <X className="w-3.5 h-3.5" aria-hidden />
              </button>
            </div>

            <div className="flex items-center gap-1.5 px-2.5 pb-2">
              <button
                data-testid={`security-alert-${alert.token_hint}-its-me`}
                onClick={() => dismiss(alert.id)}
                className="flex-1 font-mono text-[10px] tracking-wider px-2 py-1 rounded-sm border border-[var(--validation-emerald,#10b981)] text-[var(--validation-emerald,#10b981)] hover:bg-[var(--validation-emerald,#10b981)]/10"
              >
                是我
              </button>
              <button
                data-testid={`security-alert-${alert.token_hint}-not-me`}
                onClick={() => void handleNotMe(alert)}
                disabled={isBusy}
                className="flex-1 font-mono text-[10px] tracking-wider px-2 py-1 rounded-sm border border-[var(--critical-red,#ef4444)] text-[var(--critical-red,#ef4444)] hover:bg-[var(--critical-red,#ef4444)]/10 disabled:opacity-50"
              >
                {isBusy ? "踢除中…" : "這不是我 → 踢掉"}
              </button>
            </div>
          </div>
        )
      })}
    </div>
  )
}
