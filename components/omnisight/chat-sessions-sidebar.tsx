"use client"

/**
 * ZZ.B2 #304-2 checkbox 1 — left-sidebar chat/workflow session list.
 *
 * Replaces the interim "raw `session_id[:8]` hash" display with the
 * LLM-generated descriptive title stored in
 * `chat_sessions.metadata.auto_title`. The full fallback chain
 * (including `user_title`) is owned here on the frontend; the
 * backend endpoint just returns the raw metadata blob.
 *
 * Data flow:
 *   1. Mount → `GET /chat/sessions?limit=50` hydrates the list.
 *   2. SSE `session.titled` → merge the title into the matching row
 *      in-place so operators never need to refetch after a background
 *      task wrote the auto_title.
 *   3. SSE `chat.message` → bump the matching row's recency so a
 *      session that just received a new turn floats to the top.
 *      (Insert a stub row if the session isn't known yet — upsert
 *      happened server-side; the stub is our best-effort local mirror.)
 */

import { useCallback, useEffect, useMemo, useState } from "react"
import { MessageCircle, Sparkles, RefreshCw } from "lucide-react"
import {
  fetchChatSessions,
  subscribeEvents,
  type ChatSessionItem,
  type SSEEvent,
} from "@/lib/api"

export interface ChatSessionsSidebarProps {
  /** Override the page size (defaults to 50). */
  limit?: number
  /** Click handler — fired when the operator picks a session row. */
  onSelect?: (sessionId: string) => void
  /** Currently-selected session id (highlights the row). */
  selectedSessionId?: string | null
  /** Initial rows injected for test / storybook. */
  initialSessions?: ChatSessionItem[]
  className?: string
}

function hashFallback(sessionId: string): string {
  if (!sessionId) return "(no session)"
  return sessionId.slice(0, 8) + "…"
}

/**
 * Fallback chain per the ZZ.B2 spec:
 *   1. operator-set `metadata.user_title` (reserved; empty today)
 *   2. LLM-generated `metadata.auto_title`
 *   3. raw `session_id[:8]` hash
 */
export function resolveSessionTitle(item: ChatSessionItem): {
  title: string
  source: "user" | "auto" | "hash"
} {
  const userTitle = typeof item.metadata?.user_title === "string"
    ? item.metadata.user_title.trim()
    : ""
  if (userTitle) return { title: userTitle, source: "user" }
  const autoTitle = typeof item.metadata?.auto_title === "string"
    ? item.metadata.auto_title.trim()
    : ""
  if (autoTitle) return { title: autoTitle, source: "auto" }
  return { title: hashFallback(item.session_id), source: "hash" }
}

