"use client"

/**
 * W14.6 — Live Web Sandbox Preview Panel.
 *
 * Renders an `<iframe>` wrapper around the per-workspace Vite/Bun/Nuxt
 * sidecar launched by the W14.2 backend (`POST /web-sandbox/preview`).
 * Owns the operator-facing controls the row spec promised:
 *
 *   • 連線指示燈 — colored status LED that mirrors
 *     `WebSandboxInstance.status`. The same six lifecycle states the
 *     backend manager exposes (`pending` / `installing` / `running` /
 *     `stopping` / `stopped` / `failed`).
 *   • Reload — bumps the iframe `src` query string so the inner page
 *     reloads even when its server-side cache would otherwise serve a
 *     200 from memory. Local UI action; no backend round-trip.
 *   • External — opens the live preview URL in a new tab.
 *   • Kill — `DELETE /web-sandbox/preview/{workspace_id}?reason=...`
 *     and surfaces the resulting `stopped` snapshot.
 *   • Viewport simulation — Auto / Mobile (375×667) / Tablet (768×1024)
 *     / Desktop (1280×800). Pure CSS width/height swap on the iframe;
 *     the dev server itself never knows the choice changed.
 *
 * The panel is purely presentational over the W14.2 launcher contract
 * — it does NOT itself hold a Docker daemon, only a small ring of
 * fetch helpers from `lib/api.ts`. This keeps the surface trivially
 * unit-testable: every test mocks `@/lib/api` and asserts on the
 * resulting DOM/network calls.
 *
 * ─── Caller contract ─────────────────────────────────────────────────
 *
 * ```tsx
 * <LivePreviewPanel
 *   workspaceId="ws-abc123"
 *   workspacePath="/workspaces/ws-abc123"   // optional override
 *   onClosed={() => setOpen(false)}        // optional teardown hook
 * />
 * ```
 *
 * On mount the panel calls `getWebSandbox(workspaceId)`:
 *
 *   • 200 → reflect the existing instance (resume an already-launched
 *     sandbox; the operator may have left the page open during a
 *     `pnpm install` round-trip).
 *   • 404 → render a "Launch preview" CTA; clicking it calls
 *     `launchWebSandbox()` which kicks off the W14.2 cold-launch
 *     flow (docker run + pnpm install + dev server).
 *   • any other ApiError → surface in an inline banner with a RETRY
 *     button (matches `BudgetStrategyPanel` / `ModeSelector` UX).
 *
 * The iframe URL prefers `ingress_url` (CF Tunnel + Access SSO when
 * W14.3 + W14.4 are both wired) and falls back to the host-port
 * `preview_url` so the panel keeps working in pure-W14.2 dev rigs.
 *
 * ─── Idle-reaper interplay (W14.5) ────────────────────────────────────
 *
 * The panel pings `POST /touch` every 5 minutes while mounted with a
 * `running` sandbox. The W14.5 reaper kills any sandbox that goes
 * 30 min without a touch / launch / ready bump; the touch loop keeps
 * an actively-watched preview alive without reaching for a manual
 * "keep awake" button. When the panel unmounts (operator navigated
 * away) the touch loop stops, and the reaper collects the sandbox
 * naturally on its next sweep — that is the "watching me ⇒ alive,
 * forgotten ⇒ collected" UX the row owes.
 *
 * ─── Module-global state audit (SOP §1) ──────────────────────────────
 *
 * No module-level mutable state. Every interactive bit lives in
 * component-local `useState` / `useRef` / `useEffect`. Each test
 * renders a fresh panel; nothing persists across renders or tests.
 *
 * ─── Read-after-write timing audit (SOP §2) ──────────────────────────
 *
 * N/A — pure React + REST helpers. No SQL, no asyncio.gather, no
 * compat→pool migration. The `touch` loop and the user-driven
 * mutations (launch / stop) are independent timers; if a user clicks
 * "Kill" mid-touch the kill runs to completion and the next touch
 * sees `WebSandboxNotFound` → the catch swallows + clears local state.
 */

import { useCallback, useEffect, useRef, useState } from "react"
import {
  AlertTriangle,
  ExternalLink,
  Loader2,
  Monitor,
  Power,
  RefreshCw,
  Smartphone,
  Square,
  Tablet,
} from "lucide-react"

import {
  ApiError,
  getWebSandbox,
  launchWebSandbox,
  stopWebSandbox,
  touchWebSandbox,
  type WebSandboxInstanceWire,
  type WebSandboxStatus,
} from "@/lib/api"

