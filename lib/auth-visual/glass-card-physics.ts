/**
 * AS.7.0 — Glass-card physics (pure helpers).
 *
 * The floating glass card has three motion concerns the leaf
 * component composes:
 *
 *   1. **Idle drift** — slow vertical bob. Driven by `u_time` →
 *      `Math.sin`; amplitude in px comes from the motion-policy
 *      budget.
 *   2. **3D tilt** — pointer-driven `rotateX` / `rotateY` from
 *      the card's centre. Maps the pointer's offset within the
 *      card bounding box (in 0..1) to a clamped degree range.
 *   3. **Scroll parallax** — translateY that tracks scroll position
 *      at a fraction of 1.0 so the card appears to drift past
 *      static page content at a slightly different speed.
 *
 * Keeping these three transforms as pure functions (rather than
 * inline `useEffect` math in the React leaf) lets the AS.7.0 unit
 * tests pin the boundary truth tables (corner / centre / edge /
 * out-of-bounds) without mounting jsdom and emitting `mousemove`
 * events.
 *
 * Module-global state audit: zero module-level mutable state.
 * Pure functions, no globals, no React state. Per-worker /
 * per-tab derivation is trivially identical (Answer #1).
 *
 * Read-after-write timing: N/A — pure math.
 */

/** A 3D tilt expressed as `(rotateX, rotateY)` in degrees, plus a
 *  Z translation that gives the card a subtle "lift" toward the
 *  pointer (small, < 12 px, to stay inside the page composition
 *  budget). */
export interface GlassCardTilt {
  rotateXDeg: number
  rotateYDeg: number
  translateZPx: number
}

/**
 * Map a pointer position relative to a card's bounding box to a
 * 3D tilt. `pointerXNorm` / `pointerYNorm` are 0..1 with origin at
 * the top-left of the card; out-of-range values are clamped (so a
 * pointer leaving the card briefly doesn't snap to a wild angle).
 *
 *   - top-left  (0, 0)   → rotateX +max, rotateY -max
 *   - bottom-right (1,1) → rotateX -max, rotateY +max
 *   - centre   (0.5,0.5) → 0, 0
 *
 * `tiltMaxDeg = 0` short-circuits to a zero tilt — the off /
 * subtle motion levels skip the effect entirely without the leaf
 * having to special-case each call site.
 */
export function tiltFromPointer(
  pointerXNorm: number,
  pointerYNorm: number,
  tiltMaxDeg: number,
): GlassCardTilt {
  if (tiltMaxDeg <= 0) {
    return { rotateXDeg: 0, rotateYDeg: 0, translateZPx: 0 }
  }
  const x = clampUnit(pointerXNorm)
  const y = clampUnit(pointerYNorm)
  const offsetX = x - 0.5
  const offsetY = y - 0.5
  // Y axis movement → rotateX (top tilts toward viewer when
  // pointer is at the top); X axis movement → rotateY.
  // `+ 0` normalises the JS negative-zero that pops out of
  // `-0 * k` when `offsetY === 0` — callers compare via Object.is
  // and `-0 !== 0` would surprise them.
  const rotateXDeg = -offsetY * 2 * tiltMaxDeg + 0
  const rotateYDeg = offsetX * 2 * tiltMaxDeg + 0
  const proximity = 1 - Math.min(1, Math.hypot(offsetX, offsetY) * 2)
  const translateZPx = proximity * Math.min(12, tiltMaxDeg * 1.5) + 0
  return { rotateXDeg, rotateYDeg, translateZPx }
}

/**
 * Idle bob translation in px. `timeMs` is a monotonic clock
 * (`performance.now()`); `amplitudePx` is the motion-policy
 * `idleDriftPx`. Returns 0 when amplitude is 0 (off / subtle).
 *
 * Period is 6 seconds — slow enough that the bob feels like a
 * "breathing" cadence, not a noticeable bounce.
 */
export function idleDriftOffsetPx(timeMs: number, amplitudePx: number): number {
  if (amplitudePx <= 0 || !Number.isFinite(timeMs)) return 0
  const periodMs = 6000
  const phase = (timeMs % periodMs) / periodMs
  return Math.sin(phase * Math.PI * 2) * amplitudePx
}

/**
 * Scroll parallax translation in px. `scrollY` is the page scroll
 * offset; `factor` is a fraction (typically -0.1..-0.3, negative
 * because the card should drift *up* slightly when the user
 * scrolls down to give the parallax illusion). Returns 0 for
 * non-finite scroll positions or factor === 0.
 */
export function scrollParallaxOffsetPx(scrollY: number, factor: number): number {
  if (!Number.isFinite(scrollY) || factor === 0) return 0
  return scrollY * factor
}

/** Clamp `n` to [0, 1]. Used by `tiltFromPointer` to defend
 *  against pointer events that arrive after the user moves the
 *  mouse outside the card bounding rect. */
function clampUnit(n: number): number {
  if (!Number.isFinite(n)) return 0.5
  if (n <= 0) return 0
  if (n >= 1) return 1
  return n
}

/**
 * Compose the three transforms into a single CSS `transform`
 * string suitable for `element.style.transform`. Order matters —
 * translateY (drift + parallax) wraps the rotation so the tilt is
 * applied *after* translation, giving the visual "card sits on
 * its current vertical position and rotates around its centre"
 * effect rather than orbiting the page origin.
 */
export function buildGlassCardTransform(args: {
  driftPx: number
  parallaxPx: number
  tilt: GlassCardTilt
}): string {
  const { driftPx, parallaxPx, tilt } = args
  const ty = driftPx + parallaxPx
  // perspective-on-the-element keeps the tilt local to the card —
  // adding it on the parent would also tilt the rest of the auth
  // page through this element's stacking context.
  return [
    `perspective(1200px)`,
    `translate3d(0, ${round3(ty)}px, ${round3(tilt.translateZPx)}px)`,
    `rotateX(${round3(tilt.rotateXDeg)}deg)`,
    `rotateY(${round3(tilt.rotateYDeg)}deg)`,
  ].join(" ")
}

function round3(n: number): number {
  return Math.round(n * 1000) / 1000
}
