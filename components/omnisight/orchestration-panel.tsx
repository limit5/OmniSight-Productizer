"use client"

/**
 * O9 (#272) — Orchestration Panel.
 *
 * One glance, four blocks:
 *
 *   1. Queue depth by priority (P0..P3) + total + state breakdown.
 *   2. Held locks grouped by task (path count + age).
 *   3. Merger Agent vote rates (+2 / abstain / security refusal).
 *   4. Awaiting-human-+2 list with merger confidence + age (>= warn
 *      threshold rendered amber, > 2× threshold rendered red).
 *
 * Polls /orchestration/snapshot every 10 s for the baseline, then
 * upgrades fields opportunistically off the SSE stream so the panel
 * never feels stale between polls.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  GitMerge,
  Layers,
  Lock,
  Shield,
  Users,
} from "lucide-react"
import {
  getOrchestrationSnapshot,
  subscribeEvents,
  type OrchestrationSnapshot,
  type AwaitingHumanEntry,
  type SSEEvent,
} from "@/lib/api"

const POLL_MS = 10_000
const PRIORITIES = ["P0", "P1", "P2", "P3"] as const

export function OrchestrationPanel() {
  const [snap, setSnap] = useState<OrchestrationSnapshot | null>(null)
  const [error, setError] = useState<string | null>(null)
  const mountedRef = useRef(true)

  const refresh = useCallback(async () => {
    try {
      const fresh = await getOrchestrationSnapshot()
      if (!mountedRef.current) return
      setSnap(fresh)
      setError(null)
    } catch (exc) {
      if (!mountedRef.current) return
      setError(exc instanceof Error ? exc.message : String(exc))
    }
  }, [])

  useEffect(() => {
    mountedRef.current = true
    void refresh() // eslint-disable-line react-hooks/set-state-in-effect -- mount fetch
    const t = setInterval(() => void refresh(), POLL_MS)
    return () => {
      mountedRef.current = false
      clearInterval(t)
    }
  }, [refresh])

  // SSE upgrades — keep the panel reactive between polls.
  useEffect(() => {
    const sub = subscribeEvents((ev: SSEEvent) => {
      if (ev.event === "orchestration.queue.tick") {
        setSnap((prev) => prev ? {
          ...prev,
          queue: ev.data.queue,
          workers: ev.data.workers,
          checked_at: Date.now() / 1000,
        } : prev)
      } else if (ev.event === "orchestration.change.awaiting_human_plus_two") {
        const item: AwaitingHumanEntry = {
          change_id: ev.data.change_id,
          project: ev.data.project,
          file_path: ev.data.file_path,
          merger_confidence: ev.data.merger_confidence,
          merger_rationale: "",
          review_url: ev.data.review_url,
          push_sha: ev.data.push_sha,
          awaiting_since: ev.data.awaiting_since,
          jira_ticket: ev.data.jira_ticket,
          age_seconds: 0,
        }
        setSnap((prev) => {
          if (!prev) return prev
          const existing = prev.awaiting_human_plus_two
          // Idempotent insert — the registry de-dupes on change_id.
          const next = existing.some((e) => e.change_id === item.change_id)
            ? existing.map((e) => e.change_id === item.change_id ? { ...e, ...item } : e)
            : [...existing, item]
          return { ...prev, awaiting_human_plus_two: next }
        })
      }
    })
    return () => sub.close()
  }, [])

  return (
    <section
      className="holo-glass-simple corner-brackets-full rounded-sm border border-[var(--neural-border,rgba(148,163,184,0.35))]"
      aria-label="Orchestration"
    >
      <header className="flex items-center justify-between px-3 py-2 border-b border-[var(--neural-border,rgba(148,163,184,0.35))]">
        <div className="flex items-center gap-2">
          <Layers className="w-4 h-4 text-[var(--neural-cyan,#67e8f9)]" aria-hidden />
          <h2 className="font-mono text-sm tracking-wider text-[var(--neural-cyan,#67e8f9)]">
            ORCHESTRATION
          </h2>
        </div>
        <StatusDot snap={snap} error={error} />
      </header>

      {error && (
        <div
          className="px-3 py-1.5 font-mono text-[10px] text-[var(--critical-red,#ef4444)] truncate"
          title={error}
        >
          ⚠ {error}
        </div>
      )}

      {!snap && !error && (
        <div className="px-3 py-6 text-center font-mono text-xs text-[var(--muted-foreground,#94a3b8)]">
          Loading…
        </div>
      )}

      {snap && (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3 p-3">
          <QueueBlock snap={snap} />
          <WorkerBlock snap={snap} />
          <MergerBlock snap={snap} />
          <LocksBlock snap={snap} />
          <div className="md:col-span-2">
            <AwaitingHumanBlock snap={snap} />
          </div>
        </div>
      )}
    </section>
  )
}

// ─── Sub-blocks ────────────────────────────────────────────────

function QueueBlock({ snap }: { snap: OrchestrationSnapshot }) {
  const total = snap.queue.total
  return (
    <BlockShell icon={Activity} title="QUEUE">
      <div className="grid grid-cols-4 gap-1.5">
        {PRIORITIES.map((p) => {
          const value = snap.queue.by_priority[p] ?? 0
          const tone: Tone = p === "P0" && value > 0 ? "bad"
            : p === "P1" && value > 0 ? "warn" : value > 0 ? "info" : "ok"
          return (
            <Kpi key={p} label={p} value={String(value)} tone={tone} />
          )
        })}
      </div>
      <div className="mt-2 flex items-center justify-between font-mono text-[10px] text-[var(--muted-foreground,#94a3b8)]">
        <span>TOTAL: <span className="text-[var(--foreground,#e2e8f0)]">{total}</span></span>
        <span className="truncate ml-2" title="Top three states">
          {Object.entries(snap.queue.by_state)
            .filter(([, n]) => n > 0)
            .slice(0, 3)
            .map(([k, n]) => `${k}:${n}`)
            .join("  ")}
        </span>
      </div>
    </BlockShell>
  )
}

function WorkerBlock({ snap }: { snap: OrchestrationSnapshot }) {
  const w = snap.workers
  const utilTone: Tone = w.utilisation >= 0.9 ? "bad"
    : w.utilisation >= 0.7 ? "warn" : "ok"
  return (
    <BlockShell icon={Users} title="WORKERS">
      <div className="grid grid-cols-3 gap-1.5">
        <Kpi label="ACTIVE" value={String(Math.round(w.active))} tone="info" />
        <Kpi label="INFLIGHT" value={String(Math.round(w.inflight))} tone="info" />
        <Kpi
          label="CAP"
          value={w.capacity > 0 ? String(Math.round(w.capacity)) : "—"}
          tone={w.capacity > 0 ? "ok" : "info"}
        />
      </div>
      {w.capacity > 0 && (
        <div className="mt-2 font-mono text-[10px] text-[var(--muted-foreground,#94a3b8)]">
          UTIL:{" "}
          <span className={TONE_CLASS[utilTone]}>
            {(w.utilisation * 100).toFixed(0)}%
          </span>
        </div>
      )}
    </BlockShell>
  )
}

// R22 (2026-04-25): MERGER block redesigned from a cramped 3-col KPI
// tile grid (`+2 / ABSTAIN / SEC REF` with percentage values) into
// stacked horizontal bars. The old layout had three issues:
//   1. Labels were inconsistent length (+2 / ABSTAIN / SEC REF) and
//      cryptic ("SEC REF" took two glances to parse).
//   2. All three values rendered as percentages, so the eye couldn't
//      separate them visually — three identical-looking tiles.
//   3. Sibling blocks (QUEUE / WORKERS) use Kpi tiles for raw counts,
//      where the tile grid pattern works. Merger data is rate-shaped
//      (each value is a fraction of total_votes), which is what
//      stacked bars are designed for — proportion + comparison at a
//      glance.
function MergerBlock({ snap }: { snap: OrchestrationSnapshot }) {
  const m = snap.merger
  const fmtPct = (n: number) => `${(n * 100).toFixed(1)}%`
  // High +2 rate without much abstain MAY indicate LLM over-confidence;
  // ops uses this with the alert rule, but we also nudge here.
  const overconfident = m.total_votes >= 10 && m.plus_two_rate > 0.85
  const totalVotes = Math.round(m.total_votes)

  // Per-row colour comes from semantic meaning, not a generic Tone:
  //   - +2 APPROVAL      → emerald (good outcome) / warn-orange when
  //                        over-confident (too-easy approvals)
  //   - ABSTAIN          → muted (neutral) / warn-orange when > 50%
  //                        (merger is confused too often)
  //   - SECURITY REFUSAL → red whenever > 0 (notable signal of
  //                        risky-change attempts) / muted at 0
  const rows: Array<{
    label: string
    rate: number
    color: string
    track: string
  }> = [
    {
      label: "+2 APPROVAL",
      rate: m.plus_two_rate,
      color: overconfident
        ? "var(--fui-orange,#f59e0b)"
        : "var(--validation-emerald,#10b981)",
      track: overconfident
        ? "var(--fui-orange,#f59e0b)"
        : "var(--validation-emerald,#10b981)",
    },
    {
      label: "ABSTAIN",
      rate: m.abstain_rate,
      color: m.abstain_rate > 0.5
        ? "var(--fui-orange,#f59e0b)"
        : "var(--muted-foreground,#94a3b8)",
      track: m.abstain_rate > 0.5
        ? "var(--fui-orange,#f59e0b)"
        : "var(--muted-foreground,#94a3b8)",
    },
    {
      label: "SECURITY REFUSAL",
      rate: m.security_refusal_rate,
      color: m.security_refusal_rate > 0
        ? "var(--critical-red,#ef4444)"
        : "var(--muted-foreground,#94a3b8)",
      track: m.security_refusal_rate > 0
        ? "var(--critical-red,#ef4444)"
        : "var(--muted-foreground,#94a3b8)",
    },
  ]

  return (
    <BlockShell icon={GitMerge} title="MERGER">
      {/* Headline row: total votes is the denominator everything else
          is a fraction of, so it leads the block. Right-aligned warn
          chip when over-confidence threshold is hit so operators see
          the alarm next to the data that triggered it. */}
      <div className="flex items-center justify-between font-mono text-[10px] text-[var(--muted-foreground,#94a3b8)] mb-2">
        <span>
          TOTAL VOTES:{" "}
          <span className="text-[var(--foreground,#e2e8f0)] tabular-nums font-semibold">
            {totalVotes}
          </span>
        </span>
        {overconfident && (
          <span
            className="text-[var(--fui-orange,#f59e0b)] flex items-center gap-1 shrink-0"
            title="+2 rate above 85% with ≥10 votes — possible LLM over-confidence"
          >
            <AlertTriangle size={10} aria-hidden />
            +2 RATE HIGH
          </span>
        )}
      </div>

      {/* R22.2 (2026-04-25 follow-up): switch from fixed-track grid
          to flex layout. The earlier `grid-cols-[7rem_1fr_4rem]`
          set a hard minimum of ~12rem; on narrow viewports + 2-col
          parent grid the row's content overflowed into the LOCKS
          cell. Flex with explicit `shrink-0` on the value, `flex-1
          min-w-0` on the bar, and `min-w-0 + truncate` on the label
          gives a deterministic squeeze order under tight space:
          first the bar shrinks (it's the visual filler, not the
          critical info), then the label truncates. The percentage
          number is the most important data on the row, so it never
          shrinks. */}
      <div className="space-y-2">
        {rows.map((row) => {
          const pct = Math.max(0, Math.min(1, row.rate))
          const widthPct = pct === 0 ? 0 : Math.max(2, pct * 100)
          const isActive = pct > 0
          return (
            <div
              key={row.label}
              className="flex items-center gap-2 min-w-0"
              data-testid={`merger-row-${row.label.toLowerCase().replace(/\s+/g, "-")}`}
            >
              <span
                className="font-mono text-[9.5px] tracking-[0.12em] text-[var(--muted-foreground,#94a3b8)] truncate basis-24 shrink min-w-0"
                title={row.label}
              >
                {row.label}
              </span>
              <div
                className="relative h-3 rounded-full overflow-hidden border border-[var(--neural-border,rgba(148,163,184,0.25))] flex-1 min-w-0"
                style={{
                  background: `color-mix(in srgb, ${row.track} 6%, transparent)`,
                  boxShadow: "inset 0 1px 2px rgba(0,0,0,0.4)",
                }}
              >
                <div
                  className="relative h-full rounded-full transition-[width] duration-500 ease-out"
                  style={{
                    width: `${widthPct}%`,
                    background: isActive
                      ? `linear-gradient(180deg, color-mix(in srgb, ${row.color} 70%, white) 0%, ${row.color} 50%, color-mix(in srgb, ${row.color} 80%, black) 100%)`
                      : "transparent",
                    boxShadow: isActive
                      ? `0 0 10px color-mix(in srgb, ${row.color} 70%, transparent), inset 0 1px 0 color-mix(in srgb, ${row.color} 40%, white)`
                      : undefined,
                  }}
                >
                  {/* Top sheen — adds the dimensional "wet pill" look so
                      the bar reads as a 3D filled capsule, not a flat
                      rectangle. Only on non-zero bars. */}
                  {isActive && (
                    <div
                      aria-hidden
                      className="absolute inset-x-0 top-0 h-1/2 rounded-full opacity-40"
                      style={{
                        background:
                          "linear-gradient(180deg, rgba(255,255,255,0.55) 0%, transparent 100%)",
                      }}
                    />
                  )}
                </div>
              </div>
              <span
                className="font-mono text-[12px] font-bold tabular-nums text-right shrink-0 leading-none whitespace-nowrap"
                style={{
                  color: row.color,
                  textShadow: isActive
                    ? `0 0 8px color-mix(in srgb, ${row.color} 60%, transparent)`
                    : undefined,
                }}
              >
                {fmtPct(row.rate)}
              </span>
            </div>
          )
        })}
      </div>

      {/* Empty-state nudge when there have been zero votes — keeps
          three blank bars from looking like a bug. */}
      {totalVotes === 0 && (
        <div className="mt-2 font-mono text-[9px] text-[var(--muted-foreground,#94a3b8)] italic">
          No merger votes recorded yet — bars populate after the first
          conflict-resolution patchset.
        </div>
      )}
    </BlockShell>
  )
}

