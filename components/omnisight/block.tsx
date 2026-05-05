"use client"

import type { HTMLAttributes, ReactNode } from "react"
import type { LucideIcon } from "lucide-react"
import { cn } from "@/lib/utils"

type BlockElement = "div" | "section" | "article" | "aside" | "li" | "button" | "figure"
type BlockTone = "neutral" | "info" | "success" | "warning" | "danger"

const TONE_CLASS: Record<BlockTone, string> = {
  neutral: "border-[var(--neural-border,rgba(148,163,184,0.25))] bg-white/[0.02]",
  info: "border-[var(--neural-blue,#3b82f6)]/35 bg-[var(--neural-blue,#3b82f6)]/[0.05]",
  success: "border-[var(--validation-emerald,#10b981)]/35 bg-[var(--validation-emerald,#10b981)]/[0.05]",
  warning: "border-[var(--fui-orange,#f59e0b)]/40 bg-[var(--fui-orange,#f59e0b)]/[0.06]",
  danger: "border-[var(--critical-red,#ef4444)]/40 bg-[var(--critical-red,#ef4444)]/[0.06]",
}

export interface BlockProps extends Omit<HTMLAttributes<HTMLElement>, "title"> {
  as?: BlockElement
  type?: "button" | "submit" | "reset"
  title?: ReactNode
  titleRight?: ReactNode
  icon?: LucideIcon
  tone?: BlockTone
  kind?: string
  status?: string
  headerClassName?: string
  bodyClassName?: string
}

export function Block({
  as = "div",
  title,
  titleRight,
  icon: Icon,
  tone = "neutral",
  kind,
  status,
  className,
  headerClassName,
  bodyClassName,
  children,
  ...props
}: BlockProps) {
  const hasHeader = Boolean(title || titleRight || Icon)
  const Element = as

  return (
    <Element
      data-block-kind={kind}
      data-block-status={status}
      className={cn(
        "flex min-w-0 flex-col gap-1.5 rounded-sm border p-2",
        TONE_CLASS[tone],
        className,
      )}
      {...props}
    >
      {hasHeader && (
        <div
          className={cn(
            "flex items-center justify-between gap-2 font-mono text-[10px] tracking-[0.18em] text-[var(--muted-foreground,#94a3b8)]",
            headerClassName,
          )}
        >
          <div className="flex min-w-0 items-center gap-1">
            {Icon && <Icon className="h-3 w-3 shrink-0" aria-hidden />}
            {title && <span className="min-w-0 truncate">{title}</span>}
          </div>
          {titleRight && <div className="shrink-0">{titleRight}</div>}
        </div>
      )}
      {bodyClassName ? <div className={bodyClassName}>{children}</div> : children}
    </Element>
  )
}
