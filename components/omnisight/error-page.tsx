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
    friendlyMessage: "您的請求未通過格式檢查，請確認輸入內容後重試。",
    accent: "orange",
    icon: AlertTriangle,
  },
  401: {
    systemLabel: "SYS.AUTH · SESSION EXPIRED",
    displayCode: "401",
    title: "登入已過期",
    friendlyMessage: "您的登入狀態已失效，請重新登入以繼續操作。",
    accent: "orange",
    icon: LogIn,
  },
  403: {
    systemLabel: "SYS.ACL · FORBIDDEN",
    displayCode: "403",
    title: "沒有存取權限",
    friendlyMessage: "您目前的帳號沒有此頁面的存取權限，請聯繫管理員開通。",
    accent: "orange",
    icon: Lock,
  },
  404: {
    systemLabel: "SYS.ROUTE · NO MATCH",
    displayCode: "404",
    title: "找不到此頁面",
    friendlyMessage: "您所尋找的頁面不存在或已移除，請確認網址是否正確。",
    accent: "red",
    icon: AlertTriangle,
  },
  500: {
    systemLabel: "SYS.EXCEPTION · INTERNAL",
    displayCode: "500",
    title: "系統發生內部錯誤",
    friendlyMessage: "我們已收到錯誤通知，請稍後重試或回報下方 trace ID。",
    accent: "red",
    icon: ShieldAlert,
  },
  502: {
    systemLabel: "SYS.UPSTREAM · BAD GATEWAY",
    displayCode: "502",
    title: "後端服務暫時不可用",
    friendlyMessage: "上游服務目前無法回應，請稍後重試。",
    accent: "red",
    icon: AlertTriangle,
  },
  503: {
    systemLabel: "SYS.SERVICE · UNAVAILABLE",
    displayCode: "503",
    title: "服務維護中",
    friendlyMessage: "系統正在進行維護或初始設定，請稍後再試。",
    accent: "orange",
    icon: Wrench,
  },
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
function defaultActionsFor(code: ErrorCode): ErrorPageAction[] {
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
    case 500:
    case 502:
    case 503:
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

export function ErrorPage(props: ErrorPageProps) {
  const preset = CODE_PRESETS[props.code]
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
      : defaultActionsFor(props.code)

  const [expanded, setExpanded] = useState(Boolean(props.defaultExpanded))

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
