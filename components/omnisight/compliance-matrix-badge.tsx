"use client"

/**
 * BP.K.8 / OP-277 - Compliance matrix badge.
 *
 * Compact presentation badge for BP.D auxiliary compliance matrix results.
 * The shape mirrors ``backend/routers/compliance_matrix.py`` without
 * importing backend code into the frontend bundle:
 *
 *   - audit_type must remain "advisory"
 *   - requires_human_signoff must remain true
 *   - is_auxiliary_compliant is the advisory matrix result
 *
 * Scope discipline: this file only ships the badge primitive requested by
 * BP.K.8. Host page wiring, data fetching, and Jest coverage are separate
 * BP.K rows.
 */

import { AlertTriangle, CheckCircle2, HelpCircle, ShieldAlert } from "lucide-react"

import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { cn } from "@/lib/utils"

export type ComplianceMatrixName =
  | "medical"
  | "automotive"
  | "industrial"
  | "military"

export type ComplianceMatrixAuditType = "advisory"

export type ComplianceMatrixBadgeStatus =
  | "pass"
  | "warn"
  | "fail"
  | "loading"

export interface ComplianceMatrixBadgeResult {
  audit_type?: ComplianceMatrixAuditType | string | null
  requires_human_signoff?: boolean | null
  is_auxiliary_compliant?: boolean | null
  compliance_matrix?: ComplianceMatrixName | string | null
  standards?: readonly string[] | null
  claims?: readonly unknown[] | null
  gaps?: readonly string[] | null
  disclaimer?: string | null
}

export interface ComplianceMatrixBadgeProps {
  matrix: ComplianceMatrixName
  result?: ComplianceMatrixBadgeResult | null
  loading?: boolean
  compact?: boolean
  className?: string
}

const MATRIX_LABELS: Readonly<Record<ComplianceMatrixName, string>> =
  Object.freeze({
    medical: "MED",
    automotive: "AUTO",
    industrial: "IND",
    military: "MIL",
  })

const STATUS_STYLES: Readonly<
  Record<
    ComplianceMatrixBadgeStatus,
    {
      label: string
      icon: typeof CheckCircle2
      chipClass: string
    }
  >
> = Object.freeze({
  pass: {
    label: "Advisory pass",
    icon: CheckCircle2,
    chipClass:
      "border-[var(--validation-emerald)]/45 bg-[var(--validation-emerald)]/10 text-[var(--validation-emerald)]",
  },
  warn: {
    label: "Human review",
    icon: ShieldAlert,
    chipClass:
      "border-amber-500/50 bg-amber-500/10 text-amber-400",
  },
  fail: {
    label: "Gap found",
    icon: AlertTriangle,
    chipClass:
      "border-[var(--critical-red)]/50 bg-[var(--critical-red)]/10 text-[var(--critical-red)]",
  },
  loading: {
    label: "Pending",
    icon: HelpCircle,
    chipClass:
      "border-[var(--border)] bg-[var(--muted)]/20 text-[var(--muted-foreground)]",
  },
})

export function complianceMatrixBadgeStatus(
  result?: ComplianceMatrixBadgeResult | null,
  loading = false,
): ComplianceMatrixBadgeStatus {
  if (loading || !result) return "loading"

  if (
    result.audit_type !== "advisory" ||
    result.requires_human_signoff !== true
  ) {
    return "warn"
  }

  if (result.is_auxiliary_compliant === false) return "fail"
  if (result.is_auxiliary_compliant === true) return "pass"

  return "warn"
}

export function describeComplianceMatrixBadge(
  matrix: ComplianceMatrixName,
  result?: ComplianceMatrixBadgeResult | null,
  loading = false,
): string {
  const status = complianceMatrixBadgeStatus(result, loading)
  const matrixLabel = `${matrix} compliance matrix`

  if (status === "loading") return `${matrixLabel}: pending auxiliary result.`

  const standards = result?.standards?.length
    ? ` Standards: ${result.standards.join(", ")}.`
    : ""
  const claims = result?.claims ? ` Claims: ${result.claims.length}.` : ""
  const gaps = result?.gaps?.length
    ? ` Gaps: ${result.gaps.join("; ")}.`
    : ""
  const disclaimer = result?.disclaimer ? ` ${result.disclaimer}` : ""

  if (
    result?.audit_type !== "advisory" ||
    result?.requires_human_signoff !== true
  ) {
    return `${matrixLabel}: response contract drift; expected advisory output with human signoff.${standards}${claims}${gaps}${disclaimer}`
  }

  if (status === "pass") {
    return `${matrixLabel}: auxiliary advisory pass; human signoff still required.${standards}${claims}${disclaimer}`
  }

  if (status === "fail") {
    return `${matrixLabel}: auxiliary advisory gaps found; human signoff required.${standards}${claims}${gaps}${disclaimer}`
  }

  return `${matrixLabel}: human review required.${standards}${claims}${gaps}${disclaimer}`
}

export function ComplianceMatrixBadge({
  matrix,
  result,
  loading = false,
  compact = false,
  className,
}: ComplianceMatrixBadgeProps) {
  const status = complianceMatrixBadgeStatus(result, loading)
  const style = STATUS_STYLES[status]
  const Icon = style.icon
  const tooltip = describeComplianceMatrixBadge(matrix, result, loading)
  const gapCount = result?.gaps?.length ?? 0

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          role="status"
          aria-label={tooltip}
          data-testid="compliance-matrix-badge"
          data-matrix={matrix}
          data-status={status}
          className={cn(
            "inline-flex items-center gap-1 rounded border px-1.5 py-0.5 font-mono text-[9px] font-semibold uppercase leading-none cursor-default select-none",
            style.chipClass,
            className,
          )}
        >
          <Icon size={10} aria-hidden="true" />
          <span>{MATRIX_LABELS[matrix]}</span>
          {!compact && <span className="hidden sm:inline">{style.label}</span>}
          {gapCount > 0 && (
            <span
              className="ml-0.5 rounded-sm bg-current/10 px-1 tabular-nums"
              aria-hidden="true"
            >
              {gapCount}
            </span>
          )}
        </span>
      </TooltipTrigger>
      <TooltipContent
        side="top"
        sideOffset={4}
        className="max-w-[320px] border border-[var(--border)] bg-[var(--card)] font-mono text-[10px] leading-snug text-[var(--foreground)]"
      >
        {tooltip}
      </TooltipContent>
    </Tooltip>
  )
}

export default ComplianceMatrixBadge
