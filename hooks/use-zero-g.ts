"use client"

/**
 * BS.3.2 — Zero-gravity motion library hooks.
 *
 * Five hooks that drive the keyframes + utility classes shipped in
 * `app/globals.css` (BS.3.1). Each hook respects
 * `useEffectiveMotionLevel()` (BS.3.5) which layers
 * `prefers-reduced-motion` > `motion: off` > battery rule >
 * user preference.
 *
 * Contract reminder (defaults = `dramatic`, see globals.css §BS.3):
 *   --motion-amplitude  1.5  (subtle 0.5 / normal 1.0 / off 0)
 *   --motion-lift       5px  (subtle 1px / normal 3px / off 0)
 *   --motion-tilt-x/y   0deg (cursor magnetic tilt; hook-set)
 *   --glow-intensity    0    (cursor-distance glow, 0..1; hook-set)
 *   --reflect-x/y       50%  (glass reflection cursor x/y; hook-set)
 *
 * SSR safety: hooks render no class / no listeners until `useEffect`
 * runs on the client, mirroring the `useCinemaMode` precedent.
 */

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type RefObject,
} from "react"

import { useEffectiveMotionLevel } from "@/hooks/use-effective-motion-level"
import type { MotionLevel } from "@/lib/motion-preferences"

// ─────────────────────────────────────────────────────────────────────
// MotionLevel contract
// ─────────────────────────────────────────────────────────────────────

// `MotionLevel` and `useEffectiveMotionLevel` are owned by
// `@/lib/motion-preferences` (BS.3.3) and
// `@/hooks/use-effective-motion-level` (BS.3.5) respectively. We
// re-export them here so existing call sites that imported them
// from this module keep working.
export type { MotionLevel }
export { useEffectiveMotionLevel }

/** Multiplier applied to `--motion-amplitude` per level. Mirrors
 *  the CSS contract documented in `app/globals.css §BS.3`. */
const AMPLITUDE_BY_LEVEL: Record<MotionLevel, number> = {
  off: 0,
  subtle: 0.5,
  normal: 1.0,
  dramatic: 1.5,
}

/** `--motion-lift` value (px) per level. */
const LIFT_BY_LEVEL: Record<MotionLevel, string> = {
  off: "0px",
  subtle: "1px",
  normal: "3px",
  dramatic: "5px",
}

export const MOTION_AMPLITUDE_BY_LEVEL = AMPLITUDE_BY_LEVEL
export const MOTION_LIFT_BY_LEVEL = LIFT_BY_LEVEL

// ─────────────────────────────────────────────────────────────────────
// useFloatingCard — Layer 1 idle drift
// ─────────────────────────────────────────────────────────────────────

export type FloatVariant = "a" | "b" | "c" | "d"

export interface UseFloatingCardResult {
  /** Class name to apply: `float-card-{variant}` when motion is on, "" when off. */
  className: string
  /** Inline style providing per-card `--motion-amplitude` + `--motion-lift`. */
  style: CSSProperties
}

/**
 * Returns the `.float-card-{variant}` class + per-card amplitude/lift
 * vars sized to the effective motion level. Returns an empty class
 * (i.e. no animation) when level === 'off'.
 *
 * @param variant Which of the four phase-offset keyframes to use.
 *                Cycles a/b/c/d based on the index a parent assigns,
 *                so adjacent cards never sync.
 */
export function useFloatingCard(variant: FloatVariant = "a"): UseFloatingCardResult {
  const level = useEffectiveMotionLevel()
  return useMemo<UseFloatingCardResult>(() => {
    if (level === "off") {
      return { className: "", style: { "--motion-amplitude": 0 } as CSSProperties }
    }
    return {
      className: `float-card-${variant}`,
      style: {
        "--motion-amplitude": AMPLITUDE_BY_LEVEL[level],
        "--motion-lift": LIFT_BY_LEVEL[level],
      } as CSSProperties,
    }
  }, [level, variant])
}

// ─────────────────────────────────────────────────────────────────────
// useCursorMagneticTilt — Layer 6 (normal+ only)
// ─────────────────────────────────────────────────────────────────────

export interface UseCursorMagneticTiltOptions {
  /** Maximum tilt angle in degrees at the corners. Default 6deg. */
  maxTiltDeg?: number
}

export interface UseCursorMagneticTiltResult<T extends HTMLElement> {
  ref: RefObject<T | null>
  style: CSSProperties
}

/**
 * Tracks the pointer over the attached element and writes
 * `--motion-tilt-x` / `--motion-tilt-y` so consumers can apply a
 * `transform: rotateX(...) rotateY(...)` overlay. Disabled at
 * level === 'off' or 'subtle' (the layer is part of the `normal`
 * tier per ADR §5.7).
 */
