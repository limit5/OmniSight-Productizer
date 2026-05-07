"use client"

/**
 * OP-48 / MP.W5.3 - Multi-provider orchestrator onboarding tour.
 *
 * Self-contained spotlight overlay for the ADR-0007 planning surface.
 * The tour anchors to elements carrying `data-mp-onboarding-tour`.
 *
 * Module-global state audit: immutable tour copy and anchor constants
 * only. Runtime DOM measurements stay inside component state.
 */

import type { CSSProperties } from "react"
import {
  ChevronLeft,
  ChevronRight,
  CircleDollarSign,
  Gauge,
  Network,
  Palette,
  Sparkles,
  X,
  Zap,
} from "lucide-react"
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react"

export const ONBOARDING_TOUR_ANCHOR_ATTR = "data-mp-onboarding-tour"

export type OnboardingTourAnchor =
  | "start"
  | "sphere"
  | "color"
  | "core"
  | "beam"
  | "slider"

export interface OnboardingTourStep {
  id: OnboardingTourAnchor
  icon: typeof Sparkles
  placement: "bottom" | "top" | "right" | "left"
  title: string
  body: string
}

export interface OnboardingTourProps {
  open?: boolean
  initialStep?: OnboardingTourAnchor | number
  onClose?: () => void
}

export const ONBOARDING_TOUR_STEPS: readonly OnboardingTourStep[] = Object.freeze([
  {
    id: "start",
    icon: Sparkles,
    placement: "bottom",
    title: "1 / 6 - Start the workshop",
    body: "Use Start to enter the multi-provider planning surface once subscriptions and quota checks are ready.",
  },
  {
    id: "sphere",
    icon: Zap,
    placement: "right",
    title: "2 / 6 - Provider sphere",
    body: "Each sphere represents one subscribed provider. Size reflects quota headroom so stronger options are easier to spot.",
  },
  {
    id: "color",
    icon: Palette,
    placement: "right",
    title: "3 / 6 - Quota color",
    body: "Ring color carries the provider health tier: green for healthy, amber for watch, red for critical, and gray for unavailable.",
  },
  {
    id: "core",
    icon: Network,
    placement: "left",
    title: "4 / 6 - Project core",
    body: "The center core summarizes the selected task bundle, token estimate, and expected blended cost before execution.",
  },
  {
    id: "beam",
    icon: CircleDollarSign,
    placement: "top",
    title: "5 / 6 - Allocation beam",
    body: "Connection beams show how the orchestrator plans to route work between each provider and the project core.",
  },
  {
    id: "slider",
    icon: Gauge,
    placement: "top",
    title: "6 / 6 - Cheap / Fast slider",
    body: "Move the tradeoff slider to rebalance routing between lower cost and faster completion before starting the run.",
  },
])

const CTA = {
  back: "Back",
  next: "Next",
  done: "Done",
  skip: "Skip tour",
}

function initialStepIndex(initialStep: OnboardingTourProps["initialStep"]): number {
  if (typeof initialStep === "number" && Number.isFinite(initialStep)) {
    return clamp(Math.trunc(initialStep), 0, ONBOARDING_TOUR_STEPS.length - 1)
  }
  if (typeof initialStep === "string") {
    const idx = ONBOARDING_TOUR_STEPS.findIndex((step) => step.id === initialStep)
    return idx >= 0 ? idx : 0
  }
  return 0
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value))
}

