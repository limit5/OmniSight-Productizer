/**
 * AS.7.0 — Auth Visual Foundation motion policy.
 *
 * Pure mapping `MotionLevel` → render budget for the four shared
 * visual layers (nebula shader / glass card / brand wordmark /
 * traveling light). Lives outside React so the BS.3.5 resolver
 * chain can also call it from the component-tree leaves without
 * depending on a React tree, and so the AS.7.7 perf-budget tests
 * can pin the budget table without spinning up jsdom.
 *
 * Why a dedicated mapping (vs. just gating each effect by
 * `level !== "off"`):
 *
 *   - The four BS.3 levels (`off / subtle / normal / dramatic`)
 *     express *user intent*. The nebula shader needs concrete
 *     numbers to feed into uniforms (star-layer count, parallax
 *     scale, FPS cap, mouse gravity-well strength). Doing the
 *     translation centrally makes the relationship between
 *     "what level the user picked" and "what the GPU actually
 *     renders" auditable in one file.
 *   - BS.3.4 battery-aware demotion already maps user pref →
 *     effective level; the auth visual layer composes that
 *     effective level with this mapping, never the user pref
 *     directly. So a low-battery user gets the demoted budget
 *     transparently (and we don't double-count the demotion).
 *
 * The `prefers-reduced-motion: reduce` short-circuit is owned by
 * BS.3.5 (`useEffectiveMotionLevel` returns `"off"` outright for
 * R25.2 users). This module never sees `reducedMotion`; it sees
 * the already-resolved `MotionLevel`. Callers that bypass BS.3.5
 * (e.g. SSR fallback) must apply that gate themselves.
 *
 * Module-global state audit (per docs/sop/implement_phase_step.md
 * Step 1): no module-level mutable state — only `as const` lookup
 * tables and pure functions. Cross-worker derivation is irrelevant
 * (browser-only); SSR sees the same constants. Answer #1 of the
 * SOP "deterministic-by-construction".
 *
 * Read-after-write timing: N/A — pure function over an enum.
 */

import type { MotionLevel } from "@/lib/motion-preferences"

/**
 * Concrete render-budget for the AS.7.0 visual stack. Each field
 * is a number / boolean a leaf component can pass straight into a
 * uniform / CSS variable / `requestAnimationFrame` cap.
 *
 * Field invariants (pinned by `motion-policy.test.ts`):
 *
 *   - `level === "off"`              → ALL animation off, layers = 0
 *   - `level === "subtle"`           → static gradient, no shader, no tilt
 *   - `level === "normal"`           → shader on, but reduced star layers
 *                                      and frame cap (battery courtesy)
 *   - `level === "dramatic"`         → full 8-layer experience
 *   - star layer count is monotonic in level (never decreases)
 *   - frame cap is monotonic in level
 */
export interface AuthVisualBudget {
  /** Number of independently-parallaxed star layers. 0 means "no
   *  stars rendered at all" (subtle / off). The shader supports up
   *  to 3 layers per the AS.7.0 row spec ("三層星空 parallax"). */
  starLayers: 0 | 1 | 2 | 3
  /** Maximum frames-per-second to drive `requestAnimationFrame` at.
   *  60 means "uncap (browser native)"; a lower cap saves GPU when
   *  the user picked normal / battery demoted to normal. */
  frameCapFps: 0 | 30 | 45 | 60
  /** Strength of the cursor gravity-well distortion in the fragment
   *  shader. 0 disables the effect (subtle / off); higher numbers
   *  bend the nebula more aggressively. */
  gravityWellStrength: number
  /** Vertical drift amplitude for the floating glass card, in px.
   *  0 disables the idle-drift animation. */
  idleDriftPx: number
  /** Maximum 3D tilt angle in degrees triggered by pointer hover.
   *  0 disables the tilt. */
  tiltMaxDeg: number
  /** Brand wordmark "traveling light" sweep enabled. False at off
   *  / subtle (the wordmark is still rendered, just static). */
  travelingLight: boolean
  /** Brand wordmark "breathing pulse" idle animation enabled. */
  breathingPulse: boolean
  /** Glass card neon-glow flicker enabled. False at off / subtle
   *  (steady glow rendered instead — visual presence preserved
   *  without animation). */
  glowFlicker: boolean
  /** Whether the nebula WebGL canvas should mount at all. False at
   *  off / subtle — the page falls back to the static CSS gradient
   *  defined in `styles/auth-visual.css`. */
  renderShader: boolean
}

/** The four motion-level → budget mappings. Frozen `as const` so a
 *  caller can't mutate the table at runtime. Every consumer reads
 *  via `getAuthVisualBudget()` so the lookup is the single source
 *  of truth — no hand-rolled switch in component code. */
const BUDGET_TABLE = {
  off: {
    starLayers: 0,
    frameCapFps: 0,
    gravityWellStrength: 0,
    idleDriftPx: 0,
    tiltMaxDeg: 0,
    travelingLight: false,
    breathingPulse: false,
    glowFlicker: false,
    renderShader: false,
  },
  subtle: {
    starLayers: 0,
    frameCapFps: 0,
    gravityWellStrength: 0,
    idleDriftPx: 0,
    tiltMaxDeg: 0,
    travelingLight: false,
    breathingPulse: true,
    glowFlicker: false,
    renderShader: false,
  },
  normal: {
    starLayers: 2,
    frameCapFps: 45,
    gravityWellStrength: 0.4,
    idleDriftPx: 4,
    tiltMaxDeg: 4,
    travelingLight: true,
    breathingPulse: true,
    glowFlicker: false,
    renderShader: true,
  },
  dramatic: {
    starLayers: 3,
    frameCapFps: 60,
    gravityWellStrength: 1.0,
    idleDriftPx: 8,
    tiltMaxDeg: 8,
    travelingLight: true,
    breathingPulse: true,
    glowFlicker: true,
    renderShader: true,
  },
} as const satisfies Record<MotionLevel, AuthVisualBudget>

/**
 * Resolve the visual budget for a given motion level. Pure; the
 * returned object is the `BUDGET_TABLE` row for that level (not a
 * fresh copy — callers must treat it as read-only, which the
 * `Readonly<...>` return type enforces at the type layer).
 */
export function getAuthVisualBudget(level: MotionLevel): Readonly<AuthVisualBudget> {
  return BUDGET_TABLE[level]
}

/**
 * The full table. Exposed for the AS.7.0 perf-budget tests so they
 * can iterate every `(level, budget)` row without repeating the
 * keys. Frozen via `as const`; mutating it is a TS error.
 */
export const AUTH_VISUAL_BUDGET_TABLE = BUDGET_TABLE
