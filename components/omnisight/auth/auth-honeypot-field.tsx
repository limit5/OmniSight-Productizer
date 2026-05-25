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
 * OP-1730 — non-secure-context fallback: `crypto.subtle` is undefined
 * outside a secure context (HTTPS or localhost), so on a plain-HTTP /
 * bare-IP origin (self-hosted customers + internal staging at
 * http://<ip>:port) the Web Crypto derivation throws and the field
 * would never render → backend `field_missing_in_form` →
 * `bot_challenge_failed` → login impossible. When `crypto.subtle` is
 * missing (or its digest throws) we fall back to fetching the
 * server-derived field name from `GET /auth/honeypot-field-config`.
 * The secure-context Web Crypto path stays the default/fast path and
 * is unchanged; the honeypot is not weakened (the field name is not a
 * secret, and the value-must-be-empty bot check is untouched).
 *
 * Important behaviours:
 *   - Renders an empty placeholder until the SHA-256 digest finishes
 *     (single microtask in practice) or the fallback fetch lands. The
 *     submit button is gated on the resolved name via the `onResolved`
 *     callback so a pre-resolution submit can't fire a missing-field 429.
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
  isSubtleCryptoAvailable,
  loginHoneypotFieldName,
} from "@/lib/auth/login-form-helpers"
import { fetchHoneypotFieldConfig } from "@/lib/api"

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

    // OP-1730 — resolve the rotating field name with a secure-context
    // fast path and a non-secure-context fallback:
    //   1. `crypto.subtle` present (HTTPS / localhost / Node) → derive
    //      the name client-side via Web Crypto SHA-256 (the original,
    //      unchanged fast path).
    //   2. `crypto.subtle` absent (plain HTTP / bare IP) OR the digest
    //      throws → fetch the server-derived name from the backend
    //      `/auth/honeypot-field-config` endpoint. The name is not a
    //      secret, so exposing it does not weaken the honeypot.
    const resolveFieldName = async (): Promise<string | null> => {
      if (isSubtleCryptoAvailable()) {
        try {
          return await loginHoneypotFieldName()
        } catch {
          // Subtle present but digest failed — fall through to the
          // backend endpoint rather than leaving the field unrendered.
        }
      }
      const cfg = await fetchHoneypotFieldConfig("login")
      return cfg.fieldName
    }

    void resolveFieldName()
      .then((name) => {
        if (cancelled || !name) return
        setFieldName(name)
        onResolved?.(name)
      })
      .catch(() => {
        // Both the Web Crypto path and the backend fallback failed
        // (e.g. backend unreachable). Keep the field unrendered — the
        // backend will reject the submit with form_drift, but we
        // surface no other behaviour here.
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