function LocksBlock({ snap }: { snap: OrchestrationSnapshot }) {
  const l = snap.locks
  const tasks = useMemo(() => Object.values(l.by_task), [l])
  // Use the snapshot moment as the reference instant — pure within
  // render and refreshes naturally on every poll.
  const now = snap.checked_at
  return (
    <BlockShell icon={Lock} title="LOCKS">
      <div className="grid grid-cols-2 gap-1.5">
        <Kpi label="TASKS" value={String(l.total_tasks)} tone={l.total_tasks > 0 ? "info" : "ok"} />
        <Kpi label="PATHS" value={String(l.total_paths)} tone={l.total_paths > 50 ? "warn" : "info"} />
      </div>
      {tasks.length > 0 && (
        <ul className="mt-2 max-h-24 overflow-y-auto font-mono text-[10px] text-[var(--muted-foreground,#94a3b8)] space-y-0.5">
          {tasks.slice(0, 5).map((t) => {
            const ageS = Math.max(0, now - t.oldest_acquired_at)
            return (
              <li key={t.task_id} className="flex items-center justify-between gap-2">
                <span className="truncate" title={t.paths.join("\n")}>
                  {t.task_id}
                </span>
                <span className="text-[var(--foreground,#e2e8f0)] tabular-nums">
                  {t.paths.length}p · {fmtAge(ageS)}
                </span>
              </li>
            )
          })}
          {tasks.length > 5 && (
            <li className="opacity-70">+{tasks.length - 5} more…</li>
          )}
        </ul>
      )}
    </BlockShell>
  )
}

