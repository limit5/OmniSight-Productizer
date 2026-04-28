"use client"

/**
 * AS.7.4 — MFA method picker (segmented tabs).
 *
 * Renders a horizontal pill of segments — one per available MFA
 * method — for the user to switch between TOTP / WebAuthn / backup
 * code. Active segment glows with the brand-purple halo so the eye
 * is drawn there.
 *
 * Keyboard navigation: Arrow Left / Arrow Right wrap-cycle through
 * the available methods. Same semantics as the AS.7.2
 * `<PasswordStyleToggle>` so muscle memory carries over.
 *
 * Module-global state audit: pure presentation. Active state lives
 * in the parent.
 */

import { useRef } from "react"

import {
  MFA_METHOD_COPY,
  type MfaMethodKind,
} from "@/lib/auth/mfa-challenge-helpers"

interface MfaMethodTabsProps {
  /** Methods the page is offering. Order is honoured. */
  methods: readonly MfaMethodKind[]
  value: MfaMethodKind
  onChange: (next: MfaMethodKind) => void
  /** Optional disable state. When true the tabs are still visible
   *  but not interactive (used during the passed-check overlay). */
  disabled?: boolean
}

export function MfaMethodTabs({
  methods,
  value,
  onChange,
  disabled,
}: MfaMethodTabsProps) {
  const buttonsRef = useRef<HTMLButtonElement[]>([])

  const onKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (disabled) return
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return
    if (methods.length <= 1) return
    e.preventDefault()
    const idx = methods.indexOf(value)
    const delta = e.key === "ArrowRight" ? 1 : -1
    const nextIdx = (idx + delta + methods.length) % methods.length
    const nextKind = methods[nextIdx]
    onChange(nextKind)
    const btn = buttonsRef.current[nextIdx]
    btn?.focus()
  }

  return (
    <div
      role="tablist"
      aria-label="Two-factor method"
      data-testid="as7-mfa-method-tabs"
      onKeyDown={onKeyDown}
      className="as7-mfa-tabs flex items-center gap-1 rounded border border-[var(--border)] bg-[var(--background)]/30 p-1"
    >
      {methods.map((kind, i) => {
        const active = kind === value
        const copy = MFA_METHOD_COPY[kind]
        return (
          <button
            key={kind}
            ref={(el) => {
              if (el) buttonsRef.current[i] = el
            }}
            type="button"
            role="tab"
            aria-selected={active}
            aria-controls={`as7-mfa-panel-${kind}`}
            data-testid={`as7-mfa-tab-${kind}`}
            data-as7-tab-active={active ? "yes" : "no"}
            disabled={disabled}
            onClick={() => onChange(kind)}
            className={[
              "as7-mfa-tab",
              "flex-1 px-2 py-1.5 rounded font-mono text-[10px] tracking-wider transition",
              active
                ? "bg-[var(--artifact-purple)]/15 text-[var(--foreground)] shadow-[0_0_10px_rgba(168,85,247,0.4)]"
                : "text-[var(--muted-foreground)] hover:text-[var(--foreground)]",
              disabled ? "opacity-50 cursor-not-allowed" : "",
            ]
              .filter(Boolean)
              .join(" ")}
          >
            {copy.label.toUpperCase()}
          </button>
        )
      })}
    </div>
  )
}
