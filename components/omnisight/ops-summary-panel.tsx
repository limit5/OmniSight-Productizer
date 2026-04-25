"use client"

/**
 * L1-04 — Ops Summary Panel.
 *
 * Polls /api/v1/ops/summary every 10 s. Intended for a quick-glance
 * operational check: spend burn-rate, any active freeze, pending
 * Decision Engine load, SSE bus pressure, watchdog liveness.
 *
 * Deliberately minimal. Zero new deps. No charts — just 6 KPIs and
 * a 🔴 / 🟢 dot. A Grafana dashboard is the right home for history;
 * this is the "is anything on fire right now?" glance.
 */

import { useCallback, useEffect, useRef, useState } from "react"
import {
  Activity, AlertTriangle, DollarSign, Flame, Radio, Shield, Clock3, Cpu, Brain,
  Layers, TimerReset, Gauge, Zap, TrendingUp,
} from "lucide-react"
import { getOpsSummary, forceTurboOverride, type OpsSummary } from "@/lib/api"

const POLL_MS = 10_000

export function OpsSummaryPanel() {
  const [data, setData] = useState<OpsSummary | null>(null)
  const [error, setError] = useState<string | null>(null)
  const mountedRef = useRef(true)

  const refresh = useCallback(async () => {
    try {
      const info = await getOpsSummary()
      if (!mountedRef.current) return
      setData(info)
      setError(null)
    } catch (exc) {
      if (!mountedRef.current) return
      setError(exc instanceof Error ? exc.message : String(exc))
    }
  }, [])

  useEffect(() => {
    mountedRef.current = true
    void refresh() // eslint-disable-line react-hooks/set-state-in-effect -- fetch-on-mount populates state from network
    const t = setInterval(() => void refresh(), POLL_MS)
    return () => {
      mountedRef.current = false
      clearInterval(t)
    }
  }, [refresh])

  return (
    <section
      className="holo-glass-simple corner-brackets-full rounded-sm border border-[var(--neural-border,rgba(148,163,184,0.35))]"
      aria-label="Ops Summary"
    >
      <header className="flex items-center justify-between px-3 py-2 border-b border-[var(--neural-border,rgba(148,163,184,0.35))]">
        <div className="flex items-center gap-2">
          <Shield className="w-4 h-4 text-[var(--neural-cyan,#67e8f9)]" aria-hidden />
          <h2 className="font-mono text-sm tracking-wider text-[var(--neural-cyan,#67e8f9)]">
            OPS SUMMARY
          </h2>
        </div>
        <StatusDot data={data} error={error} />
      </header>

      {error && (
        <div className="px-3 py-1.5 font-mono text-[10px] text-[var(--critical-red,#ef4444)] truncate" title={error}>
          ⚠ {error}
        </div>
      )}

      {!data && !error && (
        <div className="px-3 py-6 text-center font-mono text-xs text-[var(--muted-foreground,#94a3b8)]">
          Loading…
        </div>
      )}

      {data && (
        <div className="grid grid-cols-2 gap-2 p-3">
          <Kpi icon={DollarSign} label="SPEND (hr)"
               value={`$${data.hourly_cost_usd.toFixed(4)}`}
               tone={data.token_frozen ? "bad" : "ok"} />
          <Kpi icon={DollarSign} label="SPEND (day)"
               value={`$${data.daily_cost_usd.toFixed(2)}`}
               tone={data.token_frozen ? "bad" : "ok"} />
          <Kpi icon={Flame} label="BUDGET"
               value={data.budget_level}
               tone={data.token_frozen ? "bad" : data.budget_level === "normal" ? "ok" : "warn"} />
          <Kpi icon={Activity} label="DECISIONS"
               value={String(data.decisions_pending)}
               tone={data.decisions_pending === 0 ? "ok" : data.decisions_pending > 10 ? "warn" : "info"} />
          <Kpi icon={Radio} label="SSE SUBS"
               value={String(data.sse_subscribers)}
               tone="info" />
          <Kpi icon={Clock3} label="WATCHDOG"
               value={data.watchdog_age_s === null ? "—" : `${Math.round(data.watchdog_age_s)}s`}
               tone={data.watchdog_age_s === null || data.watchdog_age_s > 120 ? "warn" : "ok"} />
        </div>
      )}

      {/* H3 row 1524: Coordinator transparency — queue depth (tasks
          currently blocked waiting for a token), deferred count in the
          last 5 min, and the effective concurrency budget (may be less
          than CAPACITY_MAX when the Coordinator has derated the host).
          Hidden until the backend actually surfaces the snapshot so
          older backends degrade gracefully. */}
      {data && data.coordinator && (
        <div
          className="px-3 pb-2 -mt-1"
          data-testid="ops-coordinator-section"
        >
          <div className="font-mono text-[9px] tracking-[0.18em] text-[var(--muted-foreground,#94a3b8)] mb-1 flex items-center gap-1 flex-wrap">
            <Gauge size={10} aria-hidden />
            COORDINATOR
            {data.coordinator.derated && (
              <DerateBadge entry={data.coordinator} />
            )}
          </div>
          <CoordinatorRow entry={data.coordinator} />
          <ForceTurboControl
            entry={data.coordinator}
            onApplied={() => void refresh()}
          />
        </div>
      )}

      {/* H4a row 2583: Adaptive (AIMD) budget — current token budget plus
          5-min rolling trace of state-changing events. Hidden when the
          backend omits the snapshot so older backends degrade gracefully.
          Sits next to COORDINATOR because the AIMD-shaped budget is
          composed with the per-mode multiplier into the effective
          admission ceiling — ops staring at "EFF BUDGET 6/12" should
          immediately see whether the AIMD controller is the throttle
          (and how it has been moving). */}
      {data && data.aimd && (
        <div className="px-3 pb-2 -mt-1" data-testid="ops-aimd-section">
          <div className="font-mono text-[9px] tracking-[0.18em] text-[var(--muted-foreground,#94a3b8)] mb-1 flex items-center gap-1 flex-wrap">
            <TrendingUp size={10} aria-hidden />
            AIMD BUDGET
            <AimdReasonPill reason={data.aimd.last_reason} />
          </div>
          <AimdRow entry={data.aimd} />
        </div>
      )}

      {/* R2 (#308): Highest-entropy agent at-a-glance. Hidden entirely
          when the monitor hasn't produced any measurement yet so the
          panel stays empty on fresh deployments. */}
      {data && data.highest_entropy_agent && (
        <div className="px-3 pb-2 -mt-1">
          <div className="font-mono text-[9px] tracking-[0.18em] text-[var(--muted-foreground,#94a3b8)] mb-1 flex items-center gap-1">
            <Brain size={10} aria-hidden />
            HIGHEST ENTROPY
          </div>
          <HighestEntropyRow entry={data.highest_entropy_agent} />
        </div>
      )}

      {/* Phase 64-C-LOCAL UX-6: T3 runner dispatch breakdown.
          Hidden entirely until there's at least one dispatch — no
          noise on fresh deployments that haven't submitted any t3
          task yet. */}
      {data && data.t3_runners && (
        data.t3_runners.local + data.t3_runners.ssh +
        data.t3_runners.qemu + data.t3_runners.bundle > 0 ? (
          <div className="px-3 pb-3 -mt-1">
            <div className="font-mono text-[9px] tracking-[0.18em] text-[var(--muted-foreground,#94a3b8)] mb-1 flex items-center gap-1">
              <Cpu size={10} aria-hidden />
              T3 RUNNERS
            </div>
            <div className="flex items-center gap-3 font-mono text-[10px] tabular-nums">
              <RunnerPill
                label="LOCAL" value={data.t3_runners.local}
                accent="var(--validation-emerald,#10b981)"
              />
              <RunnerPill
                label="BUNDLE" value={data.t3_runners.bundle}
                accent="var(--muted-foreground,#94a3b8)"
              />
              {data.t3_runners.ssh > 0 && (
                <RunnerPill
                  label="SSH" value={data.t3_runners.ssh}
                  accent="var(--neural-cyan,#67e8f9)"
                />
              )}
              {data.t3_runners.qemu > 0 && (
                <RunnerPill
                  label="QEMU" value={data.t3_runners.qemu}
                  accent="var(--fui-orange,#f59e0b)"
                />
              )}
            </div>
          </div>
        ) : null
      )}
    </section>
  )
}

