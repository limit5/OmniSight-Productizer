"use client"

/**
 * AS.7.0 — Nebula background canvas.
 *
 * Mounts a `<canvas>` filling its absolute parent and drives the
 * WebGL shader from `lib/auth-visual/nebula-shader.ts`. Three
 * concerns the component owns:
 *
 *   1. Lazy WebGL setup — defer to `useEffect` so SSR never sees a
 *      `WebGLRenderingContext`. If `getContext("webgl")` returns
 *      null (Safari with WebGL off, blocked by extensions) the
 *      component renders nothing and the parent's CSS gradient
 *      shows through.
 *   2. Frame budget — `requestAnimationFrame` cap from
 *      `getAuthVisualBudget(level)`. 60 fps means uncap; 45 / 30
 *      throttles via `performance.now()` deadline checks.
 *   3. Pointer + resize listeners — debounced `pointermove` to
 *      track the cursor in UV space; `ResizeObserver` so the
 *      backing buffer matches the canvas's CSS box.
 *
 * The component is a leaf — no children. Use `AuthVisualFoundation`
 * (composed scaffold) when building an auth page.
 */

import { useEffect, useRef } from "react"

import {
  cleanupNebulaProgram,
  drawNebulaFrame,
  setupNebulaProgram,
  type NebulaProgram,
} from "@/lib/auth-visual/nebula-shader"
import { getAuthVisualBudget } from "@/lib/auth-visual/motion-policy"
import type { MotionLevel } from "@/lib/motion-preferences"

interface AuthNebulaBackgroundProps {
  /** Resolved motion level — typically `useEffectiveMotionLevel()`. */
  level: MotionLevel
  /** Optional class on the canvas — useful for stacking-context tweaks. */
  className?: string
}

export function AuthNebulaBackground({ level, className }: AuthNebulaBackgroundProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const programRef = useRef<NebulaProgram | null>(null)
  const rafRef = useRef<number | null>(null)
  const startMsRef = useRef<number>(0)
  const lastFrameMsRef = useRef<number>(0)
  const mouseUvRef = useRef<{ x: number; y: number }>({ x: 0.5, y: 0.5 })

  const budget = getAuthVisualBudget(level)
  const renderShader = budget.renderShader
  const frameCapFps = budget.frameCapFps
  const starLayers = budget.starLayers
  const gravityStrength = budget.gravityWellStrength

  useEffect(() => {
    if (!renderShader) return
    const canvas = canvasRef.current
    if (!canvas) return
    if (typeof window === "undefined") return

    const gl =
      (canvas.getContext("webgl") as WebGLRenderingContext | null) ||
      (canvas.getContext("experimental-webgl") as WebGLRenderingContext | null)
    if (!gl) return

    const program = setupNebulaProgram(gl)
    if (!program) return
    programRef.current = program
    startMsRef.current = performance.now()
    lastFrameMsRef.current = 0

    const dpr = Math.min(window.devicePixelRatio || 1, 2)
    const resize = () => {
      const rect = canvas.getBoundingClientRect()
      const w = Math.max(1, Math.floor(rect.width * dpr))
      const h = Math.max(1, Math.floor(rect.height * dpr))
      if (canvas.width !== w) canvas.width = w
      if (canvas.height !== h) canvas.height = h
    }
    resize()

    let resizeObserver: ResizeObserver | null = null
    if (typeof ResizeObserver !== "undefined") {
      resizeObserver = new ResizeObserver(resize)
      resizeObserver.observe(canvas)
    }

    const handlePointer = (event: PointerEvent) => {
      const rect = canvas.getBoundingClientRect()
      if (rect.width === 0 || rect.height === 0) return
      const x = (event.clientX - rect.left) / rect.width
      // GLSL gl_FragCoord origin is bottom-left; flip y.
      const y = 1 - (event.clientY - rect.top) / rect.height
      mouseUvRef.current = { x, y }
    }
    window.addEventListener("pointermove", handlePointer, { passive: true })

    const minFrameMs = frameCapFps > 0 ? 1000 / frameCapFps : 0
    const tick = (nowMs: number) => {
      const last = lastFrameMsRef.current
      if (minFrameMs > 0 && last > 0 && nowMs - last < minFrameMs - 1) {
        rafRef.current = window.requestAnimationFrame(tick)
        return
      }
      lastFrameMsRef.current = nowMs
      const elapsed = (nowMs - startMsRef.current) / 1000
      drawNebulaFrame(program, {
        timeSeconds: elapsed,
        resolutionPx: { width: canvas.width, height: canvas.height },
        mouseUv: mouseUvRef.current,
        starLayers,
        gravityStrength,
      })
      rafRef.current = window.requestAnimationFrame(tick)
    }
    rafRef.current = window.requestAnimationFrame(tick)

    return () => {
      if (rafRef.current !== null) {
        window.cancelAnimationFrame(rafRef.current)
        rafRef.current = null
      }
      window.removeEventListener("pointermove", handlePointer)
      resizeObserver?.disconnect()
      if (programRef.current) {
        cleanupNebulaProgram(programRef.current)
        programRef.current = null
      }
    }
  }, [renderShader, frameCapFps, starLayers, gravityStrength])

  if (!renderShader) return null

  return (
    <canvas
      ref={canvasRef}
      data-testid="as7-nebula-canvas"
      className={["as7-canvas", className].filter(Boolean).join(" ")}
      aria-hidden="true"
    />
  )
}