function AwaitingHumanBlock({ snap }: { snap: OrchestrationSnapshot }) {
  const items = snap.awaiting_human_plus_two
  const warnSec = snap.awaiting_human_warn_hours * 3600
  return (
    <BlockShell icon={Shield} title="AWAITING HUMAN +2">
      {items.length === 0 ? (
        <div className="flex items-center gap-1 font-mono text-[10px] text-[var(--validation-emerald,#10b981)]">
          <CheckCircle2 size={11} aria-hidden />
          NONE PENDING
        </div>
      ) : (
        <ul className="font-mono text-[10px] space-y-1">
          {items.map((item) => {
            const tone: Tone = item.age_seconds > warnSec * 2 ? "bad"
              : item.age_seconds > warnSec ? "warn" : "info"
            const link = item.review_url || ""
            const Cell = link ? "a" : "span"
            const cellProps = link
              ? { href: link, target: "_blank", rel: "noreferrer" }
              : {}
            return (
              <li
                key={item.change_id}
                className="grid grid-cols-[minmax(0,1fr)_auto_auto] items-center gap-2 px-2 py-1 rounded-sm border border-[var(--neural-border,rgba(148,163,184,0.2))] bg-white/5"
              >
                <Cell
                  {...cellProps}
                  className="truncate text-[var(--foreground,#e2e8f0)] hover:underline"
                  title={`${item.change_id}\n${item.file_path}`}
                >
                  {item.change_id}{" "}
                  <span className="text-[var(--muted-foreground,#94a3b8)]">
                    · {item.file_path}
                  </span>
                </Cell>
                <span className="font-mono text-[10px] text-[var(--neural-cyan,#67e8f9)] tabular-nums">
                  c={item.merger_confidence.toFixed(2)}
                </span>
                <span className={`${TONE_CLASS[tone]} tabular-nums`}>
                  {fmtAge(item.age_seconds)}
                </span>
              </li>
            )
          })}
        </ul>
      )}
      <div className="mt-1 font-mono text-[9px] tracking-[0.18em] text-[var(--muted-foreground,#94a3b8)]">
        WARN ≥ {snap.awaiting_human_warn_hours}H
      </div>
    </BlockShell>
  )
}