type Tone = "ok" | "warn" | "bad" | "info"

const TONE_CLASS: Record<Tone, string> = {
  ok:   "text-[var(--validation-emerald,#10b981)]",
  warn: "text-[var(--fui-orange,#f59e0b)]",
  bad:  "text-[var(--critical-red,#ef4444)]",
  info: "text-[var(--neural-cyan,#67e8f9)]",
}

function Kpi({
  icon: Icon, label, value, tone, title, testId,
}: {
  icon: typeof Activity
  label: string
  value: string
  tone: Tone
  title?: string
  testId?: string
}) {
  return (
    <div
      className="flex flex-col items-start gap-0.5 p-2 rounded-sm border border-[var(--neural-border,rgba(148,163,184,0.2))] bg-white/5"
      title={title}
      data-testid={testId}
    >
      <div className="flex items-center gap-1 font-mono text-[9px] tracking-[0.18em] text-[var(--muted-foreground,#94a3b8)]">
        <Icon className="w-3 h-3" aria-hidden />
        {label}
      </div>
      <div className={`font-mono text-[14px] font-semibold tabular-nums leading-none mt-0.5 ${TONE_CLASS[tone]}`}>
        {value}
      </div>
    </div>
  )
}

function HighestEntropyRow({ entry }: {
  entry: NonNullable<OpsSummary["highest_entropy_agent"]>
}) {
  const accent = entry.verdict === "deadlock"
    ? "var(--critical-red,#ef4444)"
    : entry.verdict === "warning"
      ? "var(--fui-orange,#f59e0b)"
      : "var(--validation-emerald,#10b981)"
  const icon = entry.verdict === "deadlock" ? "🔴" : entry.verdict === "warning" ? "⚠️" : "✅"
  return (
    <div
      className="flex items-center gap-2 font-mono text-[11px] tabular-nums"
      title={`Verdict: ${entry.verdict}`}
    >
      <span aria-hidden>{icon}</span>
      <span
        className="px-1.5 py-0.5 rounded truncate max-w-[11rem]"
        style={{
          color: accent,
          backgroundColor: `color-mix(in srgb, ${accent} 15%, transparent)`,
        }}
      >
        {entry.agent_id}
      </span>
      <span style={{ color: accent }}>
        {entry.score.toFixed(2)}
      </span>
    </div>
  )
}

