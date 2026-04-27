"use client"

/**
 * AS.7.1 — Cloudflare Turnstile widget loader.
 *
 * Mounts the official Turnstile widget when `NEXT_PUBLIC_TURNSTILE_SITE_KEY`
 * is set; gracefully renders nothing when the site key is missing
 * so dev / test / unconfigured environments still work (the
 * backend AS.6.3 verify is fail-open in Phase 1, so a missing
 * widget is allowed; Phase 3 will require the token, but rolling
 * the env knob is the operator's signal to add the site key).
 *
 * The widget loads its script lazily on the first render — once
 * loaded, `window.turnstile.render(container, {...})` mounts the
 * iframe. The component cleans up on unmount via
 * `window.turnstile.remove(widgetId)`.
 *
 * `onToken(token)` fires every time Turnstile issues a fresh token
 * (initial solve + every subsequent refresh). The login page wires
 * the token into the request body field
 * `turnstile_token` per AS.6.3 backend contract.
 *
 * Module-global state audit:
 *   - The `<script>` tag is added to `document.head` once. A second
 *     render skips re-adding. No mutable module container — we use
 *     a `data-as7-turnstile="loaded"` attribute on the script tag
 *     itself as the dedupe sentinel so two component instances on
 *     the same page don't double-load the script.
 *   - Per-instance widget id lives in a `useRef` (per-component
 *     state). Cleanup runs on unmount.
 *
 * Read-after-write timing audit: N/A — single-process browser API.
 */

import { useEffect, useRef, useState } from "react"

const TURNSTILE_SCRIPT_URL =
  "https://challenges.cloudflare.com/turnstile/v0/api.js"
const TURNSTILE_SCRIPT_DEDUPE_ATTR = "data-as7-turnstile-loaded"
const TURNSTILE_GLOBAL_CALLBACK = "__as7TurnstileReady"

interface TurnstileGlobal {
  render: (
    container: HTMLElement,
    opts: {
      sitekey: string
      callback?: (token: string) => void
      "expired-callback"?: () => void
      "error-callback"?: () => void
      theme?: "auto" | "light" | "dark"
      size?: "normal" | "flexible" | "compact"
      action?: string
      appearance?: "always" | "execute" | "interaction-only"
    },
  ) => string
  remove: (widgetId: string) => void
  reset: (widgetId?: string) => void
}

interface TurnstileWindow extends Window {
  turnstile?: TurnstileGlobal
  [TURNSTILE_GLOBAL_CALLBACK]?: () => void
}

interface AuthTurnstileWidgetProps {
  siteKey?: string | null
  onToken: (token: string) => void
  onExpired?: () => void
  onError?: () => void
  /** Forwarded to the Turnstile `action` parameter for the AS.6.3
   *  per-form-action audit dimension. Default: `"login"`. */
  action?: string
  /** Visual theme. The login page is on the dark nebula so we
   *  default to "dark" — Turnstile's "auto" picks based on system
   *  prefs, which mismatches the AS.7.0 always-dark canvas. */
  theme?: "auto" | "light" | "dark"
}

export function AuthTurnstileWidget({
  siteKey,
  onToken,
  onExpired,
  onError,
  action = "login",
  theme = "dark",
}: AuthTurnstileWidgetProps) {
  const containerRef = useRef<HTMLDivElement | null>(null)
  const widgetIdRef = useRef<string | null>(null)
  const [scriptReady, setScriptReady] = useState<boolean>(false)

  // ── Lazily inject the Turnstile script ──
  useEffect(() => {
    if (!siteKey) return
    if (typeof document === "undefined") return

    const w = window as TurnstileWindow
    if (w.turnstile) {
      setScriptReady(true)
      return
    }

    const existing = document.querySelector(
      `script[${TURNSTILE_SCRIPT_DEDUPE_ATTR}]`,
    )
    const onReady = () => setScriptReady(true)

    if (existing) {
      // Another widget already injected the script. Subscribe via
      // the shared global callback so we know when it loads.
      if (w.turnstile) {
        setScriptReady(true)
      } else {
        const prev = w[TURNSTILE_GLOBAL_CALLBACK]
        w[TURNSTILE_GLOBAL_CALLBACK] = () => {
          prev?.()
          onReady()
        }
      }
      return
    }

    w[TURNSTILE_GLOBAL_CALLBACK] = onReady
    const script = document.createElement("script")
    script.src = `${TURNSTILE_SCRIPT_URL}?onload=${TURNSTILE_GLOBAL_CALLBACK}`
    script.async = true
    script.defer = true
    script.setAttribute(TURNSTILE_SCRIPT_DEDUPE_ATTR, "true")
    document.head.appendChild(script)
  }, [siteKey])

  // ── Render the widget once the script is ready ──
  useEffect(() => {
    if (!siteKey) return
    if (!scriptReady) return
    if (!containerRef.current) return
    const w = window as TurnstileWindow
    if (!w.turnstile) return

    const id = w.turnstile.render(containerRef.current, {
      sitekey: siteKey,
      action,
      theme,
      callback: (token) => onToken(token),
      "expired-callback": () => onExpired?.(),
      "error-callback": () => onError?.(),
    })
    widgetIdRef.current = id

    return () => {
      const wn = window as TurnstileWindow
      if (wn.turnstile && widgetIdRef.current) {
        try {
          wn.turnstile.remove(widgetIdRef.current)
        } catch {
          // best-effort
        }
        widgetIdRef.current = null
      }
    }
  }, [siteKey, scriptReady, onToken, onExpired, onError, action, theme])

  if (!siteKey) {
    return (
      <span
        data-testid="as7-turnstile-widget"
        data-as7-turnstile="disabled"
        className="as7-turnstile-disabled"
      />
    )
  }

  return (
    <div
      ref={containerRef}
      data-testid="as7-turnstile-widget"
      data-as7-turnstile={scriptReady ? "ready" : "loading"}
      className="as7-turnstile-widget"
    />
  )
}
