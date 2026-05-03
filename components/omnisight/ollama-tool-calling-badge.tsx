"use client"

/**
 * Z.6.4 — Ollama tool-calling compatibility badge.
 *
 * Rendered in the LLM provider model selector (integration-settings.tsx)
 * whenever the active provider is `ollama` and the selected model has an
 * entry in config/ollama_tool_calling.yaml (served via GET /providers →
 * ProviderConfig.tool_calling_compat).
 *
 * Support levels → visual treatment:
 *   full    — emerald chip  "Tool-call: Full"
 *   partial — amber chip    "Tool-call: Partial"
 *   none    — rose chip     "Tool-call: None"
 *
 * A Radix tooltip surfaces the min Ollama version + notes when the
 * operator hovers the badge, so they can quickly check whether their
 * Ollama daemon is new enough.
 *
 * Module-global state audit: pure function of props — no state, no refs,
 * no listeners.  Cross-worker consistency is answer #1 (same static
 * build artifact → same output for same props).
 *
 * Read-after-write timing: N/A — presentation only.
 */

import { Wrench } from "lucide-react"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import type { OllamaModelToolCallingCompat } from "@/lib/api"

export type { OllamaModelToolCallingCompat }

// ─────────────────────────────────────────────────────────────────────
// Visual palette
// ─────────────────────────────────────────────────────────────────────

const PALETTE: Record<
  "full" | "partial" | "none",
  { label: string; chipClass: string }
> = {
  full: {
    label: "Tool-call: Full",
    chipClass:
      "border-[var(--validation-emerald)]/50 bg-[var(--validation-emerald)]/10 text-[var(--validation-emerald)]",
  },
  partial: {
    label: "Tool-call: Partial",
    chipClass:
      "border-amber-500/50 bg-amber-500/10 text-amber-400",
  },
  none: {
    label: "Tool-call: None",
    chipClass:
      "border-rose-500/50 bg-rose-500/10 text-rose-400",
  },
}

// ─────────────────────────────────────────────────────────────────────
// Component
// ─────────────────────────────────────────────────────────────────────

export interface OllamaToolCallingBadgeProps {
  compat: OllamaModelToolCallingCompat
  className?: string
}

export function OllamaToolCallingBadge({
  compat,
  className,
}: OllamaToolCallingBadgeProps) {
  const level = (compat.support === "full" || compat.support === "partial" || compat.support === "none")
    ? compat.support
    : ("none" as const)
  const { label, chipClass } = PALETTE[level]

  const tooltipBody = [
    `Min Ollama: v${compat.min_ollama_version}`,
    compat.notes?.trim() ?? "",
  ]
    .filter(Boolean)
    .join(" — ")

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          data-testid="ollama-tool-calling-badge"
          data-support={level}
          className={[
            "inline-flex items-center gap-1 rounded-full border px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-wider cursor-default select-none",
            chipClass,
            className ?? "",
          ]
            .filter(Boolean)
            .join(" ")}
        >
          <Wrench size={9} aria-hidden />
          {label}
        </span>
      </TooltipTrigger>
      <TooltipContent
        side="top"
        sideOffset={4}
        className="max-w-[260px] border border-[var(--border)] bg-[var(--card)] font-mono text-[10px] leading-snug tracking-wide text-[var(--foreground)]"
      >
        {tooltipBody || `Min Ollama: v${compat.min_ollama_version}`}
      </TooltipContent>
    </Tooltip>
  )
}

export default OllamaToolCallingBadge
