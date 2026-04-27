"use client"

/**
 * AS.7.0 — Floating glass card primitive.
 *
 * Composes the three glass-card concerns from `lib/auth-visual/
 * glass-card-physics.ts` (idle drift / 3D tilt / scroll parallax)
 * with the AS.7.0 motion budget. Pages embed their form fields
 * inside this component:
 *
 *   <AuthGlassCard level={effectiveLevel}>
 *     <form>...</form>
 *   </AuthGlassCard>
 *
 * The card itself is unstyled at the content level — `padding`
 * and `background` come from the `.as7-glass-card` class so a
 * single CSS rule controls every auth page's glass treatment.
 *
 * Three motion-budget short-circuits the leaf handles:
 *
 *   - `idleDriftPx === 0` → skip the drift rAF loop
 *   - `tiltMaxDeg === 0` → skip the pointer listener
 *   - both 0 + parallax 0 → render no transform at all
 *
 * The component is a `forwardRef` host so callers can pass refs
 * to focus form fields after a route transition.
 */

import {
  forwardRef,
  useEffect,
  useImperativeHandle,
  useRef,
  type CSSProperties,
  type ReactNode,
} from "react"

import {
  buildGlassCardTransform,
  idleDriftOffsetPx,
  scrollParallaxOffsetPx,
  tiltFromPointer,
  type GlassCardTilt,
} from "@/lib/auth-visual/glass-card-physics"
import { getAuthVisualBudget } from "@/lib/auth-visual/motion-policy"
import type { MotionLevel } from "@/lib/motion-preferences"

interface AuthGlassCardProps {
  level: MotionLevel
  /** Negative fraction, applied to `window.scrollY`. Default -0.15. */
  parallaxFactor?: number
  className?: string
  style?: CSSProperties
  children?: ReactNode
}

export interface AuthGlassCardHandle {
  /** Bring the card into focus — typically called after a route
   *  transition so the first form field gets focus. The leaf
   *  delegates to `element.focus({ preventScroll: true })` on the
   *  outer wrapper; pages can also pass their own form ref to
   *  the child if they need finer control. */
  focus: () => void
}

export const AuthGlassCard = forwardRef<AuthGlassCardHandle, AuthGlassCardProps>(
  function AuthGlassCard(
    { level, parallaxFactor = -0.15, className, style, children },
    ref,
  ) {
    const wrapperRef = useRef<HTMLDivElement | null>(null)
    const rafRef = useRef<number | null>(null)
    const tiltRef = useRef<GlassCardTilt>({
      rotateXDeg: 0,
      rotateYDeg: 0,
      translateZPx: 0,
    })
    const scrollYRef = useRef<number>(0)

    const budget = getAuthVisualBudget(level)
    const idleDriftPx = budget.idleDriftPx
    const tiltMaxDeg = budget.tiltMaxDeg
    const flicker = budget.glowFlicker ? "on" : "off"

    useImperativeHandle(
      ref,
      () => ({
        focus: () => wrapperRef.current?.focus({ preventScroll: true }),
      }),
      [],
    )

    // ── drift + parallax rAF loop ─────────────────────────────────────
    useEffect(() => {
      const el = wrapperRef.current
      if (!el) return
      // Skip the loop entirely if every contributing source is 0.
      const driftOn = idleDriftPx > 0
      const parallaxOn = parallaxFactor !== 0
      const tiltOn = tiltMaxDeg > 0
      if (!driftOn && !parallaxOn && !tiltOn) {
        el.style.transform = ""
        return
      }

      const tick = (nowMs: number) => {
        const drift = driftOn ? idleDriftOffsetPx(nowMs, idleDriftPx) : 0
        const parallax = parallaxOn
          ? scrollParallaxOffsetPx(scrollYRef.current, parallaxFactor)
          : 0
        el.style.transform = buildGlassCardTransform({
          driftPx: drift,
          parallaxPx: parallax,
          tilt: tiltRef.current,
        })
        rafRef.current = window.requestAnimationFrame(tick)
      }
      rafRef.current = window.requestAnimationFrame(tick)

      const onScroll = () => {
        scrollYRef.current = window.scrollY
      }
      window.addEventListener("scroll", onScroll, { passive: true })

      return () => {
        if (rafRef.current !== null) {
          window.cancelAnimationFrame(rafRef.current)
          rafRef.current = null
        }
        window.removeEventListener("scroll", onScroll)
      }
    }, [idleDriftPx, parallaxFactor, tiltMaxDeg])

    // ── 3D tilt pointer listener ──────────────────────────────────────
    useEffect(() => {
      const el = wrapperRef.current
      if (!el || tiltMaxDeg <= 0) {
        tiltRef.current = { rotateXDeg: 0, rotateYDeg: 0, translateZPx: 0 }
        return
      }
      const handleMove = (event: PointerEvent) => {
        const rect = el.getBoundingClientRect()
        if (rect.width === 0 || rect.height === 0) return
        const x = (event.clientX - rect.left) / rect.width
        const y = (event.clientY - rect.top) / rect.height
        tiltRef.current = tiltFromPointer(x, y, tiltMaxDeg)
      }
      const handleLeave = () => {
        tiltRef.current = { rotateXDeg: 0, rotateYDeg: 0, translateZPx: 0 }
      }
      el.addEventListener("pointermove", handleMove)
      el.addEventListener("pointerleave", handleLeave)
      return () => {
        el.removeEventListener("pointermove", handleMove)
        el.removeEventListener("pointerleave", handleLeave)
      }
    }, [tiltMaxDeg])

    return (
      <div
        ref={wrapperRef}
        tabIndex={-1}
        data-testid="as7-glass-card"
        data-as7-flicker={flicker}
        className={["as7-glass-card", className].filter(Boolean).join(" ")}
        style={style}
      >
        {children}
      </div>
    )
  },
)
