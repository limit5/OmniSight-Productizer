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
import { Block } from "./block"

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
    <Block icon={Activity} title="QUEUE" kind="orchestration.queue">
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
    </Block>
  )
}

function WorkerBlock({ snap }: { snap: OrchestrationSnapshot }) {
  const w = snap.workers
  const utilTone: Tone = w.utilisation >= 0.9 ? "bad"
    : w.utilisation >= 0.7 ? "warn" : "ok"
  return (
    <Block icon={Users} title="WORKERS" kind="orchestration.workers">
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
    </Block>
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
    <Block icon={GitMerge} title="MERGER" kind="orchestration.merger">
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

      {/* R22.3 (2026-04-25 follow-up²): two-line layout per row.
          Earlier single-line flex (label · bar · pct) squeezed the
          bar to almost nothing on narrow panels — the bar lives in
          the same row as the label + percentage, so any space they
          need is taken from the bar. Splitting into:
              line 1: label  ───────────────  percentage
              line 2: ████████████████████████████ (full-width bar)
          gives the bar the entire block width to draw in, makes the
          label + percentage roomy and easy to read, and keeps the
          vertical rhythm tidy. Bar height bumped to ``h-2.5`` (down
          from h-3) since it's now the only thing on its line and
          doesn't need to compete for visual weight. */}
      <div className="space-y-2.5">
        {rows.map((row) => {
          const pct = Math.max(0, Math.min(1, row.rate))
          const widthPct = pct === 0 ? 0 : Math.max(2, pct * 100)
          const isActive = pct > 0
          return (
            <div
              key={row.label}
              className="space-y-1 min-w-0"
              data-testid={`merger-row-${row.label.toLowerCase().replace(/\s+/g, "-")}`}
            >
              {/* Line 1 — label left, percentage right. Items use
                  ``items-baseline`` so the small label aligns with
                  the bottom of the bigger percentage digit, not its
                  top. */}
              <div className="flex items-baseline justify-between gap-2 min-w-0">
                <span
                  className="font-mono text-[9.5px] tracking-[0.18em] text-[var(--muted-foreground,#94a3b8)] truncate min-w-0 uppercase"
                  title={row.label}
                >
                  {row.label}
                </span>
                <span
                  className="font-mono text-[13px] font-bold tabular-nums shrink-0 leading-none whitespace-nowrap"
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

              {/* Line 2 — full-width bar. Track has the recessed
                  inset shadow look so the empty channel reads as
                  carved-in behind the fill. */}
              <div
                className="relative h-2.5 rounded-full overflow-hidden border border-[var(--neural-border,rgba(148,163,184,0.25))] w-full"
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
                  {/* Top sheen — dimensional "wet pill" look so the
                      bar reads as a 3D filled capsule, not flat. */}
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
    </Block>
  )
}

function LocksBlock({ snap }: { snap: OrchestrationSnapshot }) {
  const l = snap.locks
  const tasks = useMemo(() => Object.values(l.by_task), [l])
  // Use the snapshot moment as the reference instant — pure within
  // render and refreshes naturally on every poll.
  const now = snap.checked_at
  const isActive = l.total_tasks > 0

  // R23 (2026-04-25): meta-stats so the populated case has a meta
  // header (OLDEST / WIDEST) above the task list, matching MERGER's
  // visual weight and giving operators an at-a-glance "is anything
  // stuck?" answer without reading the per-task rows.
  const oldestTask = useMemo(() => {
    let best: (typeof tasks)[number] | null = null
    for (const t of tasks) {
      if (!best || t.oldest_acquired_at < best.oldest_acquired_at) best = t
    }
    return best
  }, [tasks])
  const oldestAge = oldestTask
    ? Math.max(0, now - oldestTask.oldest_acquired_at)
    : 0
  const widestTask = useMemo(() => {
    let best: (typeof tasks)[number] | null = null
    for (const t of tasks) {
      if (!best || t.paths.length > best.paths.length) best = t
    }
    return best
  }, [tasks])

  // Tone for OLDEST: green when fresh (< 1 min), info when normal,
  // warn when stuck for > 5 min.
  const oldestToneClass =
    oldestAge >= 300
      ? TONE_CLASS.warn
      : oldestAge >= 60
        ? TONE_CLASS.info
        : TONE_CLASS.ok

  return (
    <Block icon={Lock} title="LOCKS" kind="orchestration.locks">
      <div className="grid grid-cols-2 gap-1.5">
        <Kpi
          label="TASKS"
          value={String(l.total_tasks)}
          tone={isActive ? "info" : "ok"}
        />
        <Kpi
          label="PATHS"
          value={String(l.total_paths)}
          tone={l.total_paths > 50 ? "warn" : isActive ? "info" : "ok"}
        />
      </div>

      {!isActive ? (
        // R23: ALL CLEAR empty state. Previously the panel just sat
        // showing two "0" tiles next to a much taller MERGER block,
        // which made it look broken / unfinished. The green-tinted
        // status row + helper line balance the vertical weight and
        // make the "nothing is wrong" state read as intentional.
        <div
          className="mt-2 flex flex-col gap-1 p-2 rounded-sm border border-[var(--validation-emerald,#10b981)]/25 bg-[var(--validation-emerald,#10b981)]/[0.05]"
          data-testid="locks-all-clear"
        >
          <div className="flex items-center gap-1.5 font-mono text-[10px] tracking-[0.18em] text-[var(--validation-emerald,#10b981)] font-semibold">
            <CheckCircle2 size={11} aria-hidden />
            ALL CLEAR
          </div>
          <div className="font-mono text-[9px] text-[var(--muted-foreground,#94a3b8)] leading-snug">
            No path contention right now — every workspace is free to
            acquire any file path. Bars populate the moment two tasks
            try to write the same path.
          </div>
        </div>
      ) : (
        // R23: populated state — meta header above the task list so
        // the operator sees the headline ("oldest 4m ago, widest holds
        // 12 paths") without scanning every row.
        <>
          <div className="mt-2 grid grid-cols-2 gap-1.5 font-mono text-[10px] text-[var(--muted-foreground,#94a3b8)]">
            <div className="flex items-center justify-between gap-1 px-1.5 py-0.5 rounded-sm bg-white/[0.03]">
              <span className="tracking-wider">OLDEST</span>
              <span className={`tabular-nums font-semibold ${oldestToneClass}`}>
                {oldestTask ? fmtAge(oldestAge) : "—"}
              </span>
            </div>
            <div className="flex items-center justify-between gap-1 px-1.5 py-0.5 rounded-sm bg-white/[0.03]">
              <span className="tracking-wider">WIDEST</span>
              <span className="tabular-nums font-semibold text-[var(--foreground,#e2e8f0)]">
                {widestTask ? `${widestTask.paths.length}p` : "—"}
              </span>
            </div>
          </div>
          <ul className="mt-1.5 max-h-24 overflow-y-auto font-mono text-[10px] text-[var(--muted-foreground,#94a3b8)] space-y-0.5 pr-0.5">
            {tasks.slice(0, 5).map((t) => {
              const ageS = Math.max(0, now - t.oldest_acquired_at)
              return (
                <li
                  key={t.task_id}
                  className="flex items-center justify-between gap-2 min-w-0"
                >
                  <span
                    className="truncate min-w-0"
                    title={t.paths.join("\n")}
                  >
                    {t.task_id}
                  </span>
                  <span className="text-[var(--foreground,#e2e8f0)] tabular-nums shrink-0 whitespace-nowrap">
                    {t.paths.length}p · {fmtAge(ageS)}
                  </span>
                </li>
              )
            })}
            {tasks.length > 5 && (
              <li className="opacity-70">+{tasks.length - 5} more…</li>
            )}
          </ul>
        </>
      )}
    </Block>
  )
}

function AwaitingHumanBlock({ snap }: { snap: OrchestrationSnapshot }) {
  const items = snap.awaiting_human_plus_two
  const warnSec = snap.awaiting_human_warn_hours * 3600
  return (
    <Block icon={Shield} title="AWAITING HUMAN +2" kind="orchestration.awaiting_human">
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
    </Block>
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