function escapeAttributeValue(value: string): string {
  if (typeof CSS !== "undefined" && typeof CSS.escape === "function") {
    return CSS.escape(value)
  }
  return value.replace(/\\/g, "\\\\").replace(/"/g, '\\"')
}

function queryAnchor(anchor: OnboardingTourAnchor): HTMLElement | null {
  if (typeof document === "undefined") return null
  return document.querySelector<HTMLElement>(
    `[${ONBOARDING_TOUR_ANCHOR_ATTR}="${escapeAttributeValue(anchor)}"]`,
  )
}

export function OnboardingTour({
  open = true,
  initialStep,
  onClose,
}: OnboardingTourProps) {
  const [dismissed, setDismissed] = useState(false)
  const [idx, setIdx] = useState(() => initialStepIndex(initialStep))
  const [anchorRect, setAnchorRect] = useState<DOMRect | null>(null)
  const cardRef = useRef<HTMLDivElement | null>(null)
  const active = open && !dismissed

  const closeTour = useCallback(() => {
    setDismissed(true)
    onClose?.()
  }, [onClose])

  const advance = useCallback((delta: number) => {
    setIdx((current) => {
      const next = current + delta
      if (next < 0) return 0
      if (next >= ONBOARDING_TOUR_STEPS.length) {
        closeTour()
        return current
      }
      return next
    })
  }, [closeTour])

  useLayoutEffect(() => {
    if (!active) return
    const measure = () => {
      const step = ONBOARDING_TOUR_STEPS[idx]
      if (!step) return
      const el = queryAnchor(step.id)
      if (!el) {
        setAnchorRect(null)
        return
      }
      setAnchorRect(el.getBoundingClientRect())
      el.scrollIntoView({ behavior: "smooth", block: "center", inline: "center" })
    }
    measure()
    const t = window.setTimeout(measure, 350)
    return () => window.clearTimeout(t)
  }, [active, idx])

  useEffect(() => {
    if (!active) return
    const measure = () => {
      const step = ONBOARDING_TOUR_STEPS[idx]
      const el = step ? queryAnchor(step.id) : null
      setAnchorRect(el ? el.getBoundingClientRect() : null)
    }
    window.addEventListener("resize", measure)
    window.addEventListener("scroll", measure, { capture: true })
    return () => {
      window.removeEventListener("resize", measure)
      window.removeEventListener("scroll", measure, { capture: true })
    }
  }, [active, idx])

  useEffect(() => {
    if (!active) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault()
        closeTour()
      } else if (event.key === "ArrowRight") {
        event.preventDefault()
        advance(1)
      } else if (event.key === "ArrowLeft") {
        event.preventDefault()
        advance(-1)
      }
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [active, advance, closeTour])

  const step = ONBOARDING_TOUR_STEPS[idx]
  const cardStyle = useMemo<CSSProperties>(() => {
    if (!anchorRect || !step) {
      return { left: "50%", top: "50%", transform: "translate(-50%, -50%)" }
    }
    const gap = 12
    const cardW = 360
    const cardH = 168
    const vw = typeof window !== "undefined" ? window.innerWidth : 1920
    const vh = typeof window !== "undefined" ? window.innerHeight : 1080
    let top = 0
    let left = 0
    switch (step.placement) {
      case "bottom":
        top = Math.min(anchorRect.bottom + gap, vh - cardH - 12)
        left = clamp(anchorRect.left + anchorRect.width / 2 - cardW / 2, 12, vw - cardW - 12)
        break
      case "top":
        top = Math.max(12, anchorRect.top - cardH - gap)
        left = clamp(anchorRect.left + anchorRect.width / 2 - cardW / 2, 12, vw - cardW - 12)
        break
      case "right":
        top = clamp(anchorRect.top, 12, vh - cardH - 12)
        left = Math.min(anchorRect.right + gap, vw - cardW - 12)
        break
      case "left":
        top = clamp(anchorRect.top, 12, vh - cardH - 12)
        left = Math.max(12, anchorRect.left - cardW - gap)
        break
    }
    return { top, left, width: cardW }
  }, [anchorRect, step])

  if (!active || !step) return null

  const Icon = step.icon

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Multi-provider onboarding tour"
      className="fixed inset-0 z-[100] pointer-events-auto"
      data-testid="mp-onboarding-tour"
      data-mp-onboarding-step={step.id}
    >
      <Backdrop rect={anchorRect} onClick={closeTour} />

      {anchorRect && (
        <div
          aria-hidden
          className="fixed pointer-events-none rounded-sm border-2 border-[var(--neural-cyan,#67e8f9)]"
          data-testid="mp-onboarding-tour-spotlight"
          style={{
            top: anchorRect.top - 4,
            left: anchorRect.left - 4,
            width: anchorRect.width + 8,
            height: anchorRect.height + 8,
            boxShadow:
              "0 0 24px rgba(103, 232, 249, 0.55), 0 0 4px rgba(103, 232, 249, 0.8) inset",
          }}
        />
      )}

      <div
        ref={cardRef}
        className="fixed max-w-[calc(100vw-24px)] rounded-sm border border-[var(--neural-cyan,#67e8f9)]/60 bg-[rgba(2,6,23,0.94)] p-4 text-[var(--foreground,#e2e8f0)] shadow-2xl backdrop-blur-md"
        style={cardStyle}
      >
        <div className="mb-2 flex items-start gap-2">
          <Icon
            className="mt-0.5 h-4 w-4 shrink-0 text-[var(--neural-cyan,#67e8f9)]"
            aria-hidden
          />
          <h2 className="min-w-0 flex-1 font-mono text-xs uppercase tracking-wider text-[var(--neural-cyan,#67e8f9)]">
            {step.title}
          </h2>
          <button
            type="button"
            onClick={closeTour}
            aria-label={CTA.skip}
            className="text-[var(--muted-foreground,#94a3b8)] transition-colors hover:text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--neural-cyan,#67e8f9)]"
          >
            <X className="h-4 w-4" aria-hidden />
          </button>
        </div>

        <p className="mb-3 text-sm leading-6 text-slate-200">{step.body}</p>

        <div className="flex items-center justify-between gap-3">
          <button
            type="button"
            onClick={closeTour}
            className="font-mono text-[10px] uppercase tracking-wider text-[var(--muted-foreground,#94a3b8)] transition-colors hover:text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--neural-cyan,#67e8f9)]"
          >
            {CTA.skip}
          </button>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => advance(-1)}
              disabled={idx === 0}
              className="inline-flex h-8 items-center gap-1 rounded-sm border border-[var(--neural-border,rgba(148,163,184,0.35))] px-2 font-mono text-[11px] text-[var(--muted-foreground,#94a3b8)] transition-colors hover:text-white disabled:cursor-not-allowed disabled:opacity-40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--neural-cyan,#67e8f9)]"
            >
              <ChevronLeft className="h-3 w-3" aria-hidden />
              {CTA.back}
            </button>
            <button
              type="button"
              onClick={() => advance(1)}
              className="inline-flex h-8 items-center gap-1 rounded-sm bg-[var(--neural-cyan,#67e8f9)] px-2.5 font-mono text-[11px] font-semibold text-black transition-[filter] hover:brightness-110 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white"
            >
              {idx === ONBOARDING_TOUR_STEPS.length - 1 ? CTA.done : CTA.next}
              <ChevronRight className="h-3 w-3" aria-hidden />
            </button>
          </div>
        </div>

        <div
          className="mt-3 flex items-center justify-center gap-1.5"
          aria-label={`Step ${idx + 1} of ${ONBOARDING_TOUR_STEPS.length}`}
        >
          {ONBOARDING_TOUR_STEPS.map((tourStep, i) => (
            <span
              key={tourStep.id}
              aria-hidden
              className="h-1.5 w-1.5 rounded-full"
              style={{
                background:
                  i === idx
                    ? "var(--neural-cyan,#67e8f9)"
                    : "rgba(148,163,184,0.35)",
                boxShadow:
                  i === idx
                    ? "0 0 8px var(--neural-cyan,#67e8f9)"
                    : undefined,
              }}
            />
          ))}
        </div>
      </div>
    </div>
  )
}

