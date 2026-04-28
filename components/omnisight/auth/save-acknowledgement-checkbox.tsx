"use client"

/**
 * AS.7.2 — "I have saved my password" acknowledgement checkbox.
 *
 * The signup design requires the user to explicitly confirm they
 * stored the auto-generated (or self-typed) password somewhere
 * before the submit button enables. This is a deliberate friction
 * point: an auto-gen password the user just glanced at is useless
 * if they can't recover it.
 *
 * The leaf is intentionally dumb — visual + a11y only. The parent
 * owns the `checked` state and the submit gate composition (see
 * `signupSubmitBlockedReason()`).
 *
 * Module-global state audit: leaf React component. No state. No
 * module-level mutable container. Per-tab determinism trivial.
 */

import { type ChangeEvent, type ReactNode } from "react"

interface SaveAcknowledgementCheckboxProps {
  checked: boolean
  onChange: (checked: boolean) => void
  children?: ReactNode
  disabled?: boolean
}

export function SaveAcknowledgementCheckbox({
  checked,
  onChange,
  children,
  disabled = false,
}: SaveAcknowledgementCheckboxProps) {
  const onInputChange = (e: ChangeEvent<HTMLInputElement>) => {
    onChange(e.target.checked)
  }

  return (
    <label
      data-testid="as7-save-ack-checkbox"
      data-as7-save-ack-checked={checked ? "yes" : "no"}
      className={[
        "as7-save-ack flex items-start gap-2 cursor-pointer select-none",
        "p-2 rounded border font-mono text-[11px] leading-relaxed transition-colors",
        checked
          ? "border-[var(--artifact-purple)] bg-[var(--artifact-purple)]/10 text-[var(--foreground)]"
          : "border-[var(--border)] text-[var(--muted-foreground)] hover:text-[var(--foreground)]",
        disabled ? "opacity-50 cursor-not-allowed" : "",
      ]
        .filter(Boolean)
        .join(" ")}
    >
      <input
        type="checkbox"
        checked={checked}
        onChange={onInputChange}
        disabled={disabled}
        data-testid="as7-save-ack-input"
        className="mt-0.5 accent-[var(--artifact-purple)]"
        aria-describedby="as7-save-ack-hint"
      />
      <span id="as7-save-ack-hint" className="flex-1">
        {children ??
          "I have saved this password to a secure location (password manager, browser keychain, or encrypted note)."}
      </span>
    </label>
  )
}
