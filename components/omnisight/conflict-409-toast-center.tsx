"use client"

/**
 * Q.7 #301 — 409 Conflict Toast Center.
 *
 * Subscribes to the ``onConflict409`` bus and surfaces a FUI-styled
 * warning toast with three action buttons「重載 / 覆蓋 / 合併」for
 * every Q.7-shaped 409 response (returned by ``PATCH /tasks/{id}`` /
 * ``PUT /runtime/npi`` / ``PUT /secrets/{id}`` /
 * ``PATCH /projects/runs/{id}``).
 *
 * UX policy (Q.7 spec):
 *   - 重載 (reload) is the default — social-platform norm, safest.
 *     Default-focus on mount so Enter confirms without mouse.
 *   - 覆蓋 (overwrite) — re-PATCH with the server's current_version,
 *     clobbering the peer's write. Hidden when ``onOverwrite`` is
 *     omitted by the caller (some resources have no safe overwrite
 *     path — e.g. encrypted secret rotation).
 *   - 合併 (merge) — caller-defined merge strategy. Hidden when
 *     ``onMerge`` is omitted (default, since most Q.7 resources
 *     don't have a natural merge).
 *
 * Same-resource coalesce: a burst of 409s against the same resource
 * (two devices retrying in parallel) collapses into a single toast
 * so the operator doesn't get a pile of identical warnings.
 *
 * Module-global state: ``toasts`` is per-VM React state, ``setToasts``
 * stays in-process. No durable persistence — 409s are instantaneous
 * signals, the next successful PATCH makes the warning obsolete.
 *
 * Styling mirrors ``api-error-toast-center.tsx`` warning variant
 * (orange border + corner brackets) so the conflict toast reads as
 * "warning + actionable" rather than "fatal error".
 */

import { useCallback, useEffect, useState } from "react"
import { ShieldAlert, X } from "lucide-react"

import {
  onConflict409,
  type Conflict409Event,
} from "@/lib/conflict-409-bus"

const AUTO_DISMISS_MS = 20_000
const MAX_TOASTS = 3

function _describeResource(resource: string): string {
  switch (resource) {
    case "task":
      return "工作項目"
    case "tenant_secret":
      return "租戶密鑰"
    case "runtime_settings":
    case "npi_state":
      return "Runtime 設定"
    case "project_run":
      return "專案執行"
    case "workflow_run":
      return "Workflow 執行"
    default:
      return resource || "資源"
  }
}

interface ToastItem {
  id: string
  event: Conflict409Event
  createdAt: number
}

