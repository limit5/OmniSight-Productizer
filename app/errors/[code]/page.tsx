"use client"

/**
 * Dedicated error code routes — one URL per supported HTTP code so the API
 * error interceptor (lib/api.ts) or middleware can redirect users to a FUI
 * error page instead of a blank JSON response.
 *
 * Supported codes: 400 / 401 / 403 / 404 / 500 / 502 / 503 (B13 Part B, #339).
 * Any other code falls back to the 404 preset via `notFound()`.
 *
 * Query parameters:
 *   ?traceId=<id>     — 500 / 502 / 503 render a copyable trace badge.
 *   ?retry=<seconds>  — 500 / 502 start an auto-retry countdown that reloads
 *                       the window when it hits zero. Clamped to [0, 120].
 *   ?bootstrap=1      — 503 flips to the "設定未完成" copy and wires the
 *                       primary action to /setup-required.
 *   ?next=<path>      — 401 appends the sanitized next-path to /login so the
 *                       user returns to where they were after re-auth.
 */

import { notFound, useSearchParams } from "next/navigation"
import { use } from "react"
import {
  ErrorPage,
  type ErrorCode,
  type ErrorPageAction,
} from "@/components/omnisight/error-page"
import { Home, LifeBuoy, LogIn, RefreshCw, Wrench } from "lucide-react"

const SUPPORTED: readonly ErrorCode[] = [400, 401, 403, 404, 500, 502, 503]

function parseCode(raw: string): ErrorCode | null {
  const n = Number(raw)
  if (!Number.isFinite(n)) return null
  return SUPPORTED.includes(n as ErrorCode) ? (n as ErrorCode) : null
}

/**
 * Only allow same-origin relative paths in the `next` param. Absolute URLs
 * (`//evil.example` / `https://evil`) are dropped to avoid open-redirect.
 */
function sanitizeNext(raw: string | null): string | null {
  if (!raw) return null
  if (!raw.startsWith("/")) return null
  if (raw.startsWith("//")) return null
  if (raw.length > 512) return null
  return raw
}

export default function ErrorCodePage({
  params,
}: {
  params: Promise<{ code: string }>
}) {
  const { code: codeRaw } = use(params)
  const code = parseCode(codeRaw)
  if (!code) notFound()

  const search = useSearchParams()
  const traceId = search.get("traceId") ?? search.get("trace") ?? undefined
  const retryRaw = search.get("retry")
  const retryParsed = retryRaw ? Number(retryRaw) : NaN
  const autoRetrySeconds =
    Number.isFinite(retryParsed) && retryParsed > 0
      ? Math.min(120, Math.floor(retryParsed))
      : undefined
  const bootstrapRequired =
    code === 503 && (search.get("bootstrap") === "1" || search.get("reason") === "bootstrap_required")
  const nextPath = sanitizeNext(search.get("next"))

  const actions: ErrorPageAction[] | undefined = (() => {
    if (code === 401 && nextPath) {
      return [
        {
          label: "登入",
          href: `/login?next=${encodeURIComponent(nextPath)}`,
          icon: LogIn,
          variant: "primary",
        },
        { label: "回首頁", href: "/", icon: Home, variant: "secondary" },
      ]
    }
    if (code === 503 && bootstrapRequired) {
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
    if (code === 403) {
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
    }
    if (code === 500 || code === 502) {
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
    }
    return undefined
  })()

  return (
    <ErrorPage
      code={code}
      traceId={code === 500 || code === 502 || code === 503 ? traceId : undefined}
      autoRetrySeconds={code === 500 || code === 502 ? autoRetrySeconds : undefined}
      bootstrapRequired={bootstrapRequired}
      actions={actions}
    />
  )
}
