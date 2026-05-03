"use client"

/**
 * Z.6.5 — Ollama tool-call failure alert banner.
 *
 * Displayed whenever SharedKV("ollama_tool_failures").total > 0, meaning
 * the adapter silently fell back from tool-calling to pure chat at least
 * once since the last counter reset.
 *
 * Rendered in integration-settings.tsx below the OllamaToolCallingBadge
 * when the active provider is `ollama`.
 *
 * Module-global state audit: pure function of props — no state, no
 * listeners, no side effects. Cross-worker consistency: the counter is
 * read via GET /runtime/ollama/tool-failures (Redis-backed SharedKV).
 *
 * Read-after-write timing: N/A — presentation only.
 */

import { AlertTriangle } from "lucide-react"
import type { OllamaToolFailuresResponse } from "@/lib/api"

export interface OllamaToolFailureAlertProps {
  failures: OllamaToolFailuresResponse
  className?: string
}

export function OllamaToolFailureAlert({
  failures,
  className,
}: OllamaToolFailureAlertProps) {
  if (!failures.has_warning) return null

  const parts: string[] = []
  if (failures.daemon_error > 0)
    parts.push(`${failures.daemon_error} daemon error${failures.daemon_error !== 1 ? "s" : ""}`)
  if (failures.unsupported > 0)
    parts.push(`${failures.unsupported} unsupported model${failures.unsupported !== 1 ? "s" : ""}`)
  if (failures.parse_error > 0)
    parts.push(`${failures.parse_error} parse error${failures.parse_error !== 1 ? "s" : ""}`)

  const detail = parts.length > 0 ? ` (${parts.join(", ")})` : ""

  return (
    <div
      data-testid="ollama-tool-failure-alert"
      className={[
        "flex items-start gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 font-mono text-[10px] text-amber-400",
        className ?? "",
      ]
        .filter(Boolean)
        .join(" ")}
    >
      <AlertTriangle size={12} className="mt-[1px] shrink-0" aria-hidden />
      <span>
        <span className="font-semibold">Ollama tool-call degraded</span>
        {" — "}
        {failures.total} invocation{failures.total !== 1 ? "s" : ""} fell back to pure chat
        {detail}.
        {" "}Check model compat badge or Ollama daemon logs.
      </span>
    </div>
  )
}

export default OllamaToolFailureAlert