export function useCursorMagneticTilt<T extends HTMLElement = HTMLDivElement>(
  options: UseCursorMagneticTiltOptions = {},
): UseCursorMagneticTiltResult<T> {
  const { maxTiltDeg = 6 } = options
  const ref = useRef<T | null>(null)
  const level = useEffectiveMotionLevel()
  const enabled = level === "normal" || level === "dramatic"

  useEffect(() => {
    const el = ref.current
    if (!el || !enabled) return

    const onMove = (ev: PointerEvent) => {
      const rect = el.getBoundingClientRect()
      if (rect.width === 0 || rect.height === 0) return
      const cx = rect.left + rect.width / 2
      const cy = rect.top + rect.height / 2
      // Normalised position: -1..1 across each axis.
      const nx = (ev.clientX - cx) / (rect.width / 2)
      const ny = (ev.clientY - cy) / (rect.height / 2)
      const amp = AMPLITUDE_BY_LEVEL[level]
      // Tilt-X is the rotation around the X-axis (driven by Y position),
      // and vice versa — matches CSS perspective convention.
      const tiltX = (-ny * maxTiltDeg * amp).toFixed(3)
      const tiltY = (nx * maxTiltDeg * amp).toFixed(3)
      el.style.setProperty("--motion-tilt-x", `${tiltX}deg`)
      el.style.setProperty("--motion-tilt-y", `${tiltY}deg`)
    }

    const onLeave = () => {
      el.style.setProperty("--motion-tilt-x", "0deg")
      el.style.setProperty("--motion-tilt-y", "0deg")
    }

    el.addEventListener("pointermove", onMove)
    el.addEventListener("pointerleave", onLeave)
    return () => {
      el.removeEventListener("pointermove", onMove)
      el.removeEventListener("pointerleave", onLeave)
      onLeave()
    }
  }, [enabled, level, maxTiltDeg])

  const style = useMemo<CSSProperties>(
    () =>
      enabled
        ? ({
            "--motion-amplitude": AMPLITUDE_BY_LEVEL[level],
            transform:
              "perspective(800px) rotateX(var(--motion-tilt-x)) rotateY(var(--motion-tilt-y))",
            transition: "transform 80ms ease-out",
            willChange: "transform",
          } as CSSProperties)
        : ({} as CSSProperties),
    [enabled, level],
  )

  return { ref, style }
}

// ─────────────────────────────────────────────────────────────────────
// useGlassReflection — Layer 7 (dramatic-only per ADR §5.7)
// ─────────────────────────────────────────────────────────────────────

export interface UseGlassReflectionResult<T extends HTMLElement> {
  ref: RefObject<T | null>
  /** `holo-reflect-glass` only when level === 'dramatic'; otherwise "". */
  className: string
}

/**
 * Cursor-tracking reflection layered via the `.holo-reflect-glass`
 * `::after` pseudo. Per ADR §5.7 this layer is gated to `dramatic`;
 * at any other level the class is omitted (no listener attached, no
 * CSS variable writes, no GPU layer cost).
 */
export function useGlassReflection<T extends HTMLElement = HTMLDivElement>(): UseGlassReflectionResult<T> {
  const ref = useRef<T | null>(null)
  const level = useEffectiveMotionLevel()
  const enabled = level === "dramatic"

  useEffect(() => {
    const el = ref.current
    if (!el || !enabled) return

    const onMove = (ev: PointerEvent) => {
      const rect = el.getBoundingClientRect()
      if (rect.width === 0 || rect.height === 0) return
      const x = ((ev.clientX - rect.left) / rect.width) * 100
      const y = ((ev.clientY - rect.top) / rect.height) * 100
      el.style.setProperty("--reflect-x", `${x.toFixed(2)}%`)
      el.style.setProperty("--reflect-y", `${y.toFixed(2)}%`)
    }

    const onLeave = () => {
      el.style.setProperty("--reflect-x", "50%")
      el.style.setProperty("--reflect-y", "50%")
    }

    el.addEventListener("pointermove", onMove)
    el.addEventListener("pointerleave", onLeave)
    return () => {
      el.removeEventListener("pointermove", onMove)
      el.removeEventListener("pointerleave", onLeave)
      onLeave()
    }
  }, [enabled])

  return { ref, className: enabled ? "holo-reflect-glass" : "" }
}

// ─────────────────────────────────────────────────────────────────────
// useScrollParallax — translate-Y per scroll (Layer 2)
// ─────────────────────────────────────────────────────────────────────

export interface UseScrollParallaxOptions {
  /** Scroll-delta multiplier. 0.2 = element moves 20% of scroll. */
  speed?: number
  /** Maximum |translateY| in pixels (clamps speed). Default 80. */
  maxOffsetPx?: number
}

export interface UseScrollParallaxResult<T extends HTMLElement> {
  ref: RefObject<T | null>
  style: CSSProperties
}

/**
 * Writes a `transform: translate3d(0, Ypx, 0)` style based on
 * window.scrollY × speed × amplitude. Subscribed via `requestAnimationFrame`
 * batching so multiple parallax elements share one rAF tick.
 */
