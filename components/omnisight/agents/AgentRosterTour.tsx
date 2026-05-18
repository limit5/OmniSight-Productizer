"use client"

/**
 * RPG.W10.3 - replayable Agent Roster tour.
 *
 * Triggered explicitly from the global Help dropdown via
 * `/agents?tour=1`. This mirrors the lightweight, dependency-free tour
 * style used by `first-run-tour.tsx`, but stays scoped to the RPG roster
 * route instead of adding a new global onboarding framework.
 */

import { useCallback, useEffect, useMemo, useState } from "react"
import { ChevronLeft, ChevronRight, Sparkles, X } from "lucide-react"

interface TourStep {
  id: string
  title: string
  body: string
}

const STEPS: readonly TourStep[] = [
  {
    id: "guilds",
    title: "1 / 3 - Guild Hall",
    body: "Guild Hall shows where agents sit across the RPG roster: Backend, Frontend, Security, DevOps, Data, Mobile, Embedded, and Generalist.",
  },
  {
    id: "cards",
    title: "2 / 3 - Character Cards",
    body: "Each roster row opens a Character Card with the agent's level, XP, specialization, skills, tools, and current RPG identity.",
  },
  {
    id: "parties",
    title: "3 / 3 - Party Hall",
    body: "Party Hall groups active multi-agent parties and surfaces synergy so operators can see which guild combinations are working together.",
  },
]

function resolveInitialTourState(): { active: boolean; idx: number } {
  if (typeof window === "undefined") return { active: false, idx: 0 }
  const params = new URLSearchParams(window.location.search)
  const tourParam = params.get("tour")
  if (!tourParam) return { active: false, idx: 0 }

  const asNum = parseInt(tourParam, 10)
  if (Number.isFinite(asNum) && asNum >= 1 && asNum <= STEPS.length) {
    return { active: true, idx: asNum - 1 }
  }
  const byId = STEPS.findIndex((step) => step.id === tourParam)
  return { active: true, idx: byId >= 0 ? byId : 0 }
}

export function AgentRosterTour() {
  const [state, setState] = useState(resolveInitialTourState)
  const { active, idx } = state

  const closeTour = useCallback(() => {
    setState((current) => ({ ...current, active: false }))
    if (typeof window === "undefined") return
    const u = new URL(window.location.href)
    if (u.searchParams.has("tour")) {
      u.searchParams.delete("tour")
      window.history.replaceState(null, "", u.toString())
    }
  }, [])

  const advance = useCallback((delta: number) => {
    setState((current) => {
      const next = current.idx + delta
      if (next < 0) return { ...current, idx: 0 }
      if (next >= STEPS.length) {
        closeTour()
        return current
      }
      return { ...current, idx: next }
    })
  }, [closeTour])

  useEffect(() => {
    if (!active) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault()
        closeTour()
      } else if (event.key === "ArrowLeft") {
        event.preventDefault()
        advance(-1)
      } else if (event.key === "ArrowRight") {
        event.preventDefault()
        advance(1)
      }
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [active, advance, closeTour])

  const step = STEPS[idx]
  const progress = useMemo(() => `${idx + 1} of ${STEPS.length}`, [idx])

  if (!active || !step) return null

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Agent roster tour"
      data-testid="agent-roster-tour"
      data-agent-tour-step={step.id}
      className="fixed inset-0 z-[100] flex items-center justify-center p-4"
    >
      <button
        type="button"
        aria-label="Skip tour"
        className="absolute inset-0 bg-[var(--background)]/80 backdrop-blur-[2px]"
        onClick={closeTour}
      />
      <div
        className="relative w-full max-w-md rounded-md border border-[var(--neural-cyan,#67e8f9)]/50 bg-[var(--card)] p-4 shadow-2xl"
      >
        <div className="mb-3 flex items-start gap-2">
          <Sparkles className="mt-0.5 h-4 w-4 shrink-0 text-[var(--neural-cyan,#67e8f9)]" aria-hidden />
          <div className="min-w-0 flex-1">
            <h2 className="font-mono text-xs font-semibold tracking-[0.16em] text-[var(--neural-cyan,#67e8f9)]">
              {step.title}
            </h2>
            <p className="mt-2 text-sm leading-relaxed text-[var(--foreground)]">
              {step.body}
            </p>
          </div>
          <button
            type="button"
            onClick={closeTour}
            aria-label="Skip tour"
            className="rounded-sm p-1 text-[var(--muted-foreground)] hover:bg-white/5 hover:text-[var(--foreground)]"
          >
            <X className="h-4 w-4" aria-hidden />
          </button>
        </div>

        <div className="mb-3 flex items-center justify-center gap-1.5" aria-label={progress}>
          {STEPS.map((tourStep, i) => (
            <span
              key={tourStep.id}
              aria-hidden
              className="h-1.5 w-1.5 rounded-full"
              style={{
                background: i === idx
                  ? "var(--neural-cyan,#67e8f9)"
                  : "rgba(148,163,184,0.35)",
                boxShadow: i === idx ? "0 0 8px var(--neural-cyan,#67e8f9)" : undefined,
              }}
            />
          ))}
        </div>

        <div className="flex items-center justify-between">
          <button
            type="button"
            onClick={closeTour}
            className="font-mono text-[10px] tracking-wider text-[var(--muted-foreground)] hover:text-[var(--foreground)]"
          >
            Skip tour
          </button>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => advance(-1)}
              disabled={idx === 0}
              className="inline-flex items-center gap-1 rounded-sm border border-[var(--border)] px-2 py-1 font-mono text-[11px] text-[var(--muted-foreground)] hover:text-[var(--foreground)] disabled:cursor-not-allowed disabled:opacity-40"
            >
              <ChevronLeft className="h-3 w-3" aria-hidden />
              Back
            </button>
            <button
              type="button"
              onClick={() => advance(1)}
              className="inline-flex items-center gap-1 rounded-sm bg-[var(--neural-cyan,#67e8f9)] px-2.5 py-1 font-mono text-[11px] font-semibold text-black hover:brightness-110"
            >
              {idx === STEPS.length - 1 ? "Done" : "Next"}
              <ChevronRight className="h-3 w-3" aria-hidden />
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
