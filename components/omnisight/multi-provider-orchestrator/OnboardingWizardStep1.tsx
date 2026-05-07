"use client"

/**
 * MP.W5.1 — Multi-provider orchestrator onboarding welcome step.
 *
 * First screen in the MP onboarding wizard. This leaf is deliberately
 * presentation-only: it renders the welcome copy, a deterministic
 * four-sphere orbit/converge visual, and a single continue action
 * owned by the parent wizard.
 *
 * Module-global state audit:
 *   - No mutable module-level state.
 *   - Sphere geometry is static and deterministic so SSR / browser /
 *     future tests render the same DOM shape.
 */

import type { CSSProperties, JSX } from "react"
import { ArrowRight, Sparkles } from "lucide-react"

interface ProviderSphere {
  id: string
  label: string
  shortLabel: string
  angleDeg: number
  delayMs: number
  accent: string
  glow: string
}

const PROVIDER_SPHERES: readonly ProviderSphere[] = [
  {
    id: "subscription",
    label: "Subscription capacity",
    shortLabel: "SUB",
    angleDeg: -90,
    delayMs: 0,
    accent: "#38bdf8",
    glow: "rgba(56,189,248,0.42)",
  },
  {
    id: "api",
    label: "API key lane",
    shortLabel: "API",
    angleDeg: 0,
    delayMs: 160,
    accent: "#f59e0b",
    glow: "rgba(245,158,11,0.42)",
  },
  {
    id: "fallback",
    label: "Fallback provider",
    shortLabel: "FBK",
    angleDeg: 90,
    delayMs: 320,
    accent: "#34d399",
    glow: "rgba(52,211,153,0.42)",
  },
  {
    id: "policy",
    label: "Routing policy",
    shortLabel: "POL",
    angleDeg: 180,
    delayMs: 480,
    accent: "#a78bfa",
    glow: "rgba(167,139,250,0.42)",
  },
] as const

export interface OnboardingWizardStep1Props {
  onContinue?: () => void
  continueLabel?: string
  disabled?: boolean
  className?: string
}

