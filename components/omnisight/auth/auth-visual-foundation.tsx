"use client"

/**
 * AS.7.0 — Composed Auth Visual Foundation scaffold.
 *
 * The page-level wrapper AS.7.1..AS.7.8 each use:
 *
 *   <AuthVisualFoundation>
 *     <AuthGlassCard level={level}>
 *       ... page-specific form ...
 *     </AuthGlassCard>
 *   </AuthVisualFoundation>
 *
 * Owns:
 *
 *   - Resolving the effective motion level via BS.3.5
 *     `useEffectiveMotionLevel` (hooks compose `prefers-reduced-
 *     motion` + battery rule + user pref into a single value).
 *   - Surfacing that level as `data-motion-level` on the root so
 *     `styles/auth-visual.css` can gate every CSS-side animation
 *     from one attribute.
 *   - Mounting `<AuthNebulaBackground>` lazily based on the
 *     budget's `renderShader` flag.
 *   - Optional override prop (`forceLevel`) for SSR / Storybook
 *     / unit-test paths that need a deterministic level.
 *
 * Importantly, the CSS file `styles/auth-visual.css` is imported
 * here (and only here). Pages embed the foundation; the browser
 * sees the stylesheet exactly once regardless of how many auth
 * sub-routes nest the foundation in their layout.
 */

import { useMemo, type ReactNode } from "react"

import { useEffectiveMotionLevel } from "@/hooks/use-effective-motion-level"
import { getAuthVisualBudget } from "@/lib/auth-visual/motion-policy"
import type { MotionLevel } from "@/lib/motion-preferences"

import { AuthNebulaBackground } from "./auth-nebula-background"

import "@/styles/auth-visual.css"

interface AuthVisualFoundationProps {
  /** Test / storybook escape hatch. When supplied, bypasses the
   *  BS.3.5 resolver and uses this level outright. */
  forceLevel?: MotionLevel
  className?: string
  children?: ReactNode
}

export function AuthVisualFoundation({
  forceLevel,
  className,
  children,
}: AuthVisualFoundationProps) {
  // Always call the hook so React's rules-of-hooks invariants hold
  // across renders, even when a `forceLevel` is passed.
  const resolved = useEffectiveMotionLevel()
  const level: MotionLevel = forceLevel ?? resolved

  const budget = useMemo(() => getAuthVisualBudget(level), [level])

  return (
    <div
      data-testid="as7-root"
      data-motion-level={level}
      data-as7-render-shader={budget.renderShader ? "on" : "off"}
      className={["as7-root", className].filter(Boolean).join(" ")}
    >
      <AuthNebulaBackground level={level} />
      <div className="as7-content">{children}</div>
    </div>
  )
}