function CoordinatorRow({ entry }: {
  entry: NonNullable<OpsSummary["coordinator"]>
}) {
  const queueTone: Tone = entry.queue_depth === 0
    ? "ok"
    : entry.queue_depth > 5 ? "warn" : "info"
  const deferredTone: Tone = entry.deferred_5m === 0
    ? "ok"
    : entry.deferred_5m > 20 ? "warn" : "info"
  // effective budget: warn when the coordinator is actively derated,
  // otherwise neutral info — a matching capacity_max is the happy path.
  const budgetTone: Tone = entry.derated ? "warn" : "info"
  const budgetTitle = entry.derated
    ? `Derated${entry.derate_reason ? `: ${entry.derate_reason}` : ""} — effective ${entry.effective_budget} / ${entry.capacity_max} tokens`
    : `Coordinator budget: ${entry.effective_budget} / ${entry.capacity_max} tokens`
  return (
    <div
      className="grid grid-cols-3 gap-2"
      data-testid="ops-coordinator-row"
    >
      <Kpi
        icon={Layers}
        label="QUEUE"
        value={String(entry.queue_depth)}
        tone={queueTone}
      />
      <Kpi
        icon={TimerReset}
        label="DEFERRED 5m"
        value={String(entry.deferred_5m)}
        tone={deferredTone}
      />
      <Kpi
        icon={Gauge}
        label="EFF BUDGET"
        value={`${formatBudget(entry.effective_budget)}/${entry.capacity_max}`}
        tone={budgetTone}
        title={budgetTitle}
        testId="ops-eff-budget"
      />
    </div>
  )
}


