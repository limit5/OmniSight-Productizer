"use client"

/**
 * ZZ.B3 #304-3 checkbox 3 (2026-04-24) — Burn-rate freeze ETA toast.
 *
 * Polls ``fetchTokenBurnRate("1h")`` + ``getTokenBudget()`` every 60 s
 * (matches the 60 s bucket width; 30 s poll in TokenUsageStats is for
 * sparkline animation, this center cares about the trigger condition
 * not frame rate). Emits a toast「目前速率將於 18:42 觸發 freeze」when
 * the linear extrapolation of the current hourly burn (``hourly × 24``)
 * would exceed the daily budget — that is, the unsustainable-rate
 * condition from the spec. The projected HH:MM is ``now + remaining /
 * cost_per_hour`` in the operator's local timezone.
 *
 * Dedupe policy: re-render the toast only when the projected minute
 * shifts by ≥ 5 min (ETA drifts naturally as burn rate fluctuates;
 * every bucket change would otherwise re-toast once per minute and
 * be noise). Dismiss clears the current toast; re-emits on the next
 * significant ETA shift or on frozen→unfrozen→trigger cycle.
 *
 * NULL-vs-genuine-zero contract (inherited from ZZ.A1/A2/B3-checkbox-2):
 * - ``budget <= 0`` (unlimited / unset) → no toast (nothing to exceed)
 * - ``budgetInfo.frozen`` → no toast (we're already past the gate)
 * - ``cost_per_hour <= 0`` (no recent turns) → no toast (nothing to extrapolate)
 * - ``projectedDaily <= budget`` → no toast (sustainable)
 */

import { useCallback, useEffect, useRef, useState } from "react"
import { AlertTriangle, X } from "lucide-react"

import {
  fetchTokenBurnRate,
  getTokenBudget,
  type TokenBudgetInfo,
  type TokenBurnRatePoint,
} from "@/lib/api"

const POLL_MS = 60_000
// Re-emit the toast only when the projected freeze minute drifts by
// ≥ this many minutes. Without the deadband the badge would re-render
// every 60 s bucket update as the hourly burn wobbles, spamming the
// operator. 5 min is the smallest actionable unit for a human reading
// "freeze at HH:MM".
const ETA_DEADBAND_MS = 5 * 60_000

export interface BurnRateFreezeToastCenterProps {
  // Poll override for tests — real callers never pass this.
  pollMs?: number
  // Test-only seam to drive "now" without Date.now jitter.
  nowProvider?: () => number
}

interface FreezeToast {
  id: string
  // ms epoch of projected freeze moment. Dismiss vs re-emit decisions
  // compare against the last-emitted value so the toast only re-renders
  // when the projection has meaningfully moved.
  etaMs: number
  etaLabel: string
  costPerHour: number
  budget: number
  remaining: number
  createdAt: number
}

/**
 * Compute the projected freeze ETA from the latest burn bucket + budget.
 *
 * Return value:
 * - ``null`` → no trigger (sustainable rate, no budget, already frozen,
 *   or zero burn — all "don't show a toast" cases).
 * - ``{ etaMs, etaLabel, remaining, costPerHour }`` → toast-worthy state.
 *   ``etaLabel`` is the "HH:MM" portion for the human-readable sentence;
 *   computed in the operator's local timezone so "18:42" reads correctly
 *   wherever they're sitting.
 *
 * Exposed for unit tests so the trigger contract can be locked without
 * rendering the component.
 */
export function computeFreezeEta(
  budget: TokenBudgetInfo | null,
  points: TokenBurnRatePoint[],
  nowMs: number,
): {
  etaMs: number
  etaLabel: string
  costPerHour: number
  remaining: number
  projectedDaily: number
} | null {
  if (!budget) return null
  if (budget.frozen) return null
  if (budget.budget <= 0) return null
  const remaining = budget.budget - budget.usage
  if (remaining <= 0) return null
  if (!points || points.length === 0) return null
  const latest = points[points.length - 1]
  const costPerHour = latest?.cost_per_hour ?? 0
  if (!Number.isFinite(costPerHour) || costPerHour <= 0) return null
  const projectedDaily = costPerHour * 24
  // Linear extrapolation: this rate sustained for 24 h would over-spend
  // the daily cap. That's the exact spec condition — a sustainable rate
  // (projectedDaily ≤ budget) never triggers a toast.
  if (projectedDaily <= budget.budget) return null
  const hoursToFreeze = remaining / costPerHour
  const etaMs = nowMs + hoursToFreeze * 3_600_000
  const etaDate = new Date(etaMs)
  const etaLabel = etaDate.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  })
  return { etaMs, etaLabel, costPerHour, remaining, projectedDaily }
}

