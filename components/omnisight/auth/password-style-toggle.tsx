"use client"

/**
 * AS.7.2 — Password style toggle (Random / Diceware / Pronounceable).
 *
 * A 3-segment toggle that drives which style the auto-generator
 * uses on the next 🎲 click. Toggling the active segment also
 * fires `onChange` with the new style so the parent can re-roll
 * a fresh password instantly.
 *
 * Visual: pill-shaped 3-segment selector. Active segment glows
 * with the artifact-purple brand color; inactive segments are
 * muted-foreground hover-able. Keyboard: arrow keys cycle, Space
 * / Enter activate (radio-group semantics).
 *
 * Module-global state audit: leaf React component. No module-level
 * state. Per-tab determinism is trivially identical (Answer #1 of
 * the SOP §1 audit).
 */

import { type KeyboardEvent } from "react"

import type { PasswordStyle } from "@/templates/_shared/password-generator"

interface StyleOption {
  readonly id: PasswordStyle
  readonly label: string
  readonly hint: string
}

/** Order pinned by tests — left-to-right rendering order. */
export const PASSWORD_STYLE_OPTIONS: readonly StyleOption[] = Object.freeze([
  {
    id: "random",
    label: "Random",
    hint: "20-char alphanumeric + symbols",
  },
  {
    id: "diceware",
    label: "Memorable",
    hint: "4 dictionary words",
  },
  {
    id: "pronounceable",
    label: "Pronounceable",
    hint: "syllable-based",
  },
])

interface PasswordStyleToggleProps {
  value: PasswordStyle
  onChange: (style: PasswordStyle) => void
  className?: string
}

export function PasswordStyleToggle({
  value,
  onChange,
  className,
}: PasswordStyleToggleProps) {
  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    const idx = PASSWORD_STYLE_OPTIONS.findIndex((o) => o.id === value)
    if (idx < 0) return
    if (e.key === "ArrowRight" || e.key === "ArrowDown") {
      e.preventDefault()
      const next = PASSWORD_STYLE_OPTIONS[(idx + 1) % PASSWORD_STYLE_OPTIONS.length]
      onChange(next.id)
    } else if (e.key === "ArrowLeft" || e.key === "ArrowUp") {
      e.preventDefault()
      const prev =
        PASSWORD_STYLE_OPTIONS[
          (idx - 1 + PASSWORD_STYLE_OPTIONS.length) % PASSWORD_STYLE_OPTIONS.length
        ]
      onChange(prev.id)
    }
  }

  return (
    <div
      role="radiogroup"
      aria-label="Password style"
      data-testid="as7-password-style-toggle"
      onKeyDown={onKeyDown}
      className={[
        "as7-style-toggle",
        "flex items-center gap-1 rounded-full border border-[var(--border)] bg-[var(--background)]/40 p-1",
        className,
      ]
        .filter(Boolean)
        .join(" ")}
    >
      {PASSWORD_STYLE_OPTIONS.map((opt) => {
        const active = opt.id === value
        return (
          <button
            key={opt.id}
            type="button"
            role="radio"
            aria-checked={active}
            data-testid={`as7-style-${opt.id}`}
            data-as7-style-active={active ? "yes" : "no"}
            onClick={() => onChange(opt.id)}
            className={[
              "as7-style-segment",
              "px-3 py-1 rounded-full font-mono text-[11px] tracking-wider transition-colors",
              active
                ? "bg-[var(--artifact-purple)] text-white shadow-[0_0_12px_rgba(168,85,247,0.45)]"
                : "text-[var(--muted-foreground)] hover:text-[var(--foreground)]",
            ]
              .filter(Boolean)
              .join(" ")}
            title={opt.hint}
          >
            {opt.label}
          </button>
        )
      })}
    </div>
  )
}
