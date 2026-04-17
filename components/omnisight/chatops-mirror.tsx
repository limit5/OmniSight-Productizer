"use client"

/**
 * R1 (#307) — ChatOps Mirror Panel.
 *
 * Bi-directional mirror of Discord / Teams / Line traffic plus an
 * operator-side "inject hint" / "approve PEP" surface so you don't have
 * to switch apps.
 *
 * Data flow:
 *   SSE `chatops.message` → prepend to in-memory ring (dedupe by id).
 *   Initial hydrate via GET /chatops/mirror.
 *   Adapter status chip per channel → GET /chatops/status.
 */

import { useCallback, useEffect, useMemo, useState } from "react"
import {
  Send, MessageSquare, ArrowDownCircle, ArrowUpCircle, Search, Filter,
  Wifi, WifiOff, RefreshCw, Zap, ShieldAlert, Hash,
} from "lucide-react"
import {
  type ChatOpsMessageEvent, type ChatOpsAdapterStatus,
  getChatOpsMirror, injectAgentHint, sendChatOpsInteractive,
  decidePepFromChatOps, subscribeEvents, type SSEEvent,
} from "@/lib/api"

const MAX_RING = 300

type Direction = "all" | "outbound" | "inbound"
type ChannelId = "all" | "discord" | "teams" | "line" | "dashboard"

function tsLabel(ts: number): string {
  if (!ts) return "—"
  const d = new Date(ts * 1000)
  return d.toLocaleTimeString(undefined, {
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  })
}

function truncate(s: string | undefined, n = 240): string {
  if (!s) return ""
  return s.length > n ? s.slice(0, n) + "…" : s
}

const CHANNEL_COLOR: Record<string, string> = {
  discord: "#5865F2",
  teams: "#464EB8",
  line: "#06C755",
  dashboard: "var(--neural-blue, #60a5fa)",
}

