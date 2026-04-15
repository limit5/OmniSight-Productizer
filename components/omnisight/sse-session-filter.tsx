"use client"

import { useCallback, useEffect, useState } from "react"
import { Filter, Radio } from "lucide-react"
import {
  type SSEFilterMode,
  getSSEFilterMode,
  setSSEFilterMode,
  onFilterModeChange,
  getCurrentSessionId,
} from "@/lib/api"
import { useAuth } from "@/lib/auth-context"

const MODES: { value: SSEFilterMode; label: string; hint: string }[] = [
  { value: "this_session", label: "僅本 Session", hint: "只顯示此分頁觸發的事件 + user 級通知" },
  { value: "all_sessions", label: "所有 Session", hint: "顯示所有我的 session 事件" },
]

export function SSESessionFilter({ compact = false }: { compact?: boolean }) {
  const { sessionId } = useAuth()
  const [mode, setMode] = useState<SSEFilterMode>(getSSEFilterMode)

  useEffect(() => {
    return onFilterModeChange(setMode)
  }, [])

  const toggle = useCallback(() => {
    const next: SSEFilterMode = mode === "this_session" ? "all_sessions" : "this_session"
    setSSEFilterMode(next)
  }, [mode])

  if (!sessionId) return null

  const current = MODES.find((m) => m.value === mode) ?? MODES[0]

  return (
    <button
      onClick={toggle}
      title={current.hint}
      className="inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-xs font-medium transition-colors border"
      style={{
        borderColor: mode === "this_session"
          ? "var(--neural-cyan, #67e8f9)"
          : "var(--neural-amber, #fbbf24)",
        color: mode === "this_session"
          ? "var(--neural-cyan, #67e8f9)"
          : "var(--neural-amber, #fbbf24)",
        background: "transparent",
      }}
    >
      <Filter size={12} />
      {!compact && <span>{current.label}</span>}
      {compact && <span>{mode === "this_session" ? "THIS" : "ALL"}</span>}
    </button>
  )
}
