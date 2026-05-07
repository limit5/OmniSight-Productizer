"use client"

/**
 * OP-47 / MP.W5.2 — Onboarding wizard step 2.
 *
 * Self-contained "Open the workshop" motion stage for the
 * Multi-Provider Subscription Orchestrator onboarding flow. Spheres
 * launch from the centre toward the four workshop corners, then fade
 * as the A mark resolves in place.
 *
 * Module-global state audit: deterministic render data only. No mutable
 * module state, timers, storage, network calls, or backend coupling.
 */

import type { CSSProperties } from "react"
import { ArrowRight, Sparkles } from "lucide-react"

interface WorkshopSphere {
  id: string
  label: string
  corner: "tl" | "tr" | "bl" | "br"
  x: number
  y: number
  delayMs: number
  hue: string
}

const SPHERES: readonly WorkshopSphere[] = Object.freeze([
  {
    id: "ingest",
    label: "Ingest",
    corner: "tl",
    x: -128,
    y: -76,
    delayMs: 0,
    hue: "var(--neural-cyan,#67e8f9)",
  },
  {
    id: "policy",
    label: "Policy",
    corner: "tr",
    x: 128,
    y: -76,
    delayMs: 110,
    hue: "var(--artifact-purple,#c084fc)",
  },
  {
    id: "routing",
    label: "Routing",
    corner: "bl",
    x: -128,
    y: 76,
    delayMs: 220,
    hue: "var(--validation-emerald,#34d399)",
  },
  {
    id: "billing",
    label: "Billing",
    corner: "br",
    x: 128,
    y: 76,
    delayMs: 330,
    hue: "var(--hardware-orange,#fb923c)",
  },
])

export interface OnboardingWizardStep2Props {
  onContinue?: () => void
}

export function OnboardingWizardStep2({
  onContinue,
}: OnboardingWizardStep2Props) {
  return (
    <section
      data-testid="mp-onboarding-step2"
      aria-labelledby="mp-onboarding-step2-title"
      className="relative overflow-hidden rounded-lg border border-[var(--neural-border,rgba(148,163,184,0.35))] bg-[rgba(2,6,23,0.88)] p-5 text-[var(--foreground,#e5e7eb)] shadow-[0_18px_70px_rgba(15,23,42,0.35)]"
    >
      <div className="relative z-10 flex flex-col gap-5 md:flex-row md:items-center">
        <div className="min-w-0 flex-1">
          <div className="mb-2 flex items-center gap-2 font-mono text-[11px] uppercase text-[var(--neural-cyan,#67e8f9)]">
            <Sparkles size={13} aria-hidden />
            Step 2
          </div>
          <h2
            id="mp-onboarding-step2-title"
            className="text-xl font-semibold tracking-normal text-white"
          >
            Open the workshop
          </h2>
          <p className="mt-2 max-w-md text-sm leading-6 text-slate-300">
            Provider signals split into the workshop corners while the
            orchestrator anchor comes online.
          </p>
        </div>

        <WorkshopStage />
      </div>

      <div className="relative z-10 mt-5 flex justify-end">
        <button
          type="button"
          data-testid="mp-onboarding-step2-continue"
          onClick={onContinue}
          className="inline-flex h-9 items-center gap-2 rounded-sm border border-[var(--neural-cyan,#67e8f9)]/60 px-3 font-mono text-[11px] uppercase text-[var(--neural-cyan,#67e8f9)] transition-colors hover:bg-[var(--neural-cyan,#67e8f9)]/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--neural-cyan,#67e8f9)]"
        >
          Continue
          <ArrowRight size={13} aria-hidden />
        </button>
      </div>

      <style>{`
        @keyframes mp-step2-sphere-flight {
          0% {
            opacity: 0;
            transform: translate(-50%, -50%) scale(0.58);
          }
          22% {
            opacity: 1;
          }
          72% {
            opacity: 0.82;
            transform:
              translate(calc(-50% + var(--mp-step2-x)), calc(-50% + var(--mp-step2-y)))
              scale(0.92);
          }
          100% {
            opacity: 0.2;
            transform:
              translate(calc(-50% + var(--mp-step2-x)), calc(-50% + var(--mp-step2-y)))
              scale(0.74);
          }
        }

        @keyframes mp-step2-anchor-online {
          0%, 42% {
            opacity: 0;
            transform: scale(0.72);
            filter: blur(8px);
          }
          74% {
            opacity: 1;
            transform: scale(1.08);
            filter: blur(0);
          }
          100% {
            opacity: 1;
            transform: scale(1);
            filter: blur(0);
          }
        }

        @keyframes mp-step2-workshop-scan {
          0% { transform: translateY(-100%); opacity: 0; }
          40%, 70% { opacity: 0.5; }
          100% { transform: translateY(130%); opacity: 0; }
        }

        @media (prefers-reduced-motion: reduce) {
          [data-mp-step2-sphere] {
            animation: none !important;
            opacity: 0.38;
            transform:
              translate(calc(-50% + var(--mp-step2-x)), calc(-50% + var(--mp-step2-y)))
              scale(0.78);
          }

          [data-mp-step2-anchor] {
            animation: none !important;
            opacity: 1;
            filter: none;
            transform: scale(1);
          }

          [data-mp-step2-scan] {
            display: none;
          }
        }
      `}</style>
    </section>
  )
}