export function ChatOpsMirror() {
  const [items, setItems] = useState<ChatOpsMessageEvent[]>([])
  const [status, setStatus] = useState<Record<string, ChatOpsAdapterStatus>>({})
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const [channelFilter, setChannelFilter] = useState<ChannelId>("all")
  const [directionFilter, setDirectionFilter] = useState<Direction>("all")
  const [search, setSearch] = useState("")

  const [injectAgent, setInjectAgent] = useState("")
  const [injectText, setInjectText] = useState("")
  const [injectBusy, setInjectBusy] = useState(false)
  const [injectFlash, setInjectFlash] = useState<string | null>(null)

  const [composeChannel, setComposeChannel] = useState<ChannelId>("discord")
  const [composeText, setComposeText] = useState("")
  const [composeBusy, setComposeBusy] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const snap = await getChatOpsMirror(MAX_RING)
      setItems(snap.items)
      setStatus(snap.status)
      setError(null)
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void load() }, [load])

  useEffect(() => {
    const sub = subscribeEvents((ev: SSEEvent) => {
      if (ev.event !== "chatops.message") return
      const d = ev.data as ChatOpsMessageEvent
      setItems((prev) => {
        const without = prev.filter((x) => x.id !== d.id)
        return [d, ...without].slice(0, MAX_RING)
      })
    })
    return () => { sub.close() }
  }, [])

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase()
    return items.filter((m) => {
      if (directionFilter !== "all" && m.direction !== directionFilter) return false
      if (channelFilter !== "all" && m.channel !== channelFilter) return false
      if (!q) return true
      const hay = (
        (m.body || "") + " " + (m.title || "") + " " +
        (m.author || "") + " " + (m.command || "") + " " +
        (m.button_id || "")
      ).toLowerCase()
      return hay.includes(q)
    })
  }, [items, directionFilter, channelFilter, search])

  const doInject = useCallback(async () => {
    const aid = injectAgent.trim()
    const text = injectText.trim()
    if (!aid || !text) {
      setInjectFlash("agent id and hint text required")
      return
    }
    setInjectBusy(true)
    setInjectFlash(null)
    try {
      await injectAgentHint(aid, text, "dashboard")
      setInjectFlash("✓ injected")
      setInjectText("")
    } catch (exc) {
      setInjectFlash(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setInjectBusy(false)
      setTimeout(() => setInjectFlash(null), 3000)
    }
  }, [injectAgent, injectText])

  const doCompose = useCallback(async () => {
    const body = composeText.trim()
    if (!body) return
    setComposeBusy(true)
    try {
      await sendChatOpsInteractive(composeChannel === "all" ? "*" : composeChannel, body, {
        title: "Operator message",
      })
      setComposeText("")
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setComposeBusy(false)
    }
  }, [composeChannel, composeText])

  const approvePep = useCallback(async (pepId: string) => {
    try {
      await decidePepFromChatOps(pepId, "approve")
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc))
    }
  }, [])

  const rejectPep = useCallback(async (pepId: string) => {
    try {
      await decidePepFromChatOps(pepId, "reject")
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc))
    }
  }, [])

  return (
    <div className="p-4 space-y-4">
      {/* Header with adapter status chips */}
      <div className="flex flex-wrap items-center gap-2">
        <div className="flex items-center gap-2 font-mono text-sm tracking-wider">
          <MessageSquare size={16} className="text-[var(--neural-cyan, #67e8f9)]" />
          <span className="font-semibold">ChatOps Mirror</span>
          <span className="text-xs text-[var(--muted-foreground)]">
            R1 · bi-directional bridge
          </span>
        </div>
        <div className="flex-1" />
        {Object.entries(status).map(([name, st]) => (
          <span
            key={name}
            title={st.reason}
            className="inline-flex items-center gap-1 px-2 py-0.5 rounded font-mono text-[10px]"
            style={{
              backgroundColor: st.configured ? "rgba(16,185,129,0.1)" : "rgba(148,163,184,0.1)",
              color: st.configured ? "var(--validation-emerald, #10b981)" : "var(--muted-foreground)",
              border: `1px solid ${st.configured ? "var(--validation-emerald, #10b981)" : "var(--muted)"}`,
            }}
          >
            {st.configured ? <Wifi size={10} /> : <WifiOff size={10} />}
            {name}
          </span>
        ))}
        <button
          onClick={() => void load()}
          className="p-1 rounded hover:bg-[var(--secondary)] transition-colors"
          title="Reload"
        >
          <RefreshCw size={14} className={loading ? "animate-spin" : ""} />
        </button>
      </div>

      {error && (
        <div className="px-3 py-2 rounded border border-[var(--critical-red)] bg-[var(--critical-red)]/10 font-mono text-xs text-[var(--critical-red)]">
          {error}
        </div>
      )}

      {/* Inject + compose row */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
        {/* Inject hint */}
        <div className="rounded border border-[var(--border)] p-3 space-y-2 bg-[var(--secondary)]/40">
          <div className="flex items-center gap-2 font-mono text-xs">
            <Zap size={12} className="text-[var(--fui-orange, #f59e0b)]" />
            <span className="font-semibold">Inject human hint</span>
            <span className="text-[10px] text-[var(--muted-foreground)]">
              rate-limited · sanitized · audited
            </span>
          </div>
          <div className="flex gap-2">
            <input
              value={injectAgent}
              onChange={(e) => setInjectAgent(e.target.value)}
              placeholder="agent_id"
              className="w-32 px-2 py-1 rounded border border-[var(--border)] bg-[var(--background)] font-mono text-xs"
            />
            <input
              value={injectText}
              onChange={(e) => setInjectText(e.target.value)}
              placeholder="hint text (max 2000 chars, tags stripped)"
              className="flex-1 px-2 py-1 rounded border border-[var(--border)] bg-[var(--background)] font-mono text-xs"
              maxLength={2000}
              onKeyDown={(e) => { if (e.key === "Enter") void doInject() }}
            />
            <button
              onClick={() => void doInject()}
              disabled={injectBusy}
              className="px-3 py-1 rounded bg-[var(--fui-orange, #f59e0b)]/20 border border-[var(--fui-orange, #f59e0b)] font-mono text-xs hover:bg-[var(--fui-orange, #f59e0b)]/30 disabled:opacity-50"
            >
              {injectBusy ? "…" : "Inject"}
            </button>
          </div>
          {injectFlash && (
            <div className="font-mono text-[10px] text-[var(--muted-foreground)]">{injectFlash}</div>
          )}
        </div>

        {/* Compose */}
        <div className="rounded border border-[var(--border)] p-3 space-y-2 bg-[var(--secondary)]/40">
          <div className="flex items-center gap-2 font-mono text-xs">
            <Send size={12} className="text-[var(--neural-blue, #60a5fa)]" />
            <span className="font-semibold">Send to ChatOps</span>
          </div>
          <div className="flex gap-2">
            <select
              value={composeChannel}
              onChange={(e) => setComposeChannel(e.target.value as ChannelId)}
              className="px-2 py-1 rounded border border-[var(--border)] bg-[var(--background)] font-mono text-xs"
            >
              <option value="discord">discord</option>
              <option value="teams">teams</option>
              <option value="line">line</option>
              <option value="all">* all</option>
            </select>
            <input
              value={composeText}
              onChange={(e) => setComposeText(e.target.value)}
              placeholder="broadcast message to channel"
              className="flex-1 px-2 py-1 rounded border border-[var(--border)] bg-[var(--background)] font-mono text-xs"
              onKeyDown={(e) => { if (e.key === "Enter") void doCompose() }}
            />
            <button
              onClick={() => void doCompose()}
              disabled={composeBusy || !composeText.trim()}
              className="px-3 py-1 rounded bg-[var(--neural-blue, #60a5fa)]/20 border border-[var(--neural-blue, #60a5fa)] font-mono text-xs hover:bg-[var(--neural-blue, #60a5fa)]/30 disabled:opacity-50"
            >
              {composeBusy ? "…" : "Send"}
            </button>
          </div>
        </div>
      </div>

      {/* Filters */}
      <div className="flex flex-wrap items-center gap-2">
        <Filter size={12} className="text-[var(--muted-foreground)]" />
        <select
          value={channelFilter}
          onChange={(e) => setChannelFilter(e.target.value as ChannelId)}
          className="px-2 py-1 rounded border border-[var(--border)] bg-[var(--background)] font-mono text-[10px]"
        >
          <option value="all">all channels</option>
          <option value="discord">discord</option>
          <option value="teams">teams</option>
          <option value="line">line</option>
          <option value="dashboard">dashboard</option>
        </select>
        <select
          value={directionFilter}
          onChange={(e) => setDirectionFilter(e.target.value as Direction)}
          className="px-2 py-1 rounded border border-[var(--border)] bg-[var(--background)] font-mono text-[10px]"
        >
          <option value="all">both</option>
          <option value="outbound">outbound</option>
          <option value="inbound">inbound</option>
        </select>
        <div className="flex-1 flex items-center gap-1">
          <Search size={12} className="text-[var(--muted-foreground)]" />
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="search title / body / author"
            className="flex-1 px-2 py-1 rounded border border-[var(--border)] bg-[var(--background)] font-mono text-[10px]"
          />
        </div>
        <span className="font-mono text-[10px] text-[var(--muted-foreground)]">
          {filtered.length}/{items.length}
        </span>
      </div>

      {/* Message list */}
      <div className="rounded border border-[var(--border)] bg-[var(--background)] max-h-[60vh] overflow-y-auto divide-y divide-[var(--border)]">
        {filtered.length === 0 ? (
          <div className="p-8 text-center font-mono text-xs text-[var(--muted-foreground)]">
            No ChatOps traffic yet. Messages appear here as they flow through the bridge.
          </div>
        ) : (
          filtered.map((m) => {
            const pepId = (m.meta && typeof m.meta === "object" && "pep_id" in m.meta)
              ? String((m.meta as Record<string, unknown>).pep_id)
              : ""
            const isInbound = m.direction === "inbound"
            const Icon = isInbound ? ArrowDownCircle : ArrowUpCircle
            const color = CHANNEL_COLOR[m.channel] || "var(--muted-foreground)"
            return (
              <div key={m.id} className="px-3 py-2 hover:bg-[var(--secondary)]/50">
                <div className="flex items-center gap-2 mb-1">
                  <Icon
                    size={12}
                    style={{
                      color: isInbound
                        ? "var(--validation-emerald, #10b981)"
                        : "var(--neural-blue, #60a5fa)",
                    }}
                  />
                  <Hash size={10} style={{ color }} />
                  <span className="font-mono text-[10px]" style={{ color }}>{m.channel}</span>
                  {m.kind && (
                    <span className="font-mono text-[9px] px-1 rounded bg-[var(--secondary)]">
                      {m.kind}
                    </span>
                  )}
                  {m.author && (
                    <span className="font-mono text-[10px] text-[var(--muted-foreground)]">
                      · {m.author}
                    </span>
                  )}
                  <span className="flex-1" />
                  <span className="font-mono text-[9px] text-[var(--muted-foreground)]">
                    {tsLabel(m.ts)}
                  </span>
                </div>
                {m.title && (
                  <div className="font-mono text-xs font-semibold">{m.title}</div>
                )}
                {m.body && (
                  <div className="font-mono text-[11px] text-[var(--muted-foreground)] whitespace-pre-wrap">
                    {truncate(m.body)}
                  </div>
                )}
                {m.command && (
                  <div className="font-mono text-[10px] text-[var(--neural-cyan, #67e8f9)]">
                    /{m.command} {m.command_args || ""}
                  </div>
                )}
                {m.button_id && (
                  <div className="font-mono text-[10px] text-[var(--fui-orange, #f59e0b)]">
                    button: {m.button_id}
                  </div>
                )}
                {(m.buttons && m.buttons.length > 0) && (
                  <div className="mt-1 flex flex-wrap gap-1">
                    {m.buttons.map((b) => (
                      <span
                        key={b.id}
                        className="px-1.5 py-0.5 rounded font-mono text-[9px] border border-[var(--border)] bg-[var(--secondary)]"
                      >
                        {b.label} ({b.id})
                      </span>
                    ))}
                  </div>
                )}
                {pepId && m.direction === "outbound" && (
                  <div className="mt-1 flex gap-1">
                    <button
                      onClick={() => void approvePep(pepId)}
                      className="px-2 py-0.5 rounded font-mono text-[9px] border border-[var(--validation-emerald, #10b981)] text-[var(--validation-emerald, #10b981)] hover:bg-[var(--validation-emerald, #10b981)]/10"
                    >
                      <ShieldAlert size={9} className="inline mr-0.5" />approve PEP {pepId}
                    </button>
                    <button
                      onClick={() => void rejectPep(pepId)}
                      className="px-2 py-0.5 rounded font-mono text-[9px] border border-[var(--critical-red, #ef4444)] text-[var(--critical-red, #ef4444)] hover:bg-[var(--critical-red, #ef4444)]/10"
                    >
                      reject
                    </button>
                  </div>
                )}
                {(m.errors && m.errors.length > 0) && (
                  <div className="mt-1 font-mono text-[9px] text-[var(--critical-red, #ef4444)]">
                    errors: {m.errors.join(" · ")}
                  </div>
                )}
              </div>
            )
          })
        )}
      </div>
    </div>
  )
}