function formatBudget(n: number): string {
  // Keep integer budgets clean; show one decimal only when fractional.
  return Number.isInteger(n) ? String(n) : n.toFixed(1)
}


// H3 row 1526: derate-target labels follow H4a's mode-multiplier table —
// turbo=1.0, full_auto=0.7, supervised=0.4, manual=0.15. The H2 auto
// derater specifically drops the turbo budget down to the supervised cap,
// so the "supervised" rung is the common landing zone.
function deriveDerateTargetMode(
  entry: NonNullable<OpsSummary["coordinator"]>,
): "manual" | "supervised" | "full_auto" {
  const ratio = entry.capacity_max > 0
    ? entry.effective_budget / entry.capacity_max
    : 1
  if (ratio <= 0.2) return "manual"
  if (ratio <= 0.5) return "supervised"
  return "full_auto"
}


/**
 * H3 row 1526 — overload Badge that surfaces the Coordinator's auto-derate
 * decision. Renders only when `entry.derated === true`. The label calls out
 * the target mode the Coordinator dropped the effective budget toward
 * ("Coordinator auto-derated to supervised"); the hover tooltip exposes the
 * raw `derate_reason` string the backend attached to `set_derate(...)` (e.g.
 * "CPU 87% > threshold") so operators can tell *why* it kicked in without
 * digging through the audit log.
 */
function DerateBadge({ entry }: {
  entry: NonNullable<OpsSummary["coordinator"]>
}) {
  const target = deriveDerateTargetMode(entry)
  const label = `Coordinator auto-derated to ${target}`
  const reason = entry.derate_reason?.trim()
    ? entry.derate_reason.trim()
    : "Reason unavailable"
  const tooltip = `${label} — ${reason} (effective ${formatBudget(entry.effective_budget)} / ${entry.capacity_max} tokens)`
  return (
    <span
      data-testid="ops-derate-badge"
      data-derate-target={target}
      title={tooltip}
      role="status"
      aria-label={tooltip}
      className="inline-flex items-center gap-1 ml-auto px-1.5 py-0.5 rounded-sm font-mono text-[9px] tracking-[0.12em] uppercase text-[var(--fui-orange,#f59e0b)] bg-[color-mix(in_srgb,var(--fui-orange,#f59e0b)_18%,transparent)] border border-[var(--fui-orange,#f59e0b)]/40"
    >
      <AlertTriangle size={9} aria-hidden />
      <span>{label}</span>
    </span>
  )
}


/**
 * H3 row 1527 — manual `Force turbo` override button.
 *
 * Sits under the COORDINATOR KPI row. On click it pops a native
 * `window.confirm` dialog warning the operator that bypassing the
 * H2 auto-derate safety net may cause OOM under sustained load, and
 * — only if the operator accepts — POSTs to `/coordinator/force-turbo`
 * which writes a Phase-53 hash-chain audit row and broadcasts a
 * `coordinator.force_turbo_override` SSE event.
 *
 * The button stays visible even when the coordinator isn't derated
 * (ratio=1.0) — it's the single operator escape hatch, and rendering
 * it always lets operators also clear a *capacity* derate they spot
 * creeping in before the turbo-derate state machine engages. When
 * nothing is actually derated the click is a no-op from the backend's
 * point of view (audit row still written with before==after so the
 * trail of "someone pressed the button" survives).
 */
