"use client"

/**
 * OP-1819 - per-project decision/blocker inbox (Track B / Phase-2).
 *
 * Audit §2C gap 5: there was no project-scoped place to see "my
 * project's pending decisions" — operators had to scan the global
 * Decision Queue and mentally filter. This surfaces the same pending
 * decisions on the project page, scoped to one project.
 *
 * Like ProjectLifecycleProgress, this aligns the active project context
 * before issuing requests so the existing /decisions endpoint receives
 * the X-Project-Id header and returns project-scoped rows (the Y5 row 3
 * SQLAlchemy listener injects ``WHERE project_id = :p OR project_id IS
 * NULL``). No backend change — reuses listDecisions as-is.
 *
 * Read-only by design: actions (approve/reject/undo) stay in the single
 * global Decision Queue. Each row deep-links there via `/?decision=<id>`,
 * which scrolls the matching row into view and ring-pulses it.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import Link from "next/link"
import {
  AlertOctagon,
  AlertTriangle,
  ArrowUpRight,
  CheckCircle2,
  Info,
  Inbox,
  Loader2,
  RefreshCw,
} from "lucide-react"
import {
  listDecisions,
  subscribeEvents,
  type DecisionPayload,
  type DecisionSeverity,
  type SSEEvent,
} from "@/lib/api"
import { useProject } from "@/lib/project-context"

const POLL_MS = 15_000

// Severities that gate progress are surfaced as "blockers" and floated
// to the top of the inbox; the rest are routine pending decisions.
const BLOCKER_SEVERITIES: ReadonlySet<DecisionSeverity> = new Set<DecisionSeverity>([
  "risky",
  "destructive",
])

const SEVERITY_META: Record<
  DecisionSeverity,
  { label: string; color: string; Icon: typeof Info }
> = {
  info: { label: "INFO", color: "var(--muted-foreground,#94a3b8)", Icon: Info },
  routine: { label: "ROUTINE", color: "var(--neural-blue,#60a5fa)", Icon: Info },
  risky: { label: "RISKY", color: "#eab308", Icon: AlertTriangle },
  destructive: { label: "DESTRUCTIVE", color: "var(--critical-red,#ef4444)", Icon: AlertOctagon },
}

function describeError(exc: unknown): string {
  if (exc instanceof Error) return exc.message
  return String(exc)
}

function isBlocker(d: DecisionPayload): boolean {
  return BLOCKER_SEVERITIES.has(d.severity)
}

// Blockers first, then newest-created first within each group.
function sortInboxOrder(items: DecisionPayload[]): DecisionPayload[] {
  return [...items].sort((a, b) => {
    const ba = isBlocker(a) ? 1 : 0
    const bb = isBlocker(b) ? 1 : 0
    if (ba !== bb) return bb - ba
    return (b.created_at || 0) - (a.created_at || 0)
  })
}

export function ProjectDecisionInbox({ projectId }: { projectId: string }) {
  const { currentProjectId, projectChangeEpoch, switchProject } = useProject()
  const [items, setItems] = useState<DecisionPayload[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [loaded, setLoaded] = useState(false)
  const mountedRef = useRef(true)
  const scoped = currentProjectId === projectId

  // Align the active project context so the X-Project-Id header is set
  // before listDecisions fires — same handshake as the lifecycle panel.
  useEffect(() => {
    if (currentProjectId !== projectId) {
      switchProject(projectId)
    }
  }, [currentProjectId, projectId, switchProject])

  const refresh = useCallback(async () => {
    if (!scoped) return
    setLoading(true)
    setError(null)
    try {
      const { items: pending } = await listDecisions("pending", 100)
      if (!mountedRef.current) return
      setItems(sortInboxOrder(pending))
      setLoaded(true)
    } catch (exc) {
      if (!mountedRef.current) return
      setError(describeError(exc))
    } finally {
      if (mountedRef.current) setLoading(false)
    }
  }, [scoped])

  useEffect(() => {
    mountedRef.current = true
    if (!scoped) return () => { mountedRef.current = false }
    // eslint-disable-next-line react-hooks/set-state-in-effect -- fetch-on-mount populates state from the existing endpoint
    void refresh()
    const interval = setInterval(() => void refresh(), POLL_MS)
    // Any decision lifecycle event can change what's pending for this
    // project; refetch (the endpoint stays project-scoped via header).
    const sub = subscribeEvents((ev: SSEEvent) => {
      if (
        ev.event === "decision_pending"
        || ev.event === "decision_resolved"
        || ev.event === "decision_auto_executed"
        || ev.event === "decision_undone"
      ) {
        void refresh()
      }
    })
    return () => {
      mountedRef.current = false
      clearInterval(interval)
      sub.close()
    }
  }, [refresh, scoped, projectChangeEpoch])

  const blockerCount = useMemo(() => items.filter(isBlocker).length, [items])

  if (!scoped) {
    return (
      <section
        className="rounded border border-[var(--border)] bg-[var(--card)] p-5 font-mono text-xs text-[var(--muted-foreground)]"
        data-testid="project-decision-inbox-scoping"
      >
        <Loader2 size={14} className="mr-2 inline animate-spin" />
        Scoping decision inbox...
      </section>
    )
  }

  return (
    <section
      className="holo-glass-simple corner-brackets-full rounded-sm border border-[var(--neural-border,rgba(148,163,184,0.35))]"
      aria-label="Project decision inbox"
      data-testid="project-decision-inbox"
    >
      <header className="flex flex-wrap items-center justify-between gap-2 border-b border-[var(--neural-border,rgba(148,163,184,0.35))] px-3 py-2">
        <div className="flex items-center gap-2">
          <Inbox className="h-4 w-4 text-[var(--neural-cyan,#67e8f9)]" aria-hidden />
          <h2 className="font-mono text-sm tracking-wider text-[var(--neural-cyan,#67e8f9)]">
            DECISION INBOX
          </h2>
          {items.length > 0 && (
            <span
              className="inline-block rounded-sm bg-[var(--neural-cyan,#67e8f9)] px-1.5 py-0.5 text-center font-mono text-[10px] tabular-nums text-black"
              style={{ minWidth: 22 }}
              aria-label={`${items.length} pending decisions for this project`}
              title={`${items.length} pending decision${items.length === 1 ? "" : "s"}`}
            >
              {items.length > 99 ? "99+" : items.length}
            </span>
          )}
          {blockerCount > 0 && (
            <span
              className="inline-block rounded-sm bg-[var(--critical-red,#ef4444)] px-1.5 py-0.5 text-center font-mono text-[10px] tabular-nums text-white"
              style={{ minWidth: 22 }}
              aria-label={`${blockerCount} blockers for this project`}
              title={`${blockerCount} blocker${blockerCount === 1 ? "" : "s"} (risky/destructive)`}
            >
              {blockerCount > 99 ? "99+" : blockerCount} ⚠
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          <Link
            href="/?panel=decisions"
            className="inline-flex items-center gap-1 font-mono text-[10px] text-[var(--muted-foreground)] hover:text-[var(--foreground)]"
            data-testid="project-decision-inbox-queue-link"
          >
            Global queue
            <ArrowUpRight size={12} />
          </Link>
          <button
            type="button"
            onClick={() => void refresh()}
            disabled={loading}
            className="inline-flex items-center gap-1 rounded-sm border border-[var(--border)] px-2 py-1 font-mono text-[10px] text-[var(--muted-foreground)] hover:text-[var(--foreground)] disabled:opacity-50"
            data-testid="project-decision-inbox-refresh"
          >
            <RefreshCw size={12} className={loading ? "animate-spin" : ""} />
            Refresh
          </button>
        </div>
      </header>

      {error && (
        <div
          className="border-b border-[var(--destructive)]/30 px-3 py-2 font-mono text-[10px] text-[var(--destructive)]"
          role="alert"
          data-testid="project-decision-inbox-error"
        >
          {error}
        </div>
      )}

      {loading && !loaded ? (
        <div
          className="flex items-center justify-center p-10 font-mono text-xs text-[var(--muted-foreground)]"
          data-testid="project-decision-inbox-loading"
        >
          <Loader2 size={14} className="mr-2 animate-spin" />
          Loading decisions...
        </div>
      ) : items.length === 0 ? (
        <div
          className="flex flex-col items-center gap-2 p-8 text-center font-mono text-xs text-[var(--muted-foreground)]"
          data-testid="project-decision-inbox-empty"
        >
          <CheckCircle2
            className="h-8 w-8 text-[var(--validation-emerald,#10b981)]"
            aria-hidden
          />
          <span className="font-semibold tracking-wider text-[var(--foreground)]">
            ALL CLEAR
          </span>
          <span className="max-w-[34ch] leading-snug">
            No pending decisions or blockers for this project. New approvals
            an agent needs will appear here.
          </span>
        </div>
      ) : (
        <ul
          className="max-h-[360px] divide-y divide-[var(--neural-border,rgba(148,163,184,0.15))] overflow-y-auto"
          data-testid="project-decision-inbox-list"
        >
          {items.map((d) => (
            <InboxRow key={d.id} d={d} />
          ))}
        </ul>
      )}
    </section>
  )
}

function InboxRow({ d }: { d: DecisionPayload }) {
  const meta = SEVERITY_META[d.severity] || SEVERITY_META.routine
  const { Icon } = meta
  const blocker = isBlocker(d)

  return (
    <li
      className="px-3 py-2"
      data-testid={`project-decision-inbox-row-${d.id}`}
      data-blocker={blocker ? "true" : "false"}
    >
      <Link
        href={`/?decision=${encodeURIComponent(d.id)}`}
        className="group flex items-start gap-2 rounded-sm hover:bg-white/5"
        title="Open in the global Decision Queue to act on this"
      >
        <Icon className="mt-0.5 h-4 w-4 shrink-0" style={{ color: meta.color }} aria-hidden />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span
              className="rounded-sm px-1 font-mono text-[9px]"
              style={{ backgroundColor: `${meta.color}22`, color: meta.color }}
            >
              {meta.label}
            </span>
            {blocker && (
              <span
                className="rounded-sm border px-1 font-mono text-[9px]"
                style={{
                  color: "var(--critical-red,#ef4444)",
                  borderColor: "var(--critical-red,#ef4444)",
                }}
                data-testid="project-decision-inbox-blocker-chip"
              >
                BLOCKER
              </span>
            )}
            <span className="font-mono text-[9px] text-[var(--muted-foreground)]">
              {d.kind}
            </span>
            <ArrowUpRight
              size={12}
              className="ml-auto text-[var(--muted-foreground)] opacity-0 transition-opacity group-hover:opacity-100"
              aria-hidden
            />
          </div>
          <div className="mt-0.5 break-words text-xs font-medium text-[var(--foreground)]">
            {d.title}
          </div>
          {d.detail && (
            <div className="mt-0.5 break-words text-[11px] text-[var(--muted-foreground)]">
              {d.detail}
            </div>
          )}
        </div>
      </Link>
    </li>
  )
}
