"use client"

/**
 * Shared FUI error page — powers 4xx/5xx views (#339, B13 Part B).
 *
 * The three Next.js error entry points (`app/not-found.tsx`, `app/error.tsx`,
 * `app/global-error.tsx`) and any feature-level fallback should funnel through
 * this component so every error UI stays in the dark scan-line aesthetic and
 * exposes the same "friendly + expandable technical" pattern.
 *
 * Props are intentionally flexible: consumers pass an HTTP `code` for the
 * preset defaults (system label, big display text, headline, friendly copy,
 * accent color) and override any slot they need — `actions` is a button list
 * so the caller can wire up retry / login / report-issue / home without this
 * component needing to know the router.
 */

import Link, { type LinkProps } from "next/link"
import {
  useEffect,
  useRef,
  useState,
  type ComponentType,
  type CSSProperties,
  type ReactNode,
} from "react"
import {
  AlertTriangle,
  ChevronDown,
  ChevronRight,
  Home,
  LifeBuoy,
  Lock,
  LogIn,
  RefreshCw,
  ShieldAlert,
  Wrench,
} from "lucide-react"
import { NeuralGrid } from "@/components/omnisight/neural-grid"

export type ErrorCode = 400 | 401 | 403 | 404 | 500 | 502 | 503

type AccentKey = "red" | "orange" | "blue"

type AccentTokens = {
  text: string
  border: string
  dim: string
  glow: string
  shadow: string
}

const ACCENT_TOKENS: Record<AccentKey, AccentTokens> = {
  red: {
    text: "var(--critical-red)",
    border: "var(--critical-red)",
    dim: "var(--critical-red-dim)",
    glow: "var(--critical-red)",
    shadow:
      "0 0 10px var(--critical-red), 0 0 20px var(--critical-red-dim)",
  },
  orange: {
    text: "var(--hardware-orange)",
    border: "var(--hardware-orange)",
    dim: "rgba(249,115,22,0.3)",
    glow: "var(--hardware-orange)",
    shadow:
      "0 0 10px var(--hardware-orange), 0 0 20px rgba(249,115,22,0.3)",
  },
  blue: {
    text: "var(--neural-blue)",
    border: "var(--neural-blue)",
    dim: "var(--neural-blue-dim)",
    glow: "var(--neural-blue)",
    shadow:
      "0 0 10px var(--neural-blue), 0 0 20px var(--neural-blue-glow)",
  },
}

type CodePreset = {
  systemLabel: string
  displayCode: string
  title: string
  friendlyMessage: string
  accent: AccentKey
  icon: ComponentType<{ size?: number; className?: string; style?: CSSProperties }>
}

const CODE_PRESETS: Record<ErrorCode, CodePreset> = {
  400: {
    systemLabel: "SYS.INPUT · MALFORMED",
    displayCode: "400",
    title: "請求格式有誤",
    friendlyMessage: "請求格式有誤，請檢查輸入後重試。",
    accent: "orange",
    icon: AlertTriangle,
  },
  401: {
    systemLabel: "SYS.AUTH · SESSION EXPIRED",
    displayCode: "401",
    title: "登入已過期",
    friendlyMessage: "登入已過期，請重新登入。",
    accent: "orange",
    icon: LogIn,
  },
  403: {
    systemLabel: "SYS.ACL · FORBIDDEN",
    displayCode: "403",
    title: "沒有存取權限",
    friendlyMessage: "您沒有此頁面的存取權限，請聯繫管理員開通。",
    accent: "orange",
    icon: Lock,
  },
  404: {
    systemLabel: "SYS.ROUTE · NO MATCH",
    displayCode: "404",
    title: "找不到此頁面",
    friendlyMessage: "此頁面不存在或已移除，請確認網址是否正確。",
    accent: "red",
    icon: AlertTriangle,
  },
  500: {
    systemLabel: "SYS.EXCEPTION · INTERNAL",
    displayCode: "500",
    title: "系統發生內部錯誤",
    friendlyMessage: "系統發生內部錯誤，我們已收到通知。",
    accent: "red",
    icon: ShieldAlert,
  },
  502: {
    systemLabel: "SYS.UPSTREAM · BAD GATEWAY",
    displayCode: "502",
    title: "後端服務暫時不可用",
    friendlyMessage: "後端服務暫時不可用，請稍後重試。",
    accent: "red",
    icon: AlertTriangle,
  },
  503: {
    systemLabel: "SYS.SERVICE · UNAVAILABLE",
    displayCode: "503",
    title: "系統維護中",
    friendlyMessage: "系統維護中，請稍後再試。",
    accent: "orange",
    icon: Wrench,
  },
}

