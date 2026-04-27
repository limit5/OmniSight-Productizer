"use client"

/**
 * AS.7.1 — Field 通電 input primitive.
 *
 * An <input> wrapped with the four "electric field" effects the
 * AS.7.1 spec calls for:
 *
 *   - Focus 4 corner brackets snap (4 absolutely-positioned ::before
 *     / ::after / span markers — sized 8px in each corner; CSS-only
 *     opacity + transform on focus).
 *   - Border gradient (animated linear-gradient sweeping the field
 *     border on focus).
 *   - Scan-line overlay (1px-tall gradient swept top → bottom on
 *     focus, single 600ms one-shot via CSS animation).
 *   - Spring-shake + lightning flicker on error (driven by an
 *     `errorKey` prop the parent bumps; CSS keyframe replays via
 *     `key={errorKey}` on the wrapper).
 *
 * The input itself is uncontrolled-ready: a `value` + `onChange`
 * pair forwards into a normal text-input, and the wrapper exposes
 * an `id` association via `htmlFor` on the <label>.
 *
 * Module-global state audit (per implement_phase_step.md SOP §1):
 *   - No module-level mutable state. All state is in React props /
 *     `useId`. The four electric effects are pure CSS keyframes
 *     gated by `data-as7-...` attributes.
 *
 * Read-after-write timing audit: N/A — no async work.
 */

import { type CSSProperties, type ReactNode, useId } from "react"

import { getAuthVisualBudget } from "@/lib/auth-visual/motion-policy"
import type { MotionLevel } from "@/lib/motion-preferences"

interface AuthFieldElectricProps {
  level: MotionLevel
  label: ReactNode
  /** ARIA-attached error string. When set, the field renders the
   *  red lightning border + bumps `errorKey` schedule the shake. */
  hasError?: boolean
  /** Replay-key for the spring-shake animation. Bumping this
   *  number re-mounts the keyframe via React `key`. */
  errorKey?: number
  /** Optional decorative leading icon (lucide icon, etc.). */
  leadingIcon?: ReactNode
  /** Optional trailing slot for show-password buttons / spinners. */
  trailingSlot?: ReactNode
  /** All standard input props are forwarded to the underlying
   *  `<input>`. */
  inputProps: Omit<
    React.InputHTMLAttributes<HTMLInputElement>,
    "id" | "className"
  > & {
    name: string
    type?: string
  }
  /** Additional className applied to the outer wrapper. */
  className?: string
  style?: CSSProperties
}

export function AuthFieldElectric({
  level,
  label,
  hasError = false,
  errorKey = 0,
  leadingIcon,
  trailingSlot,
  inputProps,
  className,
  style,
}: AuthFieldElectricProps) {
  const reactId = useId()
  const inputId = `${reactId}-${inputProps.name}`
  const budget = getAuthVisualBudget(level)
  // The "通電" effects (border gradient sweep, scan line, corner
  // bracket snap) are gated to motion levels normal/dramatic. At
  // off/subtle the field is a static styled input — the focus ring
  // alone communicates focus, no animation.
  const electricOn = budget.travelingLight ? "on" : "off"
  const errorOn = hasError ? "on" : "off"

  return (
    <label
      htmlFor={inputId}
      data-testid={`as7-field-${inputProps.name}`}
      data-as7-electric={electricOn}
      data-as7-error={errorOn}
      key={hasError ? `err-${errorKey}` : "ok"}
      className={["as7-field", className].filter(Boolean).join(" ")}
      style={style}
    >
      <span className="as7-field-label">{label}</span>
      <span className="as7-field-shell">
        <span className="as7-field-corner as7-field-corner-tl" aria-hidden="true" />
        <span className="as7-field-corner as7-field-corner-tr" aria-hidden="true" />
        <span className="as7-field-corner as7-field-corner-bl" aria-hidden="true" />
        <span className="as7-field-corner as7-field-corner-br" aria-hidden="true" />
        <span className="as7-field-scan" aria-hidden="true" />
        {leadingIcon ? (
          <span className="as7-field-leading" aria-hidden="true">{leadingIcon}</span>
        ) : null}
        <input
          id={inputId}
          className="as7-field-input"
          {...inputProps}
        />
        {trailingSlot ? (
          <span className="as7-field-trailing">{trailingSlot}</span>
        ) : null}
      </span>
    </label>
  )
}