// Touch interval: W14.5 reaper defaults to 30 min idle timeout; pinging
// every 5 min keeps the sandbox alive with comfortable headroom even if
// a single touch fails (stale CF token, transient 5xx).
export const TOUCH_INTERVAL_MS = 5 * 60 * 1000

export type ViewportMode = "auto" | "mobile" | "tablet" | "desktop"

interface ViewportSpec {
  label: string
  shortLabel: string
  // Pixel dimensions for the iframe. `null` width ⇒ stretch to container
  // (the "auto" / responsive option). `height` is the iframe's CSS height
  // — null means 100% of the panel's flex space.
  width: number | null
  height: number | null
  Icon: typeof Monitor
}

const VIEWPORT_SPECS: Record<ViewportMode, ViewportSpec> = {
  auto: {
    label: "Auto — fill panel",
    shortLabel: "AUTO",
    width: null,
    height: null,
    Icon: Square,
  },
  mobile: {
    label: "Mobile — 375 × 667",
    shortLabel: "MOBILE",
    width: 375,
    height: 667,
    Icon: Smartphone,
  },
  tablet: {
    label: "Tablet — 768 × 1024",
    shortLabel: "TABLET",
    width: 768,
    height: 1024,
    Icon: Tablet,
  },
  desktop: {
    label: "Desktop — 1280 × 800",
    shortLabel: "DESKTOP",
    width: 1280,
    height: 800,
    Icon: Monitor,
  },
}

const STATUS_LABEL: Record<WebSandboxStatus, string> = {
  pending: "PENDING",
  installing: "INSTALLING",
  running: "RUNNING",
  stopping: "STOPPING",
  stopped: "STOPPED",
  failed: "FAILED",
}

// Dot color uses the FUI palette tokens. `--validation-emerald` for
// healthy, `--neural-blue` for transitional (pending / installing), grey
// for terminal-clean (stopped / stopping), `--critical-red` for failed.
const STATUS_DOT: Record<WebSandboxStatus, string> = {
  pending: "var(--neural-blue)",
  installing: "var(--neural-blue)",
  running: "var(--validation-emerald)",
  stopping: "var(--muted-foreground)",
  stopped: "var(--muted-foreground)",
  failed: "var(--critical-red)",
}

// Status text is human-readable and complements the LED. Keeps the
// chip readable in monochrome (a11y).
const STATUS_DESCRIPTION: Record<WebSandboxStatus, string> = {
  pending: "Container queued, awaiting docker run.",
  installing: "Running pnpm install — first launch takes 30–90 s.",
  running: "Dev server is live.",
  stopping: "Container stopping…",
  stopped: "Container stopped.",
  failed: "Sandbox failed — check the launch error below.",
}

function isApiNotFound(err: unknown): boolean {
  if (err instanceof ApiError) {
    return err.status === 404 || err.kind === "not_found"
  }
  return false
}

function pickPreviewUrl(inst: WebSandboxInstanceWire | null): string | null {
  if (!inst) return null
  return inst.ingress_url || inst.preview_url || null
}

export interface LivePreviewPanelProps {
  /** Workspace identifier — drives every `web-sandbox` API call. */
  workspaceId: string
  /** Optional absolute host path. When omitted, the launcher will
   *  resolve from the workspace registry (Y6 #282) — matches the
   *  W14.2 router contract. */
  workspacePath?: string | null
  /** Optional callback fired after a successful kill. The parent panel
   *  uses this to close a slide-over / clear breadcrumbs. */
  onClosed?: () => void
  /** Optional injection point so tests can drive the touch loop with
   *  fake timers / a deterministic clock. */
  touchIntervalMs?: number
}

interface PanelState {
  status: "loading" | "idle" | "ready" | "error"
  instance: WebSandboxInstanceWire | null
  error: string | null
}

