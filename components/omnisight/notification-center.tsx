"use client"

import { useState } from "react"
import { createPortal } from "react-dom"
import { X, Bell, AlertTriangle, AlertOctagon, Info, ExternalLink, Zap } from "lucide-react"
import { injectAgentHint, type NotificationItem } from "@/lib/api"

const LEVEL_CONFIG = {
  info:     { icon: Info,           color: "var(--muted-foreground)", bg: "var(--secondary)",          label: "INFO" },
  warning:  { icon: AlertTriangle,  color: "#eab308",                bg: "rgba(234,179,8,0.1)",       label: "WARNING" },
  action:   { icon: AlertOctagon,   color: "var(--critical-red)",    bg: "rgba(239,68,68,0.1)",       label: "ACTION" },
  critical: { icon: AlertOctagon,   color: "#dc2626",                bg: "rgba(220,38,38,0.2)",       label: "CRITICAL" },
}

interface NotificationCenterProps {
  open: boolean
  onClose: () => void
  notifications: NotificationItem[]
  onMarkRead: (id: string) => void
}

/** R1 (#307): on P2-ish notifications (source="agent:<id>"), let the
 * operator fire off an inject hint without hopping to the ChatOps panel. */
function extractAgentId(source: string | undefined): string {
  if (!source) return ""
  const m = /^agent:([a-zA-Z0-9_\-:/.]+)/.exec(source)
  return m ? m[1] : ""
}