function WorkshopStage() {
  return (
    <div
      data-testid="mp-onboarding-step2-stage"
      className="relative mx-auto h-[220px] w-full max-w-[360px] shrink-0 overflow-hidden rounded-md border border-[var(--neural-border,rgba(148,163,184,0.28))] bg-[radial-gradient(circle_at_center,rgba(103,232,249,0.16),rgba(15,23,42,0.2)_42%,rgba(2,6,23,0.5))]"
      aria-label="Workshop animation: provider spheres fly to the corners and fade into the orchestrator A"
      role="img"
    >
      <div
        data-mp-step2-scan
        className="absolute inset-x-0 top-0 h-16 bg-gradient-to-b from-transparent via-[var(--neural-cyan,#67e8f9)]/16 to-transparent"
        style={{ animation: "mp-step2-workshop-scan 2.3s ease-out 120ms both" }}
        aria-hidden
      />
      <div className="absolute inset-4 rounded border border-dashed border-slate-500/25" aria-hidden />
      <div className="absolute left-1/2 top-1/2 h-px w-[72%] -translate-x-1/2 bg-gradient-to-r from-transparent via-slate-500/35 to-transparent" aria-hidden />
      <div className="absolute left-1/2 top-1/2 h-[72%] w-px -translate-y-1/2 bg-gradient-to-b from-transparent via-slate-500/35 to-transparent" aria-hidden />

      {SPHERES.map((sphere) => (
        <span
          key={sphere.id}
          data-testid={`mp-onboarding-step2-sphere-${sphere.id}`}
          data-mp-step2-sphere={sphere.corner}
          className="absolute left-1/2 top-1/2 flex size-12 items-center justify-center rounded-full border text-[9px] font-mono uppercase"
          style={
            {
              "--mp-step2-x": `${sphere.x}px`,
              "--mp-step2-y": `${sphere.y}px`,
              color: sphere.hue,
              borderColor: sphere.hue,
              background: `radial-gradient(circle, ${sphere.hue} 0%, rgba(15,23,42,0.88) 58%)`,
              boxShadow: `0 0 22px ${sphere.hue}`,
              animation: `mp-step2-sphere-flight 1.8s cubic-bezier(.2,.8,.2,1) ${sphere.delayMs}ms both`,
            } as CSSProperties
          }
        >
          {sphere.label}
        </span>
      ))}

      <div
        data-testid="mp-onboarding-step2-anchor"
        data-mp-step2-anchor
        className="absolute left-1/2 top-1/2 grid size-20 -translate-x-1/2 -translate-y-1/2 place-items-center rounded-full border border-white/25 bg-slate-950/90 text-5xl font-semibold text-white shadow-[0_0_44px_rgba(103,232,249,0.42)]"
        style={{ animation: "mp-step2-anchor-online 1.9s ease-out 520ms both" }}
        aria-hidden
      >
        A
      </div>
    </div>
  )
}

export default OnboardingWizardStep2