export function useScrollParallax<T extends HTMLElement = HTMLDivElement>(
  options: UseScrollParallaxOptions = {},
): UseScrollParallaxResult<T> {
  const { speed = 0.2, maxOffsetPx = 80 } = options
  const ref = useRef<T | null>(null)
  const level = useEffectiveMotionLevel()
  const enabled = level !== "off"
  const [offset, setOffset] = useState(0)

  useEffect(() => {
    if (!enabled) return
    let raf = 0
    let pending = false

    const compute = () => {
      pending = false
      const amp = AMPLITUDE_BY_LEVEL[level]
      const raw = window.scrollY * speed * amp
      const clamped = Math.max(-maxOffsetPx, Math.min(maxOffsetPx, raw))
      setOffset(clamped)
    }

    const onScroll = () => {
      if (pending) return
      pending = true
      raf = window.requestAnimationFrame(compute)
    }

    compute()
    window.addEventListener("scroll", onScroll, { passive: true })
    return () => {
      window.removeEventListener("scroll", onScroll)
      if (raf) window.cancelAnimationFrame(raf)
    }
  }, [enabled, level, speed, maxOffsetPx])

  // When the level is `off`, render `translate3d(0, 0, 0)` regardless
  // of any stale `offset` left in state from a prior level — avoids
  // calling setState inside the effect just to clear it.
  const renderedOffset = enabled ? offset : 0
  const style = useMemo<CSSProperties>(
    () =>
      enabled
        ? {
            transform: `translate3d(0, ${renderedOffset.toFixed(2)}px, 0)`,
            willChange: "transform",
          }
        : {},
    [enabled, renderedOffset],
  )

  return { ref, style }
}

// ─────────────────────────────────────────────────────────────────────
// useCursorDistanceGlow — Layer 4 catalog cards
// ─────────────────────────────────────────────────────────────────────

export interface UseCursorDistanceGlowOptions {
  /** Pixel distance at which intensity hits 0. Default 240. */
  maxDistancePx?: number
}

export interface UseCursorDistanceGlowResult<T extends HTMLElement> {
  ref: RefObject<T | null>
  /** `cursor-distance-glow` only when level !== 'off'. */
  className: string
}

/**
 * Listens to document `pointermove` and writes `--glow-intensity`
 * (0..1) on the attached element based on the cursor's distance
 * from the element's centre. Subscribed once globally via rAF
 * batching to keep the hot path compositor-only (only `box-shadow`
 * alpha changes, fixed spread per ADR §5.5).
 */
export function useCursorDistanceGlow<T extends HTMLElement = HTMLDivElement>(
  options: UseCursorDistanceGlowOptions = {},
): UseCursorDistanceGlowResult<T> {
  const { maxDistancePx = 240 } = options
  const ref = useRef<T | null>(null)
  const level = useEffectiveMotionLevel()
  const enabled = level !== "off"

  useEffect(() => {
    const el = ref.current
    if (!el || !enabled) return

    let raf = 0
    let pending = false
    let lastX = 0
    let lastY = 0

    const apply = () => {
      pending = false
      const rect = el.getBoundingClientRect()
      if (rect.width === 0 || rect.height === 0) return
      const cx = rect.left + rect.width / 2
      const cy = rect.top + rect.height / 2
      const dx = lastX - cx
      const dy = lastY - cy
      const dist = Math.sqrt(dx * dx + dy * dy)
      const intensity = Math.max(0, Math.min(1, 1 - dist / maxDistancePx))
      el.style.setProperty("--glow-intensity", intensity.toFixed(3))
    }

    const onMove = (ev: PointerEvent) => {
      lastX = ev.clientX
      lastY = ev.clientY
      if (pending) return
      pending = true
      raf = window.requestAnimationFrame(apply)
    }

    const onLeave = () => {
      el.style.setProperty("--glow-intensity", "0")
    }

    document.addEventListener("pointermove", onMove)
    document.addEventListener("pointerleave", onLeave)
    return () => {
      document.removeEventListener("pointermove", onMove)
      document.removeEventListener("pointerleave", onLeave)
      if (raf) window.cancelAnimationFrame(raf)
      onLeave()
    }
  }, [enabled, maxDistancePx])

  return { ref, className: enabled ? "cursor-distance-glow" : "" }
}

/** Reset hook for the `data-pressing` attribute used by the
 *  `.spring-press` keyframe (Layer 8). Returns a `pressProps`
 *  bundle for ergonomic spreading on a `<button>`. Independent
 *  of motion level — the user always wants click feedback —
 *  but the keyframe itself becomes a no-op via the global
 *  `prefers-reduced-motion` fallback already shipped at line ~1590
 *  of globals.css. */
export interface UseSpringPressResult {
  pressProps: {
    onPointerDown: () => void
    onPointerUp: () => void
    onPointerLeave: () => void
    "data-pressing"?: "true"
  }
  className: "spring-press"
}

export function useSpringPress(): UseSpringPressResult {
  const [pressing, setPressing] = useState(false)
  const onPointerDown = useCallback(() => setPressing(true), [])
  const onPointerUp = useCallback(() => setPressing(false), [])
  const onPointerLeave = useCallback(() => setPressing(false), [])
  return {
    className: "spring-press",
    pressProps: {
      onPointerDown,
      onPointerUp,
      onPointerLeave,
      ...(pressing ? { "data-pressing": "true" as const } : {}),
    },
  }
}
