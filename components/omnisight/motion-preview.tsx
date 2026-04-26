"use client"

/**
 * BS.3.6 — Live motion-preview demo card.
 *
 * Surfaces the BS.3.2 motion library hooks in a single self-contained
 * card so the Display Settings page can show users the actual visual
 * behaviour their `MotionLevel` choice produces. Re-renders whenever
 * `useEffectiveMotionLevel()` resolves to a new level (BS.3.5 event
 * bus → user pref change in this same tab → demo card updates without
 * a page refresh).
 *
 * Effects layered on the demo card:
 *
 *   - `useFloatingCard("a")` — Layer 1 idle drift (all non-off levels).
 *   - `useCursorMagneticTilt({maxTiltDeg: 8})` — Layer 6 cursor magnet
 *     (normal + dramatic only; `subtle`/`off` no-op).
 *   - `useGlassReflection()` — Layer 7 cursor-tracking glass reflection
 *     (dramatic-only per ADR §5.7).
 *   - `useCursorDistanceGlow({maxDistancePx: 320})` — Layer 4 distance
 *     glow (all non-off levels).
 *
 * `useScrollParallax` is deliberately NOT wired here — the demo card
 * sits inside a settings page that is rarely scrolled long enough for
 * parallax to read; instead the card describes parallax in the legend
 * row (the hook itself is exercised by the BS.5 hero / BS.6 catalog
 * cards in their own surfaces).
 *
 * Module-global state audit: none. Every effect is per-element via
 * the BS.3.2 hooks (each owning their own `ref`, `useEffect`,
 * cleanup). Re-mounting the card re-arms every listener — the desired
 * UX for "user toggled motion off then back on, layers should refresh".
 */

import { Sparkles } from "lucide-react"

import {
  useCursorDistanceGlow,
  useCursorMagneticTilt,
  useEffectiveMotionLevel,
  useFloatingCard,
  useGlassReflection,
} from "@/hooks/use-zero-g"
import type { MotionLevel } from "@/lib/motion-preferences"

// ─────────────────────────────────────────────────────────────────────
// Per-level legend copy for the bottom strip
// ─────────────────────────────────────────────────────────────────────

const LEVEL_LABEL: Record<MotionLevel, string> = {
  off: "Off — 無動畫",
  subtle: "Subtle — 輕微浮動",
  normal: "Normal — 含磁吸傾斜",
  dramatic: "Dramatic — 全 8 層全開",
}

const LAYER_AVAILABILITY: Record<MotionLevel, ReadonlyArray<string>> = {
  off: ["—"],
  subtle: ["1 漂浮", "2 視差", "4 距離光暈", "8 彈簧按壓"],
  normal: [
    "1 漂浮",
    "2 視差",
    "4 距離光暈",
    "5 群組呼吸",
    "6 磁吸傾斜",
    "8 彈簧按壓",
  ],
  dramatic: [
    "1 漂浮",
    "2 視差",
    "3 軌道旋轉",
    "4 距離光暈",
    "5 群組呼吸",
    "6 磁吸傾斜",
    "7 玻璃反射",
    "8 彈簧按壓",
  ],
}

export function MotionPreview() {
  const level = useEffectiveMotionLevel()
  const float = useFloatingCard("a")
  // Destructure each hook so the `ref` lives in a name distinct from
  // the render-safe `className` / `style` — keeps the
  // `react-hooks/refs` lint happy (it forbids any field access on an
  // object that *contains* a ref during render, even when the field
  // itself is a plain string).
  const { ref: tiltRef, style: tiltStyle } = useCursorMagneticTilt<HTMLDivElement>({
    maxTiltDeg: 8,
  })
  const { ref: reflectRef, className: reflectClassName } = useGlassReflection<HTMLDivElement>()
  const { ref: glowRef, className: glowClassName } = useCursorDistanceGlow<HTMLDivElement>({
    maxDistancePx: 320,
  })

  return (
    <div
      data-testid="motion-preview"
      data-motion-level={level}
      className="rounded-xl border border-[var(--border)] bg-[var(--card)] p-6"
    >
      <div className="mb-4 flex items-center justify-between text-[10px] font-mono uppercase tracking-wider text-[var(--muted-foreground)]">
        <span className="flex items-center gap-1.5">
          <Sparkles size={11} className="text-[var(--neural-blue)]" />
          live preview
        </span>
        <span data-testid="motion-preview-level">
          effective: <span className="text-[var(--foreground)]">{level}</span>
        </span>
      </div>

      {/* The demo surface stacks four hooks via nested wrappers —
          each hook owns its own `ref` and applies its own listener
          to the element it's bound to. Visual layering, outside-in:
            • tilt   → wraps everything, applies the perspective + rotate
            • float  → idle drift + per-card amplitude/lift CSS vars
            • reflect → glass `::after` reflection (dramatic-only)
            • glow   → cursor-distance box-shadow (all non-off levels)
          Pointer events bubble to every layer, so each hook's
          `pointermove` listener fires off the same gesture. */}
      <div ref={tiltRef} style={tiltStyle} className="mx-auto w-full max-w-md">
        <div
          style={float.style}
          className={["rounded-lg", float.className].filter(Boolean).join(" ")}
        >
          <div
            ref={reflectRef}
            className={["rounded-lg", reflectClassName].filter(Boolean).join(" ")}
          >
            <div
              ref={glowRef}
              className={[
                "group relative flex h-44 select-none items-center justify-center rounded-lg border border-[var(--border)] bg-gradient-to-br from-[var(--secondary)]/30 to-[var(--card)]",
                glowClassName,
              ]
                .filter(Boolean)
                .join(" ")}
            >
              <div className="pointer-events-none text-center">
                <div className="font-orbitron text-2xl tracking-wider text-[var(--foreground)]">
                  OmniSight
                </div>
                <div className="mt-1 font-mono text-[10px] uppercase tracking-widest text-[var(--muted-foreground)]">
                  move your cursor over this card
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>

      <div className="mt-4 grid grid-cols-1 gap-2 text-[11px] font-mono text-[var(--muted-foreground)] sm:grid-cols-2">
        <div>
          <span className="text-[var(--foreground)]">{LEVEL_LABEL[level]}</span>
        </div>
        <div className="sm:text-right">
          {LAYER_AVAILABILITY[level].join(" · ")}
        </div>
      </div>
    </div>
  )
}
