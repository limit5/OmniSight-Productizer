"use client"

/**
 * MP.W5.2 - Multi-provider onboarding, step 2.
 *
 * Renders the "Open the workshop" transition. Provider spheres start
 * near the center, fly to the four workshop corners, then fade while
 * the central Orchestrator A resolves in. The parent wizard owns
 * step state; this leaf owns only the visual timing and completion
 * callback.
 *
 * Motion policy:
 *   - full    - spheres travel to corners, fade, then A resolves in
 *   - reduce  - static corner layout + visible A, no timer delay
 *
 * Module-global state audit: immutable config only. No mutable
 * module-level container.
 */

import type { CSSProperties, JSX } from "react"
import { useEffect } from "react"

export type OnboardingWizardStep2Motion = "full" | "reduce"

export interface OnboardingWizardStep2Provider {
  id: string
  label: string
  shortLabel: string
  color: string
}

interface WorkshopSphere {
  provider: OnboardingWizardStep2Provider
  corner: "top-left" | "top-right" | "bottom-left" | "bottom-right"
  startX: number
  startY: number
  endX: number
  endY: number
  delayMs: number
}

export interface OnboardingWizardStep2Props {
  active?: boolean
  motion?: OnboardingWizardStep2Motion
  providers?: readonly OnboardingWizardStep2Provider[]
  onComplete?: () => void
}

export const ONBOARDING_STEP2_DURATION_MS = 1600

const DEFAULT_PROVIDERS: readonly OnboardingWizardStep2Provider[] =
  Object.freeze([
    {
      id: "anthropic",
      label: "Anthropic",
      shortLabel: "A",
      color: "#8b5cf6",
    },
    {
      id: "openai",
      label: "OpenAI",
      shortLabel: "O",
      color: "#22c55e",
    },
    {
      id: "google",
      label: "Google",
      shortLabel: "G",
      color: "#38bdf8",
    },
    {
      id: "groq",
      label: "Groq",
      shortLabel: "Q",
      color: "#f97316",
    },
  ])

const CORNER_POSITIONS = [
  {
    corner: "top-left",
    startX: -10,
    startY: -8,
    endX: -118,
    endY: -82,
    delayMs: 0,
  },
  {
    corner: "top-right",
    startX: 12,
    startY: -10,
    endX: 118,
    endY: -82,
    delayMs: 80,
  },
  {
    corner: "bottom-left",
    startX: -14,
    startY: 12,
    endX: -118,
    endY: 82,
    delayMs: 160,
  },
  {
    corner: "bottom-right",
    startX: 14,
    startY: 10,
    endX: 118,
    endY: 82,
    delayMs: 240,
  },
] as const

function buildSpheres(
  providers: readonly OnboardingWizardStep2Provider[],
): readonly WorkshopSphere[] {
  return CORNER_POSITIONS.map((position, index) => ({
    provider: providers[index] ?? DEFAULT_PROVIDERS[index],
    ...position,
  }))
}

