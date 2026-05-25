"use client"

/**
 * AS.7.1 — Cloudflare Turnstile widget loader.
 *
 * OP-1726: the site key is now RUNTIME-driven. When no `siteKey` prop is
 * passed, the widget fetches `GET /auth/bot-challenge-config` on mount
 * and mounts the official Turnstile widget only when the backend serves a
 * key (prod ON / internal staging OFF — one release image, no build-baked
 * `NEXT_PUBLIC_TURNSTILE_SITE_KEY`). It gracefully renders nothing when
 * the runtime config has no key so dev / test / unconfigured environments
 * still work (the backend AS.6.3 verify is fail-open in Phase 1, so a
 * missing widget is allowed). Host pages learn whether a token will be
 * required via the `onConfigResolved` callback so they can gate submit.
 *
 * An explicit `siteKey` prop (including `null`) short-circuits the fetch
 * and is used directly — handy for unit tests / storybook isolation.
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

import {
  DEFAULT_BOT_CHALLENGE_CONFIG,
  fetchBotChallengeConfig,
  type BotChallengeConfig,
} from "@/lib/api"

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
  /** Optional override. When provided (including `null`), the widget uses
   *  this site key directly and SKIPS the OP-1726 runtime config fetch —
   *  used by unit tests / storybook. When omitted, the widget fetches the
   *  runtime `/auth/bot-challenge-config`. */
  siteKey?: string | null
  onToken: (token: string) => void
  onExpired?: () => void
  onError?: () => void
  /** OP-1726 — fires once the runtime config resolves (and on the seed
   *  default before the fetch lands) so the host page knows whether a
   *  token will be required on submit. */
  onConfigResolved?: (config: BotChallengeConfig) => void
  /** Forwarded to the Turnstile `action` parameter for the AS.6.3
   *  per-form-action audit dimension. Default: `"login"`. */
  action?: string
  /** Visual theme. The login page is on the dark nebula so we
   *  default to "dark" — Turnstile's "auto" picks based on system
   *  prefs, which mismatches the AS.7.0 always-dark canvas. */
  theme?: "auto" | "light" | "dark"
}

export function AuthTurnstileWidget({
  siteKey: siteKeyOverride,
  onToken,
  onExpired,
  onError,
  onConfigResolved,
  action = "login",
  theme = "dark",
}: AuthTurnstileWidgetProps) {
  const containerRef = useRef<HTMLDivElement | null>(null)
  const widgetIdRef = useRef<string | null>(null)
  const [scriptReady, setScriptReady] = useState<boolean>(false)

  // OP-1726 — runtime config. An explicit `siteKey` prop (override / unit
  // tests) skips the fetch entirely; otherwise we fetch the backend
  // `/auth/bot-challenge-config` once on mount.
  const hasOverride = siteKeyOverride !== undefined
  const [fetchedConfig, setFetchedConfig] = useState<BotChallengeConfig>(
    DEFAULT_BOT_CHALLENGE_CONFIG,
  )

  useEffect(() => {
    if (hasOverride) return
    let active = true
    void fetchBotChallengeConfig().then((cfg) => {
      if (active) setFetchedConfig(cfg)
    })
    return () => {
      active = false
    }
  }, [hasOverride])

  // The effective key: the override when supplied, else the runtime key
  // (only when the backend reported enabled — a key with enabled=false is
  // already folded out in normalizeBotChallengeConfig, belt-and-braces).
  const siteKey = hasOverride
    ? siteKeyOverride ?? null
    : fetchedConfig.enabled
      ? fetchedConfig.siteKey
      : null

  // Report the resolved config up so the host page can require a token on
  // submit exactly when a widget is mounted.
  useEffect(() => {
    if (!onConfigResolved) return
    onConfigResolved(
      hasOverride
        ? {
            provider: null,
            siteKey: siteKeyOverride ?? null,
            enabled: Boolean(siteKeyOverride),
          }
        : fetchedConfig,
    )
  }, [hasOverride, siteKeyOverride, fetchedConfig, onConfigResolved])

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