/**
 * Alternate preset for 503 when the backend reports `error === "bootstrap_required"`.
 * The bootstrap case is semantically different from a maintenance window — we want
 * users to act on it (finish setup) rather than wait — so we flip the copy and the
 * default action via the `bootstrapRequired` prop (or `/errors/503?bootstrap=1`).
 */
const BOOTSTRAP_REQUIRED_PRESET: CodePreset = {
  systemLabel: "SYS.BOOTSTRAP · SETUP PENDING",
  displayCode: "503",
  title: "設定未完成",
  friendlyMessage: "系統初始設定尚未完成，請先完成 bootstrap 流程。",
  accent: "orange",
  icon: Wrench,
}

export interface ErrorPageAction {
  label: string
  href?: LinkProps["href"]
  onClick?: () => void
  icon?: ComponentType<{ size?: number; className?: string; style?: CSSProperties }>
  variant?: "primary" | "secondary"
  external?: boolean
  ariaLabel?: string
}

export interface ErrorPageProps {
  code: ErrorCode
  systemLabel?: string
  displayCode?: string
  title?: ReactNode
  friendlyMessage?: ReactNode
  technicalDetail?: ReactNode
  actions?: ErrorPageAction[]
  accent?: AccentKey
  icon?: ComponentType<{ size?: number; className?: string; style?: CSSProperties }>
  defaultExpanded?: boolean
  footer?: ReactNode
  /**
   * Optional trace identifier shown under the friendly message. Rendered as a
   * selectable mono badge so a user can screenshot or paste it into a bug
   * report. Primary use case is the 500 preset ("系統發生內部錯誤，我們已收到通知"
   * + trace ID).
   */
  traceId?: string
  /**
   * When set, display a countdown under the friendly message and reload the
   * window automatically once it reaches zero. Primary use case is the 502
   * preset ("後端服務暫時不可用"). Pass `0` or a negative number to disable.
   */
  autoRetrySeconds?: number
  /**
   * When true on a 503 page, swap the friendly copy to the "設定未完成"
   * bootstrap variant and default the primary action to `/setup-required`.
   * Callers use this when the backend reports `error === "bootstrap_required"`.
   */
  bootstrapRequired?: boolean
}

function actionClassName(variant: ErrorPageAction["variant"]) {
  if (variant === "secondary") {
    return "group inline-flex items-center gap-2 rounded-md border border-[var(--holo-glass-border)] px-6 py-3 font-mono text-sm font-semibold tracking-widest text-[var(--muted-foreground)] transition-all hover:border-[var(--neural-blue)] hover:text-[var(--neural-blue)]"
  }
  return "group inline-flex items-center gap-2 rounded-md border border-[var(--neural-blue)] bg-[var(--neural-blue-dim)] px-6 py-3 font-mono text-sm font-semibold tracking-widest text-[var(--foreground)] transition-all hover:bg-[var(--neural-blue)] hover:text-black hover:shadow-[0_0_20px_var(--neural-blue-glow)]"
}

function ActionNode({ action }: { action: ErrorPageAction }) {
  const Icon = action.icon
  const className = actionClassName(action.variant ?? "primary")
  const content = (
    <>
      {Icon ? <Icon size={14} aria-hidden="true" /> : null}
      <span>{action.label}</span>
      <ChevronRight
        size={14}
        className="opacity-60 transition-transform group-hover:translate-x-0.5"
        aria-hidden="true"
      />
    </>
  )

  if (action.href && !action.onClick) {
    if (action.external) {
      const href =
        typeof action.href === "string" ? action.href : String(action.href)
      return (
        <a
          href={href}
          className={className}
          aria-label={action.ariaLabel ?? action.label}
          target="_blank"
          rel="noreferrer noopener"
        >
          {content}
        </a>
      )
    }
    return (
      <Link
        href={action.href}
        className={className}
        aria-label={action.ariaLabel ?? action.label}
      >
        {content}
      </Link>
    )
  }

  return (
    <button
      type="button"
      onClick={action.onClick}
      className={className}
      aria-label={action.ariaLabel ?? action.label}
    >
      {content}
    </button>
  )
}

/**
 * Derive a sensible default action list when the caller doesn't supply one.
 * Keeps this component useful as a drop-in "just tell me the code" fallback
 * while letting feature pages fully customize when they need retry/report.
 */