// ─── Primitives ────────────────────────────────────────────────

type Tone = "ok" | "warn" | "bad" | "info"

const TONE_CLASS: Record<Tone, string> = {
  ok:   "text-[var(--validation-emerald,#10b981)]",
  warn: "text-[var(--fui-orange,#f59e0b)]",
  bad:  "text-[var(--critical-red,#ef4444)]",
  info: "text-[var(--neural-cyan,#67e8f9)]",
}

function BlockShell({
  icon: Icon, title, children,
}: { icon: typeof Activity; title: string; children: React.ReactNode }) {
  // R22.2 (2026-04-25): ``min-w-0`` is critical here. Parent dashboard
  // grid is ``grid-cols-1 md:grid-cols-2`` (each child is ``1fr``),
  // and CSS Grid items default to ``min-width: auto`` — i.e. they
  // refuse to shrink below their min-content. Without ``min-w-0``,
  // any block whose content has a wide intrinsic min-width (the
  // MERGER bars: label + bar + percent) overflowed into the
  // neighbouring column (LOCKS). Adding ``min-w-0`` lets the cell
  // shrink and the inner flex layout decide who absorbs the squeeze.
  return (
    <div className="flex flex-col gap-1.5 p-2 rounded-sm border border-[var(--neural-border,rgba(148,163,184,0.25))] bg-white/[0.02] min-w-0">
      <div className="flex items-center gap-1 font-mono text-[10px] tracking-[0.18em] text-[var(--muted-foreground,#94a3b8)]">
        <Icon className="w-3 h-3" aria-hidden />
        {title}
      </div>
      {children}
    </div>
  )
}

