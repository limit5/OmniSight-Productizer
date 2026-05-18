"use client"

// OP-1476 — Dashboard call-to-action that surfaces the /agents route
// (OP-1460). The /agents page is otherwise an orphan: only reachable via
// the help dropdown's "replay tour" entry. This card is the dashboard
// nav-entry of the scope:agents-nav-entry-2026-05-18 trio.

import Link from "next/link"
import { useTranslations } from "next-intl"
import { ChevronRight, Users } from "lucide-react"

export function AgentGuildHallCard() {
  const t = useTranslations("agentGuildHall")
  return (
    <Link
      href="/agents"
      aria-label={t("ariaLabel")}
      data-testid="dashboard-agent-guild-hall-card"
      className="holo-glass-simple group flex items-center gap-3 p-3 text-left text-[var(--neural-cyan,#67e8f9)] outline-none transition-colors hover:bg-[var(--neural-cyan,#67e8f9)]/10 focus-visible:ring-2 focus-visible:ring-[var(--neural-cyan,#67e8f9)] focus-visible:ring-offset-1 focus-visible:ring-offset-[var(--background,#010409)]"
    >
      <Users
        size={20}
        aria-hidden="true"
        className="shrink-0 text-[var(--neural-cyan,#67e8f9)]"
      />
      <span className="min-w-0 flex-1">
        <span className="block truncate font-mono text-sm font-semibold uppercase tracking-wider">
          {t("title")}
        </span>
        <span className="block truncate text-[11px] text-[var(--muted-foreground,#94a3b8)]">
          {t("subtitle")}
        </span>
      </span>
      <ChevronRight
        size={16}
        aria-hidden="true"
        className="shrink-0 text-[var(--neural-cyan,#67e8f9)]/70 transition-transform group-hover:translate-x-0.5"
      />
    </Link>
  )
}