function ForceTurboControl({
  entry, onApplied,
}: {
  entry: NonNullable<OpsSummary["coordinator"]>
  onApplied: () => void
}) {
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<{ tone: "ok" | "bad"; text: string } | null>(null)

  const onClick = useCallback(async () => {
    if (busy) return
    const warnBody = entry.derated
      ? `Coordinator is currently auto-derated${entry.derate_reason ? ` (${entry.derate_reason})` : ""}.`
      : "Coordinator is NOT derated — forcing turbo now bypasses the safety net."
    const ok = typeof window !== "undefined" && window.confirm(
      `⚠ Force turbo override\n\n${warnBody}\n\n` +
      "Forcing turbo lifts the H2 auto-derate and the DRF capacity " +
      "derate. Under sustained high CPU / memory pressure this may " +
      "cause the host to OOM and kill running sandboxes.\n\n" +
      "An audit trail row will be written under " +
      "`coordinator.force_turbo_override` with your operator identity.\n\n" +
      "Proceed?",
    )
    if (!ok) {
      return
    }
    setBusy(true)
    setMessage(null)
    try {
      const result = await forceTurboOverride({ confirm: true })
      const summary = [
        result.cleared_turbo_derate ? "turbo-derate cleared" : null,
        result.reset_capacity_derate ? "capacity-derate reset" : null,
      ].filter(Boolean).join(", ") || "no-op (nothing was derated)"
      setMessage({ tone: "ok", text: `Force turbo applied — ${summary}` })
      onApplied()
    } catch (exc) {
      const text = exc instanceof Error ? exc.message : String(exc)
      setMessage({ tone: "bad", text: `Force turbo failed: ${text}` })
    } finally {
      setBusy(false)
    }
  }, [busy, entry.derated, entry.derate_reason, onApplied])

  return (
    <div className="mt-2 flex items-center gap-2 flex-wrap" data-testid="ops-force-turbo-row">
      <button
        type="button"
        onClick={onClick}
        disabled={busy}
        data-testid="ops-force-turbo-btn"
        aria-label="Force turbo override — bypasses auto-derate (may OOM)"
        title="Force turbo override — bypasses auto-derate (may OOM). Confirm dialog + audit row required."
        className={
          "inline-flex items-center gap-1 px-2 py-0.5 rounded-sm font-mono text-[10px] uppercase tracking-[0.12em] border transition-colors " +
          (busy
            ? "text-[var(--muted-foreground,#94a3b8)] border-[var(--neural-border,rgba(148,163,184,0.35))] cursor-wait"
            : "text-[var(--critical-red,#ef4444)] border-[var(--critical-red,#ef4444)]/40 bg-[color-mix(in_srgb,var(--critical-red,#ef4444)_12%,transparent)] hover:bg-[color-mix(in_srgb,var(--critical-red,#ef4444)_20%,transparent)]")
        }
      >
        <Zap size={10} aria-hidden />
        <span>{busy ? "Applying…" : "Force turbo"}</span>
      </button>
      {message && (
        <span
          data-testid="ops-force-turbo-msg"
          className={
            "font-mono text-[10px] truncate max-w-[18rem] " +
            (message.tone === "ok"
              ? "text-[var(--validation-emerald,#10b981)]"
              : "text-[var(--critical-red,#ef4444)]")
          }
          title={message.text}
        >
          {message.text}
        </span>
      )}
    </div>
  )
}


function RunnerPill({ label, value, accent }: { label: string; value: number; accent: string }) {
  return (
    <span className="inline-flex items-center gap-1" style={{ color: accent }}>
      <span className="w-1.5 h-1.5 rounded-full" style={{ background: accent }} />
      {label}: <span className="text-[var(--foreground,#e2e8f0)]">{value}</span>
    </span>
  )
}


// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  H4a row 2583 — AIMD budget transparency (current + 5-min trace)
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

type AimdReason =
  | "init"
  | "additive_increase"
  | "multiplicative_decrease"
  | "hold"
  | "hard_cap"
  | "floor"

const REASON_COLOR: Record<string, string> = {
  init:                    "var(--muted-foreground,#94a3b8)",
  additive_increase:       "var(--validation-emerald,#10b981)",
  multiplicative_decrease: "var(--critical-red,#ef4444)",
  hold:                    "var(--muted-foreground,#94a3b8)",
  hard_cap:                "var(--neural-cyan,#67e8f9)",
  floor:                   "var(--fui-orange,#f59e0b)",
}

const REASON_SHORT: Record<string, string> = {
  init:                    "INIT",
  additive_increase:       "AI+",
  multiplicative_decrease: "MD½",
  hold:                    "HOLD",
  hard_cap:                "CAP",
  floor:                   "FLOOR",
}

function reasonColor(reason: string): string {
  return REASON_COLOR[reason] ?? REASON_COLOR.hold
}

function reasonShort(reason: string): string {
  return REASON_SHORT[reason] ?? reason.toUpperCase()
}

function AimdReasonPill({ reason }: { reason: string }) {
  const color = reasonColor(reason)
  return (
    <span
      data-testid="ops-aimd-reason"
      data-reason={reason}
      className="inline-flex items-center px-1 rounded-sm font-mono text-[8px] tracking-[0.12em]"
      style={{
        color,
        backgroundColor: `color-mix(in srgb, ${color} 18%, transparent)`,
      }}
      title={`Last AIMD cycle outcome: ${reason}`}
    >
      {reasonShort(reason)}
    </span>
  )
}

function AimdRow({ entry }: { entry: NonNullable<OpsSummary["aimd"]> }) {
  // Tone the budget tile by where the budget sits in its envelope:
  //   • at the FLOOR → bad (the AIMD controller has collapsed the budget)
  //   • at the CAPACITY_MAX → ok (running with full host envelope)
  //   • otherwise → info (mid-envelope, normal AIMD oscillation)
  let budgetTone: Tone = "info"
  if (entry.budget <= entry.floor) budgetTone = "bad"
  else if (entry.budget >= entry.capacity_max) budgetTone = "ok"

  // Count AI / MD events in the trace so the operator can see at a
  // glance "5 increases, 1 halving in the last 5 min" — handy in
  // post-incident review without leaving the panel.
  const aiCount = entry.trace.filter(
    (e) => e.reason === "additive_increase",
  ).length
  const mdCount = entry.trace.filter(
    (e) => e.reason === "multiplicative_decrease",
  ).length

  const budgetTitle =
    `AIMD budget: ${entry.budget} / ${entry.capacity_max} tokens ` +
    `(floor=${entry.floor}, init=${entry.init_budget}). ` +
    `Last reason: ${entry.last_reason}. ` +
    `Last 5 min: +${aiCount} additive increase / -${mdCount} multiplicative decrease.`

  return (
    <div className="grid grid-cols-3 gap-2" data-testid="ops-aimd-row">
      <Kpi
        icon={Gauge}
        label="BUDGET"
        value={`${entry.budget}/${entry.capacity_max}`}
        tone={budgetTone}
        title={budgetTitle}
        testId="ops-aimd-budget"
      />
      <Kpi
        icon={Activity}
        label="5m AI/MD"
        value={`+${aiCount}/-${mdCount}`}
        tone={mdCount > 0 ? "warn" : aiCount > 0 ? "ok" : "info"}
        title={`Additive-increase events: ${aiCount}; Multiplicative-decrease events: ${mdCount} (last 5 min).`}
        testId="ops-aimd-counts"
      />
      <div
        className="flex flex-col items-start gap-0.5 p-2 rounded-sm border border-[var(--neural-border,rgba(148,163,184,0.2))] bg-white/5"
        title={`5-min budget trace (${entry.trace.length} events). Floor=${entry.floor}, capacity_max=${entry.capacity_max}.`}
        data-testid="ops-aimd-trace-tile"
      >
        <div className="flex items-center gap-1 font-mono text-[9px] tracking-[0.18em] text-[var(--muted-foreground,#94a3b8)]">
          <TrendingUp className="w-3 h-3" aria-hidden />
          5m TRACE
        </div>
        <AimdTrace
          trace={entry.trace}
          floor={entry.floor}
          capacityMax={entry.capacity_max}
        />
      </div>
    </div>
  )
}