function InlineInject({ agentId }: { agentId: string }) {
  const [text, setText] = useState("")
  const [busy, setBusy] = useState(false)
  const [flash, setFlash] = useState<string | null>(null)
  const doInject = async () => {
    const t = text.trim()
    if (!t) return
    setBusy(true)
    setFlash(null)
    try {
      await injectAgentHint(agentId, t, "notification-center")
      setFlash("✓ injected")
      setText("")
    } catch (exc) {
      setFlash(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setBusy(false)
      setTimeout(() => setFlash(null), 3000)
    }
  }
  return (
    <div className="mt-1.5 flex items-center gap-1" onClick={(e) => e.stopPropagation()}>
      <Zap size={10} className="text-[var(--fui-orange, #f59e0b)]" />
      <input
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder={`inject hint → ${agentId}`}
        maxLength={2000}
        className="flex-1 px-1.5 py-0.5 rounded border border-[var(--border)] bg-[var(--background)] font-mono text-[10px]"
        onKeyDown={(e) => { if (e.key === "Enter") void doInject() }}
      />
      <button
        onClick={() => void doInject()}
        disabled={busy || !text.trim()}
        className="px-1.5 py-0.5 rounded font-mono text-[9px] border border-[var(--fui-orange, #f59e0b)] text-[var(--fui-orange, #f59e0b)] hover:bg-[var(--fui-orange, #f59e0b)]/10 disabled:opacity-40"
      >
        {busy ? "…" : "Inject"}
      </button>
      {flash && (
        <span className="font-mono text-[9px] text-[var(--muted-foreground)]">{flash}</span>
      )}
    </div>
  )
}

export function NotificationCenter({ open, onClose, notifications, onMarkRead }: NotificationCenterProps) {
  const [filter, setFilter] = useState<string>("all")

  if (!open) return null

  const filtered = filter === "all"
    ? notifications
    : notifications.filter(n => n.level === filter)

  const unreadCount = notifications.filter(n => !n.read).length

  if (typeof document === "undefined") return null

  return createPortal(
    <div className="fixed inset-0 z-50 flex justify-end" onClick={onClose}>
      {/* Backdrop */}
      <div className="absolute inset-0 bg-black/40" />

      {/* Panel */}
      <div
        className="relative w-full max-w-sm h-full bg-[var(--background)] border-l border-[var(--border)] flex flex-col animate-in slide-in-from-right duration-200"
        onClick={e => e.stopPropagation()}
      >
        {/* Header */}
        <div className="px-4 py-3 border-b border-[var(--border)] flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Bell size={14} className="text-[var(--foreground)]" />
            <h2 className="font-mono text-sm font-semibold text-[var(--foreground)]">NOTIFICATIONS</h2>
            {unreadCount > 0 && (
              <span className="px-1.5 py-0.5 rounded-full bg-[var(--critical-red)] text-white text-[9px] font-mono font-bold">
                {unreadCount}
              </span>
            )}
          </div>
          <button onClick={onClose} className="p-1 rounded hover:bg-[var(--secondary)] transition-colors">
            <X size={14} className="text-[var(--muted-foreground)]" />
          </button>
        </div>

        {/* Filter tabs */}
        <div className="px-3 py-2 border-b border-[var(--border)] flex gap-1">
          {["all", "warning", "action", "critical"].map(f => (
            <button
              key={f}
              onClick={() => setFilter(f)}
              className={`px-2 py-0.5 rounded font-mono text-[9px] transition-colors ${
                filter === f
                  ? "bg-[var(--foreground)]/10 text-[var(--foreground)]"
                  : "text-[var(--muted-foreground)] hover:text-[var(--foreground)]"
              }`}
            >
              {f.toUpperCase()}
            </button>
          ))}
        </div>

        {/* Notification list */}
        <div className="flex-1 overflow-y-auto">
          {filtered.length === 0 ? (
            <div className="p-8 text-center">
              <Bell size={24} className="mx-auto mb-2 text-[var(--muted-foreground)] opacity-30" />
              <p className="font-mono text-xs text-[var(--muted-foreground)]">No notifications</p>
            </div>
          ) : (
            <div className="divide-y divide-[var(--border)]">
              {filtered.map(n => {
                const cfg = LEVEL_CONFIG[n.level] || LEVEL_CONFIG.info
                const Icon = cfg.icon
                return (
                  <div
                    key={n.id}
                    className={`px-4 py-3 transition-colors ${!n.read ? "bg-[var(--secondary)]/50" : ""} hover:bg-[var(--secondary)]/30`}
                    onClick={() => { if (!n.read) onMarkRead(n.id) }}
                  >
                    <div className="flex items-start gap-2.5">
                      <div
                        className="w-6 h-6 rounded flex items-center justify-center shrink-0 mt-0.5"
                        style={{ backgroundColor: cfg.bg, color: cfg.color }}
                      >
                        <Icon size={12} />
                      </div>
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2 mb-0.5">
                          <span className="font-mono text-[9px] px-1 py-0.5 rounded" style={{ backgroundColor: cfg.bg, color: cfg.color }}>
                            {cfg.label}
                          </span>
                          {!n.read && <span className="w-1.5 h-1.5 rounded-full bg-[var(--neural-blue)]" />}
                          <span className="font-mono text-[9px] text-[var(--muted-foreground)] ml-auto shrink-0">
                            {n.timestamp.includes("T") ? n.timestamp.split("T")[1]?.slice(0, 8) : n.timestamp}
                          </span>
                        </div>
                        <p className="font-mono text-xs text-[var(--foreground)] leading-relaxed">{n.title}</p>
                        {n.message && (
                          <p className="font-mono text-[10px] text-[var(--muted-foreground)] mt-0.5 leading-relaxed">{n.message}</p>
                        )}
                        {n.action_url && (
                          <a
                            href={n.action_url}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="inline-flex items-center gap-1 mt-1 font-mono text-[10px] text-[var(--neural-blue)] hover:underline"
                            onClick={e => e.stopPropagation()}
                          >
                            <ExternalLink size={9} />
                            {n.action_label || "View"}
                          </a>
                        )}
                        {(n.level === "action" || n.level === "critical") && (() => {
                          const aid = extractAgentId(n.source)
                          return aid ? <InlineInject agentId={aid} /> : null
                        })()}
                      </div>
                    </div>
                  </div>
                )
              })}
            </div>
          )}
        </div>
      </div>
    </div>,
    document.body,
  )
}
