"use client"

import { useCallback, useEffect, useState } from "react"
import { Shield, Filter, Monitor, Smartphone, Globe, RefreshCw } from "lucide-react"
import {
  listAuditEntries,
  listSessions,
  type AuditEntry,
  type SessionItem,
} from "@/lib/api"
import { useAuth } from "@/lib/auth-context"

function parseUA(ua: string | null): string {
  if (!ua) return "—"
  const browser =
    ua.includes("Firefox") ? "Firefox" :
    ua.includes("Edg") ? "Edge" :
    ua.includes("Chrome") ? "Chrome" :
    ua.includes("Safari") ? "Safari" :
    "Browser"
  const os =
    ua.includes("Windows") ? "Windows" :
    ua.includes("Mac OS") ? "macOS" :
    ua.includes("Linux") ? "Linux" :
    ua.includes("Android") ? "Android" :
    ua.includes("iPhone") || ua.includes("iPad") ? "iOS" :
    "OS"
  return `${browser} on ${os}`
}

function formatTime(epoch: number): string {
  if (!epoch) return "—"
  const d = new Date(epoch * 1000)
  return d.toLocaleString(undefined, {
    month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  })
}

function relativeTime(epoch: number): string {
  if (!epoch) return "—"
  const diff = Date.now() / 1000 - epoch
  if (diff < 60) return "just now"
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`
  return `${Math.floor(diff / 86400)}d ago`
}

const ACTION_COLORS: Record<string, string> = {
  set_mode: "var(--neural-cyan, #67e8f9)",
  set_strategy: "var(--neural-amber, #fbbf24)",
  resolve: "var(--validation-emerald, #34d399)",
  undo: "var(--critical-red, #f87171)",
  create: "var(--validation-emerald, #34d399)",
  update: "var(--neural-blue, #60a5fa)",
  delete: "var(--critical-red, #f87171)",
}

function actionColor(action: string): string {
  for (const [key, color] of Object.entries(ACTION_COLORS)) {
    if (action.toLowerCase().includes(key)) return color
  }
  return "var(--muted-foreground)"
}

type SessionFilter = "all" | string

export function AuditPanel() {
  const { sessionId } = useAuth()
  const [entries, setEntries] = useState<AuditEntry[]>([])
  const [sessions, setSessions] = useState<SessionItem[]>([])
  const [loading, setLoading] = useState(true)
  const [sessionFilter, setSessionFilter] = useState<SessionFilter>("all")
  const [expanded, setExpanded] = useState<number | null>(null)

  const fetchData = useCallback(async () => {
    setLoading(true)
    try {
      const [auditRes, sessRes] = await Promise.all([
        listAuditEntries({
          session_id: sessionFilter !== "all" ? sessionFilter : undefined,
          limit: 200,
        }),
        listSessions(),
      ])
      setEntries(auditRes.items)
      setSessions(sessRes.items)
    } catch (e) {
      console.error("[audit] fetch failed:", e)
    } finally {
      setLoading(false)
    }
  }, [sessionFilter])

  useEffect(() => {
    fetchData()
    const iv = setInterval(fetchData, 15000)
    return () => clearInterval(iv)
  }, [fetchData])

  const setCurrentSession = useCallback(() => {
    if (sessionId) setSessionFilter(sessionId)
  }, [sessionId])

  const sessionLabel = (sid: string): string => {
    const s = sessions.find((s) => sid.startsWith(s.token_hint) || s.token_hint === sid.slice(0, 8))
    if (s) {
      const tag = s.is_current ? " (current)" : ""
      return `${parseUA(s.user_agent)} — ${s.ip}${tag}`
    }
    return sid.slice(0, 8) + "…"
  }

  return (
    <section className="flex flex-col gap-4 h-full">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Shield size={20} style={{ color: "var(--neural-cyan, #67e8f9)" }} />
          <h2 className="font-sans text-base font-semibold tracking-fui" style={{ color: "var(--neural-cyan, #67e8f9)" }}>
            AUDIT LOG
          </h2>
          <span className="text-xs text-[var(--muted-foreground)]">({entries.length})</span>
        </div>
        <button
          onClick={fetchData}
          className="p-1.5 rounded-md text-[var(--muted-foreground)] hover:text-[var(--neural-cyan)] hover:bg-[var(--neural-cyan)]/10 transition-colors"
          title="Refresh"
        >
          <RefreshCw size={14} className={loading ? "animate-spin" : ""} />
        </button>
      </div>

      {/* Session filter bar */}
      <div className="flex items-center gap-2 flex-wrap">
        <Filter size={14} className="text-[var(--muted-foreground)]" />
        <button
          onClick={() => setSessionFilter("all")}
          className="px-2 py-1 text-xs rounded-md border transition-colors"
          style={{
            borderColor: sessionFilter === "all" ? "var(--neural-cyan, #67e8f9)" : "var(--border)",
            color: sessionFilter === "all" ? "var(--neural-cyan, #67e8f9)" : "var(--muted-foreground)",
            background: sessionFilter === "all" ? "rgba(103,232,249,0.1)" : "transparent",
          }}
        >
          All Sessions
        </button>
        {sessionId && (
          <button
            onClick={setCurrentSession}
            className="px-2 py-1 text-xs rounded-md border transition-colors"
            style={{
              borderColor: sessionFilter === sessionId ? "var(--validation-emerald, #34d399)" : "var(--border)",
              color: sessionFilter === sessionId ? "var(--validation-emerald, #34d399)" : "var(--muted-foreground)",
              background: sessionFilter === sessionId ? "rgba(52,211,153,0.1)" : "transparent",
            }}
          >
            Current Session
          </button>
        )}
        {sessions.filter((s) => !s.is_current).map((s) => (
          <button
            key={s.token_hint}
            onClick={() => setSessionFilter(s.token_hint)}
            className="px-2 py-1 text-xs rounded-md border transition-colors max-w-[200px] truncate"
            title={`${parseUA(s.user_agent)} — ${s.ip}`}
            style={{
              borderColor: sessionFilter === s.token_hint ? "var(--neural-amber, #fbbf24)" : "var(--border)",
              color: sessionFilter === s.token_hint ? "var(--neural-amber, #fbbf24)" : "var(--muted-foreground)",
              background: sessionFilter === s.token_hint ? "rgba(251,191,36,0.1)" : "transparent",
            }}
          >
            {parseUA(s.user_agent)} · {s.ip}
          </button>
        ))}
      </div>

      {/* Entries list */}
      <div className="flex-1 overflow-auto space-y-1">
        {loading && entries.length === 0 && (
          <p className="text-xs text-[var(--muted-foreground)] text-center py-8">Loading audit log…</p>
        )}
        {!loading && entries.length === 0 && (
          <p className="text-xs text-[var(--muted-foreground)] text-center py-8">No audit entries found.</p>
        )}
        {entries.map((e) => (
          <button
            key={e.id}
            onClick={() => setExpanded(expanded === e.id ? null : e.id)}
            className="w-full text-left p-3 rounded-lg border border-[var(--border)] hover:border-[var(--neural-cyan)]/30 transition-colors bg-[var(--secondary)]/30"
          >
            <div className="flex items-center justify-between gap-2">
              <div className="flex items-center gap-2 min-w-0">
                <span
                  className="text-xs font-mono font-bold shrink-0"
                  style={{ color: actionColor(e.action) }}
                >
                  {e.action}
                </span>
                <span className="text-xs text-[var(--muted-foreground)] truncate">
                  {e.entity_kind}{e.entity_id ? `/${e.entity_id}` : ""}
                </span>
              </div>
              <span className="text-[10px] text-[var(--muted-foreground)] shrink-0" title={formatTime(e.ts)}>
                {relativeTime(e.ts)}
              </span>
            </div>
            <div className="flex items-center gap-3 mt-1.5 text-[10px] text-[var(--muted-foreground)]">
              <span>{e.actor}</span>
              {e.session_ip && (
                <span className="inline-flex items-center gap-1">
                  <Globe size={10} />
                  {e.session_ip}
                </span>
              )}
              {e.session_ua && (
                <span className="inline-flex items-center gap-1">
                  {e.session_ua.includes("Mobile") || e.session_ua.includes("Android") || e.session_ua.includes("iPhone")
                    ? <Smartphone size={10} />
                    : <Monitor size={10} />}
                  {parseUA(e.session_ua)}
                </span>
              )}
            </div>
            {expanded === e.id && (
              <div className="mt-2 pt-2 border-t border-[var(--border)] text-[10px] font-mono space-y-1">
                <div className="flex gap-4">
                  <span className="text-[var(--muted-foreground)]">Time:</span>
                  <span>{formatTime(e.ts)}</span>
                </div>
                <div className="flex gap-4">
                  <span className="text-[var(--muted-foreground)]">Session:</span>
                  <span>{e.session_id ? e.session_id.slice(0, 12) + "…" : "—"}</span>
                </div>
                {Object.keys(e.before).length > 0 && (
                  <div>
                    <span className="text-[var(--critical-red)]">Before:</span>
                    <pre className="mt-0.5 p-1.5 rounded bg-[var(--secondary)] overflow-x-auto whitespace-pre-wrap break-all">
                      {JSON.stringify(e.before, null, 2)}
                    </pre>
                  </div>
                )}
                {Object.keys(e.after).length > 0 && (
                  <div>
                    <span className="text-[var(--validation-emerald)]">After:</span>
                    <pre className="mt-0.5 p-1.5 rounded bg-[var(--secondary)] overflow-x-auto whitespace-pre-wrap break-all">
                      {JSON.stringify(e.after, null, 2)}
                    </pre>
                  </div>
                )}
                <div className="flex gap-4">
                  <span className="text-[var(--muted-foreground)]">Hash:</span>
                  <span className="truncate">{e.curr_hash.slice(0, 16)}…</span>
                </div>
              </div>
            )}
          </button>
        ))}
      </div>
    </section>
  )
}