/**
 * Pure SVG sparkline of the AIMD budget over the last 5 min, with a
 * coloured dot for each state-changing cycle (green = AI+, red = MD½,
 * orange = FLOOR, cyan = CAP, grey = INIT/HOLD).
 *
 * Y-axis is anchored to ``[floor, capacity_max]`` so two snapshots taken
 * minutes apart can be compared visually — auto-fitting the y-axis to
 * the trace's own min/max would lie about how much headroom the
 * controller actually has.
 */
function AimdTrace({
  trace,
  floor,
  capacityMax,
  width = 96,
  height = 22,
}: {
  trace: Array<{ timestamp: number; budget: number; reason: string }>
  floor: number
  capacityMax: number
  width?: number
  height?: number
}) {
  if (trace.length < 2) {
    return (
      <div
        data-testid="ops-aimd-trace"
        data-empty="true"
        className="opacity-30 font-mono text-[9px] flex items-center justify-end mt-0.5"
        style={{ width, height }}
      >
        —
      </div>
    )
  }
  // X positions are time-proportional so a long calm followed by a
  // burst of AI events doesn't get visually compressed into a single
  // tick — operators reading the sparkline should see "calm, then a
  // cluster", not an artificially even cadence.
  const t0 = trace[0].timestamp
  const tN = trace[trace.length - 1].timestamp
  const span = Math.max(0.001, tN - t0)
  const range = Math.max(0.001, capacityMax - floor)
  const xs = trace.map((e) => ((e.timestamp - t0) / span) * width)
  const ys = trace.map((e) => {
    const clamped = Math.max(floor, Math.min(capacityMax, e.budget))
    return height - ((clamped - floor) / range) * height
  })
  const pts = xs.map((x, i) => `${x.toFixed(1)},${ys[i].toFixed(1)}`).join(" ")
  return (
    <svg
      data-testid="ops-aimd-trace"
      data-points={trace.length}
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      preserveAspectRatio="xMidYMid meet"
      className="shrink-0 mt-0.5"
      aria-label={`AIMD budget trace, ${trace.length} events over last 5 min`}
      role="img"
    >
      <polyline
        fill="none"
        stroke="var(--neural-cyan,#67e8f9)"
        strokeWidth={1.2}
        points={pts}
      />
      {trace.map((e, i) => (
        <circle
          key={`${e.timestamp}-${i}`}
          cx={xs[i].toFixed(1)}
          cy={ys[i].toFixed(1)}
          r={e.reason === "additive_increase" || e.reason === "multiplicative_decrease" ? 1.8 : 1.1}
          fill={reasonColor(e.reason)}
          data-reason={e.reason}
        />
      ))}
    </svg>
  )
}


function StatusDot({ data, error }: { data: OpsSummary | null; error: string | null }) {
  let color = "var(--muted-foreground,#94a3b8)"
  let label = "loading"
  if (error) {
    color = "var(--critical-red,#ef4444)"
    label = "error"
  } else if (data) {
    if (data.token_frozen) {
      color = "var(--critical-red,#ef4444)"
      label = "frozen"
    } else if (
      data.budget_level !== "normal"
      || (data.watchdog_age_s !== null && data.watchdog_age_s > 120)
      || data.highest_entropy_agent?.verdict === "deadlock"
    ) {
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
