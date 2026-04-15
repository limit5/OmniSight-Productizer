"use client"

import { useCallback, useEffect, useState } from "react"
import { Monitor, Trash2, LogOut, RefreshCw, Smartphone, Globe } from "lucide-react"
import {
  listSessions,
  revokeSession,
  revokeAllOtherSessions,
  type SessionItem,
} from "@/lib/api"

function parseUA(ua: string): string {
  if (!ua) return "Unknown device"
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
    hour: "2-digit", minute: "2-digit",
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

export function SessionManagerPanel() {
  const [sessions, setSessions] = useState<SessionItem[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [revoking, setRevoking] = useState<string | null>(null)
  const [revokingAll, setRevokingAll] = useState(false)

  const fetchSessions = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await listSessions()
      setSessions(res.items)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void fetchSessions() }, [fetchSessions])

  const handleRevoke = async (tokenHint: string) => {
    setRevoking(tokenHint)
    try {
      await revokeSession(tokenHint)
      setSessions(prev => prev.filter(s => s.token_hint !== tokenHint))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setRevoking(null)
    }
  }

  const handleRevokeAll = async () => {
    setRevokingAll(true)
    try {
      await revokeAllOtherSessions()
      setSessions(prev => prev.filter(s => s.is_current))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setRevokingAll(false)
    }
  }

  const otherCount = sessions.filter(s => !s.is_current).length

  return (
    <div className="flex flex-col gap-3" data-testid="session-manager-panel">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Monitor size={14} className="text-[var(--foreground)]" />
          <h3 className="font-mono text-sm font-semibold text-[var(--foreground)]">
            ACTIVE SESSIONS
          </h3>
          <span className="px-1.5 py-0.5 rounded-full bg-[var(--secondary)] text-[var(--muted-foreground)] text-[9px] font-mono font-bold">
            {sessions.length}
          </span>
        </div>
        <div className="flex items-center gap-1">
          <button
            onClick={fetchSessions}
            disabled={loading}
            className="p-1 rounded hover:bg-[var(--secondary)] transition-colors disabled:opacity-50"
            aria-label="Refresh sessions"
            data-testid="sessions-refresh"
          >
            <RefreshCw size={12} className={`text-[var(--muted-foreground)] ${loading ? "animate-spin" : ""}`} />
          </button>
          {otherCount > 0 && (
            <button
              onClick={handleRevokeAll}
              disabled={revokingAll}
              className="flex items-center gap-1 px-2 py-1 rounded font-mono text-[10px] bg-[var(--destructive)]/10 text-[var(--destructive)] hover:bg-[var(--destructive)]/20 transition-colors disabled:opacity-50"
              data-testid="revoke-all-others"
            >
              <LogOut size={10} />
              {revokingAll ? "Revoking..." : `Sign out all others (${otherCount})`}
            </button>
          )}
        </div>
      </div>

      {error && (
        <div className="px-3 py-2 rounded bg-[var(--destructive)]/10 text-[var(--destructive)] font-mono text-xs">
          {error}
        </div>
      )}

      {/* Session list */}
      <div className="flex flex-col gap-1.5">
        {loading && sessions.length === 0 ? (
          <div className="text-center py-6 text-[var(--muted-foreground)] font-mono text-xs">
            Loading sessions...
          </div>
        ) : sessions.length === 0 ? (
          <div className="text-center py-6 text-[var(--muted-foreground)] font-mono text-xs">
            No active sessions
          </div>
        ) : (
          sessions.map(s => (
            <div
              key={s.token_hint}
              data-testid={`session-row-${s.token_hint}`}
              className={`flex items-center gap-3 px-3 py-2 rounded border transition-colors ${
                s.is_current
                  ? "border-[var(--neural-blue)]/40 bg-[var(--neural-blue)]/5"
                  : "border-[var(--border)] bg-[var(--card)]"
              }`}
            >
              <div className="shrink-0">
                {s.user_agent.includes("Mobile") || s.user_agent.includes("Android") || s.user_agent.includes("iPhone") ? (
                  <Smartphone size={16} className="text-[var(--muted-foreground)]" />
                ) : (
                  <Globe size={16} className="text-[var(--muted-foreground)]" />
                )}
              </div>

              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <span className="font-mono text-xs text-[var(--foreground)] truncate">
                    {parseUA(s.user_agent)}
                  </span>
                  {s.is_current && (
                    <span
                      className="shrink-0 px-1.5 py-0.5 rounded bg-[var(--neural-blue)]/20 text-[var(--neural-blue)] text-[9px] font-mono font-bold"
                      data-testid="this-device-badge"
                    >
                      This device
                    </span>
                  )}
                </div>
                <div className="flex items-center gap-3 mt-0.5">
                  <span className="font-mono text-[10px] text-[var(--muted-foreground)]" title={`IP: ${s.ip}`}>
                    {s.ip || "—"}
                  </span>
                  <span className="font-mono text-[10px] text-[var(--muted-foreground)]" title={`Created: ${formatTime(s.created_at)}`}>
                    Created {formatTime(s.created_at)}
                  </span>
                  <span className="font-mono text-[10px] text-[var(--muted-foreground)]" title={`Last seen: ${formatTime(s.last_seen_at)}`}>
                    Active {relativeTime(s.last_seen_at)}
                  </span>
                </div>
              </div>

              {!s.is_current && (
                <button
                  onClick={() => handleRevoke(s.token_hint)}
                  disabled={revoking === s.token_hint}
                  className="shrink-0 flex items-center gap-1 px-2 py-1 rounded font-mono text-[10px] text-[var(--destructive)] hover:bg-[var(--destructive)]/10 transition-colors disabled:opacity-50"
                  aria-label={`Revoke session ${s.token_hint}`}
                  data-testid={`revoke-${s.token_hint}`}
                >
                  <Trash2 size={10} />
                  {revoking === s.token_hint ? "..." : "Revoke"}
                </button>
              )}
            </div>
          ))
        )}
      </div>
    </div>
  )
}