export function BurnRateFreezeToastCenter({
  pollMs = POLL_MS,
  nowProvider,
}: BurnRateFreezeToastCenterProps = {}) {
  const [toast, setToast] = useState<FreezeToast | null>(null)
  // Snapshot of last-emitted etaMs per session — used by the deadband
  // so a jittery rate doesn't re-toast once per minute.
  const lastEtaRef = useRef<number | null>(null)
  // Track whether the operator dismissed the current toast; stays true
  // until the next meaningful ETA shift (≥ deadband) or a frozen/unset
  // transition. Without this the toast would re-appear on every poll.
  const dismissedAtEtaRef = useRef<number | null>(null)

  const now = useCallback(
    () => (nowProvider ? nowProvider() : Date.now()),
    [nowProvider],
  )

  const dismiss = useCallback(() => {
    if (toast) {
      dismissedAtEtaRef.current = toast.etaMs
    }
    setToast(null)
  }, [toast])

  useEffect(() => {
    let cancelled = false

    const tick = async () => {
      let budget: TokenBudgetInfo | null = null
      let points: TokenBurnRatePoint[] = []
      try {
        budget = await getTokenBudget()
      } catch {
        // Endpoint transiently down — skip this tick. Toast stays in
        // its current state; next tick retries.
        return
      }
      try {
        const resp = await fetchTokenBurnRate("1h")
        points = resp.points ?? []
      } catch {
        return
      }
      if (cancelled) return

      const eta = computeFreezeEta(budget, points, now())

      // Condition cleared — hide any active toast and reset dedupe.
      // budget.frozen transition also flows through here so the toast
      // disappears the instant the backend freezes (we no longer need
      // to warn about imminent freeze — it's happened).
      if (!eta) {
        lastEtaRef.current = null
        dismissedAtEtaRef.current = null
        setToast(null)
        return
      }

      // Deadband: only re-emit when the projection has meaningfully
      // moved. First-fire (lastEtaRef === null) always shows. Within
      // deadband we leave the existing toast alone — no setState and no
      // lastEtaRef update, so the rendered ``data-eta-ms`` stays put.
      const last = lastEtaRef.current
      const insideDeadband =
        last !== null && Math.abs(eta.etaMs - last) < ETA_DEADBAND_MS

      if (insideDeadband) {
        // Operator dismissed this ETA → stay silent until deadband is
        // broken. Otherwise the existing toast (if any) remains exactly
        // as it was painted — refreshing on every bucket would spam.
        return
      }

      // Crossed the deadband (or first fire) — clear any prior dismissal
      // so the new ETA is visible and update bookkeeping before emit.
      dismissedAtEtaRef.current = null
      lastEtaRef.current = eta.etaMs
      setToast({
        id: `burn-rate-freeze-${eta.etaMs}`,
        etaMs: eta.etaMs,
        etaLabel: eta.etaLabel,
        costPerHour: eta.costPerHour,
        budget: budget.budget,
        remaining: eta.remaining,
        createdAt: now(),
      })
    }

    void tick()
    const interval = setInterval(() => {
      void tick()
    }, pollMs)
    return () => {
      cancelled = true
      clearInterval(interval)
    }
  }, [pollMs, now])

  if (!toast) return null

  return (
    <div
      aria-live="polite"
      aria-atomic="true"
      aria-label="burn-rate freeze toasts"
      data-testid="burn-rate-freeze-toast-center"
      className="fixed bottom-4 left-4 z-[55] flex flex-col gap-2 w-[min(380px,calc(100vw-2rem))] pointer-events-none"
    >
      <div
        key={toast.id}
        role="status"
        data-testid="burn-rate-freeze-toast"
        data-eta-ms={toast.etaMs}
        className="pointer-events-auto holo-glass-simple rounded-sm border backdrop-blur-sm"
        style={{
          borderColor: "var(--hardware-orange,#f59e0b)",
          boxShadow:
            "0 8px 28px -10px var(--hardware-orange,#f59e0b), 0 0 0 1px var(--hardware-orange,#f59e0b), inset 0 0 28px -18px var(--hardware-orange,#f59e0b)",
        }}
      >
        <div className="flex items-start gap-2 p-2.5">
          <AlertTriangle
            className="w-4 h-4 shrink-0 mt-0.5 animate-pulse"
            style={{ color: "var(--hardware-orange,#f59e0b)" }}
            aria-hidden
          />
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 mb-0.5">
              <span
                className="font-mono text-[9px] tracking-[0.25em] font-bold"
                style={{ color: "var(--hardware-orange,#f59e0b)" }}
              >
                BURN RATE WARNING
              </span>
              <span
                className="font-mono text-[9px] text-[var(--muted-foreground,#94a3b8)] truncate"
                data-testid="burn-rate-freeze-rate"
              >
                ${toast.costPerHour.toFixed(toast.costPerHour >= 1 ? 2 : 3)}/hr
              </span>
            </div>
            <div
              className="font-mono font-bold text-[12px] tracking-[0.04em] leading-tight text-[var(--foreground,#e2e8f0)] break-words"
              data-testid="burn-rate-freeze-message"
            >
              目前速率將於 {toast.etaLabel} 觸發 freeze
            </div>
            <div className="font-mono text-[10px] text-[var(--muted-foreground,#94a3b8)] leading-tight mt-0.5 break-words">
              Remaining ${toast.remaining.toFixed(2)} of ${toast.budget.toFixed(2)} daily budget.
            </div>
          </div>
          <button
            type="button"
            data-testid="burn-rate-freeze-toast-dismiss"
            onClick={dismiss}
            aria-label="dismiss"
            className="p-0.5 rounded-sm text-[var(--muted-foreground,#94a3b8)] hover:text-[var(--foreground,#e2e8f0)] hover:bg-white/5 shrink-0"
          >
            <X className="w-3.5 h-3.5" aria-hidden />
          </button>
        </div>
      </div>
    </div>
  )
}

export default BurnRateFreezeToastCenter
