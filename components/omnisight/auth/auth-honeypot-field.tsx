"use client"

/**
 * AS.7.1 — Hidden honeypot input rendered into the login form.
 *
 * Resolves the rotating field name on mount via Web Crypto and
 * renders a single off-screen <input> matching the AS.4.1 5-attr
 * spec the backend `validate_honeypot` enforces. Once the name
 * resolves the parent reads it via `onResolved` so the form-submit
 * payload can include the right key.
 *
 * Important behaviours:
 *   - Renders an empty placeholder until the SHA-256 digest finishes
 *     (single microtask in practice). The submit button is gated
 *     on the resolved name via the `onResolved` callback so a
 *     pre-resolution submit can't fire a missing-field 429.
 *   - Off-screen positioning (NOT `display:none` / `visibility:
 *     hidden` — Selenium / Playwright headless skip those, defeating
 *     the trap per AS.0.7 §2.2).
 *
 * Module-global state audit: leaf React state only (`useState` for
 * the resolved name + abort flag). No module-level mutable container.
 *
 * Read-after-write timing audit: the resolver is a single async
 * microtask; cancellation flag prevents stale setState after the
 * component unmounts mid-resolution.
 */

import { useEffect, useState } from "react"

import {
  HONEYPOT_INPUT_ATTRS,
  OS_HONEYPOT_CLASS,
  loginHoneypotFieldName,
} from "@/lib/auth/login-form-helpers"

interface AuthHoneypotFieldProps {
  /** Called once the rotating field name has been resolved so the
   *  parent can include the right key in the submit payload. The
   *  parent is responsible for refusing submit until it sees a
   *  resolved (truthy) name. */
  onResolved?: (fieldName: string) => void
  /** Test escape hatch. Forces the field name without going through
   *  Web Crypto so unit tests can render the field deterministically. */
  forceFieldName?: string
}

export function AuthHoneypotField({
  onResolved,
  forceFieldName,
}: AuthHoneypotFieldProps) {
  const [fieldName, setFieldName] = useState<string | null>(
    forceFieldName ?? null,
  )

  useEffect(() => {
    if (forceFieldName) {
      setFieldName(forceFieldName)
      onResolved?.(forceFieldName)
      return
    }
    let cancelled = false
    void loginHoneypotFieldName()
      .then((name) => {
        if (cancelled) return
        setFieldName(name)
        onResolved?.(name)
      })
      .catch(() => {
        // Web Crypto unavailable. Keep field unrendered — the
        // backend will reject the submit with form_drift, but we
        // surface no other behaviour here. Production environments
        // running modern browsers / Node 16+ never hit this path.
      })
    return () => {
      cancelled = true
    }
    // We deliberately depend ONLY on the override prop. Re-running
    // the resolver on every parent re-render would chum CPU.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [forceFieldName])

  if (!fieldName) {
    return (
      <input
        type="hidden"
        name=""
        data-testid="as7-honeypot-field"
        data-as7-honeypot="pending"
        aria-hidden="true"
      />
    )
  }

  // Spread the canonical 5-attr set + render the off-screen hide
  // style inline so the field works even when the AS.7.1 stylesheet
  // is not yet loaded.
  return (
    <input
      type="text"
      name={fieldName}
      defaultValue=""
      data-testid="as7-honeypot-field"
      data-as7-honeypot="ready"
      className={OS_HONEYPOT_CLASS}
      style={{
        position: "absolute",
        left: "-9999px",
        top: "auto",
        width: "1px",
        height: "1px",
        overflow: "hidden",
      }}
      tabIndex={Number(HONEYPOT_INPUT_ATTRS.tabindex)}
      autoComplete={HONEYPOT_INPUT_ATTRS.autocomplete}
      data-1p-ignore={HONEYPOT_INPUT_ATTRS["data-1p-ignore"]}
      data-lpignore={HONEYPOT_INPUT_ATTRS["data-lpignore"]}
      data-bwignore={HONEYPOT_INPUT_ATTRS["data-bwignore"]}
      aria-hidden={HONEYPOT_INPUT_ATTRS["aria-hidden"] === "true"}
      aria-label={HONEYPOT_INPUT_ATTRS["aria-label"]}
    />
  )
}