export function OnboardingWizardStep2({
  active = true,
  motion = "full",
  providers = DEFAULT_PROVIDERS,
  onComplete,
}: OnboardingWizardStep2Props): JSX.Element {
  const spheres = buildSpheres(providers)
  const shouldAnimate = active && motion === "full"

  useEffect(() => {
    if (!active || !onComplete) return
    if (!shouldAnimate) {
      const id = window.setTimeout(onComplete, 0)
      return () => window.clearTimeout(id)
    }

    const id = window.setTimeout(onComplete, ONBOARDING_STEP2_DURATION_MS)
    return () => window.clearTimeout(id)
  }, [active, onComplete, shouldAnimate])

  return (
    <section
      data-testid="mp-w5-step2"
      data-mp-w5-step2-active={active ? "yes" : "no"}
      data-mp-w5-step2-motion={motion}
      aria-labelledby="mp-w5-step2-title"
      className="relative isolate min-h-[320px] overflow-hidden rounded-lg border border-slate-200 bg-slate-950 px-6 py-7 text-white shadow-sm"
    >
      <div className="relative z-10 flex items-start justify-between gap-4">
        <div className="min-w-0">
          <p className="text-xs font-medium uppercase tracking-[0.18em] text-cyan-200">
            Step 2
          </p>
          <h2
            id="mp-w5-step2-title"
            className="mt-2 text-xl font-semibold text-white"
          >
            Open the workshop
          </h2>
        </div>
        <span className="rounded border border-white/15 bg-white/10 px-2 py-1 text-xs font-medium text-cyan-100">
          Multi-provider
        </span>
      </div>

      <div
        data-testid="mp-w5-step2-stage"
        data-mp-w5-step2-animate={shouldAnimate ? "yes" : "no"}
        className="absolute inset-x-6 bottom-7 top-24"
        aria-hidden="true"
      >
        <div className="absolute inset-0 rounded-lg border border-white/10 bg-white/[0.03]" />
        <div className="mp-w5-step2-grid" />

        {spheres.map((sphere) => (
          <span
            key={sphere.provider.id}
            data-testid={`mp-w5-step2-sphere-${sphere.provider.id}`}
            data-mp-w5-step2-corner={sphere.corner}
            className="mp-w5-step2-sphere"
            style={
              {
                "--mp-w5-step2-color": sphere.provider.color,
                "--mp-w5-step2-start-x": `${sphere.startX}px`,
                "--mp-w5-step2-start-y": `${sphere.startY}px`,
                "--mp-w5-step2-end-x": `${sphere.endX}px`,
                "--mp-w5-step2-end-y": `${sphere.endY}px`,
                "--mp-w5-step2-delay": `${sphere.delayMs}ms`,
              } as CSSProperties
            }
          >
            <span className="mp-w5-step2-sphere-glow" />
            <span className="mp-w5-step2-sphere-label">
              {sphere.provider.shortLabel}
            </span>
          </span>
        ))}

        <div
          data-testid="mp-w5-step2-a-mark"
          className="mp-w5-step2-a-mark"
        >
          <span className="mp-w5-step2-a-ring" />
          <span className="mp-w5-step2-a-letter">A</span>
        </div>
      </div>

      <div className="absolute inset-0 -z-10 bg-[radial-gradient(circle_at_50%_35%,rgba(14,165,233,0.24),transparent_42%)]" />

      <style>{`
        .mp-w5-step2-grid {
          position: absolute;
          inset: 16px;
          opacity: 0.38;
          background-image:
            linear-gradient(rgba(255,255,255,0.08) 1px, transparent 1px),
            linear-gradient(90deg, rgba(255,255,255,0.08) 1px, transparent 1px);
          background-size: 28px 28px;
          mask-image: radial-gradient(circle at center, black 34%, transparent 76%);
        }

        .mp-w5-step2-sphere,
        .mp-w5-step2-a-mark {
          position: absolute;
          left: 50%;
          top: 50%;
        }

        .mp-w5-step2-sphere {
          display: grid;
          height: 46px;
          width: 46px;
          place-items: center;
          border-radius: 999px;
          border: 1px solid rgba(255,255,255,0.38);
          background:
            radial-gradient(circle at 32% 28%, rgba(255,255,255,0.86), transparent 18%),
            radial-gradient(circle at center, var(--mp-w5-step2-color), rgba(15,23,42,0.92) 72%);
          box-shadow:
            0 0 26px color-mix(in srgb, var(--mp-w5-step2-color) 62%, transparent),
            inset 0 0 18px rgba(255,255,255,0.18);
          transform: translate(calc(-50% + var(--mp-w5-step2-end-x)), calc(-50% + var(--mp-w5-step2-end-y))) scale(0.82);
          opacity: 0.28;
        }

        [data-mp-w5-step2-animate="yes"] .mp-w5-step2-sphere {
          animation: mp-w5-step2-sphere-flight 1280ms cubic-bezier(0.22, 0.86, 0.28, 1) both;
          animation-delay: var(--mp-w5-step2-delay);
        }

        .mp-w5-step2-sphere-glow {
          position: absolute;
          inset: -10px;
          border-radius: inherit;
          background: radial-gradient(circle, color-mix(in srgb, var(--mp-w5-step2-color) 42%, transparent), transparent 64%);
        }

        .mp-w5-step2-sphere-label {
          position: relative;
          font-size: 0.76rem;
          font-weight: 800;
          line-height: 1;
          text-shadow: 0 1px 8px rgba(15,23,42,0.7);
        }

        .mp-w5-step2-a-mark {
          display: grid;
          height: 86px;
          width: 86px;
          place-items: center;
          transform: translate(-50%, -50%);
          opacity: 1;
        }

        [data-mp-w5-step2-animate="yes"] .mp-w5-step2-a-mark {
          animation: mp-w5-step2-a-resolve 760ms ease-out 780ms both;
        }

        .mp-w5-step2-a-ring {
          position: absolute;
          inset: 0;
          border-radius: 999px;
          border: 1px solid rgba(125,211,252,0.58);
          background: radial-gradient(circle, rgba(14,165,233,0.2), rgba(15,23,42,0.84) 70%);
          box-shadow:
            0 0 34px rgba(14,165,233,0.45),
            inset 0 0 24px rgba(125,211,252,0.12);
        }

        .mp-w5-step2-a-letter {
          position: relative;
          font-size: 3rem;
          font-weight: 900;
          line-height: 1;
          color: white;
          text-shadow: 0 0 22px rgba(125,211,252,0.86);
        }

        @keyframes mp-w5-step2-sphere-flight {
          0% {
            transform: translate(calc(-50% + var(--mp-w5-step2-start-x)), calc(-50% + var(--mp-w5-step2-start-y))) scale(1);
            opacity: 1;
          }
          58% {
            transform: translate(calc(-50% + var(--mp-w5-step2-end-x)), calc(-50% + var(--mp-w5-step2-end-y))) scale(0.9);
            opacity: 1;
          }
          100% {
            transform: translate(calc(-50% + var(--mp-w5-step2-end-x)), calc(-50% + var(--mp-w5-step2-end-y))) scale(0.72);
            opacity: 0.18;
          }
        }

        @keyframes mp-w5-step2-a-resolve {
          0% {
            transform: translate(-50%, -50%) scale(0.72);
            opacity: 0;
            filter: blur(10px);
          }
          100% {
            transform: translate(-50%, -50%) scale(1);
            opacity: 1;
            filter: blur(0);
          }
        }

        @media (prefers-reduced-motion: reduce) {
          [data-mp-w5-step2-animate="yes"] .mp-w5-step2-sphere,
          [data-mp-w5-step2-animate="yes"] .mp-w5-step2-a-mark {
            animation: none;
          }
        }
      `}</style>
    </section>
  )
}