export function Conflict409ToastCenter() {
  const [toasts, setToasts] = useState<ToastItem[]>([])
  const [busyAction, setBusyAction] = useState<string | null>(null)

  const dismiss = useCallback((id: string) => {
    setToasts((cur) => cur.filter((t) => t.id !== id))
  }, [])

  useEffect(() => {
    const off = onConflict409((event) => {
      const item: ToastItem = {
        id: event.id,
        event,
        createdAt: Date.now(),
      }
      setToasts((cur) => {
        // Coalesce same-resource bursts — keep the newest event so the
        // resolution handlers are the ones from the most recent
        // callsite (stale closures on a prior toast's handlers would
        // operate on a stale form state).
        const filtered = cur.filter(
          (t) => t.event.resource !== event.resource,
        )
        return [item, ...filtered].slice(0, MAX_TOASTS)
      })
    })
    return off
  }, [])

  useEffect(() => {
    if (toasts.length === 0) return
    const timers = toasts.map((t) => {
      const remaining = Math.max(0, AUTO_DISMISS_MS - (Date.now() - t.createdAt))
      return setTimeout(() => dismiss(t.id), remaining)
    })
    return () => {
      for (const timer of timers) clearTimeout(timer)
    }
  }, [toasts, dismiss])

  const runAction = useCallback(
    async (
      toastId: string,
      action: "reload" | "overwrite" | "merge",
      handler: () => void | Promise<void>,
    ) => {
      setBusyAction(`${toastId}:${action}`)
      try {
        await handler()
      } catch (err) {
        console.warn("[Conflict409ToastCenter] action failed", err)
      } finally {
        setBusyAction(null)
        dismiss(toastId)
      }
    },
    [dismiss],
  )

  if (toasts.length === 0) return null

  return (
    <div
      aria-live="assertive"
      aria-atomic="true"
      aria-label="conflict 409 toasts"
      data-testid="conflict-409-toast-center"
      className="fixed bottom-4 right-4 z-[60] flex flex-col-reverse gap-2 w-[min(420px,calc(100vw-2rem))] pointer-events-none"
    >
      {toasts.map((t) => {
        const { event } = t
        const resourceLabel = _describeResource(event.resource)
        const busyKey = `${t.id}:`
        const disabled =
          busyAction !== null && busyAction.startsWith(busyKey)
        return (
          <div
            key={t.id}
            role="alert"
            data-testid={`conflict-409-toast-${event.resource}`}
            className="pointer-events-auto holo-glass-simple rounded-sm border backdrop-blur-sm"
            style={{
              borderColor: "var(--fui-orange,#f59e0b)",
              boxShadow:
                "0 8px 28px -10px var(--fui-orange,#f59e0b), 0 0 0 1px var(--fui-orange,#f59e0b), inset 0 0 28px -18px var(--fui-orange,#f59e0b)",
            }}
          >
            <div className="flex items-start gap-2 p-2.5">
              <ShieldAlert
                className="w-4 h-4 shrink-0 mt-0.5"
                style={{ color: "var(--fui-orange,#f59e0b)" }}
                aria-hidden
              />
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 mb-0.5">
                  <span
                    className="font-mono text-[9px] tracking-[0.25em] font-bold"
                    style={{ color: "var(--fui-orange,#f59e0b)" }}
                  >
                    CONFLICT 409
                  </span>
                  <span className="font-mono text-[9px] text-[var(--muted-foreground,#94a3b8)] truncate">
                    {resourceLabel}
                  </span>
                </div>
                <div className="font-mono font-bold text-[12px] tracking-[0.04em] leading-tight text-[var(--foreground,#e2e8f0)] break-words">
                  {event.hint}
                </div>
                <div className="font-mono text-[10px] text-[var(--muted-foreground,#94a3b8)] leading-tight mt-0.5 break-words">
                  伺服器版本 {event.currentVersion ?? "?"} · 您的版本 {event.yourVersion}
                </div>
                <div className="mt-2 flex items-center gap-2 flex-wrap">
                  <button
                    type="button"
                    autoFocus
                    data-testid={`conflict-409-reload-${event.resource}`}
                    disabled={disabled}
                    onClick={() => runAction(t.id, "reload", event.onReload)}
                    className="px-2 py-1 rounded-sm border font-mono text-[10px] tracking-[0.15em] font-bold hover:bg-white/5 disabled:opacity-50"
                    style={{
                      borderColor: "var(--fui-orange,#f59e0b)",
                      color: "var(--fui-orange,#f59e0b)",
                    }}
                  >
                    重載
                  </button>
                  {event.onOverwrite ? (
                    <button
                      type="button"
                      data-testid={`conflict-409-overwrite-${event.resource}`}
                      disabled={disabled}
                      onClick={() =>
                        runAction(t.id, "overwrite", event.onOverwrite!)
                      }
                      className="px-2 py-1 rounded-sm border font-mono text-[10px] tracking-[0.15em] font-bold hover:bg-white/5 disabled:opacity-50"
                      style={{
                        borderColor: "var(--muted-foreground,#94a3b8)",
                        color: "var(--muted-foreground,#94a3b8)",
                      }}
                    >
                      覆蓋
                    </button>
                  ) : null}
                  {event.onMerge ? (
                    <button
                      type="button"
                      data-testid={`conflict-409-merge-${event.resource}`}
                      disabled={disabled}
                      onClick={() =>
                        runAction(t.id, "merge", event.onMerge!)
                      }
                      className="px-2 py-1 rounded-sm border font-mono text-[10px] tracking-[0.15em] font-bold hover:bg-white/5 disabled:opacity-50"
                      style={{
                        borderColor: "var(--muted-foreground,#94a3b8)",
                        color: "var(--muted-foreground,#94a3b8)",
                      }}
                    >
                      合併
                    </button>
                  ) : null}
                </div>
              </div>
              <button
                type="button"
                data-testid={`conflict-409-dismiss-${event.resource}`}
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

export default Conflict409ToastCenter
