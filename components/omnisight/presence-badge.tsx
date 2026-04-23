"use client"

/**
 * PresenceBadge — header chip showing how many of the operator's own
 * devices currently hold a live SSE connection ("3 台裝置在線").
 *
 * Reads `GET /auth/sessions/presence` (Q.5 #299 backend endpoint, written
 * by the SSE event_stream heartbeat) and surfaces:
 *   - a compact badge with a green dot + "{n} 在線" in the header
 *   - a hover popover with a mini list (one row per active device)
 *     reusing the same UA-label vocabulary as `session-manager-panel.tsx`
 *
 * Scoped to logged-in users in session/strict auth modes — render-null
 * in `auth_mode=open` to match the rest of the multi-device security
 * UI (`UserMenu`, `SessionManagerPanel`, etc.).
 */

import { useCallback, useEffect, useRef, useState } from "react"
import { Monitor, Smartphone, Globe } from "lucide-react"
import { useAuth } from "@/lib/auth-context"
import {
  getSessionsPresence,
  type PresenceDevice,
  type PresenceResponse,
} from "@/lib/api"

const POLL_INTERVAL_MS = 15000

function deviceIcon(name: string) {
  // The backend `_label_ua` helper joins "<browser> on <os>"; we infer
  // mobile from the OS half of the label so iOS / Android pick the
  // phone glyph. Mirrors `session-manager-panel.tsx` glyph rules.
  if (name.includes("Android") || name.includes("iOS")) {
    return Smartphone
  }
  return Globe
}

function relativeTime(idleSeconds: number): string {
  if (idleSeconds < 5) return "just now"
  if (idleSeconds < 60) return `${Math.floor(idleSeconds)}s ago`
  return `${Math.floor(idleSeconds / 60)}m ago`
}

export function PresenceBadge() {
  const auth = useAuth()
  const [presence, setPresence] = useState<PresenceResponse | null>(null)
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const wrapperRef = useRef<HTMLDivElement>(null)

  const isAuthed = auth.authMode !== "open" && !!auth.user

  const fetchPresence = useCallback(async () => {
    if (!isAuthed) return
    setLoading(true)
    setError(null)
    try {
      const res = await getSessionsPresence()
      setPresence(res)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [isAuthed])

  useEffect(() => {
    if (!isAuthed) return
    void fetchPresence()
    const id = setInterval(() => { void fetchPresence() }, POLL_INTERVAL_MS)
    return () => clearInterval(id)
  }, [isAuthed, fetchPresence])

  // Close popover on outside click / Escape — same shape as UserMenu.
  useEffect(() => {
    if (!open) return
    const onDoc = (e: MouseEvent) => {
      if (wrapperRef.current && !wrapperRef.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false) }
    document.addEventListener("mousedown", onDoc)
    document.addEventListener("keydown", onKey)
    return () => {
      document.removeEventListener("mousedown", onDoc)
      document.removeEventListener("keydown", onKey)
    }
  }, [open])

  if (!isAuthed) return null

  const count = presence?.active_count ?? 0
  const devices: PresenceDevice[] = presence?.devices ?? []

  return (
    <div
      ref={wrapperRef}
      className="relative inline-flex"
      onMouseEnter={() => { if (count > 0 || error) setOpen(true) }}
      onMouseLeave={() => setOpen(false)}
      data-testid="presence-badge"
    >
      <button
        type="button"
        onClick={() => setOpen(true)}
        aria-label={`${count} active device${count === 1 ? "" : "s"}`}
        aria-haspopup="dialog"
        aria-expanded={open}
        title={`${count} 台裝置在線`}
        className="flex items-center gap-1.5 px-2 py-1 rounded border border-[var(--border)] bg-[var(--secondary)]/40 hover:bg-[var(--neural-blue)]/10 transition-colors"
        data-testid="presence-badge-button"
      >
        <span
          className={`relative inline-block w-2 h-2 rounded-full ${
            count > 0
              ? "bg-[var(--validation-emerald)]"
              : "bg-[var(--muted-foreground)]"
          }`}
          aria-hidden="true"
        >
          {count > 0 && (
            <span className="absolute inset-0 rounded-full bg-[var(--validation-emerald)] opacity-60 animate-ping" />
          )}
        </span>
        <Monitor size={12} className="text-[var(--muted-foreground)]" />
        <span
          className="font-mono text-[11px] tabular-nums text-[var(--foreground)]"
          data-testid="presence-badge-count"
        >
          {count}
        </span>
        <span className="hidden lg:inline font-mono text-[10px] uppercase tracking-wider text-[var(--muted-foreground)]">
          在線
        </span>
      </button>

      {open && (
        <div
          role="dialog"
          aria-label="Active devices"
          className="absolute right-0 top-full mt-1 z-50 min-w-[260px] max-w-[320px] rounded border border-[var(--border)] bg-[var(--card)] shadow-lg p-2 font-mono text-xs"
          data-testid="presence-badge-popover"
        >
          <div className="flex items-center justify-between px-1 pb-1.5 border-b border-[var(--border)] mb-1.5">
            <div className="flex items-center gap-1.5">
              <Monitor size={12} className="text-[var(--foreground)]" />
              <span className="text-[10px] uppercase tracking-wider font-semibold text-[var(--foreground)]">
                Active devices
              </span>
            </div>
            <span
              className="px-1.5 py-0.5 rounded-full bg-[var(--secondary)] text-[var(--muted-foreground)] text-[9px] font-mono font-bold"
              data-testid="presence-badge-popover-count"
            >
              {count}
            </span>
          </div>

          {error ? (
            <div
              className="px-2 py-1.5 rounded bg-[var(--destructive)]/10 text-[var(--destructive)] text-[10px]"
              data-testid="presence-badge-error"
            >
              {error}
            </div>
          ) : devices.length === 0 ? (
            <div className="px-2 py-3 text-center text-[var(--muted-foreground)] text-[10px]">
              {loading ? "Loading…" : "No active devices"}
            </div>
          ) : (
            <ul className="flex flex-col gap-1">
              {devices.map(d => {
                const Icon = deviceIcon(d.device_name)
                return (
                  <li
                    key={d.session_id}
                    data-testid={`presence-row-${d.session_id}`}
                    className={`flex items-center gap-2 px-2 py-1.5 rounded border ${
                      d.is_current
                        ? "border-[var(--neural-blue)]/40 bg-[var(--neural-blue)]/5"
                        : "border-transparent"
                    }`}
                  >
                    <Icon size={12} className="shrink-0 text-[var(--muted-foreground)]" />
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-1.5">
                        <span className="truncate text-[11px] text-[var(--foreground)]">
                          {d.device_name}
                        </span>
                        {d.is_current && (
                          <span
                            className="shrink-0 px-1 py-0.5 rounded bg-[var(--neural-blue)]/20 text-[var(--neural-blue)] text-[8px] font-bold"
                            data-testid={`presence-row-current-${d.session_id}`}
                          >
                            This device
                          </span>
                        )}
                      </div>
                      <div className="flex items-center gap-1.5 text-[9px] text-[var(--muted-foreground)]">
                        <span
                          className={
                            d.status === "active"
                              ? "text-[var(--validation-emerald)]"
                              : "text-[var(--muted-foreground)]"
                          }
                        >
                          {d.status}
                        </span>
                        <span aria-hidden="true">·</span>
                        <span title={`Last heartbeat ${d.idle_seconds.toFixed(1)}s ago`}>
                          {relativeTime(d.idle_seconds)}
                        </span>
                      </div>
                    </div>
                  </li>
                )
              })}
            </ul>
          )}
        </div>
      )}
    </div>
  )
}