export default function OnboardingWizardStep1({
  onContinue,
  continueLabel = "Begin orchestration",
  disabled = false,
  className,
}: OnboardingWizardStep1Props): JSX.Element {
  return (
    <section
      data-testid="mp-onboarding-step-1"
      aria-labelledby="mp-onboarding-step-1-title"
      className={[
        "relative overflow-hidden rounded-lg border border-cyan-400/20",
        "bg-slate-950 px-6 py-7 text-slate-100 shadow-[0_0_40px_rgba(8,47,73,0.28)]",
        "md:grid md:grid-cols-[minmax(0,1fr)_320px] md:items-center md:gap-8 md:px-8 md:py-9",
        className,
      ]
        .filter(Boolean)
        .join(" ")}
    >
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-0 bg-[linear-gradient(120deg,rgba(14,165,233,0.10),transparent_45%,rgba(245,158,11,0.08))]"
      />
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-0 opacity-30 [background-image:linear-gradient(rgba(148,163,184,0.12)_1px,transparent_1px),linear-gradient(90deg,rgba(148,163,184,0.12)_1px,transparent_1px)] [background-size:28px_28px]"
      />

      <div className="relative z-10 max-w-2xl space-y-5">
        <div className="inline-flex items-center gap-2 rounded border border-cyan-300/25 bg-cyan-300/10 px-3 py-1 font-mono text-[11px] uppercase tracking-[0.22em] text-cyan-100">
          <Sparkles size={13} aria-hidden="true" />
          MP onboarding
        </div>
        <div className="space-y-3">
          <h1
            id="mp-onboarding-step-1-title"
            className="font-orbitron text-3xl font-semibold tracking-normal text-white md:text-4xl"
          >
            Welcome to the multi-provider orchestrator
          </h1>
          <p className="max-w-xl text-sm leading-6 text-slate-300 md:text-base">
            Connect subscription capacity, direct API access, fallback
            providers, and routing policy into one operator-controlled
            launch path.
          </p>
        </div>
        <div className="grid max-w-xl gap-2 sm:grid-cols-2">
          {PROVIDER_SPHERES.map((sphere) => (
            <div
              key={sphere.id}
              data-testid={`mp-onboarding-step-1-lane-${sphere.id}`}
              className="flex items-center gap-2 rounded-md border border-white/10 bg-white/[0.04] px-3 py-2 text-xs text-slate-200"
            >
              <span
                aria-hidden="true"
                className="size-2 rounded-full"
                style={{
                  backgroundColor: sphere.accent,
                  boxShadow: `0 0 12px ${sphere.glow}`,
                }}
              />
              {sphere.label}
            </div>
          ))}
        </div>
        <button
          type="button"
          data-testid="mp-onboarding-step-1-continue"
          onClick={onContinue}
          disabled={disabled}
          className="inline-flex min-h-10 items-center gap-2 rounded-md border border-cyan-300/50 bg-cyan-300/15 px-4 py-2 text-sm font-medium text-cyan-50 shadow-[0_0_20px_rgba(56,189,248,0.20)] transition hover:border-cyan-200 hover:bg-cyan-300/25 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {continueLabel}
          <ArrowRight size={16} aria-hidden="true" />
        </button>
      </div>

      <div
        data-testid="mp-onboarding-step-1-orbit"
        className="relative z-10 mx-auto mt-8 aspect-square w-full max-w-[300px] md:mt-0"
        aria-label="Four provider lanes orbit and converge into one orchestrator core"
        role="img"
      >
        <div className="absolute inset-[13%] rounded-full border border-dashed border-cyan-200/20" />
        <div className="absolute inset-[25%] rounded-full border border-dashed border-amber-200/15" />
        <div className="mp-w5-orbit absolute inset-0 rounded-full">
          {PROVIDER_SPHERES.map((sphere) => (
            <span
              key={sphere.id}
              data-testid={`mp-onboarding-step-1-sphere-${sphere.id}`}
              className="mp-w5-sphere absolute left-1/2 top-1/2 grid size-16 place-items-center rounded-full border bg-slate-950/90 font-mono text-[11px] font-semibold text-white"
              style={
                {
                  "--mp-w5-angle": `${sphere.angleDeg}deg`,
                  "--mp-w5-delay": `${sphere.delayMs}ms`,
                  "--mp-w5-accent": sphere.accent,
                  "--mp-w5-glow": sphere.glow,
                } as CSSProperties
              }
              aria-label={sphere.label}
            >
              <span className="relative z-10">{sphere.shortLabel}</span>
            </span>
          ))}
        </div>
        <div className="absolute left-1/2 top-1/2 grid size-24 -translate-x-1/2 -translate-y-1/2 place-items-center rounded-full border border-white/20 bg-white/[0.06] text-center shadow-[0_0_34px_rgba(255,255,255,0.16)] backdrop-blur">
          <span className="font-orbitron text-xs font-semibold uppercase tracking-[0.18em] text-white">
            Omni
          </span>
        </div>
      </div>

      <style>{`
        @keyframes mp-w5-orbit-spin {
          from {
            transform: rotate(0deg);
          }
          to {
            transform: rotate(360deg);
          }
        }

        @keyframes mp-w5-sphere-converge {
          0%, 28% {
            transform:
              translate(-50%, -50%)
              rotate(var(--mp-w5-angle))
              translateX(118px)
              rotate(calc(var(--mp-w5-angle) * -1))
              scale(1);
            opacity: 0.88;
          }
          58%, 72% {
            transform:
              translate(-50%, -50%)
              rotate(var(--mp-w5-angle))
              translateX(20px)
              rotate(calc(var(--mp-w5-angle) * -1))
              scale(0.74);
            opacity: 1;
          }
          100% {
            transform:
              translate(-50%, -50%)
              rotate(var(--mp-w5-angle))
              translateX(118px)
              rotate(calc(var(--mp-w5-angle) * -1))
              scale(1);
            opacity: 0.88;
          }
        }

        .mp-w5-orbit {
          animation: mp-w5-orbit-spin 18s linear infinite;
        }

        .mp-w5-sphere {
          border-color: color-mix(in srgb, var(--mp-w5-accent) 72%, transparent);
          box-shadow:
            0 0 22px var(--mp-w5-glow),
            inset 0 0 18px color-mix(in srgb, var(--mp-w5-accent) 24%, transparent);
          animation: mp-w5-sphere-converge 4.8s ease-in-out infinite;
          animation-delay: var(--mp-w5-delay);
        }

        @media (prefers-reduced-motion: reduce) {
          .mp-w5-orbit,
          .mp-w5-sphere {
            animation: none;
          }

          .mp-w5-sphere {
            transform:
              translate(-50%, -50%)
              rotate(var(--mp-w5-angle))
              translateX(118px)
              rotate(calc(var(--mp-w5-angle) * -1));
          }
        }
      `}</style>
    </section>
  )
}
