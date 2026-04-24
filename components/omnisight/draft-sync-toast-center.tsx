"use client"

/**
 * Q.6 #300 (2026-04-24, checkbox 4) — Draft Sync Toast Center.
 *
 * Subscribes to the ``onDraftSynced`` bus exported by
 * ``lib/draft-sync-bus.ts`` and surfaces a short-lived FUI-styled
 * toast「從他裝置同步了草稿」when ``useDraftRestore`` adopts a remote
 * draft whose ``updated_at`` beats this device's local-storage cache.
 *
 * Conflict policy reminder (Q.6 spec): draft is ephemeral — last
 * writer wins server-side (no optimistic lock). The comparison
 * happens only here at restore time; the restored content is already
 * adopted by the composer before this toast renders, so the UX is
 * purely informational. The operator's next keystroke will PUT over
 * the adopted content and the cycle closes.
 *
 * Styling mirrors ``components/omnisight/api-error-toast-center.tsx``
 * (cyan info variant) so the draft-sync toast reads as a neutral
 * "info / auto-action" signal rather than a warning.
 */

import { useCallback, useEffect, useState } from "react"
import { RefreshCw, X } from "lucide-react"

import {
  onDraftSynced,
  type DraftSyncEvent,
} from "@/lib/draft-sync-bus"

const AUTO_DISMISS_MS = 6000
const MAX_TOASTS = 3

interface ToastItem {
  id: string
  slotKey: string
  description: string
  createdAt: number
}

function _describeSlot(slotKey: string): string {
  if (slotKey === "invoke:main") return "INVOKE 指令輸入框"
  if (slotKey === "chat:main") return "Workspace chat 輸入框"
  if (slotKey.startsWith("chat:")) return "Workspace chat 輸入框"
  if (slotKey.startsWith("invoke:")) return "INVOKE 指令輸入框"
  return slotKey
}

function _itemFor(event: DraftSyncEvent): ToastItem {
  const slotLabel = _describeSlot(event.slotKey)
  return {
    id: `draft-synced-${event.slotKey}-${Date.now()}-${Math.random()
      .toString(36)
      .slice(2, 8)}`,
    slotKey: event.slotKey,
    description: `${slotLabel} — 從他裝置同步了草稿`,
    createdAt: Date.now(),
  }
}

export function DraftSyncToastCenter() {
  const [toasts, setToasts] = useState<ToastItem[]>([])

  const dismiss = useCallback((id: string) => {
    setToasts((cur) => cur.filter((t) => t.id !== id))
  }, [])

  useEffect(() => {
    const off = onDraftSynced((event) => {
      const item = _itemFor(event)
      setToasts((cur) => {
        // Coalesce bursts on the same slot — two restores triggered
        // in quick succession (fresh tab + fresh workspace shell)
        // must not stack two identical toasts on top of each other.
        const filtered = cur.filter((t) => t.slotKey !== event.slotKey)
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

  if (toasts.length === 0) return null

  return (
    <div
      aria-live="polite"
      aria-atomic="true"
      aria-label="draft sync toasts"
      data-testid="draft-sync-toast-center"
      className="fixed bottom-4 left-4 z-[55] flex flex-col-reverse gap-2 w-[min(360px,calc(100vw-2rem))] pointer-events-none"
    >
      {toasts.map((t) => (
        <div
          key={t.id}
          role="status"
          data-testid={`draft-sync-toast-${t.slotKey}`}
          className="pointer-events-auto holo-glass-simple rounded-sm border backdrop-blur-sm"
          style={{
            borderColor: "var(--fui-cyan,#22d3ee)",
            boxShadow:
              "0 8px 28px -10px var(--fui-cyan,#22d3ee), 0 0 0 1px var(--fui-cyan,#22d3ee), inset 0 0 28px -18px var(--fui-cyan,#22d3ee)",
          }}
        >
          <div className="flex items-start gap-2 p-2.5">
            <RefreshCw
              className="w-4 h-4 shrink-0 mt-0.5"
              style={{ color: "var(--fui-cyan,#22d3ee)" }}
              aria-hidden
            />
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2 mb-0.5">
                <span
                  className="font-mono text-[9px] tracking-[0.25em] font-bold"
                  style={{ color: "var(--fui-cyan,#22d3ee)" }}
                >
                  DRAFT SYNC
                </span>
                <span className="font-mono text-[9px] text-[var(--muted-foreground,#94a3b8)] truncate">
                  {t.slotKey}
                </span>
              </div>
              <div className="font-mono font-bold text-[12px] tracking-[0.04em] leading-tight text-[var(--foreground,#e2e8f0)] break-words">
                從他裝置同步了草稿
              </div>
              <div className="font-mono text-[10px] text-[var(--muted-foreground,#94a3b8)] leading-tight mt-0.5 break-words">
                {t.description}
              </div>
            </div>
            <button
              type="button"
              data-testid={`draft-sync-toast-dismiss-${t.slotKey}`}
              onClick={() => dismiss(t.id)}
              aria-label="dismiss"
              className="p-0.5 rounded-sm text-[var(--muted-foreground,#94a3b8)] hover:text-[var(--foreground,#e2e8f0)] hover:bg-white/5 shrink-0"
            >
              <X className="w-3.5 h-3.5" aria-hidden />
            </button>
          </div>
        </div>
      ))}
    </div>
  )
}

export default DraftSyncToastCenter