function defaultActionsFor(
  code: ErrorCode,
  opts: { bootstrapRequired?: boolean } = {},
): ErrorPageAction[] {
  switch (code) {
    case 401:
      return [
        { label: "登入", href: "/login", icon: LogIn, variant: "primary" },
        { label: "回首頁", href: "/", icon: Home, variant: "secondary" },
      ]
    case 403:
      return [
        { label: "回首頁", href: "/", icon: Home, variant: "primary" },
        {
          label: "聯繫管理員",
          href: "mailto:admin@omnisight.local",
          icon: LifeBuoy,
          variant: "secondary",
          external: true,
        },
      ]
    case 503:
      if (opts.bootstrapRequired) {
        return [
          {
            label: "前往設定",
            href: "/setup-required",
            icon: Wrench,
            variant: "primary",
          },
          { label: "回首頁", href: "/", icon: Home, variant: "secondary" },
        ]
      }
      return [
        {
          label: "重試",
          onClick: () => {
            if (typeof window !== "undefined") window.location.reload()
          },
          icon: RefreshCw,
          variant: "primary",
        },
        { label: "回首頁", href: "/", icon: Home, variant: "secondary" },
      ]
    case 500:
    case 502:
      return [
        {
          label: "重試",
          onClick: () => {
            if (typeof window !== "undefined") window.location.reload()
          },
          icon: RefreshCw,
          variant: "primary",
        },
        { label: "回首頁", href: "/", icon: Home, variant: "secondary" },
      ]
    case 400:
    case 404:
    default:
      return [
        { label: "回首頁", href: "/", icon: Home, variant: "primary" },
      ]
  }
}

/**
 * Countdown badge that triggers `window.location.reload()` once it reaches 0.
 * Split out so 502 (and any future caller) can opt in via `autoRetrySeconds`
 * without the component needing to know about React timers elsewhere.
 */
function AutoRetryCountdown({ seconds }: { seconds: number }) {
  const [remaining, setRemaining] = useState(Math.max(0, Math.floor(seconds)))
  const reloadedRef = useRef(false)

  useEffect(() => {
    setRemaining(Math.max(0, Math.floor(seconds)))
    reloadedRef.current = false
  }, [seconds])

  useEffect(() => {
    if (remaining <= 0) {
      if (!reloadedRef.current && typeof window !== "undefined") {
        reloadedRef.current = true
        window.location.reload()
      }
      return
    }
    const t = window.setTimeout(() => setRemaining((v) => v - 1), 1000)
    return () => window.clearTimeout(t)
  }, [remaining])

  return (
    <div
      className="mt-4 inline-flex items-center gap-2 rounded border border-[var(--holo-glass-border)] bg-black/30 px-3 py-1.5 font-mono text-[11px] text-[var(--muted-foreground)]"
      role="status"
      aria-live="polite"
      data-testid="error-page-auto-retry"
    >
      <RefreshCw
        size={12}
        className={remaining > 0 ? "animate-spin" : ""}
        aria-hidden="true"
      />
      <span>
        將於 <span className="text-[var(--neural-blue)]">{remaining}s</span> 後自動重試
      </span>
    </div>
  )
}