function Kpi({ label, value, tone }: { label: string; value: string; tone: Tone }) {
  return (
    <div className="flex flex-col items-start gap-0.5 p-1.5 rounded-sm border border-[var(--neural-border,rgba(148,163,184,0.2))] bg-white/5">
      <div className="font-mono text-[9px] tracking-[0.18em] text-[var(--muted-foreground,#94a3b8)]">
        {label}
      </div>
      <div className={`font-mono text-[14px] font-semibold tabular-nums leading-none ${TONE_CLASS[tone]}`}>
        {value}
      </div>
    </div>
  )
}

function StatusDot({
  snap, error,
}: { snap: OrchestrationSnapshot | null; error: string | null }) {
  let color = "var(--muted-foreground,#94a3b8)"
  let label = "loading"
  if (error) {
    color = "var(--critical-red,#ef4444)"
    label = "error"
  } else if (snap) {
    const items = snap.awaiting_human_plus_two
    const warnSec = snap.awaiting_human_warn_hours * 3600
    const queueP0 = snap.queue.by_priority["P0"] ?? 0
    if (items.some((i) => i.age_seconds > warnSec * 2) || queueP0 > 5) {
      color = "var(--critical-red,#ef4444)"
      label = "alert"
    } else if (items.some((i) => i.age_seconds > warnSec) || queueP0 > 0) {
      color = "var(--fui-orange,#f59e0b)"
      label = "degraded"
    } else {
      color = "var(--validation-emerald,#10b981)"
      label = "healthy"
    }
  }
  return (
    <span
      className="inline-flex items-center gap-1 font-mono text-[10px]"
      style={{ color }}
      aria-label={`status: ${label}`}
    >
      <span className="w-1.5 h-1.5 rounded-full" style={{ background: color }} />
      {label}
    </span>
  )
}

function fmtAge(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`
  if (seconds < 86_400) return `${(seconds / 3600).toFixed(1)}h`
  return `${(seconds / 86_400).toFixed(1)}d`
}