export function ChatSessionsSidebar({
  limit = 50,
  onSelect,
  selectedSessionId,
  initialSessions,
  className,
}: ChatSessionsSidebarProps) {
  const [sessions, setSessions] = useState<ChatSessionItem[]>(
    () => initialSessions ?? [],
  )
  const [loading, setLoading] = useState(initialSessions === undefined)
  const [error, setError] = useState<string | null>(null)

  const reload = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await fetchChatSessions({ limit })
      setSessions(res.items)
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setLoading(false)
    }
  }, [limit])

  useEffect(() => {
    if (initialSessions !== undefined) return
    void reload()
  }, [reload, initialSessions])

  useEffect(() => {
    const sub = subscribeEvents((ev: SSEEvent) => {
      if (ev.event === "session.titled") {
        const d = ev.data
        setSessions((prev) => {
          const idx = prev.findIndex((s) => s.session_id === d.session_id)
          if (idx < 0) {
            // Unknown session (e.g. we mounted after the row appeared
            // on another worker). Inject a lightweight stub so the
            // operator sees the title immediately; the next reload
            // fills in the real created_at / updated_at.
            const now = Date.now() / 1000
            return [
              {
                session_id: d.session_id,
                user_id: d.user_id,
                tenant_id: "",
                metadata: { auto_title: d.title },
                created_at: now,
                updated_at: now,
              },
              ...prev,
            ]
          }
          const existing = prev[idx]
          const nextMeta =
            d.source === "user"
              ? { ...existing.metadata, user_title: d.title }
              : { ...existing.metadata, auto_title: d.title }
          const updated: ChatSessionItem = {
            ...existing,
            metadata: nextMeta,
            updated_at: Date.now() / 1000,
          }
          const without = prev.filter((_, i) => i !== idx)
          return [updated, ...without]
        })
        return
      }
      if (ev.event === "chat.message") {
        // Bump recency on the matching row. The backend already
        // upserted the chat_sessions row, so the row must exist
        // server-side; we just reflect it locally so the sidebar
        // reorders without a refetch.
        const d = ev.data
        const sid =
          (d as { session_id?: string }).session_id ??
          (d as Record<string, unknown>)["session_id"] as string | undefined
        if (!sid || typeof sid !== "string") return
        setSessions((prev) => {
          const idx = prev.findIndex((s) => s.session_id === sid)
          if (idx < 0) return prev
          const updated: ChatSessionItem = {
            ...prev[idx],
            updated_at: Date.now() / 1000,
          }
          const without = prev.filter((_, i) => i !== idx)
          return [updated, ...without]
        })
      }
    })
    return () => { sub.close() }
  }, [])

  const sorted = useMemo(
    () => [...sessions].sort((a, b) => b.updated_at - a.updated_at),
    [sessions],
  )

  return (
    <aside
      data-testid="chat-sessions-sidebar"
      className={
        "flex min-h-0 w-full flex-col gap-2 rounded border border-[var(--border)] bg-[var(--card)] p-3 " +
        (className ?? "")
      }
    >
      <header className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 font-mono text-xs font-semibold tracking-wider text-[var(--foreground)]">
          <MessageCircle size={14} />
          <span>WORKFLOW / CHAT</span>
          <span className="rounded-full bg-[var(--secondary)] px-1.5 py-0.5 text-[9px] font-bold text-[var(--muted-foreground)]">
            {sorted.length}
          </span>
        </div>
        <button
          type="button"
          onClick={() => void reload()}
          disabled={loading}
          data-testid="chat-sessions-refresh"
          className="rounded p-1 text-[var(--muted-foreground)] hover:bg-[var(--secondary)] disabled:opacity-50"
          aria-label="Refresh sessions"
        >
          <RefreshCw size={12} className={loading ? "animate-spin" : ""} />
        </button>
      </header>
      {error && (
        <div
          data-testid="chat-sessions-error"
          className="rounded border border-[var(--critical-red)] bg-[var(--critical-red)]/10 px-2 py-1 font-mono text-[10px] text-[var(--critical-red)]"
        >
          {error}
        </div>
      )}
      <ul
        data-testid="chat-sessions-list"
        className="flex min-h-0 flex-1 flex-col gap-1 overflow-y-auto"
      >
        {sorted.length === 0 && !loading ? (
          <li
            data-testid="chat-sessions-empty"
            className="rounded border border-dashed border-[var(--border)] px-3 py-4 text-center font-mono text-[10px] text-[var(--muted-foreground)]"
          >
            No chat sessions yet — start a conversation to see it here.
          </li>
        ) : (
          sorted.map((s) => {
            const resolved = resolveSessionTitle(s)
            const selected = s.session_id === selectedSessionId
            return (
              <li
                key={s.session_id}
                data-testid={`chat-session-row-${s.session_id}`}
                data-title-source={resolved.source}
              >
                <button
                  type="button"
                  onClick={() => onSelect?.(s.session_id)}
                  className={
                    "flex w-full items-center gap-2 rounded border px-2 py-1.5 text-left transition-colors " +
                    (selected
                      ? "border-[var(--neural-blue)] bg-[var(--neural-blue)]/10"
                      : "border-transparent hover:border-[var(--border)] hover:bg-[var(--secondary)]/60")
                  }
                >
                  {resolved.source === "auto" && (
                    <Sparkles
                      size={10}
                      className="shrink-0 text-[var(--validation-emerald,#10b981)]"
                      aria-label="auto-titled"
                      data-testid={`chat-session-auto-badge-${s.session_id}`}
                    />
                  )}
                  <span
                    data-testid={`chat-session-title-${s.session_id}`}
                    className="truncate font-mono text-xs text-[var(--foreground)]"
                    title={
                      resolved.source === "hash"
                        ? s.session_id
                        : resolved.title
                    }
                  >
                    {resolved.title}
                  </span>
                </button>
              </li>
            )
          })
        )}
      </ul>
    </aside>
  )
}

export default ChatSessionsSidebar