export function LivePreviewPanel({
  workspaceId,
  workspacePath,
  onClosed,
  touchIntervalMs = TOUCH_INTERVAL_MS,
}: LivePreviewPanelProps) {
  const [state, setState] = useState<PanelState>({
    status: "loading",
    instance: null,
    error: null,
  })
  const [busy, setBusy] = useState(false)
  const [viewport, setViewport] = useState<ViewportMode>("auto")
  // Reload nonce — appended to the iframe `src` so the browser refetches
  // even when an aggressive cache would otherwise short-circuit.
  const [reloadNonce, setReloadNonce] = useState(0)
  const mountedRef = useRef(true)

  useEffect(() => {
    mountedRef.current = true
    return () => { mountedRef.current = false }
  }, [])

  const refresh = useCallback(async () => {
    setState((cur) => ({ ...cur, status: "loading", error: null }))
    try {
      const inst = await getWebSandbox(workspaceId)
      if (!mountedRef.current) return
      setState({ status: "ready", instance: inst, error: null })
    } catch (err) {
      if (!mountedRef.current) return
      if (isApiNotFound(err)) {
        setState({ status: "idle", instance: null, error: null })
        return
      }
      const msg = err instanceof Error ? err.message : String(err)
      setState({ status: "error", instance: null, error: msg })
    }
  }, [workspaceId])

  useEffect(() => {
    void refresh()
  }, [refresh])

  // Touch loop — keeps the W14.5 idle reaper from collecting an
  // actively-watched sandbox. Only runs while the sandbox is in a
  // non-terminal state; restarts whenever the workspace_id changes
  // or the panel transitions back into a live state.
  useEffect(() => {
    const inst = state.instance
    if (!inst) return
    if (inst.status === "stopped" || inst.status === "failed") return
    let cancelled = false
    const tick = async () => {
      try {
        const next = await touchWebSandbox(workspaceId)
        if (cancelled || !mountedRef.current) return
        setState((cur) => ({ ...cur, instance: next }))
      } catch (err) {
        if (cancelled || !mountedRef.current) return
        // Silent best-effort. If the sandbox went away (404) drop the
        // local state so the CTA re-renders; any other failure is left
        // alone — the next touch may succeed and we don't want to
        // spam the operator with mid-flight noise.
        if (isApiNotFound(err)) {
          setState({ status: "idle", instance: null, error: null })
        }
      }
    }
    const handle = setInterval(() => { void tick() }, touchIntervalMs)
    return () => { cancelled = true; clearInterval(handle) }
  }, [state.instance, workspaceId, touchIntervalMs])

  const handleLaunch = useCallback(async () => {
    setBusy(true)
    setState((cur) => ({ ...cur, error: null }))
    try {
      const inst = await launchWebSandbox({
        workspace_id: workspaceId,
        workspace_path: workspacePath ?? null,
      })
      if (!mountedRef.current) return
      setState({ status: "ready", instance: inst, error: null })
    } catch (err) {
      if (!mountedRef.current) return
      const msg = err instanceof Error ? err.message : String(err)
      setState({ status: "error", instance: null, error: msg })
    } finally {
      if (mountedRef.current) setBusy(false)
    }
  }, [workspaceId, workspacePath])

  const handleReload = useCallback(() => {
    setReloadNonce((n) => n + 1)
  }, [])

  const handleExternal = useCallback(() => {
    const url = pickPreviewUrl(state.instance)
    if (!url || typeof window === "undefined") return
    // `noopener,noreferrer` keeps the new tab from reaching back into
    // window.opener (defence-in-depth on top of CF Access SSO).
    window.open(url, "_blank", "noopener,noreferrer")
  }, [state.instance])

  const handleKill = useCallback(async () => {
    if (!state.instance) return
    setBusy(true)
    try {
      const stopped = await stopWebSandbox(workspaceId, {
        reason: "operator_request",
      })
      if (!mountedRef.current) return
      setState({ status: "ready", instance: stopped, error: null })
      onClosed?.()
    } catch (err) {
      if (!mountedRef.current) return
      if (isApiNotFound(err)) {
        setState({ status: "idle", instance: null, error: null })
        onClosed?.()
        return
      }
      const msg = err instanceof Error ? err.message : String(err)
      setState((cur) => ({ ...cur, error: msg }))
    } finally {
      if (mountedRef.current) setBusy(false)
    }
  }, [state.instance, workspaceId, onClosed])

  const previewUrl = pickPreviewUrl(state.instance)
  const status: WebSandboxStatus | null = state.instance?.status ?? null
  const showIframe = status === "running" && previewUrl !== null
  const isInstalling = status === "pending" || status === "installing"

  // Append the reload nonce as a query param so the iframe forces a
  // fresh fetch when "Reload" is clicked — Vite's HMR will replace the
  // module graph but the iframe-document itself only reloads on src
  // change. Non-`running` states never render the iframe so the nonce
  // is moot then.
  const iframeSrc = previewUrl
    ? `${previewUrl}${previewUrl.includes("?") ? "&" : "?"}__omnisight_reload=${reloadNonce}`
    : null

  const viewportSpec = VIEWPORT_SPECS[viewport]

  return (
    <div
      data-testid="live-preview-panel"
      data-workspace-id={workspaceId}
      data-status={status ?? "missing"}
      className="flex flex-col rounded-xl border border-[var(--border)] bg-[var(--card)]"
    >
      {/* ─── Toolbar ─── */}
      <div className="flex flex-wrap items-center gap-2 border-b border-[var(--border)] px-3 py-2">
        <StatusBadge status={status} />
        <div className="hidden flex-1 truncate text-[10px] font-mono text-[var(--muted-foreground)] sm:block">
          {previewUrl ?? "—"}
        </div>
        <ViewportSwitcher value={viewport} onChange={setViewport} />
        <ToolbarButton
          icon={RefreshCw}
          label="Reload"
          onClick={handleReload}
          disabled={!showIframe}
          testId="live-preview-reload"
        />
        <ToolbarButton
          icon={ExternalLink}
          label="Open in new tab"
          onClick={handleExternal}
          disabled={!previewUrl}
          testId="live-preview-external"
        />
        <ToolbarButton
          icon={Power}
          label="Kill sandbox"
          onClick={() => { void handleKill() }}
          disabled={!state.instance || state.instance.status === "stopped" || busy}
          danger
          testId="live-preview-kill"
        />
      </div>

      {/* ─── Body — varies by lifecycle state ─── */}
      <div
        className="relative flex min-h-[480px] flex-1 items-center justify-center overflow-auto bg-[var(--background)]"
        data-testid="live-preview-body"
      >
        {state.status === "loading" && <BodyLoader label="Loading preview…" />}

        {state.status === "error" && state.error && (
          <BodyError message={state.error} onRetry={() => { void refresh() }} />
        )}

        {state.status === "idle" && (
          <BodyIdle
            workspaceId={workspaceId}
            busy={busy}
            onLaunch={() => { void handleLaunch() }}
          />
        )}

        {state.status === "ready" && state.instance && (
          <>
            {isInstalling && <BodyInstalling instance={state.instance} />}

            {state.instance.status === "stopped" && (
              <BodyStopped
                instance={state.instance}
                onRelaunch={() => { void handleLaunch() }}
                busy={busy}
              />
            )}

            {state.instance.status === "failed" && (
              <BodyFailed
                instance={state.instance}
                onRetry={() => { void handleLaunch() }}
                busy={busy}
              />
            )}

            {showIframe && iframeSrc && (
              <div
                data-testid="live-preview-viewport"
                data-viewport={viewport}
                className="flex items-start justify-center p-3"
                style={{
                  width: viewportSpec.width === null ? "100%" : "100%",
                  height: viewportSpec.height === null ? "100%" : "auto",
                }}
              >
                <iframe
                  data-testid="live-preview-iframe"
                  title={`Live preview — ${workspaceId}`}
                  src={iframeSrc}
                  // CF Access (W14.4) handles auth at the edge — the
                  // iframe still needs same-origin storage access for
                  // Vite HMR's localStorage reconnection bookkeeping
                  // when W14.7 lands. `sandbox` left off intentionally:
                  // browsers downgrade it to opaque-origin which breaks
                  // HMR. The dev-server is operator-launched and we
                  // already gate it behind RBAC + SSO.
                  referrerPolicy="no-referrer"
                  className="border border-[var(--border)] bg-white shadow-sm"
                  style={{
                    width: viewportSpec.width === null
                      ? "100%"
                      : `${viewportSpec.width}px`,
                    height: viewportSpec.height === null
                      ? "100%"
                      : `${viewportSpec.height}px`,
                    minHeight: viewportSpec.height === null ? "100%" : undefined,
                  }}
                />
              </div>
            )}
          </>
        )}

        {state.status === "ready" && state.error && (
          <div
            data-testid="live-preview-inline-error"
            className="absolute bottom-3 right-3 max-w-md rounded-md border border-[var(--critical-red)] bg-[var(--card)] px-3 py-2 text-[11px] font-mono text-[var(--critical-red)]"
          >
            <AlertTriangle size={12} className="mr-1 inline" />
            {state.error}
          </div>
        )}
      </div>

      {/* ─── Footer description ─── */}
      {status && (
        <div className="border-t border-[var(--border)] px-3 py-2 text-[10px] font-mono text-[var(--muted-foreground)]">
          {STATUS_DESCRIPTION[status]}
          {state.instance?.warnings && state.instance.warnings.length > 0 && (
            <ul
              data-testid="live-preview-warnings"
              className="mt-1 space-y-0.5 text-[var(--warning-amber,var(--muted-foreground))]"
            >
              {state.instance.warnings.map((w) => (
                <li key={w}>⚠ {w}</li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  )
}

// ─── Sub-components ─────────────────────────────────────────────────

function StatusBadge({ status }: { status: WebSandboxStatus | null }) {
  const label = status ? STATUS_LABEL[status] : "NOT LAUNCHED"
  const color = status ? STATUS_DOT[status] : "var(--muted-foreground)"
  return (
    <span
      data-testid="live-preview-status"
      data-status={status ?? "missing"}
      className="inline-flex items-center gap-1.5 rounded px-2 py-0.5 text-[10px] font-mono font-bold uppercase tracking-wider"
      style={{ color }}
    >
      <span
        aria-hidden
        data-testid="live-preview-status-led"
        className="inline-block h-2 w-2 rounded-full"
        style={{ backgroundColor: color, boxShadow: `0 0 6px ${color}` }}
      />
      {label}
    </span>
  )
}

interface ToolbarButtonProps {
  icon: typeof RefreshCw
  label: string
  onClick: () => void
  disabled?: boolean
  danger?: boolean
  testId?: string
}

function ToolbarButton({
  icon: Icon,
  label,
  onClick,
  disabled,
  danger,
  testId,
}: ToolbarButtonProps) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-label={label}
      title={label}
      data-testid={testId}
      className={[
        "inline-flex h-7 w-7 items-center justify-center rounded border text-[var(--muted-foreground)]",
        "border-[var(--border)] hover:bg-[var(--secondary)] hover:text-[var(--foreground)]",
        "disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-transparent",
        danger ? "hover:!text-[var(--critical-red)] hover:!border-[var(--critical-red)]" : "",
      ]
        .filter(Boolean)
        .join(" ")}
    >
      <Icon size={13} />
    </button>
  )
}

interface ViewportSwitcherProps {
  value: ViewportMode
  onChange: (mode: ViewportMode) => void
}

function ViewportSwitcher({ value, onChange }: ViewportSwitcherProps) {
  return (
    <div
      role="radiogroup"
      aria-label="Viewport simulator"
      data-testid="live-preview-viewport-switcher"
      className="inline-flex rounded border border-[var(--border)]"
    >
      {(Object.entries(VIEWPORT_SPECS) as [ViewportMode, ViewportSpec][]).map(
        ([mode, spec]) => {
          const Icon = spec.Icon
          const active = value === mode
          return (
            <button
              key={mode}
              type="button"
              role="radio"
              aria-checked={active}
              aria-label={spec.label}
              title={spec.label}
              data-testid={`live-preview-viewport-${mode}`}
              onClick={() => onChange(mode)}
              className={[
                "inline-flex h-7 items-center gap-1 px-2 text-[9px] font-mono font-bold uppercase tracking-wider",
                "border-r border-[var(--border)] last:border-r-0",
                active
                  ? "bg-[var(--neural-blue)] text-white"
                  : "text-[var(--muted-foreground)] hover:bg-[var(--secondary)] hover:text-[var(--foreground)]",
              ].join(" ")}
            >
              <Icon size={10} />
              <span className="hidden sm:inline">{spec.shortLabel}</span>
            </button>
          )
        },
      )}
    </div>
  )
}

function BodyLoader({ label }: { label: string }) {
  return (
    <div
      data-testid="live-preview-loader"
      className="flex flex-col items-center gap-2 text-[var(--muted-foreground)]"
    >
      <Loader2 size={20} className="animate-spin" />
      <span className="text-[11px] font-mono uppercase tracking-wider">{label}</span>
    </div>
  )
}

function BodyError({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div
      data-testid="live-preview-error"
      className="flex max-w-md flex-col items-center gap-3 px-6 text-center"
    >
      <AlertTriangle size={24} className="text-[var(--critical-red)]" />
      <p className="text-[11px] font-mono text-[var(--critical-red)]">{message}</p>
      <button
        type="button"
        onClick={onRetry}
        className="rounded border border-[var(--border)] px-3 py-1 text-[10px] font-mono uppercase tracking-wider text-[var(--foreground)] hover:bg-[var(--secondary)]"
      >
        Retry
      </button>
    </div>
  )
}

function BodyIdle({
  workspaceId,
  busy,
  onLaunch,
}: {
  workspaceId: string
  busy: boolean
  onLaunch: () => void
}) {
  return (
    <div
      data-testid="live-preview-idle"
      className="flex max-w-md flex-col items-center gap-3 px-6 text-center"
    >
      <Monitor size={28} className="text-[var(--muted-foreground)]" />
      <p className="text-[11px] font-mono text-[var(--muted-foreground)]">
        No live preview running for{" "}
        <span className="text-[var(--foreground)]">{workspaceId}</span>.
      </p>
      <button
        type="button"
        onClick={onLaunch}
        disabled={busy}
        data-testid="live-preview-launch"
        className="inline-flex items-center gap-1.5 rounded border border-[var(--neural-blue)] bg-[var(--neural-blue)]/10 px-3 py-1.5 text-[10px] font-mono font-bold uppercase tracking-wider text-[var(--neural-blue)] hover:bg-[var(--neural-blue)]/20 disabled:opacity-50"
      >
        {busy ? <Loader2 size={11} className="animate-spin" /> : <Power size={11} />}
        Launch preview
      </button>
    </div>
  )
}

function BodyInstalling({ instance }: { instance: WebSandboxInstanceWire }) {
  const elapsed = instance.started_at
    ? Math.max(0, Math.floor(Date.now() / 1000 - instance.started_at))
    : null
  return (
    <div
      data-testid="live-preview-installing"
      className="flex max-w-md flex-col items-center gap-3 px-6 text-center"
    >
      <Loader2 size={20} className="animate-spin text-[var(--neural-blue)]" />
      <p className="text-[11px] font-mono text-[var(--muted-foreground)]">
        Running <span className="text-[var(--foreground)]">pnpm install</span> …
      </p>
      {elapsed !== null && (
        <p
          data-testid="live-preview-installing-elapsed"
          className="text-[10px] font-mono text-[var(--muted-foreground)]"
        >
          Elapsed: {elapsed}s — first launch typically 30–90 s.
        </p>
      )}
    </div>
  )
}

function BodyStopped({
  instance,
  onRelaunch,
  busy,
}: {
  instance: WebSandboxInstanceWire
  onRelaunch: () => void
  busy: boolean
}) {
  return (
    <div
      data-testid="live-preview-stopped"
      className="flex max-w-md flex-col items-center gap-3 px-6 text-center"
    >
      <Power size={24} className="text-[var(--muted-foreground)]" />
      <p className="text-[11px] font-mono text-[var(--muted-foreground)]">
        Sandbox stopped
        {instance.killed_reason && (
          <>
            {" — "}
            <span className="text-[var(--foreground)]">{instance.killed_reason}</span>
          </>
        )}
        .
      </p>
      <button
        type="button"
        onClick={onRelaunch}
        disabled={busy}
        data-testid="live-preview-relaunch"
        className="inline-flex items-center gap-1.5 rounded border border-[var(--neural-blue)] bg-[var(--neural-blue)]/10 px-3 py-1.5 text-[10px] font-mono font-bold uppercase tracking-wider text-[var(--neural-blue)] hover:bg-[var(--neural-blue)]/20 disabled:opacity-50"
      >
        {busy ? <Loader2 size={11} className="animate-spin" /> : <RefreshCw size={11} />}
        Relaunch
      </button>
    </div>
  )
}

function BodyFailed({
  instance,
  onRetry,
  busy,
}: {
  instance: WebSandboxInstanceWire
  onRetry: () => void
  busy: boolean
}) {
  return (
    <div
      data-testid="live-preview-failed"
      className="flex max-w-md flex-col items-center gap-3 px-6 text-center"
    >
      <AlertTriangle size={24} className="text-[var(--critical-red)]" />
      <p className="text-[11px] font-mono text-[var(--critical-red)]">
        Sandbox failed{instance.error ? `: ${instance.error}` : ""}.
      </p>
      <button
        type="button"
        onClick={onRetry}
        disabled={busy}
        data-testid="live-preview-retry-failed"
        className="inline-flex items-center gap-1.5 rounded border border-[var(--neural-blue)] bg-[var(--neural-blue)]/10 px-3 py-1.5 text-[10px] font-mono font-bold uppercase tracking-wider text-[var(--neural-blue)] hover:bg-[var(--neural-blue)]/20 disabled:opacity-50"
      >
        {busy ? <Loader2 size={11} className="animate-spin" /> : <RefreshCw size={11} />}
        Retry launch
      </button>
    </div>
  )
}

export default LivePreviewPanel