function Backdrop({
  rect,
  onClick,
}: {
  rect: DOMRect | null
  onClick: () => void
}) {
  if (!rect) {
    return (
      <button
        type="button"
        aria-label={CTA.skip}
        className="fixed inset-0 cursor-default bg-[var(--deep-space-start,#010409)]/80 p-0 backdrop-blur-[2px]"
        onClick={onClick}
      />
    )
  }

  const vw = typeof window !== "undefined" ? window.innerWidth : 1920
  const vh = typeof window !== "undefined" ? window.innerHeight : 1080
  const pad = 6
  const x = Math.max(0, rect.left - pad)
  const y = Math.max(0, rect.top - pad)
  const w = Math.min(vw - x, rect.width + pad * 2)
  const h = Math.min(vh - y, rect.height + pad * 2)
  const path = `M0 0 H${vw} V${vh} H0 Z M${x} ${y} H${x + w} V${y + h} H${x} Z`

  return (
    <button
      type="button"
      aria-label={CTA.skip}
      className="fixed inset-0 cursor-default p-0"
      onClick={onClick}
    >
      <svg className="h-full w-full" width={vw} height={vh} aria-hidden>
        <path d={path} fill="rgba(1,4,9,0.78)" fillRule="evenodd" />
      </svg>
    </button>
  )
}

export default OnboardingTour