export function ErrorPage(props: ErrorPageProps) {
  const useBootstrapPreset =
    props.code === 503 && Boolean(props.bootstrapRequired)
  const preset = useBootstrapPreset
    ? BOOTSTRAP_REQUIRED_PRESET
    : CODE_PRESETS[props.code]
  const accentKey: AccentKey = props.accent ?? preset.accent
  const accent = ACCENT_TOKENS[accentKey]
  const Icon = props.icon ?? preset.icon ?? AlertTriangle
  const systemLabel = props.systemLabel ?? preset.systemLabel
  const displayCode = props.displayCode ?? preset.displayCode
  const title = props.title ?? preset.title
  const friendlyMessage = props.friendlyMessage ?? preset.friendlyMessage
  const actions =
    props.actions && props.actions.length > 0
      ? props.actions
      : defaultActionsFor(props.code, {
          bootstrapRequired: useBootstrapPreset,
        })

  const [expanded, setExpanded] = useState(Boolean(props.defaultExpanded))
  const autoRetryActive =
    typeof props.autoRetrySeconds === "number" && props.autoRetrySeconds > 0

  return (
    <div className="relative min-h-screen flex items-center justify-center overflow-hidden bg-[var(--deep-space-start)]">
      <NeuralGrid />

      <div
        aria-hidden="true"
        className="pointer-events-none fixed inset-0 z-0 opacity-40"
        style={{
          background:
            "repeating-linear-gradient(0deg, transparent 0px, transparent 2px, rgba(56,189,248,0.05) 2px, rgba(56,189,248,0.05) 3px)",
        }}
      />

      <div aria-hidden="true" className="fui-scan-sweep" />

      <main className="relative z-10 w-full max-w-2xl px-6 py-10">
        <div className="holo-glass-simple corner-brackets rounded-lg p-8 md:p-10">
          <div className="mb-4 flex items-center justify-center gap-3">
            <div
              className="h-10 w-10 rounded-full border-2 flex items-center justify-center animate-pulse"
              style={{ borderColor: accent.border }}
            >
              <Icon size={18} style={{ color: accent.text }} />
            </div>
            <span
              className="text-[10px] uppercase tracking-[0.3em]"
              style={{
                color: accent.text,
                fontFamily: "var(--font-orbitron), sans-serif",
              }}
            >
              {systemLabel}
            </span>
          </div>

          <div
            className="mb-2 text-center text-6xl md:text-7xl font-bold uppercase tracking-[0.2em]"
            style={{
              color: accent.text,
              fontFamily: "var(--font-orbitron), sans-serif",
              textShadow: accent.shadow,
            }}
            data-testid="error-page-display-code"
          >
            {displayCode}
          </div>

          <h1
            className="text-2xl md:text-3xl font-semibold tracking-fui text-[var(--foreground)] text-center"
            style={{ fontFamily: "var(--font-orbitron), sans-serif" }}
          >
            {title}
          </h1>
          <div className="mt-3 font-mono text-sm text-[var(--muted-foreground)] text-center leading-relaxed">
            {friendlyMessage}
          </div>

          {(props.traceId || autoRetryActive) && (
            <div className="mt-4 flex flex-col items-center gap-2">
              {props.traceId ? (
                <div
                  className="inline-flex items-center gap-2 rounded border border-[var(--holo-glass-border)] bg-black/30 px-3 py-1.5 font-mono text-[11px]"
                  data-testid="error-page-trace-id"
                >
                  <span className="uppercase tracking-widest text-[var(--muted-foreground)]">
                    trace id
                  </span>
                  <span
                    className="select-all break-all text-[var(--neural-blue)]"
                    style={{ color: accent.text }}
                  >
                    {props.traceId}
                  </span>
                </div>
              ) : null}
              {autoRetryActive ? (
                <AutoRetryCountdown seconds={props.autoRetrySeconds as number} />
              ) : null}
            </div>
          )}

          {actions.length > 0 && (
            <div className="mt-8 flex flex-col sm:flex-row items-center justify-center gap-3">
              {actions.map((action, idx) => (
                <ActionNode
                  key={`${action.label}-${idx}`}
                  action={action}
                />
              ))}
            </div>
          )}

          {props.technicalDetail ? (
            <div className="mt-8">
              <button
                type="button"
                onClick={() => setExpanded((v) => !v)}
                className="flex w-full items-center justify-between rounded border border-[var(--holo-glass-border)] bg-black/20 px-3 py-2 font-mono text-xs text-[var(--muted-foreground)] transition-colors hover:text-[var(--neural-blue)]"
                aria-expanded={expanded}
                aria-controls="error-page-technical-detail"
              >
                <span className="flex items-center gap-2">
                  <AlertTriangle size={12} aria-hidden="true" />
                  <span>技術詳情 (for engineers)</span>
                </span>
                <ChevronDown
                  size={14}
                  className={`transition-transform ${
                    expanded ? "rotate-180" : ""
                  }`}
                  aria-hidden="true"
                />
              </button>

              {expanded && (
                <div
                  id="error-page-technical-detail"
                  className="mt-3 space-y-3 rounded border border-[var(--holo-glass-border)] bg-black/40 p-3"
                >
                  {props.technicalDetail}
                </div>
              )}
            </div>
          ) : null}
        </div>

        {props.footer !== undefined ? (
          <div className="mt-6 text-center">{props.footer}</div>
        ) : (
          <p className="mt-6 text-center font-mono text-[10px] uppercase tracking-widest text-[var(--muted-foreground)]">
            OmniSight Productizer · neural command center
          </p>
        )}
      </main>
    </div>
  )
}

export default ErrorPage
